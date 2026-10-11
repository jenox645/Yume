"""Server lifecycle — start translation + Whisper servers, runtime menu, stop."""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from yume.hardware import IS_WIN, detect_gpu
from yume.network import (
    HEALTH_PATH_OLLAMA,
    check_ollama_models,
    check_server,
    check_translation_server,
    discover_servers,
    reset_api_token,
)
from yume.ports import ensure_port_free, find_free_port, is_port_free, kill_port_process
from yume.ui import (
    C,
    ask_arrow,
    ask_yn,
    clear,
    error,
    header,
    info,
    panel,
    pause,
    section,
    spin_wait,
    success,
    warn,
)
from yume.utils import LOGS_DIR, SERVER_DIR, TOOLS_DIR, _run, find_gguf_models

_log = logging.getLogger("pocket_yume")

# Injected at startup by pocket_yume
_BACKEND_INFO: dict = {}
_VERSION: str = "0.1.0"

# Ports belonging to each server process Yume starts. Only these are freed at
# shutdown: an Ollama service or a server the user started is never killed.
_PROC_PORT_KEYS = {"Whisper": "whisper_port", "Translation": "translation_port"}

# Python packages llama-cpp-python's OpenAI server needs (one pinned list,
# used by the launcher, the setup wizard and the installer).
LLAMA_SERVER_DEPS = [
    "uvicorn==0.42.0",
    "fastapi==0.135.1",
    "sse-starlette==3.3.3",
    "starlette-context==0.5.1",
    "pydantic-settings==2.13.1",
]


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


def _offer_llama_server() -> bool:
    """When llama-cpp-python would translate on the CPU although a GPU is
    there, offer llama.cpp's prebuilt GPU server instead (much faster)."""
    gpu = detect_gpu()
    if not (gpu["has_nvidia"] or gpu["has_amd"]):
        return False
    try:
        import llama_cpp.llama_cpp as _lib  # type: ignore[import]

        if callable(getattr(_lib, "ggml_backend_cuda_reg", None)):
            return False  # llama-cpp-python already runs on the GPU
    except ImportError:
        pass
    warn("The translator would run on the CPU (llama-cpp-python has no GPU support here).")
    info("llama.cpp's own GPU server translates ~10x faster — about 200-700 MB to download.")
    if not ask_yn("Install it now?", True):
        return False
    from yume import llama_server

    return llama_server.install(gpu)


def _free_our_ports(cfg: dict, procs: list) -> None:
    """After stopping our processes, free the ports they held if a child lingers
    (and the PO-token server the Whisper server started, see stop_bgutil_server)."""
    if any(name == "Whisper" for name, _p in procs):
        from yume.ports import stop_bgutil_server

        stop_bgutil_server()
    for name, _p in procs:
        key = _PROC_PORT_KEYS.get(name)
        port = cfg.get(key) if key else None
        if port and not is_port_free(port):
            kill_port_process(port, interactive=False)


def set_launch_context(backend_info: dict, version: str) -> None:
    """Called by pocket_yume at startup to inject BACKEND_INFO and VERSION."""
    global _BACKEND_INFO, _VERSION
    _BACKEND_INFO = backend_info
    _VERSION = version


# ── Resource detection helpers ────────────────────────────────────────────────


def _get_available_ram_mb() -> int:
    """Return available RAM in MB. Returns 0 if detection fails."""
    try:
        import psutil  # type: ignore[import]

        return int(psutil.virtual_memory().available / (1024 * 1024))
    except ImportError:
        pass
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except Exception:
        pass
    return 0


def _get_available_vram_mb() -> int:
    """Return free VRAM in MB for the primary NVIDIA GPU. Returns 0 if detection fails."""
    try:
        import pynvml  # type: ignore[import]

        pynvml.nvmlInit()
        handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
        pynvml.nvmlShutdown()
        return int(mem.free / (1024 * 1024))
    except Exception:
        pass
    try:
        out = _run(["nvidia-smi", "--query-gpu=memory.free", "--format=csv,noheader,nounits"], timeout=10)
        if out.returncode == 0 and out.stdout.strip():
            return int(out.stdout.strip().split("\n")[0].strip())
    except Exception:
        pass
    return 0


def _check_resources(cfg: dict) -> bool:
    """Pre-launch RAM/VRAM check. Returns True to proceed, False to cancel.

    Never hard-blocks — the user can always choose 'Launch anyway'.
    """
    issues: list[tuple[str, str]] = []  # (kind, human-readable message)

    bk = cfg.get("translation_backend", "llamacpp")
    gpu = detect_gpu()

    # ── VRAM check for Whisper (NVIDIA GPU path only) ─────────────────────────
    whisper_dev = cfg.get("whisper_device", "auto")
    using_nvidia = gpu.get("has_nvidia", False) if whisper_dev in ("auto", "cuda") else False

    whisper_vram = 0
    if using_nvidia:
        from yume.hardware import WHISPER_MODEL_VRAM_MB

        model_name = cfg.get("whisper_model", "large-v3-turbo")
        required_vram = WHISPER_MODEL_VRAM_MB.get(model_name, 4_500)  # custom paths: assume large
        if cfg.get("whisper_compute_type", "auto") in ("int8", "int8_float16"):
            required_vram = int(required_vram * 0.65)
        whisper_vram = required_vram
        avail_vram = _get_available_vram_mb()
        if avail_vram > 0 and avail_vram < required_vram:
            issues.append(
                (
                    "vram",
                    f"Whisper '{model_name}' needs ~{required_vram / 1024:.1f} GB VRAM, "
                    f"but only {avail_vram / 1024:.1f} GB is free.",
                )
            )

    # ── RAM/VRAM check for GGUF translation model (file size × 1.2) ─────────
    # llama.cpp uses --n_gpu_layers -1 when a GPU is present, so the model
    # lives in VRAM rather than system RAM. Only warn about RAM when no GPU
    # offloading is available.
    if bk == "llamacpp":
        gp = cfg.get("gguf_model_path", "")
        if gp and Path(gp).exists():
            model_size_mb = Path(gp).stat().st_size / (1024 * 1024)
            required_mb = int(model_size_mb * 1.2)
            has_gpu = gpu.get("has_nvidia", False) or gpu.get("has_amd", False)
            if has_gpu:
                avail_vram_mb = _get_available_vram_mb()
                # Both models end up on the same GPU: check them together
                together = required_mb + whisper_vram
                if avail_vram_mb > 0 and avail_vram_mb < together:
                    both = f" (plus ~{whisper_vram / 1024:.1f} GB for Whisper)" if whisper_vram else ""
                    issues.append(
                        (
                            "vram",
                            f"Translation model ({Path(gp).name}) needs ~{required_mb / 1024:.1f} GB VRAM{both}, "
                            f"but only {avail_vram_mb / 1024:.1f} GB is free.",
                        )
                    )
            else:
                avail_ram_mb = _get_available_ram_mb()
                if avail_ram_mb > 0 and avail_ram_mb < required_mb:
                    issues.append(
                        (
                            "ram",
                            f"Translation model ({Path(gp).name}) needs ~{required_mb / 1024:.1f} GB RAM, "
                            f"but only {avail_ram_mb / 1024:.1f} GB is available.",
                        )
                    )

    if not issues:
        return True

    # ── Show warning panel ────────────────────────────────────────────────────
    print()
    panel(
        "\n".join(f"  • {msg}" for _, msg in issues),
        title=f"{C.YELLOW}Low Resources Warning{C.RESET}",
        style=C.YELLOW,
    )
    print()
    info("Launching with low resources may cause crashes or heavy swap usage.")
    print()

    ch = ask_arrow(
        "How would you like to proceed?",
        [
            ("Launch anyway", "Override — proceed despite the warning"),
            ("Retry check", "Close other apps to free resources, then re-check"),
            ("Switch Whisper model", "Pick a smaller Whisper model that fits available VRAM/RAM"),
            ("Cancel", "Return to main menu without launching"),
        ],
        default=0,
        allow_back=False,
    )

    if ch == 0:
        warn("Launching with low resources — watch logs if servers crash.")
        return True
    if ch == 1:
        info("Re-checking resources — close unused apps first, then press Enter.")
        try:
            input(f"  {C.DIM}Press Enter when ready...{C.RESET}")
        except (EOFError, KeyboardInterrupt):
            print()
        return _check_resources(cfg)
    if ch == 2:
        from yume.menus import _menu_whisper_model

        _menu_whisper_model(cfg)
        return _check_resources(cfg)
    return False


# ── Public entry point ────────────────────────────────────────────────────────


def _open_rotating_log(name: str):
    """Open a log file for subprocess output, rotating if it exceeds 5 MB.

    Rotation happens before opening so the subprocess always writes to a fresh
    (or small) file. Returns a file handle suitable for Popen(stdout=...).

    RotatingFileHandler only fires when Python writes through the handler — it
    cannot rotate data written directly by a subprocess to a plain file handle.
    Manual pre-open rotation is the correct approach here.
    """
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    lp = LOGS_DIR / name
    max_bytes = 5 * 1024 * 1024  # 5 MB
    backup_count = 3
    if lp.exists() and lp.stat().st_size > max_bytes:
        for i in range(backup_count - 1, 0, -1):
            old = LOGS_DIR / f"{name}.{i}"
            new_p = LOGS_DIR / f"{name}.{i + 1}"
            if old.exists():
                old.replace(new_p)
        lp.replace(LOGS_DIR / f"{name}.1")
    return open(str(lp), "a", encoding="utf-8", errors="replace")  # noqa: SIM115


def launch_services(cfg: dict) -> None:
    header("Launching Yume")
    procs: list = []
    lhs: list = []
    # Each server run mints a fresh API token. Drop any token cached from an earlier
    # launch in this same interactive session, so the runtime menu's blacklist/stats/
    # model-switch don't fail auth against the new server and look like it's offline.
    reset_api_token()

    def _cleanup() -> None:
        for _name, p in procs:
            try:
                p.terminate()
                p.wait(timeout=1)
            except Exception:
                try:
                    p.kill()
                    p.wait(timeout=1)
                except OSError:
                    pass
        for lh in lhs:
            try:
                lh.close()
            except OSError:
                pass
        _free_our_ports(cfg, procs)

    try:
        _launch_inner(cfg, procs, lhs)
    except KeyboardInterrupt:
        print(f"\n  {C.YELLOW}Cancelled — cleaning up...{C.RESET}")
        _cleanup()
        print(f"  {C.GREEN}Processes stopped.{C.RESET}\n")
        return
    except Exception as e:
        error(f"Launch failed: {e}")
        info(f"Try: {C.CYAN}python pocket_yume.py health{C.RESET} to diagnose the issue.")
        _cleanup()
        pause()
        return


# ── Internal launch logic ─────────────────────────────────────────────────────


def _launch_inner(cfg: dict, procs: list, lhs: list) -> None:
    """Actual server startup. Ctrl+C caught by launch_services()."""
    from config import DEFAULT_TRANSLATION_PORT, DEFAULT_WHISPER_PORT, save_config

    # --- Check for already-running servers ---
    existing = discover_servers(cfg, _BACKEND_INFO)
    if existing.get("whisper") and existing.get("translation"):
        info("Both servers are already running!")
        if ask_yn("Open runtime menu with existing servers?", True):
            _runtime_menu(cfg, [], [], cfg.get("translation_backend", "llamacpp"))
            return
    if existing.get("ollama_found"):
        port = existing["ollama_found"]
        info(f"Found Ollama running on port {port}")
        if cfg["translation_port"] != port:
            if ask_yn(f"Update translation port to {port}?", True):
                cfg["translation_port"] = port
                save_config(cfg)

    # Pre-flight port check
    wport = cfg.get("whisper_port", DEFAULT_WHISPER_PORT)
    tport = cfg.get("translation_port", DEFAULT_TRANSLATION_PORT)
    if wport == tport:
        error("Whisper and translation ports are the same!")
        tport = find_free_port(wport + 1, exclude={wport})
        if tport:
            cfg["translation_port"] = tport
            save_config(cfg)
            success(f"Auto-assigned translation to port {tport}")
        else:
            error("Could not find a free port. Many ports may be in use on this system.")
            info(f"Change ports manually in: {C.CYAN}python pocket_yume.py settings{C.RESET}")
            pause()
            return
    bk = cfg.get("translation_backend", "llamacpp")
    if not existing.get("whisper") and not is_port_free(wport):
        info(f"Port {wport} is busy. Another Yume instance or other app may be using it.")
        wport = ensure_port_free(wport, cfg, "whisper", exclude={tport})
        if wport is None:
            error("Cannot proceed without a free whisper port.")
            info(f"Change the port in: {C.CYAN}python pocket_yume.py settings{C.RESET}")
            pause()
            return
    # Only llama.cpp is started by Yume on the translation port; for Ollama /
    # LM Studio / custom servers that port is SUPPOSED to be in use.
    if bk == "llamacpp" and not existing.get("translation") and not is_port_free(tport):
        info(f"Port {tport} is busy. Another Yume instance or other app may be using it.")
        tport = ensure_port_free(tport, cfg, "translation", exclude={wport})
        if tport is None:
            error("Cannot proceed without a free translation port.")
            info(f"Change the port in: {C.CYAN}python pocket_yume.py settings{C.RESET}")
            pause()
            return

    # Missing server packages: things still run (pypinyin → slower LLM pinyin,
    # waitress → Flask's dev server), so warn and offer the install, don't block
    from yume.utils import missing_requirements

    miss = missing_requirements()
    if miss:
        warn(f"Missing server packages: {', '.join(miss)}")
        if ask_yn("Install them now?", True):
            from yume.installers import install_python_deps

            install_python_deps()

    # Pre-flight resource check
    if not _check_resources(cfg):
        return

    env = build_server_env()
    if env.get("HSA_OVERRIDE_GFX_VERSION") and "HSA_OVERRIDE_GFX_VERSION" not in os.environ:
        warn("AMD RDNA1 GPU detected. Setting HSA_OVERRIDE_GFX_VERSION=10.3.0")
        info("Add 'export HSA_OVERRIDE_GFX_VERSION=10.3.0' to ~/.bashrc to make this permanent.")

    bi = _BACKEND_INFO.get(bk, _BACKEND_INFO.get("custom", {"hp": "/health"}))

    # --- START TRANSLATION BACKEND ---
    if bk == "llamacpp":
        _start_llamacpp(cfg, procs, lhs, bi, env, tport)
        if procs and procs[-1][0] == "Translation" and procs[-1][1].poll() is not None:
            return  # crashed during startup — _start_llamacpp already paused
    elif bk == "ollama":
        if not _start_ollama(cfg, procs, env):
            return
    else:
        bn = _BACKEND_INFO.get(bk, {}).get("name", bk)
        info(f"{bn} -- make sure it's running at {cfg['translation_host']}:{cfg['translation_port']}")

    # --- START WHISPER SERVER ---
    if not _start_whisper(cfg, procs, lhs, env):
        return

    # --- READY ---
    clear()
    print()
    success(f"Yume is running!  {C.DIM}v{_VERSION}{C.RESET}")
    tn = _BACKEND_INFO.get(bk, {}).get("name", bk)
    print(f"  {C.CYAN}Whisper{C.RESET}      http://{cfg['whisper_host']}:{cfg['whisper_port']}")  # noqa: S5332 — display only, local server
    print(f"  {C.MAGENTA}Translation{C.RESET}  http://{cfg['translation_host']}:{cfg['translation_port']}  ({tn})")  # noqa: S5332 — display only, local server
    print()
    info(f"{C.DIM}Chrome -> Japanese video -> Yume extension -> Enable{C.RESET}")
    print()

    _runtime_menu(cfg, procs, lhs, bk)


def _start_llamacpp(cfg: dict, procs: list, lhs: list, bi: dict, env: dict, tport: int) -> None:
    """Start llama.cpp translation server."""
    from yume.installers import install_llamacpp_python

    configured = cfg.get("gguf_model_path", "")
    gp = resolve_gguf(cfg)
    if gp is None:
        from yume.utils import GGUF_DIR

        error("No .gguf model found in models/translation/")
        info("Fix: Main Menu → Tools → Download GGUF Model")
        info(f"Or: place a .gguf file in {GGUF_DIR}")
        pause()
        return
    if gp != configured:
        info(f"Auto-selected GGUF: {Path(gp).name}")

    from yume import llama_server

    native = llama_server.server_path() is not None
    if not native and _offer_llama_server():
        native = llama_server.server_path() is not None

    try:
        if not native:
            import llama_cpp  # noqa: F401
    except ImportError:
        error("llama-cpp-python not installed! This is the translation engine.")
        info(f"You can also install it manually: {C.CYAN}pip install llama-cpp-python{C.RESET}")
        if ask_yn("Install now?"):
            install_llamacpp_python()
        else:
            pause()
        return

    try:
        if not native:
            import uvicorn  # noqa: F401
            import fastapi  # noqa: F401
    except ImportError:
        warn("Server dependencies missing (uvicorn/fastapi)")
        info("Installing them now...")
        try:
            _run(
                [sys.executable, "-m", "pip", "install", *LLAMA_SERVER_DEPS, "-q", "--no-warn-script-location"],
                timeout=300,
                env=env,
            )
            success("Server dependencies installed!")
        except Exception as e:
            error(f"Failed to install: {e}")
            pause()
            return

    port = cfg.get("translation_port", tport)
    st = check_translation_server(cfg["translation_host"], port, bi)
    if st["up"]:
        success("llama.cpp server already running!")
        return

    if not kill_port_process(port):
        error(f"Port {port} is still in use — cannot start the translation server.")
        pause()
        return
    info(f"Starting llama.cpp server with {Path(gp).name}...")
    gpu = detect_gpu()
    using_gpu_layers = gpu["has_nvidia"] or (gpu["has_amd"] and not IS_WIN)
    if native:
        build = llama_server.installed_build()
        info(f"Engine: llama-server {build.get('tag', '')} ({build.get('variant', '?')})")
    elif using_gpu_layers:
        try:
            import llama_cpp.llama_cpp as _lib  # type: ignore[import]

            backend_ok = callable(getattr(_lib, "ggml_backend_cuda_reg", None))
        except Exception:
            backend_ok = False
        if backend_ok:
            info("GPU offloading enabled (--n_gpu_layers -1)")
        else:
            warn("llama-cpp-python is a CPU-only build — model will load into RAM, not VRAM.")
            warn("Run: python pocket_yume.py setup → reinstall packages to fix this.")
    else:
        info("No supported GPU found — running translation model on CPU")
    cmd = llamacpp_command(cfg, gp, port)

    lp = LOGS_DIR / "translation_server.log"
    lh = _open_rotating_log("translation_server.log")
    lhs.append(lh)
    tl_env = env.copy()
    tl_env["PYTHONUTF8"] = "1"
    p = subprocess.Popen(cmd, stdout=lh, stderr=subprocess.STDOUT, env=tl_env)
    procs.append(("Translation", p))
    info(f"Log: {lp}")

    def _trans_ready() -> bool | None:
        if p.poll() is not None:
            return None
        return check_translation_server(cfg["translation_host"], port, bi)["up"]

    ready = spin_wait(lambda: _trans_ready() is True, "Loading translation model...", timeout=180, interval=2)
    if p.poll() is not None:
        error("llama.cpp server crashed!")
        crash_log = ""
        try:
            with open(lp, encoding="utf-8", errors="replace") as f:
                crash_lines = f.readlines()[-15:]
                crash_log = "".join(crash_lines)
                for line in crash_lines[-10:]:
                    print(f"  {C.DIM}{line.rstrip()}{C.RESET}")
        except Exception as e:
            _log.debug("[_start_llamacpp] translation-crash-log-read failed: %s", e)
        cl = crash_log.lower()
        if "cuda" in cl and ("out of memory" in cl or "oom" in cl or "alloc" in cl):
            print()
            info(f"{C.BOLD}Diagnosis: GPU out of VRAM for this model.{C.RESET}")
            info(f"Try: {C.CYAN}python pocket_yume.py settings{C.RESET} -> change model to a smaller quantization,")
            info("  or reduce n_gpu_layers, or set device to 'cpu'.")
        elif "modulenotfounderror" in cl or "no module named" in cl:
            print()
            info(f"{C.BOLD}Diagnosis: Missing Python dependency.{C.RESET}")
            info(f"Run: {C.CYAN}python pocket_yume.py setup{C.RESET} to reinstall packages.")
        elif "address already in use" in cl or ("port" in cl and "in use" in cl):
            print()
            info(f"{C.BOLD}Diagnosis: Port {port} is already in use.{C.RESET}")
            info("Another process is using this port. Yume will find a free port automatically,")
            info(f"or change it in: {C.CYAN}python pocket_yume.py settings{C.RESET}")
        info(f"Full log: {lp}")
        pause()
        return
    if ready:
        success("Translation server ready!")
    else:
        warn("Still loading — large models may take a few minutes")


def _start_ollama(cfg: dict, procs: list, env: dict) -> bool:
    """Start Ollama translation server. Returns False if startup failed."""
    from yume.installers import pull_ollama_model

    st = check_server(cfg["translation_host"], cfg["translation_port"], HEALTH_PATH_OLLAMA)
    if not st["up"]:
        info("Starting Ollama...")
        try:
            p = subprocess.Popen(["ollama", "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
            procs.append(("Ollama", p))
            time.sleep(3)
            if check_server(cfg["translation_host"], cfg["translation_port"], HEALTH_PATH_OLLAMA)["up"]:
                success("Ollama running!")
            else:
                warn("Ollama may still be starting...")
        except FileNotFoundError:
            error("Ollama not found. Is it installed and on your PATH?")
            info(f"Install Ollama from: {C.CYAN}https://ollama.com/download{C.RESET}")
            info(f"Or switch to llama.cpp backend in: {C.CYAN}python pocket_yume.py settings{C.RESET}")
            pause()
            return False
    else:
        success("Ollama already running")
    if not cfg.get("translation_model") or cfg["translation_model"].endswith(".gguf"):
        # Empty (or a leftover GGUF filename from llama.cpp): Ollama needs a real
        # model name in every request, so pick the default instead of failing later.
        from config import save_config

        cfg["translation_model"] = "qwen2.5:7b"
        save_config(cfg)
        info("Translation model set to qwen2.5:7b (change it in Settings → Translation settings)")
    model = cfg["translation_model"]
    ms = check_ollama_models(cfg["translation_host"], cfg["translation_port"])
    if model not in ms and f"{model}:latest" not in ms:
        warn(f"Model {model} not found in Ollama. Pulling...")
        pull_ollama_model(model)
    return True


def _start_whisper(cfg: dict, procs: list, lhs: list, env: dict) -> bool:
    """Start Whisper server. Returns False if startup failed."""
    ws = check_server(cfg["whisper_host"], cfg["whisper_port"], "/health")
    if ws["up"]:
        success("Whisper already running!")
        return True

    if not kill_port_process(cfg["whisper_port"]):
        error(f"Port {cfg['whisper_port']} is still in use — cannot start the Whisper server.")
        pause()
        return False
    info("Starting Whisper server...")
    cmd = whisper_command(cfg)
    if cmd is None:
        error("Whisper server script not found in server/")
        info("Fix: Re-extract Yume or run Setup again")
        pause()
        return False
    dev, comp = resolve_whisper_device(cfg)

    lp = LOGS_DIR / "whisper_server.log"
    lh = _open_rotating_log("whisper_server.log")
    lhs.append(lh)
    info(f"Device: {dev} | Compute: {comp} | Port: {cfg['whisper_port']}")
    info(f"Log: {lp}")

    p = subprocess.Popen(cmd, stdout=lh, stderr=subprocess.STDOUT, env=env)
    procs.append(("Whisper", p))

    load_error: dict = {}

    def _whisper_ready() -> bool | None:
        """True = ready, False = keep waiting, None = gave up (exited or load failed)."""
        if p.poll() is not None:
            return None
        st = check_server(cfg["whisper_host"], cfg["whisper_port"], "/health")
        if st["data"].get("status") == "error":
            load_error["msg"] = st["data"].get("error") or "unknown error"
            return None
        return st["up"] and st["data"].get("status") == "ready"

    ready = spin_wait(
        lambda: _whisper_ready() is not False,
        f"Loading Whisper model ({cfg['whisper_model']})...",
        timeout=240,
        interval=2,
    )
    if p.poll() is not None or load_error:
        if load_error:
            error(f"Whisper failed to load the model: {load_error['msg']}")
        else:
            error("Whisper server crashed!")
        crash_log = ""
        try:
            with open(lp, encoding="utf-8", errors="replace") as f:
                crash_lines = f.readlines()[-15:]
                crash_log = "".join(crash_lines)
                for line in crash_lines[-10:]:
                    print(f"  {C.DIM}{line.rstrip()}{C.RESET}")
        except Exception as e:
            _log.debug("[_start_whisper] crash-log-read failed: %s", e)
        cl = crash_log.lower()
        diagnosed = False
        if "cuda" in cl and ("out of memory" in cl or "oom" in cl or "alloc" in cl):
            print()
            info(f"{C.BOLD}Diagnosis: Your GPU doesn't have enough VRAM for this model.{C.RESET}")
            info(f"Try: {C.CYAN}python pocket_yume.py settings{C.RESET} -> change Whisper model to 'small' or 'tiny',")
            info("  or set device to 'cpu'.")
            diagnosed = True
        elif "cublas" in cl or "cudnn" in cl or "cudart" in cl:
            print()
            info(f"{C.BOLD}Diagnosis: CUDA libraries missing or incomplete.{C.RESET}")
            info(f"Fix: Install CUDA Toolkit: {C.CYAN}https://developer.nvidia.com/cuda-toolkit{C.RESET}")
            info(f"Or switch to CPU: {C.CYAN}python pocket_yume.py settings{C.RESET} -> set device to 'cpu'")
            diagnosed = True
        elif "modulenotfounderror" in cl or "no module named" in cl:
            print()
            info(f"{C.BOLD}Diagnosis: Missing Python dependency.{C.RESET}")
            info(f"Run: {C.CYAN}python pocket_yume.py setup{C.RESET} to reinstall packages.")
            diagnosed = True
        elif "address already in use" in cl or ("port" in cl and "in use" in cl):
            print()
            info(f"{C.BOLD}Diagnosis: Port {cfg['whisper_port']} is already busy.{C.RESET}")
            info("Another Yume instance may be running. Restart or change port in Settings.")
            diagnosed = True
        elif "permission" in cl or "access denied" in cl or "errno 13" in cl:
            print()
            info(f"{C.BOLD}Diagnosis: Permission denied — can't access model files or GPU.{C.RESET}")
            info("Try running as Administrator (Windows) or with sudo (Linux/macOS).")
            diagnosed = True
        elif "connection" in cl and ("error" in cl or "refused" in cl or "timeout" in cl):
            print()
            info(f"{C.BOLD}Diagnosis: Network error while downloading the Whisper model.{C.RESET}")
            info("Check your internet connection and try again.")
            info("The model downloads from HuggingFace (~1-3 GB) on first run.")
            diagnosed = True
        if not diagnosed:
            print()
            info(f"{C.BOLD}Quick fixes to try:{C.RESET}")
            info(f"  1. Switch to CPU: {C.CYAN}python pocket_yume.py settings{C.RESET} -> device = cpu")
            info(f"  2. Use smaller model: {C.CYAN}python pocket_yume.py settings{C.RESET} -> model = small")
            info(f"  3. Re-run setup: {C.CYAN}python pocket_yume.py setup{C.RESET}")
        info(f"Full log: {lp}")
        pause()
        return False
    if ready:
        success("Whisper server ready!")
    else:
        warn("Still loading — first run takes 30-60s while the Whisper model downloads.")
        info("This is a one-time download. Future launches will start much faster.")
    return True


# ── Runtime menu ──────────────────────────────────────────────────────────────


def _runtime_menu(cfg: dict, procs: list, lhs: list, bk: str) -> None:
    """Live menu: stats, blacklist, model swap, logs."""
    from yume.benchmark import benchmark_whisper
    from yume.menus import _menu_blacklist, _menu_whisper_model, _test_translation, cli_server_stats

    bi = _BACKEND_INFO.get(bk, _BACKEND_INFO.get("custom", {}))

    def _check_procs() -> bool:
        for n, p in procs:
            if p.poll() is not None:
                warn(f"{n} exited unexpectedly (code {p.returncode})")
                return False
        return True

    def _stop_all() -> None:
        print(f"\n  {C.YELLOW}Shutting down...{C.RESET}")
        if not procs:
            # Servers this menu did not start: if the background service runs
            # them (one-click start), stop it; otherwise leave them alone.
            from yume.service import read_state, request_stop

            if read_state()["state"] != "stopped":
                success("Background Yume stopped" if request_stop() else "Asked background Yume to stop")
            else:
                info("These servers were started elsewhere — they keep running.")
        for n, p in procs:
            try:
                p.terminate()
                p.wait(timeout=1)
                success(f"{n} stopped")
            except Exception:
                try:
                    p.kill()
                    p.wait(timeout=1)
                except Exception as e:
                    _log.debug("[_runtime_menu] process-cleanup failed: %s", e)
        for lh in lhs:
            try:
                lh.close()
            except OSError:
                pass
        _free_our_ports(cfg, procs)

    def _show_logs() -> None:
        section("Recent Logs")
        for name in ["whisper_server.log", "translation_server.log"]:
            lp = LOGS_DIR / name
            if lp.exists():
                info(f"{C.BOLD}{name}{C.RESET}")
                try:
                    with open(lp, encoding="utf-8", errors="replace") as f:
                        lines = f.readlines()
                    lines = lines[-2000:]  # logs reach 5 MB; only the tail matters
                    yume_lines = [
                        ln
                        for ln in lines
                        if "[Yume]" in ln or "error" in ln.lower() or "fail" in ln.lower() or "warn" in ln.lower()
                    ]
                    access_lines = [ln for ln in lines if "HTTP/" in ln and "/health" not in ln]
                    seen = set(yume_lines) | set(access_lines)
                    other_lines = [ln for ln in lines if ln not in seen and "GET /health" not in ln]
                    shown = yume_lines[-10:] + access_lines[-5:] + other_lines[-5:]
                    if not shown:
                        shown = lines[-15:]
                    for line in shown[-20:]:
                        print(f"    {C.DIM}{line.rstrip()}{C.RESET}")
                except Exception as e:
                    warn(f"Could not read: {e}")
                print()

    try:
        while True:
            clear()
            print()
            if not _check_procs():
                warn("A server has stopped unexpectedly.")
                dead = [(n, p) for n, p in procs if p.poll() is not None]
                for name, p in dead:
                    error(f"{name} exited with code {p.returncode}")
                info(f"Its log: {C.CYAN}{LOGS_DIR}{C.RESET}")
                if ask_yn("Stop the other servers and go back to the main menu (choose Launch to restart)?", True):
                    _stop_all()
                    return
                for item in dead:  # keep going with what still runs; don't ask again
                    procs.remove(item)

            ws_up = check_server(cfg["whisper_host"], cfg["whisper_port"], "/health")["up"]
            ts_up = check_translation_server(cfg["translation_host"], cfg["translation_port"], bi)["up"]

            # Word + colour so status is readable without colour vision
            ws_st = f"{C.GREEN}● up{C.RESET}  " if ws_up else f"{C.RED}● DOWN{C.RESET}"
            ts_st = f"{C.GREEN}● up{C.RESET}  " if ts_up else f"{C.RED}● DOWN{C.RESET}"

            print()
            panel(
                f"  {ws_st} Whisper     {cfg['whisper_host']}:{cfg['whisper_port']}\n"
                f"  {ts_st} Translation {cfg['translation_host']}:{cfg['translation_port']}",
                title=f"{C.GREEN}Yume Running{C.RESET}",
                style=C.GREEN,
            )

            ch = ask_arrow(
                "Runtime:",
                [
                    ("Server Stats", "GPU usage, memory, how many chunks have been processed"),
                    ("Subtitle Filter", "Block phrases Whisper hallucinates (fake 'Subscribe' etc.)"),
                    ("Whisper Model", "Switch speech recognition model without restarting"),
                    ("Test Translation", "Send a test sentence to verify the translation pipeline"),
                    ("Benchmark Whisper", "Measure how fast each model runs on your hardware"),
                    ("View Logs", "Recent server output (for troubleshooting)"),
                    ("Stop & Return", "Shut down all servers and go back to main menu"),
                ],
                default=6,
                allow_back=False,
            )

            if ch == 0:
                cli_server_stats(cfg)
                pause()
            elif ch == 1:
                _menu_blacklist(cfg)
            elif ch == 2:
                _menu_whisper_model(cfg)
            elif ch == 3:
                _test_translation(cfg)
            elif ch == 4:
                benchmark_whisper(cfg)
            elif ch == 5:
                _show_logs()
                pause()
            elif ch == 6:
                # Default No: this menu opens with Stop highlighted, so two
                # accidental Enters must not kill running servers.
                if ask_yn("Stop all servers?", default=False):
                    _stop_all()
                    return
    except KeyboardInterrupt:
        print()
        _stop_all()
