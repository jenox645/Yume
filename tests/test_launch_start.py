"""The launcher starting each server (yume/launch.py): prompts, install offers and
what it says when a start fails. Processes and prompts are faked."""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import yume.installers  # noqa: E402
import yume.launch as launch  # noqa: E402
import yume.llama_server  # noqa: E402
from yume.servers import StartResult  # noqa: E402

NVIDIA = {"has_nvidia": True, "has_amd": False, "vram_mb": 12288, "name": "RTX 3060"}
AMD = {"has_nvidia": False, "has_amd": True, "vram_mb": 8192, "name": "RX 6800"}
CPU = {"has_nvidia": False, "has_amd": False, "vram_mb": 0, "name": None}
CFG = {
    "translation_host": "127.0.0.1", "translation_port": 5000, "gguf_model_path": "m.gguf",
    "whisper_host": "127.0.0.1", "whisper_port": 5001, "whisper_model": "small",
    "whisper_device": "cpu", "whisper_compute_type": "int8",
}  # fmt: skip


class Spin:
    def __init__(self, message=""):
        self.finished = []
        self.tick = MagicMock()

    def finish(self, outcome):
        self.finished.append(outcome)


@pytest.fixture
def said(monkeypatch):
    """What the launcher printed through the ui helpers."""
    lines = []
    for name in ("info", "warn", "error", "success"):
        monkeypatch.setattr(launch, name, lambda msg, name=name: lines.append(f"{name}: {msg}"))
    monkeypatch.setattr(launch, "pause", lambda *a: lines.append("pause"))
    monkeypatch.setattr(launch, "Spinner", Spin)
    return lines


def has(lines, text):
    return any(text in line for line in lines)


# ── _report ───────────────────────────────────────────────────────────────────


def test_a_ready_server_goes_on(said):
    spin = Spin()
    assert launch._report(StartResult(), spin, "Ready!", ["slow"]) is True
    assert spin.finished == ["ok"] and said == ["success: Ready!"]


def test_a_slow_server_warns_and_goes_on(said):
    spin = Spin()
    result = StartResult(error="x: did not become ready in time", timed_out=True)
    assert launch._report(result, spin, "Ready!", ["Still loading", "Hang on"]) is True
    assert spin.finished == ["timeout"] and said == ["warn: Still loading", "info: Hang on"]


def test_a_crash_shows_the_log_and_the_diagnosis_then_waits(said, monkeypatch, capsys):
    spin = Spin()
    monkeypatch.setattr(launch.servers, "log_tail", lambda name: [f"line {i}" for i in range(12)])
    result = StartResult(error="Whisper: exited with code 1", log_name="w.log", diagnosis=["Too big.", "Use small."])
    assert launch._report(result, spin, "Ready!", []) is False
    assert spin.finished == ["failed"]
    out = capsys.readouterr().out
    assert "line 11" in out and "line 2" in out and "line 1\x1b" not in out  # the last ten
    assert said[0] == "error: Whisper: exited with code 1"
    assert has(said, "Diagnosis: Too big.") and "info: Use small." in said
    assert has(said, f"Full log: {launch.LOGS_DIR / 'w.log'}") and said[-1] == "pause"


def test_a_failure_without_a_log_says_only_what_it_knows(said):
    assert launch._report(StartResult(error="no model"), Spin(), "Ready!", []) is False
    assert said == ["error: no model", "pause"]


# ── _start_llamacpp ───────────────────────────────────────────────────────────


class Llama:
    """_start_llamacpp with everything around it faked."""

    def __init__(self, monkeypatch, said, *, up=False, gguf="m.gguf", native=True, freed=True, result=None):
        self.said = said
        self.spawned = []
        self.server_paths = [Path("llama-server")] if native else []
        monkeypatch.setattr(launch, "check_translation_server", lambda *a: {"up": up})
        monkeypatch.setattr(launch.servers, "resolve_gguf", lambda cfg: gguf)
        monkeypatch.setattr(yume.llama_server, "server_path", self._server_path)
        monkeypatch.setattr(yume.llama_server, "installed_build", lambda: {"tag": "b1", "variant": "cuda"})
        monkeypatch.setattr(launch, "kill_port_process", lambda port: freed)
        monkeypatch.setattr(launch, "detect_gpu", lambda: CPU)

        def start(cfg, bi, procs, env, tick=None):
            self.spawned.append(gguf)
            return result or StartResult()

        monkeypatch.setattr(launch.servers, "start_llamacpp", start)

    def _server_path(self):
        return self.server_paths[0] if self.server_paths else None

    def go(self):
        return launch._start_llamacpp(dict(CFG), MagicMock(), {"hp": "/v1/models"}, {"E": "1"})


def test_llamacpp_already_running_starts_nothing(said, monkeypatch):
    llama = Llama(monkeypatch, said, up=True)
    assert llama.go() is True
    assert llama.spawned == [] and said == ["success: llama.cpp server already running!"]


def test_no_gguf_says_where_to_get_one(said, monkeypatch):
    llama = Llama(monkeypatch, said, gguf=None)
    assert llama.go() is False
    assert has(said, "No .gguf model found") and has(said, "Download GGUF Model") and said[-1] == "pause"
    assert llama.spawned == []


def test_the_built_in_engine_starts_and_says_which_model(said, monkeypatch):
    llama = Llama(monkeypatch, said)
    monkeypatch.setitem(sys.modules, "llama_cpp", None)  # not needed with llama-server
    assert llama.go() is True
    assert llama.spawned == ["m.gguf"]
    assert has(said, "Starting llama.cpp server with m.gguf") and has(said, "Engine: llama-server b1 (cuda)")
    assert "success: Translation server ready!" in said


def test_an_auto_selected_model_is_named(said, monkeypatch):
    llama = Llama(monkeypatch, said, gguf="models/other.gguf")
    assert llama.go() is True
    assert "info: Auto-selected GGUF: other.gguf" in said


def test_the_engine_installed_from_the_offer_is_used(said, monkeypatch):
    llama = Llama(monkeypatch, said, native=False)
    monkeypatch.setitem(sys.modules, "llama_cpp", None)

    def offer():
        llama.server_paths.append(Path("llama-server"))
        return True

    monkeypatch.setattr(launch, "_offer_llama_server", offer)
    assert llama.go() is True
    assert llama.spawned == ["m.gguf"] and has(said, "Engine: llama-server")


def test_no_engine_offers_llama_cpp_python(said, monkeypatch):
    for yes in (True, False):
        said.clear()
        llama = Llama(monkeypatch, said, native=False)
        monkeypatch.setattr(launch, "_offer_llama_server", lambda: False)
        monkeypatch.setitem(sys.modules, "llama_cpp", None)
        monkeypatch.setattr(launch, "ask_yn", lambda *a, **k: yes)
        installed = MagicMock()
        monkeypatch.setattr(yume.installers, "install_llamacpp_python", installed)
        assert llama.go() is False
        assert installed.called is yes and ("pause" in said) is not yes  # nothing to wait for after an install
        assert has(said, "llama-cpp-python not installed") and llama.spawned == []


def _python_engine(monkeypatch):
    monkeypatch.setitem(sys.modules, "llama_cpp", types.ModuleType("llama_cpp"))
    monkeypatch.setattr(launch, "_offer_llama_server", lambda: False)


def test_llama_cpp_python_with_its_server_packages_starts(said, monkeypatch):
    llama = Llama(monkeypatch, said, native=False)
    _python_engine(monkeypatch)
    monkeypatch.setitem(sys.modules, "fastapi", types.ModuleType("fastapi"))
    monkeypatch.setitem(sys.modules, "uvicorn", types.ModuleType("uvicorn"))
    monkeypatch.setattr(launch, "_run", lambda *a, **k: pytest.fail("nothing to install"))
    assert llama.go() is True
    assert llama.spawned == ["m.gguf"] and has(said, "running translation model on CPU")


def test_missing_server_packages_are_installed_with_the_server_env(said, monkeypatch):
    llama = Llama(monkeypatch, said, native=False)
    _python_engine(monkeypatch)
    monkeypatch.setitem(sys.modules, "fastapi", None)
    ran = []
    monkeypatch.setattr(
        launch, "_run", lambda cmd, **k: ran.append((cmd, k)) or types.SimpleNamespace(returncode=0, stderr="")
    )
    assert llama.go() is True
    cmd, kw = ran[0]
    assert cmd[1:4] == ["-m", "pip", "install"] and kw["env"] == {"E": "1"} and kw["timeout"] == 300
    assert "success: Server dependencies installed!" in said and llama.spawned == ["m.gguf"]


def test_a_failing_package_install_stops_the_launch(said, monkeypatch):
    llama = Llama(monkeypatch, said, native=False)
    _python_engine(monkeypatch)
    monkeypatch.setitem(sys.modules, "uvicorn", None)

    def run(*a, **k):
        raise OSError("no pip")

    monkeypatch.setattr(launch, "_run", run)
    assert llama.go() is False
    assert "error: Failed to install: no pip" in said and said[-1] == "pause" and llama.spawned == []


def test_a_port_that_stays_busy_stops_the_launch(said, monkeypatch):
    llama = Llama(monkeypatch, said, freed=False)
    assert llama.go() is False
    assert has(said, "Port 5000 is still in use") and said[-1] == "pause" and llama.spawned == []


def test_a_crashing_translation_server_is_reported(said, monkeypatch):
    llama = Llama(monkeypatch, said, result=StartResult(error="Translation: exited with code 3"))
    assert llama.go() is False
    assert "error: Translation: exited with code 3" in said


# ── which engine runs the model ───────────────────────────────────────────────


def _llama_module(monkeypatch, gpu_build):
    lib = types.ModuleType("llama_cpp.llama_cpp")
    if gpu_build:
        lib.ggml_backend_cuda_reg = lambda: None
    package = types.ModuleType("llama_cpp")
    package.llama_cpp = lib
    monkeypatch.setitem(sys.modules, "llama_cpp", package)
    monkeypatch.setitem(sys.modules, "llama_cpp.llama_cpp", lib)


@pytest.mark.parametrize(
    ("gpu", "gpu_build", "expected"),
    [
        (NVIDIA, True, "GPU offloading enabled"),
        (NVIDIA, False, "CPU-only build"),
        (CPU, True, "running translation model on CPU"),
    ],
)
def test_describe_engine(said, monkeypatch, gpu, gpu_build, expected):
    monkeypatch.setattr(launch, "detect_gpu", lambda: gpu)
    _llama_module(monkeypatch, gpu_build)
    launch._describe_engine(native=False)
    assert has(said, expected)


def test_describe_engine_on_a_machine_without_llama_cpp(said, monkeypatch):
    monkeypatch.setattr(launch, "detect_gpu", lambda: NVIDIA)
    monkeypatch.setitem(sys.modules, "llama_cpp", None)
    monkeypatch.setitem(sys.modules, "llama_cpp.llama_cpp", None)
    launch._describe_engine(native=False)
    assert has(said, "CPU-only build")


def test_amd_on_windows_runs_on_the_cpu(said, monkeypatch):
    monkeypatch.setattr(launch, "detect_gpu", lambda: AMD)
    monkeypatch.setattr(launch, "IS_WIN", True)
    launch._describe_engine(native=False)
    assert has(said, "running translation model on CPU")


class TestOfferLlamaServer:
    def _offer(self, monkeypatch, gpu, *, yes=True):
        monkeypatch.setattr(launch, "detect_gpu", lambda: gpu)
        monkeypatch.setattr(launch, "ask_yn", lambda *a, **k: yes)
        self.install = MagicMock(return_value=True)
        monkeypatch.setattr(yume.llama_server, "install", self.install)
        return launch._offer_llama_server()

    def test_nothing_to_offer_without_a_gpu(self, said, monkeypatch):
        assert self._offer(monkeypatch, CPU) is False
        assert said == [] and not self.install.called

    def test_nothing_to_offer_when_python_already_uses_the_gpu(self, said, monkeypatch):
        _llama_module(monkeypatch, True)
        assert self._offer(monkeypatch, NVIDIA) is False
        assert said == []

    @pytest.mark.parametrize("gpu_build_missing", [True, False])
    def test_a_gpu_on_the_cpu_build_is_offered_the_server(self, said, monkeypatch, gpu_build_missing):
        if gpu_build_missing:
            monkeypatch.setitem(sys.modules, "llama_cpp", None)
            monkeypatch.setitem(sys.modules, "llama_cpp.llama_cpp", None)
        else:
            _llama_module(monkeypatch, False)
        assert self._offer(monkeypatch, NVIDIA) is True
        self.install.assert_called_once_with(NVIDIA)
        assert has(said, "would run on the CPU")

    def test_declining_keeps_the_cpu_build(self, said, monkeypatch):
        monkeypatch.setitem(sys.modules, "llama_cpp", None)
        monkeypatch.setitem(sys.modules, "llama_cpp.llama_cpp", None)
        assert self._offer(monkeypatch, NVIDIA, yes=False) is False
        assert not self.install.called


# ── _start_ollama ─────────────────────────────────────────────────────────────


class Ollama:
    def __init__(self, monkeypatch, result, *, model="qwen2.5:7b", pulled=("qwen2.5:7b",)):
        self.cfg = {**CFG, "translation_port": 11434, "translation_model": model}
        self.saved = MagicMock()
        self.pull = MagicMock()
        monkeypatch.setattr(launch.servers, "start_ollama", lambda cfg, procs, env: result)
        monkeypatch.setattr(launch, "check_ollama_models", lambda host, port: list(pulled))
        monkeypatch.setattr(yume.installers, "pull_ollama_model", self.pull)
        monkeypatch.setattr("config.save_config", self.saved)

    def go(self):
        return launch._start_ollama(self.cfg, MagicMock(), {})


def test_ollama_already_running(said, monkeypatch):
    o = Ollama(monkeypatch, StartResult(already_running=True))
    assert o.go() is True
    assert said == ["success: Ollama already running"] and not o.pull.called


def test_ollama_started(said, monkeypatch):
    assert Ollama(monkeypatch, StartResult()).go() is True
    assert said == ["success: Ollama running!"]


def test_a_slow_ollama_goes_on(said, monkeypatch):
    assert Ollama(monkeypatch, StartResult(error="Ollama: slow", timed_out=True)).go() is True
    assert said == ["warn: Ollama may still be starting..."]


def test_a_failed_ollama_stops_and_suggests_llamacpp(said, monkeypatch):
    assert Ollama(monkeypatch, StartResult(error="Ollama is not installed")).go() is False
    assert said[0] == "error: Ollama is not installed" and has(said, "llama.cpp backend") and said[-1] == "pause"


@pytest.mark.parametrize("model", ["", "old-model.gguf"])
def test_ollama_needs_a_real_model_name(said, monkeypatch, model):
    o = Ollama(monkeypatch, StartResult(), model=model)
    assert o.go() is True
    assert o.cfg["translation_model"] == "qwen2.5:7b"
    o.saved.assert_called_once()
    assert has(said, "Translation model set to qwen2.5:7b") and not o.pull.called


@pytest.mark.parametrize(("pulled", "pulls"), [(["m"], False), (["m:latest"], False), (["other"], True)])
def test_a_model_ollama_does_not_have_is_pulled(said, monkeypatch, pulled, pulls):
    o = Ollama(monkeypatch, StartResult(), model="m", pulled=pulled)
    assert o.go() is True
    assert o.pull.called is pulls


# ── _start_whisper ────────────────────────────────────────────────────────────


def whisper(monkeypatch, *, up=False, freed=True, result=None):
    monkeypatch.setattr(launch.servers, "whisper_up", lambda cfg: up)
    monkeypatch.setattr(launch, "kill_port_process", lambda port: freed)
    monkeypatch.setattr(launch.servers, "resolve_whisper_device", lambda cfg: ("cuda", "float16"))
    start = MagicMock(return_value=result or StartResult())
    monkeypatch.setattr(launch.servers, "start_whisper", start)
    return launch._start_whisper(dict(CFG), MagicMock(), {}), start


def test_whisper_already_running(said, monkeypatch):
    ok, start = whisper(monkeypatch, up=True)
    assert ok is True and not start.called and said == ["success: Whisper already running!"]


def test_a_busy_whisper_port_stops_the_launch(said, monkeypatch):
    ok, start = whisper(monkeypatch, freed=False)
    assert ok is False and not start.called
    assert has(said, "Port 5001 is still in use") and said[-1] == "pause"


def test_whisper_started(said, monkeypatch):
    ok, _ = whisper(monkeypatch)
    assert ok is True
    assert "info: Device: cuda | Compute: float16 | Port: 5001" in said and "success: Whisper server ready!" in said


def test_a_first_run_download_keeps_going(said, monkeypatch):
    ok, _ = whisper(monkeypatch, result=StartResult(error="Whisper: slow", timed_out=True))
    assert ok is True and has(said, "first run takes 30-60s") and has(said, "one-time download")


def test_an_unrecognised_crash_gets_the_quick_fixes(said, monkeypatch):
    ok, _ = whisper(monkeypatch, result=StartResult(error="Whisper: exited with code 1"))
    assert ok is False
    assert has(said, "Not recognised from the log") and has(said, "Switch to CPU") and has(said, "Re-run setup")


def test_a_recognised_crash_keeps_its_diagnosis(said, monkeypatch):
    result = StartResult(error="Whisper: exited with code 1", diagnosis=["Out of VRAM."])
    ok, _ = whisper(monkeypatch, result=result)
    assert ok is False and has(said, "Diagnosis: Out of VRAM.") and not has(said, "Not recognised")
