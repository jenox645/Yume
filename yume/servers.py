"""Yume's server processes: commands, environment, logs, start, readiness, stop.

Shared by the two ways Yume runs: the interactive launcher (launch.py: asks,
shows a spinner, explains a crash on screen) and the headless supervisor that
one-click start runs (service.py: no one to ask, writes its state for the
extension). Each used to carry its own copy of spawning, waiting, crash
diagnosis and stopping; only the interaction differs now.

Starting a server returns a StartResult instead of printing, so each front end
decides how to show it. Servers found already running are used, never
restarted, and never stopped by Yume.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from yume.constants import TOOLS_DIR, SERVER_DIR, LOGS_DIR, IS_WIN, HEALTH_PATH_OLLAMA, CREATE_NO_WINDOW
from yume.hardware import detect_gpu
from yume.utils import _run, find_gguf_models, find_ollama

_log = logging.getLogger("pocket_yume")

WHISPER_LOG = "whisper_server.log"
TRANSLATION_LOG = "translation_server.log"
OLLAMA_LOG = "ollama.log"
WHISPER_START_TIMEOUT_S = 240  # first run downloads the model (~1-3 GB)
TRANSLATION_START_TIMEOUT_S = 180  # a large GGUF takes a while to load
OLLAMA_START_TIMEOUT_S = 60

# The config key holding each server's port: only these ports are freed when
# Yume stops its servers (an Ollama service the user runs is never touched)
PORT_KEYS = {"Whisper": "whisper_port", "Translation": "translation_port"}

# Python packages llama-cpp-python's OpenAI server needs (one pinned list,
# used by the launcher, the setup wizard and the installer).
LLAMA_SERVER_DEPS = [
    "uvicorn==0.42.0",
    "fastapi==0.135.1",
    "sse-starlette==3.3.3",
    "starlette-context==0.5.1",
    "pydantic-settings==2.13.1",
]


# ── Interpreters ──────────────────────────────────────────────────────────────


def console_python() -> str:
    """python.exe next to the running interpreter (pythonw has no stdout)."""
    exe = Path(sys.executable)
    if exe.stem.lower() == "pythonw":
        cand = exe.with_name("python" + exe.suffix)
        if cand.exists():
            return str(cand)
    return str(exe)


def windowless_python() -> str:
    """pythonw.exe on Windows (no console flashes up), the interpreter elsewhere."""
    exe = Path(sys.executable)
    if IS_WIN and exe.stem.lower() == "python":
        cand = exe.with_name("pythonw" + exe.suffix)
        if cand.exists():
            return str(cand)
    return str(exe)


# ── Commands and environment ──────────────────────────────────────────────────


def build_server_env() -> dict:
    """Environment for server processes: Yume's tools/ on PATH, AMD RDNA1 override."""
    env = os.environ.copy()
    tp = str(TOOLS_DIR)
    if tp not in env.get("PATH", ""):
        env["PATH"] = tp + os.pathsep + env.get("PATH", "")
    if not IS_WIN and "HSA_OVERRIDE_GFX_VERSION" not in env and detect_gpu().get("has_amd"):
        try:
            out = _run(["rocminfo"], timeout=10)
            if out.returncode == 0:
                arches = re.findall(r"gfx(\d+)", out.stdout)
                if any(a in ("1010", "1011", "1012") for a in arches):
                    env["HSA_OVERRIDE_GFX_VERSION"] = "10.3.0"
        except Exception as e:
            _log.debug("[build_server_env] rocm-detect failed: %s", e)
    return env


def resolve_whisper_device(cfg: dict) -> tuple[str, str]:
    """(device, compute_type) with "auto" resolved for this machine."""
    dev = cfg["whisper_device"]
    comp = cfg["whisper_compute_type"]
    gpu = detect_gpu()
    if dev == "auto":
        dev = "cuda" if gpu["has_nvidia"] or (gpu.get("has_amd") and not IS_WIN) else "cpu"
    if comp == "auto":
        comp = (
            "float16"
            if dev == "cuda" and gpu.get("vram_mb", 0) >= 8000
            else ("int8_float16" if dev == "cuda" else "int8")
        )
    return dev, comp


def whisper_command(cfg: dict) -> list | None:
    """Command line for the Whisper server, or None if the script is missing."""
    from config import CONFIG_FILE

    ss = SERVER_DIR / "faster_whisper_server.py"
    if not ss.exists():
        return None
    dev, comp = resolve_whisper_device(cfg)
    cmd = [sys.executable, str(ss), "--model", cfg["whisper_model"], "--device", dev,
           "--compute-type", comp, "--port", str(cfg["whisper_port"])]  # fmt: skip
    if CONFIG_FILE.exists():
        cmd.extend(["--config", str(CONFIG_FILE)])
    return cmd


def llamacpp_command(cfg: dict, gguf_path: str, port: int) -> list:
    """Command line for the llama.cpp translation server: llama.cpp's own
    llama-server when installed (prebuilt for the GPU), else llama-cpp-python."""
    from yume import llama_server

    if llama_server.server_path():
        gpu_build = llama_server.installed_build().get("variant", "cpu") != "cpu"
        return llama_server.command(gguf_path, cfg.get("translation_host", "127.0.0.1"), port, gpu_build)
    cmd = [
        sys.executable, "-m", "llama_cpp.server", "--model", gguf_path,
        "--host", cfg.get("translation_host", "127.0.0.1"), "--port", str(port),
        # Batch translation requests up to ~2k output tokens on top of the
        # prompt — a 2048 context leaves no room for long sections.
        "--n_ctx", "4096",
    ]  # fmt: skip
    gpu = detect_gpu()
    if gpu["has_nvidia"] or (gpu["has_amd"] and not IS_WIN):
        cmd.extend(["--n_gpu_layers", "-1"])
    return cmd


def resolve_gguf(cfg: dict) -> str | None:
    """Configured GGUF path, or the first model in models/translation/ (saved)."""
    gp = cfg.get("gguf_model_path", "")
    if gp and Path(gp).exists():
        return gp
    gfs = find_gguf_models()
    if not gfs:
        return None
    from config import save_config

    cfg["gguf_model_path"] = str(gfs[0])
    save_config(cfg)
    return str(gfs[0])


# ── Logs ──────────────────────────────────────────────────────────────────────


def open_rotating_log(name: str):
    """Open a log file for subprocess output, rotating if it exceeds 5 MB.

    Rotation happens before opening so the subprocess always writes to a fresh
    (or small) file. RotatingFileHandler only fires when Python writes through
    the handler — it cannot rotate data a subprocess writes to a plain handle.
    """
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    lp = LOGS_DIR / name
    max_bytes = 5 * 1024 * 1024
    backup_count = 3
    if lp.exists() and lp.stat().st_size > max_bytes:
        for i in range(backup_count - 1, 0, -1):
            old = LOGS_DIR / f"{name}.{i}"
            if old.exists():
                old.replace(LOGS_DIR / f"{name}.{i + 1}")
        lp.replace(LOGS_DIR / f"{name}.1")
    return open(str(lp), "a", encoding="utf-8", errors="replace")  # noqa: SIM115


def log_tail(name: str, lines: int = 15) -> list[str]:
    """The last lines of a server log ([] if it cannot be read)."""
    try:
        with open(LOGS_DIR / name, encoding="utf-8", errors="replace") as f:
            return [ln.rstrip() for ln in f.readlines()[-lines:]]
    except OSError:
        return []


# ── Crash diagnosis ───────────────────────────────────────────────────────────

# (servers it applies to, test on the lower-cased log tail, advice). The first
# line of the advice is the diagnosis; the rest says what to do.
_DIAGNOSES = [
    (
        {"Whisper", "Translation"},
        lambda t: "cuda" in t and ("out of memory" in t or "oom" in t or "alloc" in t),
        [
            "The GPU does not have enough free VRAM for this model.",
            "Choose a smaller model in Settings (Whisper: 'small'; translation: a smaller quantization),",
            "or close other programs using the GPU.",
        ],
    ),
    (
        {"Whisper"},
        lambda t: "cublas" in t or "cudnn" in t or "cudart" in t,
        [
            "CUDA libraries are missing or incomplete.",
            "Install the CUDA Toolkit (https://developer.nvidia.com/cuda-toolkit),",
            "or set the Whisper device to 'cpu' in Settings.",
        ],
    ),
    (
        {"Whisper", "Translation"},
        lambda t: "modulenotfounderror" in t or "no module named" in t,
        ["A Python package is missing.", "Run: python pocket_yume.py setup (it reinstalls the packages)."],
    ),
    (
        {"Whisper", "Translation", "Ollama"},
        lambda t: "address already in use" in t or ("port" in t and "in use" in t),
        ["Its port is already in use by another program.", "Change the port in Settings, or stop the other program."],
    ),
    (
        {"Whisper"},
        lambda t: "permission" in t or "access denied" in t or "errno 13" in t,
        ["Permission denied — it cannot read the model files or use the GPU."],
    ),
    (
        {"Whisper"},
        lambda t: "connection" in t and ("error" in t or "refused" in t or "timeout" in t),
        [
            "A network error stopped the Whisper model download (~1-3 GB from HuggingFace on first run).",
            "Check the internet connection and try again.",
        ],
    ),
]


def diagnose(name: str, log_lines: list[str]) -> list[str]:
    """Advice for a server that crashed, from its log ([] if not recognised)."""
    text = "\n".join(log_lines).lower()
    for servers, matches, advice in _DIAGNOSES:
        if name in servers and matches(text):
            return list(advice)
    return []


# ── Processes ─────────────────────────────────────────────────────────────────


@dataclass
class StartResult:
    """How starting a server went. error "" = ready, or already running."""

    error: str = ""
    timed_out: bool = False  # still loading when the wait ended (a large model)
    already_running: bool = False
    log_name: str = ""
    diagnosis: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.error

    def message(self) -> str:
        """One line for a status file or a notification."""
        return f"{self.error} — {self.diagnosis[0]}" if self.diagnosis else self.error


class Processes:
    """The servers Yume started (never ones it found running), with their logs.

    hidden: no console windows (the supervisor runs without one) and Python
    children run on python.exe even if Yume itself runs on pythonw.
    """

    def __init__(self, hidden: bool = False):
        self.hidden = hidden
        self.procs: list[tuple[str, subprocess.Popen]] = []
        self.logs: list = []

    def __iter__(self):
        return iter(self.procs)

    def __bool__(self) -> bool:
        return bool(self.procs)

    def names(self) -> list[str]:
        return [name for name, _p in self.procs]

    def spawn(self, name: str, cmd: list[str], log_name: str, env: dict) -> subprocess.Popen:
        if self.hidden and cmd and cmd[0] == sys.executable:
            cmd = [console_python(), *cmd[1:]]
        lh = open_rotating_log(log_name)
        self.logs.append(lh)
        flags = CREATE_NO_WINDOW if (self.hidden and IS_WIN) else 0
        # stdin: a child must never read the launcher's menu keys
        p = subprocess.Popen(  # nosec B603 — argv from the builders above
            cmd, stdout=lh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env, creationflags=flags
        )
        self.procs.append((name, p))
        _log.info("started %s (pid %s): %s", name, p.pid, " ".join(cmd))
        return p

    def dead(self) -> list[str]:
        """Servers that exited, as "Name (exit code N)"."""
        return [f"{n} (exit code {p.returncode})" for n, p in self.procs if p.poll() is not None]

    def stop(self, cfg: dict, timeout: float = 10) -> None:
        """Stop every server Yume started, newest first, then free their ports if
        a child of theirs lingers (and the PO-token helper the Whisper server ran)."""
        for name, p in reversed(self.procs):
            try:
                p.terminate()
                p.wait(timeout=timeout)
            except Exception:
                try:
                    p.kill()
                    p.wait(timeout=1)
                except Exception:  # nosec B110 — already gone
                    pass
            _log.info("stopped %s", name)
        for lh in self.logs:
            try:
                lh.close()
            except OSError:
                pass
        from yume.ports import is_port_free, kill_port_process, stop_bgutil_server

        if "Whisper" in self.names():
            stop_bgutil_server()
        for name in self.names():
            port = cfg.get(PORT_KEYS.get(name, ""))
            if port and not is_port_free(port):
                kill_port_process(port, interactive=False)


def wait_ready(p: subprocess.Popen | None, ready, timeout: float, stop=None, tick=None) -> tuple[str, bool]:
    """Wait for a server. ready() returns None once ready, "" while not yet, or
    an error message. Returns (error, timed_out); ("", False) = ready.

    stop(): give up early (the supervisor was asked to stop). tick(elapsed):
    called about once a second (the launcher's spinner)."""
    start = time.time()
    while True:
        elapsed = time.time() - start
        if tick is not None:
            tick(elapsed)
        if stop is not None and stop():
            return "stop requested", False
        if p is not None and p.poll() is not None:
            return f"exited with code {p.returncode}", False
        err = ready()
        if err is None:
            return "", False
        if err:  # a definitive failure reported by the server
            return err, False
        if elapsed >= timeout:
            return "did not become ready in time", True
        time.sleep(1)


def _started(name: str, p, log_name: str, ready, timeout, stop, tick) -> StartResult:
    err, timed_out = wait_ready(p, ready, timeout, stop, tick)
    if not err:
        return StartResult(log_name=log_name)
    crashed = p.poll() is not None
    return StartResult(
        error=f"{name}: {err}",
        timed_out=timed_out,
        log_name=log_name,
        diagnosis=diagnose(name, log_tail(log_name)) if crashed or not timed_out else [],
    )


# ── Health checks ─────────────────────────────────────────────────────────────


def whisper_ready(cfg: dict):
    """For wait_ready: None once the Whisper server has its model, "" while it
    loads, an error message if the model failed to load."""
    from yume.network import check_server

    data = check_server(cfg["whisper_host"], cfg["whisper_port"], "/health")["data"]
    if data.get("status") == "error":
        return f"the model failed to load: {data.get('error') or 'unknown error'}"
    return None if data.get("status") == "ready" else ""


def whisper_up(cfg: dict) -> bool:
    from yume.network import check_server

    return check_server(cfg["whisper_host"], cfg["whisper_port"], "/health")["up"]


def translation_up(cfg: dict, backend: dict) -> bool:
    from yume.network import check_translation_server

    return check_translation_server(cfg["translation_host"], cfg["translation_port"], backend)["up"]


# ── Starting the servers ──────────────────────────────────────────────────────


def start_whisper(cfg: dict, procs: Processes, env: dict, stop=None, tick=None, timeout=None) -> StartResult:
    """Start the Whisper server unless it runs already, and wait for its model."""
    from yume.ports import kill_port_process

    if whisper_up(cfg):
        return StartResult(already_running=True)
    cmd = whisper_command(cfg)
    if cmd is None:
        return StartResult(error="server/faster_whisper_server.py is missing — re-extract Yume or run Setup")
    if not kill_port_process(cfg["whisper_port"], interactive=False):
        return StartResult(error=f"Whisper port {cfg['whisper_port']} is used by another program")
    p = procs.spawn("Whisper", cmd, WHISPER_LOG, env)
    return _started(
        "Whisper", p, WHISPER_LOG, lambda: whisper_ready(cfg), timeout or WHISPER_START_TIMEOUT_S, stop, tick
    )


def start_llamacpp(
    cfg: dict, backend: dict, procs: Processes, env: dict, stop=None, tick=None, timeout=None
) -> StartResult:
    """Start the llama.cpp translation server unless it runs already."""
    from yume.ports import kill_port_process

    if translation_up(cfg, backend):
        return StartResult(already_running=True)
    gguf = resolve_gguf(cfg)
    if gguf is None:
        return StartResult(error="no .gguf translation model in models/translation/")
    port = cfg["translation_port"]
    if not kill_port_process(port, interactive=False):
        return StartResult(error=f"translation port {port} is used by another program")
    p = procs.spawn("Translation", llamacpp_command(cfg, gguf, port), TRANSLATION_LOG, {**env, "PYTHONUTF8": "1"})
    return _started(
        "Translation", p, TRANSLATION_LOG, lambda: None if translation_up(cfg, backend) else "",
        timeout or TRANSLATION_START_TIMEOUT_S, stop, tick,
    )  # fmt: skip


def start_ollama(cfg: dict, procs: Processes, env: dict, stop=None, tick=None, timeout=None) -> StartResult:
    """Start `ollama serve` unless Ollama runs already."""
    from yume.network import check_server

    def up() -> bool:
        return check_server(cfg["translation_host"], cfg["translation_port"], HEALTH_PATH_OLLAMA)["up"]

    if up():
        return StartResult(already_running=True)
    ollama = find_ollama()
    try:
        if not ollama:
            raise FileNotFoundError("ollama")
        p = procs.spawn("Ollama", [ollama, "serve"], OLLAMA_LOG, env)
    except FileNotFoundError:
        return StartResult(error="Ollama is not installed (https://ollama.com/download)")
    return _started(
        "Ollama", p, OLLAMA_LOG, lambda: None if up() else "", timeout or OLLAMA_START_TIMEOUT_S, stop, tick
    )
