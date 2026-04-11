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
)
from yume.ports import ensure_port_free, find_free_port, is_port_free, kill_port_process
from yume.ui import (
    C,
    ask_choice,
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
from yume.utils import LOGS_DIR, SERVER_DIR, TOOLS_DIR, _run, find_gguf_models, rotate_logs

_log = logging.getLogger("pocket_yume")

# Injected at startup by pocket_yume
_BACKEND_INFO: dict = {}
_VERSION: str = "0.0.8"


def set_launch_context(backend_info: dict, version: str) -> None:
    """Called by pocket_yume at startup to inject BACKEND_INFO and VERSION."""
    global _BACKEND_INFO, _VERSION
    _BACKEND_INFO = backend_info
    _VERSION = version


# ── Public entry point ────────────────────────────────────────────────────────


def launch_services(cfg: dict) -> None:
    header("Launching Yume")
    rotate_logs()  # Clean up old logs before starting
    procs: list = []
    lhs: list = []

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
        # Verify ports are actually freed
        for key in ["whisper_port", "translation_port"]:
            port = cfg.get(key)
            if port and not is_port_free(port):
                kill_port_process(port)

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
    if not is_port_free(wport):
        info(f"Port {wport} is busy. Another Yume instance or other app may be using it.")
        wport = ensure_port_free(wport, cfg, "whisper", exclude={tport})
        if wport is None:
            error("Cannot proceed without a free whisper port.")
            info(f"Change the port in: {C.CYAN}python pocket_yume.py settings{C.RESET}")
            pause()
            return
    if not is_port_free(tport):
        info(f"Port {tport} is busy. Another Yume instance or other app may be using it.")
        tport = ensure_port_free(tport, cfg, "translation", exclude={wport})
        if tport is None:
            error("Cannot proceed without a free translation port.")
            info(f"Change the port in: {C.CYAN}python pocket_yume.py settings{C.RESET}")
            pause()
            return

    # Build PATH with our tools
    env = os.environ.copy()
    tp = str(TOOLS_DIR)
    if tp not in env.get("PATH", ""):
        env["PATH"] = tp + os.pathsep + env.get("PATH", "")

    # AMD GPU: auto-detect if HSA_OVERRIDE_GFX_VERSION is needed
    if not IS_WIN and "HSA_OVERRIDE_GFX_VERSION" not in env:
        gpu = detect_gpu()
        if gpu.get("has_amd"):
            try:
                out = _run(["rocminfo"], timeout=10)
                if out.returncode == 0:
                    arches = re.findall(r"gfx(\d+)", out.stdout)
                    rdna1 = [a for a in arches if a in ("1010", "1011", "1012")]
                    if rdna1:
                        env["HSA_OVERRIDE_GFX_VERSION"] = "10.3.0"
                        warn(f"AMD RDNA1 GPU detected (gfx{rdna1[0]}). Setting HSA_OVERRIDE_GFX_VERSION=10.3.0")
                        info("Add 'export HSA_OVERRIDE_GFX_VERSION=10.3.0' to ~/.bashrc to make this permanent.")
            except Exception as e:
                _log.debug("[_launch_inner] rocm-detect failed: %s", e)

    bk = cfg.get("translation_backend", "llamacpp")
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
    print(f"  {C.CYAN}Whisper{C.RESET}      http://{cfg['whisper_host']}:{cfg['whisper_port']}")
    print(f"  {C.MAGENTA}Translation{C.RESET}  http://{cfg['translation_host']}:{cfg['translation_port']}  ({tn})")
    print()
    info(f"{C.DIM}Chrome -> Japanese video -> Yume extension -> Enable{C.RESET}")
    print()

    _runtime_menu(cfg, procs, lhs, bk)


def _start_llamacpp(cfg: dict, procs: list, lhs: list, bi: dict, env: dict, tport: int) -> None:
    """Start llama.cpp translation server."""
    from yume.installers import install_llamacpp_python

    gp = cfg.get("gguf_model_path", "")
    if not gp or not Path(gp).exists():
        gfs = find_gguf_models()
        if gfs:
            from config import save_config

            gp = str(gfs[0])
            cfg["gguf_model_path"] = gp
            save_config(cfg)
            info(f"Auto-selected GGUF: {gfs[0].name}")
        else:
            from yume.utils import GGUF_DIR

            error("No .gguf model found in models/translation/")
            info("Fix: Main Menu → Tools → Download GGUF Model")
            info(f"Or: place a .gguf file in {GGUF_DIR}")
            pause()
            return

    try:
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
        import uvicorn  # noqa: F401
        import fastapi  # noqa: F401
    except ImportError:
        warn("Server dependencies missing (uvicorn/fastapi)")
        info("Installing them now...")
        try:
            _run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "uvicorn==0.42.0",
                    "fastapi==0.135.1",
                    "sse-starlette==3.3.3",
                    "starlette-context==0.5.1",
                    "pydantic-settings==2.13.1",
                    "-q",
                    "--no-warn-script-location",
                ],
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

    kill_port_process(port)
    info(f"Starting llama.cpp server with {Path(gp).name}...")
    gpu = detect_gpu()
    cmd = [
        sys.executable,
        "-m",
        "llama_cpp.server",
        "--model",
        gp,
        "--host",
        cfg.get("translation_host", "127.0.0.1"),
        "--port",
        str(port),
        "--n_ctx",
        "2048",
    ]
    if gpu["has_nvidia"]:
        cmd.extend(["--n_gpu_layers", "-1"])
    elif gpu["has_amd"] and not IS_WIN:
        cmd.extend(["--n_gpu_layers", "-1"])

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    lp = LOGS_DIR / "translation_server.log"
    lh = open(lp, "w", encoding="utf-8", errors="replace")  # noqa: SIM115
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
    ms = check_ollama_models(cfg["translation_host"], cfg["translation_port"])
    mb = cfg.get("translation_model", "qwen2.5:7b").split(":")[0]
    if not any(mb in m for m in ms):
        warn(f"Model {cfg['translation_model']} not found. Pulling...")
        pull_ollama_model(cfg.get("translation_model", "qwen2.5:7b"))
    return True


def _start_whisper(cfg: dict, procs: list, lhs: list, env: dict) -> bool:
    """Start Whisper server. Returns False if startup failed."""
    from config import CONFIG_FILE

    ws = check_server(cfg["whisper_host"], cfg["whisper_port"], "/health")
    if ws["up"]:
        success("Whisper already running!")
        return True

    kill_port_process(cfg["whisper_port"])
    info("Starting Whisper server...")
    ss = SERVER_DIR / "faster_whisper_server.py"
    if not ss.exists():
        from yume.utils import BASE_DIR

        ss = BASE_DIR / "faster_whisper_server.py"
    if not ss.exists():
        error("Whisper server script not found in server/ or root")
        info("Fix: Re-extract Yume or run Setup again")
        pause()
        return False

    dev = cfg["whisper_device"]
    comp = cfg["whisper_compute_type"]
    gpu = detect_gpu()
    if dev == "auto":
        if gpu["has_nvidia"]:
            dev = "cuda"
        elif gpu.get("has_amd") and not IS_WIN:
            dev = "cuda"
        else:
            dev = "cpu"
    if comp == "auto":
        comp = (
            "float16"
            if dev == "cuda" and gpu.get("vram_mb", 0) >= 8000
            else ("int8_float16" if dev == "cuda" else "int8")
        )

    cmd = [
        sys.executable,
        str(ss),
        "--model",
        cfg["whisper_model"],
        "--device",
        dev,
        "--compute-type",
        comp,
        "--port",
        str(cfg["whisper_port"]),
        "--pause-threshold",
        str(cfg["pause_threshold"]),
    ]
    if not cfg.get("word_timestamps"):
        cmd.append("--no-word-timestamps")
    if CONFIG_FILE.exists():
        cmd.extend(["--config", str(CONFIG_FILE)])

    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    lp = LOGS_DIR / "whisper_server.log"
    lh = open(lp, "w", encoding="utf-8", errors="replace")  # noqa: SIM115
    lhs.append(lh)
    info(f"Device: {dev} | Compute: {comp} | Port: {cfg['whisper_port']}")
    info(f"Log: {lp}")

    p = subprocess.Popen(cmd, stdout=lh, stderr=subprocess.STDOUT, env=env)
    procs.append(("Whisper", p))

    def _whisper_ready() -> bool | None:
        if p.poll() is not None:
            return None
        st = check_server(cfg["whisper_host"], cfg["whisper_port"], "/health")
        return st["up"] and st["data"].get("status") == "ready"

    ready = spin_wait(
        lambda: _whisper_ready() is True,
        f"Loading Whisper model ({cfg['whisper_model']})...",
        timeout=240,
        interval=2,
    )
    if p.poll() is not None:
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
        for port_key in ["whisper_port", "translation_port"]:
            port = cfg.get(port_key)
            if port and not is_port_free(port):
                kill_port_process(port)

    def _show_logs() -> None:
        section("Recent Logs")
        for name in ["whisper_server.log", "translation_server.log"]:
            lp = LOGS_DIR / name
            if lp.exists():
                info(f"{C.BOLD}{name}{C.RESET}")
                try:
                    with open(lp, encoding="utf-8", errors="replace") as f:
                        lines = f.readlines()
                    yume_lines = [
                        ln
                        for ln in lines
                        if "[Yume]" in ln or "error" in ln.lower() or "fail" in ln.lower() or "warn" in ln.lower()
                    ]
                    access_lines = [ln for ln in lines if "HTTP/" in ln and "/health" not in ln]
                    other_lines = [
                        ln
                        for ln in lines
                        if ln not in yume_lines and ln not in access_lines and "GET /health" not in ln
                    ]
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
                if ask_yn("Restart crashed server(s)?", default=True):
                    info("Restarting... (use Launch from main menu for full restart)")
                    _stop_all()
                    return

            ws_up = check_server(cfg["whisper_host"], cfg["whisper_port"], "/health")["up"]
            ts_up = check_translation_server(cfg["translation_host"], cfg["translation_port"], bi)["up"]

            ws_dot = f"{C.GREEN}●{C.RESET}" if ws_up else f"{C.RED}●{C.RESET}"
            ts_dot = f"{C.GREEN}●{C.RESET}" if ts_up else f"{C.RED}●{C.RESET}"

            print()
            panel(
                f"  {ws_dot} Whisper     {cfg['whisper_host']}:{cfg['whisper_port']}\n"
                f"  {ts_dot} Translation {cfg['translation_host']}:{cfg['translation_port']}",
                title=f"{C.GREEN}Yume Running{C.RESET}",
                style=C.GREEN,
            )

            ch = ask_choice(
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
                if ask_yn("Stop all servers?", default=True):
                    _stop_all()
                    return
    except KeyboardInterrupt:
        print()
        _stop_all()
