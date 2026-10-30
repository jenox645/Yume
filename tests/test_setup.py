"""The setup wizard, driven end to end with every prompt and probe faked.

Each test scripts the answers to the wizard's questions and checks the config
it leaves behind and the installers it ran — no network, GPU or real tools.
"""

from __future__ import annotations

import sys
from pathlib import Path
from contextlib import ExitStack
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import yume.setup as setup  # noqa: E402

NVIDIA_6GB = {"has_nvidia": True, "has_amd": False, "vram_mb": 6144, "name": "RTX 3060", "vendor": "nvidia"}
NVIDIA_12GB = {**NVIDIA_6GB, "vram_mb": 12288}
AMD_8GB = {"has_nvidia": False, "has_amd": True, "vram_mb": 8192, "name": "RX 6600", "vendor": "amd"}
NO_GPU = {"has_nvidia": False, "has_amd": False, "vram_mb": 0, "name": None, "vendor": "none"}

BACKEND_INFO = {
    "llamacpp": {"name": "llama.cpp", "dh": "127.0.0.1", "dp": 5000, "hp": "/health"},
    "ollama": {"name": "Ollama", "dh": "127.0.0.1", "dp": 11434, "hp": "/api/tags"},
    "custom": {"name": "Custom", "dh": "127.0.0.1", "dp": 8080, "hp": "/"},
}


class Wizard:
    """Fakes for everything the wizard touches; `run()` drives it."""

    def __init__(self, gpu, *, tools=("yt-dlp", "ffmpeg", "deno"), gguf=True, whisper=True, engine=True):
        self.gpu = gpu
        self.tools = set(tools)
        self.gguf = [Path("models/translation/qwen.gguf")] if gguf else []
        self.whisper = whisper
        self.engine = engine
        self.arrows: list[int] = []
        self.yns: list[bool] = []
        self.arrow_prompts: list[str] = []
        self.installed: list[str] = []
        self.saved = 0
        self.finished = MagicMock()

    def _arrow(self, prompt, *a, **k):
        self.arrow_prompts.append(prompt)
        return self.arrows.pop(0)

    def _yn(self, *a, **k):
        return self.yns.pop(0)

    def _installer(self, name):
        def install(*a, **k):
            self.installed.append(name)
            if name in ("yt-dlp", "ffmpeg"):
                self.tools.add(name)
            return True

        return install

    def _save(self, cfg):
        self.saved += 1

    def run(self, cfg):
        import yume
        import yume.installers
        import yume.llama_server
        import yume.menus

        ls = MagicMock()
        ls.server_path.return_value = "llama-server.exe" if self.engine else None
        ls.installed_build.return_value = {"tag": "b9000", "variant": "cuda"}
        ls.install = self._installer("llama-server")
        ls.LLAMA_DIR = Path("tools/llama.cpp")
        modules = {
            "faster_whisper": MagicMock() if self.whisper else None,
            "llama_cpp": None,
            "llama_cpp.llama_cpp": None,
            "yume.llama_server": ls,
        }
        fakes = {
            "BACKEND_INFO": BACKEND_INFO,
            "detect_gpu": MagicMock(return_value=self.gpu),
            "detect_ram_gb": MagicMock(return_value=32.0),
            "disk_free_gb": MagicMock(return_value=100.0),
            "find_tool": lambda t: f"/tools/{t}" if t in self.tools else None,
            "find_gguf_models": lambda: list(self.gguf),
            "check_server": MagicMock(return_value={"up": False}),
            "_try_import": lambda m: self.whisper,
            "ask_arrow": self._arrow,
            "ask_yn": self._yn,
            "pause": MagicMock(),
            "header": MagicMock(),  # it clears the console
            "time": MagicMock(),
            "_offer_one_click": self.finished,
            "_offer_vocal_isolation": MagicMock(),
            "_extension_guide": MagicMock(),
        }
        installers = {
            "install_ytdlp": "yt-dlp",
            "install_ffmpeg": "ffmpeg",
            "install_deno": "deno",
            "install_python_deps": "python-deps",
            "install_ollama": "ollama",
            "pull_ollama_model": "ollama-pull",
        }
        with ExitStack() as stack:
            stack.enter_context(patch.dict(sys.modules, modules))
            stack.enter_context(patch.object(yume, "llama_server", ls))
            for name, fake in fakes.items():
                stack.enter_context(patch.object(setup, name, fake))
            for name, label in installers.items():
                stack.enter_context(patch.object(yume.installers, name, self._installer(label)))
            stack.enter_context(patch.object(yume.menus, "browse_hf", self._installer("hf-browse")))
            stack.enter_context(patch("config.save_config", side_effect=self._save))
            stack.enter_context(patch("config.config_export"))
            return setup.setup_wizard(cfg)


def test_everything_present_finishes_after_the_scan():
    w = Wizard(NVIDIA_6GB)
    cfg = w.run({"translation_backend": "llamacpp"})
    assert cfg["first_run_complete"] is True
    assert cfg["whisper_device"] == "cuda"
    assert cfg["whisper_compute_type"] == "int8_float16"
    assert cfg["gguf_model_path"] == str(Path("models/translation/qwen.gguf"))
    assert w.arrow_prompts == []  # no questions when nothing is missing
    assert w.installed == []
    w.finished.assert_called_once()


@pytest.mark.parametrize(
    ("gpu", "device", "compute"),
    [
        (NVIDIA_12GB, "cuda", "float16"),
        (NVIDIA_6GB, "cuda", "int8_float16"),
        ({**NVIDIA_6GB, "vram_mb": 2048}, "cuda", "int8"),
        (NO_GPU, "cpu", "int8"),
    ],
)
def test_whisper_device_and_precision_follow_the_gpu(gpu, device, compute):
    cfg = Wizard(gpu).run({})
    assert (cfg["whisper_device"], cfg["whisper_compute_type"]) == (device, compute)


def test_amd_uses_rocm_off_windows():
    with patch.object(setup, "IS_WIN", False):
        cfg = Wizard(AMD_8GB).run({})
    assert (cfg["whisper_device"], cfg["whisper_compute_type"]) == ("auto", "float16")


def test_a_custom_whisper_model_path_is_kept():
    custom = str(Path("D:/models/my-whisper"))
    cfg = Wizard(NVIDIA_6GB).run({"whisper_model": custom})
    assert cfg["whisper_model"] == custom


def test_a_chosen_gguf_is_kept_on_a_rerun(tmp_path):
    mine = tmp_path / "mine.gguf"
    mine.write_text("x")
    cfg = Wizard(NVIDIA_6GB).run({"gguf_model_path": str(mine), "translation_model": "mine.gguf"})
    assert cfg["gguf_model_path"] == str(mine)
    assert cfg["translation_model"] == "mine.gguf"


def test_missing_tools_install_all_with_browser_cookies():
    w = Wizard(NVIDIA_6GB, tools=(), whisper=False, engine=False)
    # auth: cookies → firefox; mode: install all
    w.arrows = [0, 1, 0]
    cfg = w.run({})
    assert cfg["youtube_auth_method"] == "cookies"
    assert cfg["cookies_browser"] == "firefox"
    assert w.installed == ["yt-dlp", "ffmpeg", "python-deps", "llama-server"]
    assert cfg["first_run_complete"] is True


def test_choose_each_installs_only_what_is_accepted():
    w = Wizard(NO_GPU, tools=("deno",))
    w.arrows = [2, 1]  # auth: skip; mode: choose each
    w.yns = [False, True]  # yt-dlp: no; FFmpeg: yes
    cfg = w.run({})
    assert w.installed == ["ffmpeg"]
    assert "youtube_auth_method" not in cfg


def test_deno_auth_installs_deno():
    w = Wizard(NVIDIA_6GB, tools=("ffmpeg",))
    w.arrows = [1, 0]  # auth: deno; mode: install all
    cfg = w.run({})
    assert cfg["youtube_auth_method"] == "deno"
    assert w.installed == ["deno", "yt-dlp"]


def test_skip_installing_still_completes_setup():
    w = Wizard(NVIDIA_6GB, tools=())
    w.arrows = [2, 2]  # auth: skip; mode: skip
    cfg = w.run({})
    assert w.installed == []
    assert cfg["first_run_complete"] is True
    w.finished.assert_not_called()  # skipping returns before the closing offers


def test_no_model_download_from_huggingface():
    w = Wizard(NVIDIA_6GB, gguf=False)
    w.arrows = [2, 0, 0]  # auth: skip; mode: install all; model: HuggingFace
    w.run({})
    assert w.installed == ["hf-browse"]


def test_no_model_switch_to_ollama():
    w = Wizard(NVIDIA_6GB, gguf=False)
    w.arrows = [2, 0, 1]  # auth: skip; mode: install all; model: Ollama
    w.yns = [True, True]  # install Ollama, pull the model
    cfg = w.run({})
    assert cfg["translation_backend"] == "ollama"
    assert cfg["translation_port"] == 11434
    assert cfg["translation_model"] == "qwen2.5:7b"
    assert w.installed == ["ollama", "ollama-pull"]


def test_pulling_an_ollama_model_starts_ollama_when_it_is_down():
    import types

    import yume.installers as inst
    import yume.network

    ran = []
    for up in (False, True):
        with (
            patch.object(yume.network, "check_server", return_value={"up": up}),
            patch.object(inst, "find_ollama", return_value="ollama"),
            patch.object(inst.subprocess, "Popen") as popen,
            patch.object(inst, "_run", lambda cmd, **kw: ran.append(cmd) or types.SimpleNamespace(returncode=0)),
            patch.object(inst.time, "sleep"),
            patch.object(inst, "info"),
            patch.object(inst, "success"),
        ):
            assert inst.pull_ollama_model("qwen2.5:7b")
        assert popen.called is not up  # `ollama serve` only when nothing answered
    assert ran == [["ollama", "pull", "qwen2.5:7b"]] * 2


def test_other_backends_need_no_engine_or_gguf():
    w = Wizard(NVIDIA_6GB, gguf=False, engine=False)
    cfg = w.run({"translation_backend": "ollama"})
    assert w.arrow_prompts == []
    assert cfg["first_run_complete"] is True


def test_uninstall_lists_every_package_yume_installs_and_not_shared_ones():
    pkgs = setup._yume_pip_packages()
    for name in ("faster-whisper", "fugashi", "unidic-lite", "pypinyin", "llama-cpp-python", "uvicorn",
                 "bgutil-ytdlp-pot-provider", "demucs"):  # fmt: skip
        assert name in pkgs
    assert not {"numpy", "av", "torch"} & set(pkgs)
    assert len(pkgs) == len(set(pkgs))


@pytest.mark.parametrize(
    ("choice", "data", "pip"), [(0, True, True), (1, True, False), (2, False, True), (3, False, False)]
)
def test_uninstall_choices(choice, data, pip):
    with (
        patch.object(setup, "ask_arrow", return_value=choice),
        patch.object(setup, "ask_yn", return_value=True),
        patch.object(setup, "_uninstall_targets", return_value=[]),
        patch.object(setup, "_remove_data") as remove_data,
        patch.object(setup, "_remove_packages") as remove_packages,
        patch.object(setup, "header"),
        patch.object(setup, "pause"),
    ):
        setup.uninstall_yume()
    assert remove_data.called is data and remove_packages.called is pip
