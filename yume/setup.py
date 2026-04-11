"""First-run setup wizard and uninstall."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from yume.hardware import IS_WIN, detect_gpu, detect_ram_gb, disk_free_gb, recommend_whisper_model
from yume.network import HEALTH_PATH_OLLAMA, check_server
from yume.ui import C, ask_arrow, ask_choice, ask_yn, center, error, header, info, pause, section, success, warn
from yume.utils import BASE_DIR, EXE, LOGS_DIR, SERVER_DIR, TOOLS_DIR, _run, _try_import, find_gguf_models, find_tool

_log = logging.getLogger("pocket_yume")

# Injected at startup by pocket_yume
_BACKEND_INFO: dict = {}
_MODELS_DIR: Path = BASE_DIR / "models"
_GGUF_DIR: Path = BASE_DIR / "models" / "translation"
_VERSION: str = "0.0.9"


def set_setup_context(backend_info: dict, models_dir: Path, gguf_dir: Path, version: str) -> None:
    """Called by pocket_yume at startup to inject shared state."""
    global _BACKEND_INFO, _MODELS_DIR, _GGUF_DIR, _VERSION
    _BACKEND_INFO = backend_info
    _MODELS_DIR = models_dir
    _GGUF_DIR = gguf_dir
    _VERSION = version


# ── Setup wizard ──────────────────────────────────────────────────────────────


_TOTAL_STEPS = 5


def _step(n: int, name: str) -> None:
    """Print a step indicator: Step N/5: Name."""
    info(f"\n{C.GOLD}Step {n}/{_TOTAL_STEPS}: {C.BOLD}{name}{C.RESET}")
    print()


def _install_with_retry(label: str, install_fn, manual_hint: str = "") -> bool:
    """Run install_fn(); on failure offer Retry / Skip / Abort."""
    for attempt in range(1, 4):
        try:
            result = install_fn()
            if result is not False:
                return True
            raise RuntimeError("install returned False")
        except Exception as exc:
            error(f"{label} failed (attempt {attempt}/3): {exc}")
        ch = ask_arrow(
            f"{label} failed. What now?",
            [
                ("Retry", "Try installing again"),
                ("Skip", "Continue without this component"),
                ("Abort setup", "Exit the wizard now"),
            ],
            default=0,
            allow_back=False,
        )
        if ch == 1:
            if manual_hint:
                info(manual_hint)
            return False
        if ch == 2:
            info("Aborting setup.")
            raise SystemExit(0)
    return False


def setup_wizard(cfg: dict) -> dict:
    from config import DEFAULT_OLLAMA_PORT, save_config

    from yume.installers import (
        install_deno,
        install_ffmpeg,
        install_llamacpp_python,
        install_ollama,
        install_python_deps,
        install_ytdlp,
        pull_ollama_model,
    )
    from yume.menus import browse_hf

    header("First-Time Setup")
    print(center(f"{C.BOLD}Welcome to Pocket Yume!{C.RESET}"))
    print(center(f"{C.DIM}Let's get Yume AI subtitles ready on your machine.{C.RESET}"))
    import platform

    plat = platform.system()
    arch = platform.machine().lower()
    print(center(f"{C.DIM}Platform: {plat} ({arch}){C.RESET}"))
    print()
    info(f"{C.DIM}Note: First startup downloads the Whisper model (~1-3 GB) — subsequent launches are fast.{C.RESET}")
    print()

    # ── Step 1: System Scan ────────────────────────────────────────────────────
    _step(1, "Detecting Hardware")
    info(f"{C.DIM}Scanning GPU, RAM, and disk to recommend the best settings...{C.RESET}")
    print()
    gpu = detect_gpu()
    ram = detect_ram_gb()
    disk = disk_free_gb()

    pyver = f"{sys.version_info.major}.{sys.version_info.minor}"
    if sys.version_info >= (3, 13):
        warn(f"Python {pyver} detected — some packages may need to build from source.")
        info("This requires C++ build tools. If installs fail, use Python 3.11 or 3.12.")
        print()

    has_gpu = gpu["has_nvidia"] or (gpu.get("has_amd") and not IS_WIN)
    (success if has_gpu else warn)(
        f"GPU:  {gpu['name'] or 'None'}"
        + (
            f" ({gpu['vram_mb']} MB)"
            if gpu["has_nvidia"]
            else f" ({gpu.get('vram_mb', '?')} MB, ROCm)"
            if gpu.get("has_amd") and not IS_WIN
            else " -- CPU mode"
        )
    )
    if not has_gpu:
        info(f"{C.DIM}No compatible GPU found. Yume will use your CPU for transcription.{C.RESET}")
        info(f"{C.DIM}This works fine but is slower. NVIDIA GPUs with 4+ GB VRAM give best performance.{C.RESET}")
    success(f"RAM:  {ram:.1f} GB")
    (success if disk >= 10 else warn)(f"Disk: {disk:.1f} GB free" + ("" if disk >= 10 else " -- need ~8 GB"))

    existing_model = cfg.get("whisper_model", "")
    is_custom_path = existing_model and (os.path.sep in existing_model or "/" in existing_model)
    rec_model, rec_reason = recommend_whisper_model(gpu)
    if is_custom_path:
        info(f"Custom Whisper model preserved: {C.BOLD}{Path(existing_model).name}{C.RESET}")
        info(f"{C.DIM}{existing_model}{C.RESET}")
    else:
        info(f"Recommendation: {rec_reason}")
        cfg["whisper_model"] = rec_model
    if gpu["has_nvidia"]:
        cfg["whisper_device"] = "cuda"
        if gpu["vram_mb"] >= 8000:
            cfg["whisper_compute_type"] = "float16"
        elif gpu["vram_mb"] >= 4000:
            cfg["whisper_compute_type"] = "int8_float16"
        else:
            cfg["whisper_compute_type"] = "int8"
    elif gpu["has_amd"] and not IS_WIN:
        cfg["whisper_device"] = "auto"
        vram = gpu.get("vram_mb", 0)
        if vram >= 8000:
            cfg["whisper_compute_type"] = "float16"
        elif vram >= 4000:
            cfg["whisper_compute_type"] = "int8_float16"
        else:
            cfg["whisper_compute_type"] = "int8"
        info(f"AMD GPU detected — ROCm (Linux), compute: {cfg['whisper_compute_type']}")
    else:
        cfg["whisper_device"] = "cpu"
        cfg["whisper_compute_type"] = "int8"

    pause()

    # ── Step 2: Component Check ────────────────────────────────────────────────
    _step(2, "Component Check")
    info(f"{C.DIM}Checking which tools and packages are already installed...{C.RESET}")
    print()
    header("Component Check")
    missing = []
    yt = find_tool("yt-dlp")
    ff = find_tool("ffmpeg")
    dn = find_tool("deno")
    (success if yt else warn)(f"yt-dlp: {'OK ' + yt if yt else 'MISSING'}")
    (success if ff else warn)(f"FFmpeg: {'OK ' + ff if ff else 'MISSING'}")
    info(f"Deno:   {'OK ' + dn if dn else '-- not installed (optional)'}")

    if not yt:
        missing.append("yt-dlp")
    if not ff:
        missing.append("ffmpeg")

    try:
        import faster_whisper  # noqa: F401

        success("faster-whisper: OK")
    except ImportError:
        warn("faster-whisper: MISSING")
        missing.append("python_deps")

    try:
        import llama_cpp  # noqa: F401

        success("llama-cpp-python: OK")
    except ImportError:
        warn("llama-cpp-python: MISSING")
        missing.append("llama_cpp")

    try:
        import uvicorn  # noqa: F401
        import fastapi  # noqa: F401

        success("Server deps (uvicorn/fastapi): OK")
    except ImportError:
        warn("Server deps (uvicorn/fastapi): MISSING")
        missing.append("server_deps")

    gf = find_gguf_models()
    if gf:
        success(f"GGUF models: {len(gf)} found")
        cfg["gguf_model_path"] = str(gf[0])
        cfg["translation_model"] = gf[0].name
    else:
        warn("No GGUF translation model found")
        missing.append("gguf_model")

    print()
    detected = []
    for k, bi in _BACKEND_INFO.items():
        if k == "custom":
            continue
        if check_server(bi["dh"], bi["dp"], bi["hp"])["up"]:
            detected.append(k)
            success(f"Detected: {bi['name']} on port {bi['dp']}")

    if not missing and (detected or gf):
        success(f"\n  {C.BOLD}Everything ready!{C.RESET}")
        cfg["first_run_complete"] = True
        save_config(cfg)
        pause()
        return cfg

    if missing:
        print(f"\n  Missing: {C.BOLD}{', '.join(missing)}{C.RESET}\n")
        mode = ask_arrow(
            "Proceed?",
            [
                ("Install all", "Download everything needed automatically"),
                ("Choose each", "Ask before installing each component"),
                ("Skip", "Set up later from the Tools menu"),
            ],
            default=0,
            allow_back=False,
        )

        if mode == 2:
            cfg["first_run_complete"] = True
            save_config(cfg)
            pause()
            return cfg

        ae = mode == 1  # ask-each

        # ── Step 3: YouTube Authentication ────────────────────────────────────
        _step(3, "YouTube Authentication")
        header("YouTube Authentication")
        info("YouTube blocks automated downloads to prevent bots.")
        info("Yume needs a way to prove you're a real person.")
        info(f"\n{C.DIM}Browser Cookies is the easiest — just be logged into YouTube in your browser.{C.RESET}")
        print()
        ch = ask_arrow(
            "How do you want to authenticate with YouTube?",
            [
                (
                    "Browser Cookies (recommended)",
                    "Uses your browser's YouTube login. Be logged into YouTube in your browser.",
                ),
                (
                    "Deno (no account needed)",
                    "Downloads Deno (~35 MB) + sets up a local server that solves YouTube's bot challenge.",
                ),
                ("Skip for now", "You can set this up later in Settings."),
            ],
            default=0,
            allow_back=False,
        )
        if ch == 0:
            cfg["youtube_auth_method"] = "cookies"
            browsers = ["chrome", "firefox", "edge", "brave", "safari"]
            print()
            info("This only affects audio downloading — the extension itself works in any browser.")
            bc = ask_arrow(
                "Which browser are you logged into YouTube with?",
                [(b.capitalize(), None) for b in browsers],
                default=0,
                allow_back=False,
            )
            cfg["cookies_browser"] = browsers[bc]
            success(f"Using cookies from: {browsers[bc]}")
        elif ch == 1:
            _install_with_retry(
                "Deno",
                install_deno,
                f"Download Deno manually: {C.CYAN}https://deno.land{C.RESET}",
            )
            cfg["youtube_auth_method"] = "deno"
        save_config(cfg)

        # ── Step 4: Install components ─────────────────────────────────────────
        _step(4, "Installing Components")
        header("Installing")

        if "yt-dlp" in missing:
            if not ae or ask_yn("Install yt-dlp?"):
                _install_with_retry(
                    "yt-dlp",
                    install_ytdlp,
                    f"Download manually: {C.CYAN}https://github.com/yt-dlp/yt-dlp/releases/latest{C.RESET}\n"
                    f"Place the binary in: {C.BOLD}{TOOLS_DIR}{C.RESET}",
                )

        if "ffmpeg" in missing:
            if not ae or ask_yn("Install FFmpeg?"):
                _install_with_retry(
                    "FFmpeg",
                    install_ffmpeg,
                    f"Download manually: {C.CYAN}https://ffmpeg.org/download.html{C.RESET}\n"
                    f"Place ffmpeg{EXE} in: {C.BOLD}{TOOLS_DIR}{C.RESET}",
                )

        if "python_deps" in missing:
            if not ae or ask_yn("Install Python packages?"):
                _install_with_retry("Python packages", install_python_deps)

        if "llama_cpp" in missing:
            if not ae or ask_yn("Install llama-cpp-python (translation engine)?"):
                _install_with_retry(
                    "llama-cpp-python",
                    install_llamacpp_python,
                    f"Install manually: {C.CYAN}pip install llama-cpp-python{C.RESET}",
                )

        if "server_deps" in missing and "llama_cpp" not in missing:
            if not ae or ask_yn("Install server dependencies (uvicorn, fastapi)?"):
                def _install_server_deps():
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
                    )

                info("Installing server dependencies...")
                _install_with_retry(
                    "Server dependencies",
                    _install_server_deps,
                    f"Install manually: {C.CYAN}pip install uvicorn fastapi sse-starlette{C.RESET}",
                )

        # ── Step 5: Translation Model ──────────────────────────────────────────
        _step(5, "Translation Model")
        if "gguf_model" not in missing:
            gf = find_gguf_models()
            gf_name = gf[0].name if gf else cfg.get("translation_model", "configured")
            success(f"Translation model already configured: {C.BOLD}{gf_name}{C.RESET}")
        if "gguf_model" in missing:
            _step(5, "Translation Model")
            section("Translation Model")
            info("Yume needs a GGUF model file to translate subtitles into English.")
            info(f"{C.DIM}llama.cpp loads it directly — no external servers required.{C.RESET}")
            print()
            ch = ask_arrow(
                "How to get a model?",
                [
                    ("Download from HuggingFace", "Browse model repos and pick a quantization"),
                    ("Use Ollama instead", "One-click install with built-in model management"),
                    ("Skip", "I'll add a .gguf file to models/translation/ later"),
                ],
                default=0,
                allow_back=False,
            )
            if ch == 0:
                browse_hf(cfg)
            elif ch == 1:
                bi = _BACKEND_INFO["ollama"]
                cfg["translation_backend"] = "ollama"
                cfg["translation_host"] = bi["dh"]
                cfg["translation_port"] = bi["dp"]
                cfg["translation_model"] = "qwen2.5:7b"
                if ask_yn("Install Ollama?"):
                    _install_with_retry(
                        "Ollama",
                        install_ollama,
                        f"Install manually: {C.CYAN}https://ollama.com{C.RESET}",
                    )
                    time.sleep(2)
                    if ask_yn(f"Download model ({cfg['translation_model']})?"):
                        if not check_server("127.0.0.1", DEFAULT_OLLAMA_PORT, HEALTH_PATH_OLLAMA)["up"]:
                            try:
                                subprocess.Popen(
                                    ["ollama", "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                                )
                                time.sleep(3)
                            except Exception as e:
                                _log.debug("[setup_wizard] post-setup-backup failed: %s", e)
                        pull_ollama_model(cfg["translation_model"])
            save_config(cfg)

    cfg["first_run_complete"] = True
    save_config(cfg)
    try:
        from config import config_export

        config_export(cfg, BASE_DIR / "yume_config_post_setup.json")
    except Exception as e:
        _log.debug("[setup_wizard] config-backup failed: %s", e)

    print()
    section("Installation Summary")
    bk = cfg.get("translation_backend", "llamacpp")
    checks = [
        ("yt-dlp", find_tool("yt-dlp") is not None),
        ("ffmpeg", find_tool("ffmpeg") is not None),
        ("faster-whisper", _try_import("faster_whisper")),
    ]
    if bk == "llamacpp":
        checks.append(("llama-cpp-python", _try_import("llama_cpp")))
        checks.append(("GGUF model", len(find_gguf_models()) > 0))
    elif bk == "ollama":
        checks.append(
            ("Ollama", check_server("127.0.0.1", cfg.get("translation_port", 11434), HEALTH_PATH_OLLAMA)["up"])
        )

    all_ok = True
    for name, ok in checks:
        if ok:
            success(f"{name}")
        else:
            error(f"{name} — not installed")
            all_ok = False

    print()
    if all_ok:
        success(f"{C.BOLD}Setup complete! All components installed.{C.RESET}")
    else:
        warn("Setup finished with missing components.")
        info("Run 'python pocket_yume.py health' to see details.")
        info("Missing components can be installed from the Tools menu.")
    pause()
    return cfg


# ── Uninstall ─────────────────────────────────────────────────────────────────


def uninstall_yume() -> None:
    from config import CONFIG_DIR

    MiB = 1024**2

    header("Uninstall Yume")
    warn("This will remove all Yume data from your system.")
    print()
    info("What will be removed:")
    dirs = [
        ("Tools (yt-dlp, ffmpeg, deno)", TOOLS_DIR),
        ("Translation models (downloaded AI model files)", _GGUF_DIR),
        ("Server files", SERVER_DIR),
        ("Configuration", CONFIG_DIR),
        ("Logs", LOGS_DIR),
    ]
    for label, d in dirs:
        sz = 0
        if d.exists():
            for f in d.rglob("*"):
                try:
                    sz += f.stat().st_size
                except Exception as e:
                    _log.debug("[uninstall_yume] file-stat failed: %s", e)

        mb = sz / MiB
        marker = f"{C.GREEN}✓{C.RESET}" if d.exists() else f"{C.DIM}—{C.RESET}"
        print(f"    {marker} {label:40s} {mb:>8.1f} MB  {d}")
    print()

    pip_pkgs = [
        "faster-whisper",
        "llama-cpp-python",
        "flask",
        "flask-cors",
        "uvicorn",
        "fastapi",
        "sse-starlette",
        "starlette-context",
        "pydantic-settings",
    ]
    info("Pip packages that can be removed:")
    print(f"    {', '.join(pip_pkgs)}")
    print()

    ch = ask_choice(
        "What to remove?",
        [
            ("Everything (data + pip packages)", "Full clean uninstall"),
            ("Data only (keep pip packages)", "Remove tools, models, config, logs"),
            ("Pip packages only", "Uninstall Python packages installed by Yume"),
            ("Cancel", None),
        ],
        default=3,
    )

    if ch in (3, -1):
        info("Cancelled.")
        pause()
        return

    remove_data = ch in (0, 1)
    remove_pip = ch in (0, 2)

    if not ask_yn("Are you sure? This cannot be undone.", False):
        info("Cancelled.")
        pause()
        return

    if remove_data:
        for label, d in dirs:
            if d.exists():
                try:
                    shutil.rmtree(d)
                    success(f"Removed: {label}")
                except Exception as e:
                    error(f"Failed to remove {label}: {e}")

        if _MODELS_DIR.exists():
            try:
                shutil.rmtree(_MODELS_DIR)
            except Exception as e:
                _log.debug("[uninstall_yume] cleanup failed: %s", e)

    if remove_pip:
        info("Uninstalling pip packages...")
        for pkg in pip_pkgs:
            try:
                r = _run([sys.executable, "-m", "pip", "uninstall", "-y", pkg], timeout=30)
                if r.returncode == 0:
                    success(f"  Removed: {pkg}")
            except Exception as e:
                _log.debug("[uninstall_yume] pip-remove failed: %s", e)

    print()
    success("Uninstall complete!")
    info("You can delete pocket_yume.py and the extension/ folder manually.")
    info("To remove the browser extension: go to chrome://extensions and remove Yume.")
    pause()
