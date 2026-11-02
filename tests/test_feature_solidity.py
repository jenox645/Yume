"""Features that went wrong for a real user: choices lost, silent stops, misleading states."""

from __future__ import annotations

import datetime
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

import _settings  # noqa: E402
import _ytdlp  # noqa: E402

from yume import hardware, health, launch, service, servers, setup  # noqa: E402
from yume.menus import whisper_model  # noqa: E402

NVIDIA = {"has_nvidia": True, "has_amd": False, "vram_mb": 12288, "name": "RTX 3060"}
AMD = {"has_nvidia": False, "has_amd": True, "vram_mb": 8192, "name": "RX 6800"}
NO_GPU = {"has_nvidia": False, "has_amd": False, "vram_mb": 0, "name": None}


def test_running_setup_again_keeps_the_chosen_whisper_model():
    cfg = {"whisper_model": "large-v2"}
    setup._pick_whisper_model(cfg, NVIDIA, rerun=True)
    assert cfg["whisper_model"] == "large-v2"


def test_the_first_setup_takes_the_recommended_model():
    cfg = {"whisper_model": "large-v2"}
    setup._pick_whisper_model(cfg, NVIDIA)
    assert cfg["whisper_model"] == "large-v3-turbo"


def test_running_setup_again_keeps_the_chosen_device_and_precision():
    cfg = {"whisper_device": "cpu", "whisper_compute_type": "float32"}
    setup._pick_whisper_device(cfg, NVIDIA, rerun=True)
    assert cfg == {"whisper_device": "cpu", "whisper_compute_type": "float32"}


def test_choosing_the_cpu_stays_but_its_precision_is_repaired():
    cfg = {"whisper_device": "cpu", "whisper_compute_type": "int8_float16"}
    setup._pick_whisper_device(cfg, NVIDIA, rerun=True)
    assert cfg == {"whisper_device": "cpu", "whisper_compute_type": "int8"}


def test_the_server_and_the_cli_agree_on_what_only_cuda_runs():
    assert _settings._CUDA_ONLY_COMPUTE == hardware.CUDA_ONLY_COMPUTE


def test_a_gpu_that_is_gone_does_not_stay_the_device():
    cfg = {"whisper_device": "cuda", "whisper_compute_type": "float16"}
    setup._pick_whisper_device(cfg, NO_GPU, rerun=True)
    assert cfg == {"whisper_device": "cpu", "whisper_compute_type": "int8"}


@pytest.mark.parametrize(
    ("device", "compute"), [("auto", "float16"), ("auto", "int8_float16"), ("cpu", "float16"), ("cuda", "float16")]
)
def test_a_precision_only_cuda_can_run_does_not_stay_without_nvidia(device, compute):
    cfg = {"whisper_device": device, "whisper_compute_type": compute}
    setup._pick_whisper_device(cfg, AMD, rerun=True)
    assert cfg == {"whisper_device": "cpu", "whisper_compute_type": "int8"}


@pytest.mark.parametrize(("device", "compute"), [("auto", "auto"), ("auto", "int8"), ("cpu", "float32")])
def test_a_choice_that_can_run_stays_on_amd(device, compute):
    cfg = {"whisper_device": device, "whisper_compute_type": compute}
    setup._pick_whisper_device(cfg, AMD, rerun=True)
    assert cfg == {"whisper_device": device, "whisper_compute_type": compute}


@pytest.mark.parametrize(("installed", "method"), [(True, "deno"), (False, "cookies")])
def test_deno_auth_is_saved_only_when_deno_was_installed(installed, method):
    cfg = {"youtube_auth_method": "cookies"}
    with (
        patch.object(setup, "ask_arrow", return_value=1),
        patch.object(setup, "_install_with_retry", return_value=installed),
    ):
        setup._choose_youtube_auth(cfg)
    assert cfg["youtube_auth_method"] == method


@pytest.mark.parametrize(("works_after", "goes_on"), [(True, True), (False, False)])
def test_launch_goes_on_after_installing_the_translation_engine(works_after, goes_on):
    imports = iter([False, works_after])
    with (
        patch.object(launch, "_try_import", lambda m: next(imports)),
        patch.object(launch, "ask_yn", return_value=True),
        patch.object(launch, "pause"),
        patch("yume.installers.install_llamacpp_python"),
        patch.dict(sys.modules, {"fastapi": types.ModuleType("fastapi"), "uvicorn": types.ModuleType("uvicorn")}),
    ):
        assert launch._ensure_python_engine({}) is goes_on


@pytest.mark.parametrize(
    ("typed", "saved"), [("turbo", "large-v3-turbo"), ("large-v1", "large-v1"), ("large-v3", "large-v3")]
)
def test_model_switch_takes_what_the_server_takes_while_it_is_down(typed, saved):
    cfg = {"whisper_host": "127.0.0.1", "whisper_port": 5001}
    with (
        patch.object(whisper_model, "_server_get", return_value=None),
        patch.object(whisper_model, "_use_model") as use,
        patch.object(whisper_model, "success"),
    ):
        whisper_model._model_switch(cfg, typed)
    use.assert_called_once_with(cfg, saved)


def test_model_switch_still_refuses_a_typo_while_the_server_is_down():
    with (
        patch.object(whisper_model, "_server_get", return_value=None),
        patch.object(whisper_model, "_use_model") as use,
        patch.object(whisper_model, "error") as said,
    ):
        whisper_model._model_switch({"whisper_host": "h", "whisper_port": 1}, "turob")
    use.assert_not_called()
    said.assert_called_once()


def _version(days_old):
    return (datetime.date.today() - datetime.timedelta(days=days_old)).strftime("%Y.%m.%d")


@pytest.mark.parametrize(
    ("days_old", "method", "passed", "hint"),
    [
        (10, "cookies", True, ""),
        (200, "cookies", False, "Tools"),
        (200, "deno", False, "pip install -U yt-dlp"),
    ],
)
def test_health_flags_a_yt_dlp_too_old_for_youtube(days_old, method, passed, hint):
    reply = types.SimpleNamespace(returncode=0, stdout=_version(days_old), stderr="")
    with patch.object(health, "find_tool", return_value="/bin/yt-dlp"), patch("yume.utils._run", return_value=reply):
        _name, ok, detail = health._chk_ytdlp_age({"youtube_auth_method": method})
    assert ok is passed and hint in detail


class _Proc:
    def __init__(self, code=None):
        self.code = code
        self.returncode = code

    def poll(self):
        return self.code


def _supervisor(monkeypatch, tmp_path, ticks, starts=None):
    """A supervisor whose loop runs `ticks`: lists of (name, exit code or None)."""
    monkeypatch.setattr(service, "TICK_S", 0)
    monkeypatch.setattr(service, "STOP_FILE", tmp_path / "stop")
    monkeypatch.setattr(service.servers, "build_server_env", dict)
    sup = service._Supervisor({"auto_stop_minutes": 0})
    states, ticks = [], iter(ticks)
    monkeypatch.setattr(sup, "_state", lambda state, message="": states.append((state, message)))
    monkeypatch.setattr(sup, "_active", lambda: True)
    monkeypatch.setattr(sup, "_start_translation", lambda env: starts.append(1) or None)

    def dead():
        try:
            sup.procs.procs = [(n, _Proc(c)) for n, c in next(ticks)]
        except StopIteration:
            sup.procs.procs = []
            (tmp_path / "stop").write_text("")
        return servers.Processes.dead(sup.procs)

    monkeypatch.setattr(sup.procs, "dead", dead)
    return sup, states


def test_a_translator_crash_restarts_it_and_keeps_whisper(monkeypatch, tmp_path):
    starts = []
    sup, states = _supervisor(monkeypatch, tmp_path, [[("Whisper", None), ("Translation", 1)]], starts)
    assert sup._supervise() == 0
    assert starts == [1]
    assert [n for n, _p in sup.procs.procs] == []  # the fake loop ended
    assert all(state != "error" for state, _m in states)


def test_a_translator_that_keeps_crashing_stops_being_restarted(monkeypatch, tmp_path):
    starts = []
    crash = [("Whisper", None), ("Translation", 1)]
    sup, states = _supervisor(monkeypatch, tmp_path, [crash] * 5, starts)
    assert sup._supervise() == 0
    assert len(starts) == service.MAX_TRANSLATION_RESTARTS
    assert "untranslated" in states[-1][1] and states[-1][0] == "running"


def test_a_whisper_crash_still_ends_yume(monkeypatch, tmp_path):
    sup, states = _supervisor(monkeypatch, tmp_path, [[("Whisper", 1), ("Translation", None)]], [])
    assert sup._supervise() == 1
    assert states[-1][0] == "error" and "Whisper" in states[-1][1]


def test_forget_dead_keeps_what_runs():
    procs = servers.Processes()
    alive = _Proc()
    procs.procs = [("Whisper", alive), ("Translation", _Proc(1))]
    procs.forget_dead()
    assert procs.procs == [("Whisper", alive)]


@pytest.mark.parametrize(
    ("raw", "shown"),
    [
        ("ERROR: [youtube] aaaaaaaaaaa: This video is unavailable", "it may be private, deleted"),
        ("ERROR: [youtube] jfKfPfyJRdk: This live stream recording is not available.", "once a stream has ended"),
        ("ERROR: [generic] Something else went wrong", "Something else went wrong"),
        ("ERROR: [generic] Unable to download webpage: HTTP Error 404", "Cannot reach this page"),
        ("ERROR: [vimeo] The web client only works when logged-in. Use --cookies", "only works when you are logged in"),
        ("ERROR: [twitch:stream] some_channel: Nothing here", "Nothing here"),
    ],
)
def test_download_errors_read_as_sentences(raw, shown):
    message = _ytdlp.friendlify_ytdlp_error(raw)
    assert shown in message and not message.startswith("ERROR")
