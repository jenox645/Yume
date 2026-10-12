"""The interactive launcher's flow (yume/launch.py) with servers, ports and prompts faked."""

from __future__ import annotations

import sys
import types
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import yume.launch as launch  # noqa: E402

NVIDIA = {"has_nvidia": True, "has_amd": False, "vram_mb": 12288, "name": "RTX 3060"}
CPU = {"has_nvidia": False, "has_amd": False, "vram_mb": 0, "name": None}


# ── resource check ────────────────────────────────────────────────────────────


def resources(cfg, *, gpu=NVIDIA, free_vram=12000, free_ram=16000, choices=(), gguf_mb=0, tmp=None):
    real_stat = Path.stat

    def stat(self, *a, **k):
        st = real_stat(self, *a, **k)
        if self.name != "m.gguf":
            return st
        # A multi-GB GGUF without writing one (truncate is not sparse on Windows)
        return types.SimpleNamespace(**{**{n: getattr(st, n) for n in dir(st) if n.startswith("st_")}, "st_size": size})

    size = int(gguf_mb * 1024 * 1024)
    if gguf_mb:
        f = tmp / "m.gguf"
        f.write_bytes(b"GGUF")
        cfg["gguf_model_path"] = str(f)
    panels = []
    fakes = {
        "detect_gpu": lambda: gpu,
        "_get_available_vram_mb": lambda: free_vram,
        "_get_available_ram_mb": lambda: free_ram,
        "panel": lambda text, **kw: panels.append(text),
        "ask_arrow": MagicMock(side_effect=list(choices)),
        "info": MagicMock(),
        "warn": MagicMock(),
    }
    with ExitStack() as stack:
        for name, fake in fakes.items():
            stack.enter_context(patch.object(launch, name, fake))
        stack.enter_context(patch("builtins.input", lambda *a: ""))
        stack.enter_context(patch.object(Path, "stat", stat))
        ok = launch._check_resources(cfg)
    return ok, panels


def test_enough_resources_launches_without_asking():
    ok, panels = resources({"whisper_model": "large-v3-turbo", "translation_backend": "ollama"})
    assert ok and panels == []


def test_whisper_needing_more_vram_than_free_asks(tmp_path):
    cfg = {"whisper_model": "large-v3", "whisper_compute_type": "float16", "translation_backend": "ollama"}
    ok, panels = resources(cfg, free_vram=2000, choices=[0])
    assert ok  # "Launch anyway"
    assert "Whisper 'large-v3' needs ~4.4 GB VRAM, but only 2.0 GB is free." in panels[0]
    assert not resources(cfg, free_vram=2000, choices=[3])[0]  # Cancel


def test_int8_needs_less_vram():
    cfg = {"whisper_model": "large-v3", "whisper_compute_type": "int8_float16", "translation_backend": "ollama"}
    assert resources(cfg, free_vram=3500)[1] == []  # 4.5 GB x 0.65 fits in 3.5 GB


def test_whisper_and_gguf_are_checked_together_on_one_gpu(tmp_path):
    cfg = {"whisper_model": "large-v3-turbo", "translation_backend": "llamacpp"}
    ok, panels = resources(cfg, free_vram=6000, gguf_mb=4000, choices=[3], tmp=tmp_path)
    assert not ok
    assert "needs ~4.7 GB VRAM (plus ~" in panels[0]


def test_cpu_only_checks_ram_for_the_gguf(tmp_path):
    cfg = {"whisper_model": "small", "translation_backend": "llamacpp"}
    ok, panels = resources(cfg, gpu=CPU, free_ram=3000, gguf_mb=4000, choices=[0], tmp=tmp_path)
    assert ok and "GB RAM, but only 2.9 GB is available." in panels[0]


def test_retry_checks_again():
    cfg = {"whisper_model": "large-v3", "whisper_compute_type": "float16", "translation_backend": "ollama"}
    frees = iter([2000, 12000])
    with patch.object(launch, "_get_available_vram_mb", lambda: next(frees)):
        ok, panels = resources(cfg, choices=[1])  # retry; then enough VRAM
    assert ok


# ── the launch flow ───────────────────────────────────────────────────────────


class Launch:
    def __init__(self, cfg=None, *, existing=None, busy=(), ensure=None, yns=(), missing=()):
        self.cfg = {
            "translation_backend": "llamacpp", "whisper_port": 5001, "translation_port": 5000,
            "whisper_host": "127.0.0.1", "translation_host": "127.0.0.1", **(cfg or {}),
        }  # fmt: skip
        self.existing = existing or {}
        self.busy = set(busy)
        self.ensure = ensure  # ensure_port_free result (None: no free port)
        self.yns = list(yns)
        self.missing = list(missing)
        self.started: list[str] = []
        self.runtime = MagicMock()
        self.saved = MagicMock()
        self.installed = MagicMock()

    def _start(self, name):
        def start(*a, **k):
            self.started.append(name)
            return True

        return start

    def go(self):
        import yume.installers
        import yume.utils

        fakes = {
            "discover_servers": lambda cfg, bi: self.existing,
            "is_port_free": lambda port, *a: port not in self.busy,
            "ensure_port_free": lambda port, cfg, kind, exclude=None: self.ensure,
            "find_free_port": lambda start, exclude=None: 5002,
            "_check_resources": lambda cfg: True,
            "_start_llamacpp": self._start("llamacpp"),
            "_start_ollama": self._start("ollama"),
            "_start_whisper": self._start("whisper"),
            "_runtime_menu": self.runtime,
            "ask_yn": MagicMock(side_effect=self.yns),
            "clear": MagicMock(),
            "pause": MagicMock(),
            "info": MagicMock(),
            "warn": MagicMock(),
            "error": MagicMock(),
            "success": MagicMock(),
        }
        with ExitStack() as stack:
            for name, fake in fakes.items():
                stack.enter_context(patch.object(launch, name, fake))
            stack.enter_context(patch.object(launch.servers, "build_server_env", lambda: {}))
            stack.enter_context(patch.object(yume.utils, "missing_requirements", lambda: self.missing))
            stack.enter_context(patch.object(yume.installers, "install_python_deps", self.installed))
            stack.enter_context(patch("config.save_config", self.saved))
            launch._launch_inner(self.cfg, MagicMock())
        return self


def test_launch_starts_translation_then_whisper_then_the_menu():
    r = Launch().go()
    assert r.started == ["llamacpp", "whisper"]
    r.runtime.assert_called_once()


@pytest.mark.parametrize(("backend", "started"), [("ollama", ["ollama", "whisper"]), ("lmstudio", ["whisper"])])
def test_other_backends(backend, started):
    assert Launch({"translation_backend": backend}).go().started == started


def test_both_servers_running_opens_the_menu_directly():
    r = Launch(existing={"whisper": True, "translation": True}, yns=[True]).go()
    assert r.started == []
    r.runtime.assert_called_once()


def test_a_running_ollama_on_another_port_is_adopted():
    r = Launch({"translation_backend": "ollama"}, existing={"ollama_found": 11500}, yns=[True]).go()
    assert r.cfg["translation_port"] == 11500


def test_same_port_for_both_gets_a_new_translation_port():
    r = Launch({"translation_port": 5001}).go()
    assert r.cfg["translation_port"] == 5002
    assert r.started == ["llamacpp", "whisper"]


def test_a_busy_port_that_cannot_be_freed_stops_the_launch():
    r = Launch(busy={5001}, ensure=None).go()
    assert r.started == []


def test_a_busy_translation_port_only_matters_for_llamacpp():
    assert Launch({"translation_backend": "ollama"}, busy={5000}).go().started == ["ollama", "whisper"]
    assert Launch(busy={5000}, ensure=None).go().started == []


def test_missing_packages_are_offered():
    r = Launch(missing=["pypinyin==0.55.0"], yns=[True]).go()
    r.installed.assert_called_once()
    assert r.started == ["llamacpp", "whisper"]
