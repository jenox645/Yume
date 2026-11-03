"""The setup wizard, driven end to end with every prompt and probe faked.

Each test scripts the answers to the wizard's questions and checks the config
it leaves behind and the installers it ran — no network, GPU or real tools.
"""

from __future__ import annotations

import io
import sys
from pathlib import Path
from contextlib import ExitStack, redirect_stdout
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

    def run(self, cfg, rerun=False):
        import yume
        import yume.installers
        import yume.llama_server
        import yume.menus

        ls = MagicMock()
        ls.server_path.return_value = "llama-server.exe" if self.engine else None
        ls.build_label.return_value = "llama-server b9000 (cuda)"
        ls.python_engine_on_gpu.return_value = None
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
            "_missing_for_amd": lambda gpu: [],  # tests/test_whispercpp.py covers the offer
            "_present_models": MagicMock(return_value=False),  # its own tests below
            "_fetch_whisper": MagicMock(),  # never a real download
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
            return setup.setup_wizard(cfg, rerun=rerun)


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


@pytest.mark.parametrize("win", [True, False])
def test_amd_leaves_the_device_to_what_is_installed_on_every_os(win):
    with (
        patch.object(setup, "IS_WIN", win),
        patch("yume.hardware.detect_ram_gb", return_value=32.0),
        patch("yume.whisper_cpp.server_path", return_value=None),
    ):
        cfg = Wizard(AMD_8GB).run({})
    assert (cfg["whisper_device"], cfg["whisper_compute_type"]) == ("auto", "auto")
    assert cfg["whisper_model"] == "small"  # the CPU's pick until the engine is in


@pytest.mark.parametrize("compute", ["float16", "int8_float16"])
def test_running_setup_again_repairs_what_the_old_amd_setup_saved(compute):
    w = Wizard(AMD_8GB)
    cfg = w.run({"whisper_model": "large-v3-turbo", "whisper_device": "auto", "whisper_compute_type": compute}, True)
    assert (cfg["whisper_device"], cfg["whisper_compute_type"]) == ("auto", "auto")
    assert cfg["whisper_model"] == "large-v3-turbo"  # the model stays the user's choice
    assert w.saved


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


def _present(gpu, arrows, model="small"):
    cfg = {"whisper_model": model}
    out = io.StringIO()
    with patch.object(setup, "ask_arrow", side_effect=arrows), redirect_stdout(out):
        taken = setup._present_models(cfg, gpu)
    return taken, cfg, out.getvalue()


@pytest.mark.parametrize(
    ("gpu", "translator"),
    [
        ({**NVIDIA_12GB}, "Hy-MT2-7B"),
        ({**NVIDIA_6GB, "vram_mb": 8192}, "Hy-MT2-7B"),
        ({**NVIDIA_6GB, "vram_mb": 4096}, "Hy-MT2-1.8B"),  # a 4 GB laptop
        (NO_GPU, "Hy-MT2-1.8B"),
    ],
)
def test_a_first_setup_names_both_models_for_this_pc(gpu, translator):
    taken, _cfg, said = _present(gpu, [0])
    assert taken is True
    assert "Hears" in said and "Translates" in said and translator in said
    assert "GB from Hugging Face" in said and "GGUF" in said and "faster-whisper" in said


def test_choosing_myself_lists_every_whisper_model_with_the_recommended_one_marked():
    asked = []

    def arrows(prompt, options, **k):
        asked.append(options)
        return 1 if len(asked) == 1 else [o[0] for o in options].index("base")

    taken, cfg, _said = _present(NVIDIA_6GB, arrows)
    assert taken is False and cfg["whisper_model"] == "base"
    labels = [o[0] for o in asked[1]]
    assert len(labels) == len(setup.WHISPER_MODELS) and "small  ★ recommended" in labels


def test_choosing_myself_with_a_custom_whisper_model_still_offers_the_list():
    taken, cfg, _said = _present(NVIDIA_6GB, [1, 0], model="D:/models/mine")
    assert taken is False and cfg["whisper_model"] == "tiny"


def test_the_recommended_translator_is_downloaded_with_its_pin(tmp_path):
    cfg = {"translation_backend": "llamacpp"}
    with (
        patch("yume.network.hf_download", return_value=True) as download,
        patch.object(setup, "GGUF_DIR", tmp_path),
        patch.object(setup, "detect_gpu", return_value=NO_GPU),
        patch.object(setup, "_fetch_whisper"),
        patch.object(setup, "success"),
    ):
        setup._choose_translation_model(cfg, True, recommended=True)
    pick = setup.TRANSLATORS["Hy-MT2-1.8B"]
    download.assert_called_once_with(pick["repo"], pick["file"], pick["sha256"])
    assert cfg["gguf_model_path"] == str(tmp_path / pick["file"])


def test_a_failed_download_falls_back_to_the_choice():
    cfg = {"translation_backend": "llamacpp"}
    with (
        patch("yume.network.hf_download", return_value=False),
        patch.object(setup, "detect_gpu", return_value=NO_GPU),
        patch.object(setup, "_fetch_whisper"),
        patch.object(setup, "warn"),
        patch.object(setup, "ask_arrow", return_value=2) as asked,
    ):
        setup._choose_translation_model(cfg, True, recommended=True)
    assert asked.call_args.args[0] == "How to get a model?" and "gguf_model_path" not in cfg


@pytest.mark.parametrize(
    ("cfg", "device", "fetched"),
    [
        ({"whisper_model": "small"}, "cuda", True),
        ({"whisper_model": "small"}, "vulkan", False),  # whisper.cpp has its own files
        ({"whisper_model": "D:/models/mine"}, "cuda", False),
    ],
)
def test_the_whisper_model_downloads_during_setup(cfg, device, fetched):
    download = MagicMock()
    with (
        patch("yume.servers.resolve_whisper_device", return_value=(device, "int8")),
        patch.dict(sys.modules, {"faster_whisper": MagicMock(download_model=download)}),
        patch.object(setup, "info"),
        patch.object(setup, "success"),
    ):
        setup._fetch_whisper(cfg)
    assert download.called is fetched


def test_every_recommended_translator_is_one_the_browser_lists():
    from yume.menus.hf_browser import _HF_RECOMMENDED_REPOS

    assert {t["repo"] for t in setup.TRANSLATORS.values()} <= {r for r, _d in _HF_RECOMMENDED_REPOS}


def test_a_coloured_cell_keeps_the_columns(capsys):
    setup._row("Whisper", "[1msmall[0m", "ready")
    setup._row("Translation model", "plain", "ready")
    lines = [ln.replace("[1m", "").replace("[0m", "") for ln in capsys.readouterr().out.splitlines()]
    assert lines[0].index("ready") == lines[1].index("ready")


@pytest.mark.parametrize(("complete", "ending"), [(True, "Ready."), (False, "Almost ready.")])
def test_setup_says_ready_only_when_nothing_is_missing(capsys, complete, ending):
    with (
        patch.object(setup, "_offer_one_click"),
        patch.object(setup, "_offer_vocal_isolation"),
        patch.object(setup, "_extension_guide"),
        patch.object(setup, "pause"),
    ):
        setup._closing_offers(complete)
    out = capsys.readouterr().out
    assert ending in out and ("Ready." in out) is complete


def test_other_backends_need_no_engine_or_gguf():
    w = Wizard(NVIDIA_6GB, gguf=False, engine=False)
    cfg = w.run({"translation_backend": "ollama"})
    assert w.arrow_prompts == []
    assert cfg["first_run_complete"] is True


def _uninstall(tmp_path, monkeypatch, answer):
    """A full uninstall with every folder in tmp_path; `answer` is every yes/no after the first."""
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path / "hf"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("DENO_DIR", str(tmp_path / "deno"))
    monkeypatch.delenv("TORCH_HOME", raising=False)
    yume_model = tmp_path / "hf" / "models--Systran--faster-whisper-small"
    other_tool = tmp_path / "hf" / "models--someone--my-faster-whisper-finetune"
    weights = tmp_path / "cache" / "torch" / "hub" / "checkpoints" / setup._HTDEMUCS_WEIGHTS
    own = tmp_path / "tools"
    for folder in (yume_model, other_tool, tmp_path / "deno", tmp_path / "cache" / "yt-dlp", own, weights.parent):
        folder.mkdir(parents=True)
    weights.write_bytes(b"x")
    data = MagicMock()
    with (
        patch.object(setup, "ask_arrow", return_value=1),
        patch.object(setup, "ask_yn", side_effect=lambda *a, **k: answer),
        patch.object(setup, "_uninstall_targets", return_value=[("Tools", own)]),
        patch.object(setup, "_remove_data", data),
        patch.object(setup, "header"),
        patch.object(setup, "pause"),
    ):
        setup.uninstall_yume()
    return data.call_args.args[0], own, yume_model, other_tool, weights


def test_shared_files_are_only_removed_when_asked_one_by_one(tmp_path, monkeypatch):
    removed, own, yume_model, other_tool, weights = _uninstall(tmp_path, monkeypatch, True)
    paths = [p for _label, p in removed]
    assert own in paths and yume_model in paths and weights in paths
    assert other_tool not in paths  # the name alone does not make it Yume's


def test_a_model_of_another_tool_is_never_even_asked_about(tmp_path, monkeypatch):
    monkeypatch.setenv("HF_HUB_CACHE", str(tmp_path))
    assert not [p for _label, p in setup._shared_targets() if "finetune" in p.name]


@pytest.mark.parametrize("answers", [[False, False, False, False], [False, True, False, False]])
def test_the_answer_decides_each_shared_item(tmp_path, answers):
    a, b, c, d = (tmp_path / n for n in "abcd")
    for p in (a, b, c, d):
        p.mkdir()
    targets = [("a", a), ("b", b), ("c", c), ("d", d), ("gone", tmp_path / "nope")]
    with patch.object(setup, "ask_yn", side_effect=answers), patch.object(setup, "info"):
        chosen = setup._ask_shared(targets)
    assert [t[0] for t in chosen] == [t[0] for t, yes in zip(targets, answers, strict=False) if yes]


def test_the_default_answer_keeps_shared_files(tmp_path):
    (tmp_path / "a").mkdir()
    with patch.object(setup, "ask_yn", return_value=False) as ask, patch.object(setup, "info"):
        setup._ask_shared([("a", tmp_path / "a")])
    assert ask.call_args.args[1] is False


def test_the_whisper_models_listed_are_the_ones_faster_whisper_downloads():
    utils = pytest.importorskip("faster_whisper.utils")
    names = ("tiny", "base", "small", "medium", "large-v1", "large-v2", "large-v3", "large-v3-turbo")
    expected = {"models--" + utils._MODELS[m].replace("/", "--") for m in names}
    assert expected == set(setup._WHISPER_REPOS)


def test_a_file_is_removed_as_a_file(tmp_path):
    weights = tmp_path / "w.th"
    weights.write_bytes(b"x")
    with (
        patch("yume.service.request_stop"),
        patch("yume.native_host.unregister"),
        patch.object(setup, "BASE_DIR", tmp_path),
        patch.object(setup, "MODELS_DIR", tmp_path / "models"),
        patch.object(setup, "success"),
    ):
        setup._remove_data([("Weights", weights)])
    assert not weights.exists()


@pytest.mark.parametrize(("own_venv", "installed", "yes", "expected"), [
    (True, True, True, []),  # it goes with the folder
    (False, False, True, []),
    (False, True, False, []),
    (False, True, True, ["torch", "torchaudio"]),
])  # fmt: skip
def test_pytorch_goes_only_when_asked(own_venv, installed, yes, expected):
    with (
        patch.object(setup, "_in_own_venv", return_value=own_venv),
        patch.object(setup.importlib.util, "find_spec", return_value=object() if installed else None),
        patch.object(setup, "ask_yn", return_value=yes),
    ):
        assert setup._ask_torch() == expected


def test_a_venv_elsewhere_is_not_yumes(tmp_path):
    with (
        patch.object(setup.sys, "prefix", str(tmp_path / "other")),
        patch.object(setup.sys, "base_prefix", "/usr"),
        patch.object(setup, "BASE_DIR", tmp_path / "yume"),
    ):
        assert setup._in_own_venv() is False
    with (
        patch.object(setup.sys, "prefix", str(tmp_path / "yume" / "yume-env")),
        patch.object(setup.sys, "base_prefix", "/usr"),
        patch.object(setup, "BASE_DIR", tmp_path / "yume"),
    ):
        assert setup._in_own_venv() is True


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
        patch.object(setup, "_shared_targets", return_value=[]),
        patch.object(setup, "_ask_torch", return_value=[]),
        patch.object(setup, "_remove_data") as remove_data,
        patch.object(setup, "_remove_packages") as remove_packages,
        patch.object(setup, "header"),
        patch.object(setup, "pause"),
    ):
        setup.uninstall_yume()
    assert remove_data.called is data and remove_packages.called is pip
