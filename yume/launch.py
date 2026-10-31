"""Interactive launcher: start Yume's servers, then the runtime menu.

The process work (commands, spawn, wait, crash diagnosis, stop) lives in
yume/servers.py, shared with the headless supervisor (yume/service.py); this
module adds what needs a person: port and install prompts, a spinner, and the
crash advice on screen. yume/resource_check.py and yume/runtime_menu.py hold the
check before starting and the menu after it.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from yume import servers
from yume.constants import VERSION, GGUF_DIR, LOGS_DIR, IS_WIN, BACKEND_INFO, DEFAULT_OLLAMA_MODEL, backend_info
from yume.hardware import detect_gpu
from yume.network import check_ollama_models, check_translation_server, discover_servers, reset_api_token
from yume.ports import ensure_port_free, find_free_port, is_port_free, kill_port_process
from yume.resource_check import check_resources
from yume.runtime_menu import runtime_menu
from yume.ui import C, Spinner, ask_yn, clear, error, header, info, pause, success, warn
from yume.utils import _run


def _offer_llama_server() -> bool:
    """When llama-cpp-python would translate on the CPU although a GPU is
    there, offer llama.cpp's prebuilt GPU server instead (much faster)."""
    from yume import llama_server

    gpu = detect_gpu()
    if not (gpu["has_nvidia"] or gpu["has_amd"]) or llama_server.python_engine_on_gpu():
        return False
    warn("The translator would run on the CPU (llama-cpp-python has no GPU support here).")
    info("llama.cpp's own GPU server translates ~10x faster — about 200-700 MB to download.")
    if not ask_yn("Install it now?", True):
        return False
    return llama_server.install(gpu)


# ── Public entry point ────────────────────────────────────────────────────────


def launch_services(cfg: dict) -> None:
    header("Launching Yume")
    procs = servers.Processes()
    # Each server run mints a fresh API token. Drop any token cached from an earlier
    # launch in this same interactive session, so the runtime menu's blacklist/stats/
    # model-switch don't fail auth against the new server and look like it's offline.
    reset_api_token()
    try:
        _launch_inner(cfg, procs)
    except KeyboardInterrupt:
        print(f"\n  {C.YELLOW}Cancelled — cleaning up...{C.RESET}")
        procs.stop(cfg, timeout=1)
        print(f"  {C.GREEN}Processes stopped.{C.RESET}\n")
    except Exception as e:
        error(f"Launch failed: {e}")
        info(f"Try: {C.CYAN}python pocket_yume.py health{C.RESET} to diagnose the issue.")
        procs.stop(cfg, timeout=1)
        pause()


# ── Internal launch logic ─────────────────────────────────────────────────────


def _use_running_servers(cfg: dict, existing: dict) -> bool:
    """Servers already up: reuse them (True = the launch is done)."""
    from config import save_config

    if existing.get("whisper") and existing.get("translation"):
        info("Both servers are already running!")
        if ask_yn("Open runtime menu with existing servers?", True):
            runtime_menu(cfg, servers.Processes(), cfg.get("translation_backend", "llamacpp"))
            return True
    port = existing.get("ollama_found")
    if port:
        info(f"Found Ollama running on port {port}")
        if cfg["translation_port"] != port and ask_yn(f"Update translation port to {port}?", True):
            cfg["translation_port"] = port
            save_config(cfg)
    return False


def _free_port_or_stop(port: int, cfg: dict, kind: str, other: int) -> int | None:
    """A busy port: offer to free it or move to another; None = cannot launch."""
    info(f"Port {port} is busy. Another Yume instance or other app may be using it.")
    port = ensure_port_free(port, cfg, kind, exclude={other})
    if port is None:
        error(f"Cannot proceed without a free {kind} port.")
        info(f"Change the port in: {C.CYAN}python pocket_yume.py settings{C.RESET}")
        pause()
    return port


def _prepare_ports(cfg: dict, existing: dict) -> bool:
    """Distinct, free ports for Whisper and (llama.cpp) translation. False = cannot launch."""
    from config import DEFAULT_TRANSLATION_PORT, DEFAULT_WHISPER_PORT, save_config

    wport = cfg.get("whisper_port", DEFAULT_WHISPER_PORT)
    tport = cfg.get("translation_port", DEFAULT_TRANSLATION_PORT)
    if wport == tport:
        error("Whisper and translation ports are the same!")
        tport = find_free_port(wport + 1, exclude={wport})
        if not tport:
            error("Could not find a free port. Many ports may be in use on this system.")
            info(f"Change ports manually in: {C.CYAN}python pocket_yume.py settings{C.RESET}")
            pause()
            return False
        cfg["translation_port"] = tport
        save_config(cfg)
        success(f"Auto-assigned translation to port {tport}")
    if not existing.get("whisper") and not is_port_free(wport):
        wport = _free_port_or_stop(wport, cfg, "whisper", tport)
        if wport is None:
            return False
    # Only llama.cpp is started by Yume on the translation port; for Ollama /
    # LM Studio / custom servers that port is SUPPOSED to be in use.
    llamacpp = cfg.get("translation_backend", "llamacpp") == "llamacpp"
    if llamacpp and not existing.get("translation") and not is_port_free(tport):
        return _free_port_or_stop(tport, cfg, "translation", wport) is not None
    return True


def _offer_missing_packages() -> None:
    """Missing server packages: things still run (pypinyin → slower LLM pinyin,
    waitress → Flask's dev server), so warn and offer the install, don't block."""
    from yume.utils import missing_requirements

    missing = missing_requirements()
    if missing:
        warn(f"Missing server packages: {', '.join(missing)}")
        if ask_yn("Install them now?", True):
            from yume.installers import install_python_deps

            install_python_deps()


def _start_translation(cfg: dict, procs: servers.Processes, env: dict) -> bool:
    bk = cfg.get("translation_backend", "llamacpp")
    if bk == "llamacpp":
        return _start_llamacpp(cfg, procs, backend_info(bk), env)
    if bk == "ollama":
        return _start_ollama(cfg, procs, env)
    name = BACKEND_INFO.get(bk, {}).get("name", bk)
    info(f"{name} -- make sure it's running at {cfg['translation_host']}:{cfg['translation_port']}")
    return True


def _launch_inner(cfg: dict, procs: servers.Processes) -> None:
    """Actual server startup. Ctrl+C caught by launch_services()."""
    existing = discover_servers(cfg, BACKEND_INFO)
    if _use_running_servers(cfg, existing) or not _prepare_ports(cfg, existing):
        return
    _offer_missing_packages()
    if not check_resources(cfg):
        return

    env = servers.build_server_env()
    if env.get("HSA_OVERRIDE_GFX_VERSION") and "HSA_OVERRIDE_GFX_VERSION" not in os.environ:
        warn("AMD RDNA1 GPU detected. Setting HSA_OVERRIDE_GFX_VERSION=10.3.0")
        info("Add 'export HSA_OVERRIDE_GFX_VERSION=10.3.0' to ~/.bashrc to make this permanent.")
    if not _start_translation(cfg, procs, env) or not _start_whisper(cfg, procs, env):
        return

    bk = cfg.get("translation_backend", "llamacpp")
    clear()
    print()
    success(f"Yume is running!  {C.DIM}v{VERSION}{C.RESET}")
    tn = BACKEND_INFO.get(bk, {}).get("name", bk)
    print(f"  {C.CYAN}Whisper{C.RESET}      http://{cfg['whisper_host']}:{cfg['whisper_port']}")  # noqa: S5332 — display only, local server
    print(f"  {C.MAGENTA}Translation{C.RESET}  http://{cfg['translation_host']}:{cfg['translation_port']}  ({tn})")  # noqa: S5332 — display only, local server
    print()
    info(f"{C.DIM}Chrome -> Japanese video -> Yume extension -> Enable{C.RESET}")
    print()
    runtime_menu(cfg, procs, bk)


def _show_crash(result: servers.StartResult) -> None:
    error(result.error)
    if result.log_name:
        for line in servers.log_tail(result.log_name)[-10:]:
            print(f"  {C.DIM}{line}{C.RESET}")
    if result.diagnosis:
        print()
        info(f"{C.BOLD}Diagnosis: {result.diagnosis[0]}{C.RESET}")
        for line in result.diagnosis[1:]:
            info(line)
    if result.log_name:
        info(f"Full log: {LOGS_DIR / result.log_name}")


def _report(result: servers.StartResult, spin: Spinner, ready_msg: str, slow_msg: list[str]) -> bool:
    """Show how starting a server went. False = stop launching (after a pause)."""
    if result.ok:
        spin.finish("ok")
        success(ready_msg)
        return True
    if result.timed_out:  # a large model can take longer: keep going
        spin.finish("timeout")
        for i, line in enumerate(slow_msg):
            (warn if i == 0 else info)(line)
        return True
    spin.finish("failed")
    _show_crash(result)
    pause()
    return False


def _pick_gguf(cfg: dict) -> str | None:
    """The model llama.cpp will load; None (said why) without one."""
    configured = cfg.get("gguf_model_path", "")
    gp = servers.resolve_gguf(cfg)
    if gp is None:
        error("No .gguf model found in models/translation/")
        info("Fix: Main Menu → Tools → Download GGUF Model")
        info(f"Or: place a .gguf file in {GGUF_DIR}")
        pause()
    elif gp != configured:
        info(f"Auto-selected GGUF: {Path(gp).name}")
    return gp


def _install_server_deps(env: dict) -> bool:
    try:
        r = _run([sys.executable, "-m", "pip", "install", *servers.LLAMA_SERVER_DEPS, "-q",
                  "--no-warn-script-location"], timeout=300, env=env)  # fmt: skip
    except Exception as e:
        error(f"Failed to install: {e}")
        return False
    if r.returncode != 0:
        error(f"Failed to install: {(r.stderr or '').strip()[-300:]}")
        return False
    success("Server dependencies installed!")
    return True


def _ensure_python_engine(env: dict) -> bool:
    """llama-cpp-python and the packages its server needs. False = launching cannot go on."""
    from yume.installers import install_llamacpp_python

    try:
        import llama_cpp  # noqa: F401
    except ImportError:
        error("llama-cpp-python not installed! This is the translation engine.")
        info(f"You can also install it manually: {C.CYAN}pip install llama-cpp-python{C.RESET}")
        if ask_yn("Install now?"):
            install_llamacpp_python()
        else:
            pause()
        return False
    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
    except ImportError:
        warn("Server dependencies missing (uvicorn/fastapi)")
        info("Installing them now...")
        if not _install_server_deps(env):
            pause()
            return False
    return True


def _start_llamacpp(cfg: dict, procs: servers.Processes, bi: dict, env: dict) -> bool:
    """Start the llama.cpp translation server (after the install prompts it may
    need). False = launching cannot go on."""
    from yume import llama_server

    if check_translation_server(cfg["translation_host"], cfg["translation_port"], bi)["up"]:
        success("llama.cpp server already running!")
        return True
    gp = _pick_gguf(cfg)
    if gp is None:
        return False

    native = llama_server.server_path() is not None
    if not native and _offer_llama_server():
        native = llama_server.server_path() is not None
    if not native and not _ensure_python_engine(env):
        return False

    if not kill_port_process(cfg["translation_port"]):  # asks before killing another program
        error(f"Port {cfg['translation_port']} is still in use — cannot start the translation server.")
        pause()
        return False
    info(f"Starting llama.cpp server with {Path(gp).name}...")
    _describe_engine(native)
    info(f"Log: {LOGS_DIR / servers.TRANSLATION_LOG}")
    spin = Spinner("Loading translation model...")
    r = servers.start_llamacpp(cfg, bi, procs, env, tick=spin.tick)
    return _report(r, spin, "Translation server ready!", ["Still loading — large models may take a few minutes"])


def _describe_engine(native: bool) -> None:
    """Say what will run the translation model (and warn when it is the CPU)."""
    from yume import llama_server

    gpu = detect_gpu()
    if native:
        info(f"Engine: {llama_server.build_label()}")
    elif gpu["has_nvidia"] or (gpu["has_amd"] and not IS_WIN):
        if llama_server.python_engine_on_gpu():
            info("GPU offloading enabled (--n_gpu_layers -1)")
        else:
            warn("llama-cpp-python is a CPU-only build — model will load into RAM, not VRAM.")
            warn("Run: python pocket_yume.py setup → reinstall packages to fix this.")
    else:
        info("No supported GPU found — running translation model on CPU")


def _start_ollama(cfg: dict, procs: servers.Processes, env: dict) -> bool:
    """Start Ollama and make sure the translation model is pulled."""
    from yume.installers import pull_ollama_model

    r = servers.start_ollama(cfg, procs, env)
    if r.already_running:
        success("Ollama already running")
    elif not r.ok and not r.timed_out:
        error(r.error)
        info(f"Or switch to the llama.cpp backend in: {C.CYAN}python pocket_yume.py settings{C.RESET}")
        pause()
        return False
    elif r.ok:
        success("Ollama running!")
    else:
        warn("Ollama may still be starting...")
    if not cfg.get("translation_model") or cfg["translation_model"].endswith(".gguf"):
        # Empty (or a leftover GGUF filename from llama.cpp): Ollama needs a real
        # model name in every request, so pick the default instead of failing later.
        from config import save_config

        cfg["translation_model"] = DEFAULT_OLLAMA_MODEL
        save_config(cfg)
        info(f"Translation model set to {DEFAULT_OLLAMA_MODEL} (change it in Settings → Translation settings)")
    model = cfg["translation_model"]
    ms = check_ollama_models(cfg["translation_host"], cfg["translation_port"])
    if model not in ms and f"{model}:latest" not in ms:
        warn(f"Model {model} not found in Ollama. Pulling...")
        pull_ollama_model(model)
    return True


def _start_whisper(cfg: dict, procs: servers.Processes, env: dict) -> bool:
    """Start the Whisper server. False = launching cannot go on."""
    if servers.whisper_up(cfg):
        success("Whisper already running!")
        return True
    if not kill_port_process(cfg["whisper_port"]):  # asks before killing another program
        error(f"Port {cfg['whisper_port']} is still in use — cannot start the Whisper server.")
        pause()
        return False
    info("Starting Whisper server...")
    dev, comp = servers.resolve_whisper_device(cfg)
    info(f"Device: {dev} | Compute: {comp} | Port: {cfg['whisper_port']}")
    info(f"Log: {LOGS_DIR / servers.WHISPER_LOG}")
    spin = Spinner(f"Loading Whisper model ({cfg['whisper_model']})...")
    r = servers.start_whisper(cfg, procs, env, tick=spin.tick)
    if r.error and not r.timed_out and not r.diagnosis:
        r.diagnosis = [
            "Not recognised from the log. Quick fixes to try:",
            f"  1. Switch to CPU: {C.CYAN}python pocket_yume.py settings{C.RESET} -> device = cpu",
            f"  2. Use a smaller model: {C.CYAN}python pocket_yume.py settings{C.RESET} -> model = small",
            f"  3. Re-run setup: {C.CYAN}python pocket_yume.py setup{C.RESET}",
        ]
    return _report(
        r,
        spin,
        "Whisper server ready!",
        [
            "Still loading — first run takes 30-60s while the Whisper model downloads.",
            "This is a one-time download. Future launches will start much faster.",
        ],
    )
