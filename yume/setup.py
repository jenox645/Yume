"""First-run setup wizard and uninstall."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from yume.constants import (
    BASE_DIR,
    TOOLS_DIR,
    MODELS_DIR,
    GGUF_DIR,
    LOGS_DIR,
    EXT_DIR,
    IS_WIN,
    EXE,
    MiB,
    HEALTH_PATH_OLLAMA,
    BACKEND_INFO,
    use_backend,
)
from yume.hardware import detect_gpu, detect_ram_gb, disk_free_gb, recommend_whisper_model
from yume.network import check_server
from yume.ui import C, ask_arrow, ask_yn, center, error, header, info, pause, section, success, warn
from yume.utils import _run, _try_import, find_gguf_models, find_tool

_log = logging.getLogger("pocket_yume")


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


def _open_folder(path: Path) -> bool:
    """Open a folder in the system file manager. Best-effort, never raises."""
    try:
        if IS_WIN:
            os.startfile(str(path))  # noqa: S606  # nosec B606 — fixed local path, not user input
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])  # noqa: S603, S607  # nosec B603 B607
        else:
            subprocess.Popen(["xdg-open", str(path)])  # noqa: S603, S607  # nosec B603 B607
        return True
    except Exception:
        return False


def _extension_guide() -> None:
    """Walk the user through loading the browser extension.

    This is the step non-technical users miss: the wizard installs servers and
    models, but subtitles only appear once the extension is loaded in the
    browser — and nothing else in the first-run path explains how.
    """

    print()
    section("Browser Extension  (required — this is where subtitles appear)")
    info("One minute in your browser and you're done:")
    print()
    print(f"    1. Open {C.CYAN}chrome://extensions{C.RESET} in Chrome, Edge or Brave")
    print(f"       {C.DIM}(type it into the address bar and press Enter){C.RESET}")
    print(f"    2. Turn ON {C.BOLD}Developer mode{C.RESET}  {C.DIM}(toggle in the top-right corner){C.RESET}")
    print(f"    3. Click {C.BOLD}Load unpacked{C.RESET} and select this folder:")
    print(f"       {C.BOLD}{EXT_DIR}{C.RESET}")
    print(f"    4. Open any video, click the Yume icon in the toolbar, press {C.BOLD}Enable{C.RESET}")
    print(f"       {C.DIM}(with one-click start on, Yume starts by itself — otherwise launch it here first){C.RESET}")
    print()
    info(
        f"{C.DIM}Firefox (121+): about:debugging -> This Firefox -> Load Temporary Add-on"
        f" -> select extension/manifest.json (the same manifest works in both browsers){C.RESET}"
    )
    print()
    if ask_yn("Open the extension folder now (so it's ready to select in step 3)?", True):
        if not _open_folder(EXT_DIR):
            info(f"Could not open it automatically — the folder is: {EXT_DIR}")


def _offer_one_click() -> None:
    """Offer to let the extension start Yume by itself (native messaging)."""
    from yume import native_host

    print()
    section("One-click start")
    info("Yume can start by itself when you press Enable in the extension —")
    info("no need to open this launcher first. It stops on its own when you stop watching.")
    if native_host.registered_browsers():
        success("Already on.")
        return
    if ask_yn("Turn on one-click start?", True):
        try:
            done = native_host.register()
            success(f"On for: {', '.join(done) or 'no browser found'}")
        except Exception as e:
            warn(f"Could not turn it on ({e}) — try again later in Settings → One-click start.")


def _offer_vocal_isolation() -> None:
    """Offer vocal isolation on NVIDIA machines (optional, ~3.5 GB)."""
    from yume import vocals
    from yume.hardware import detect_gpu

    if not detect_gpu().get("has_nvidia") or vocals.status()[0]:
        return
    print()
    section("Vocal isolation (optional)")
    info("Separates the singer from the music before Whisper listens: fewer")
    info(f"transcription errors on songs. ~{vocals.DOWNLOAD_GB:.0f} GB download (PyTorch with CUDA).")
    if ask_yn("Install vocal isolation?", True):
        vocals.install()


_MISSING_LABELS = {
    "yt-dlp": "yt-dlp",
    "ffmpeg": "FFmpeg",
    "python_deps": "faster-whisper",
    "llama_server": "translation engine (llama.cpp)",
    "gguf_model": "GGUF translation model",
}


def _compute_type(vram_mb: int) -> str:
    """Whisper precision that fits in the GPU's memory."""
    if vram_mb >= 8000:
        return "float16"
    if vram_mb >= 4000:
        return "int8_float16"
    return "int8"


def _welcome() -> None:
    import platform

    # Also reached via "Re-run Setup", so not "First-Time"
    header("Setup Wizard")
    print(center(f"{C.BOLD}Welcome to Pocket Yume!{C.RESET}"))
    print(center(f"{C.DIM}Let's get Yume AI subtitles ready on your machine.{C.RESET}"))
    print(center(f"{C.DIM}Platform: {platform.system()} ({platform.machine().lower()}){C.RESET}"))
    print()
    info(f"{C.DIM}Note: First startup downloads the Whisper model (~1-3 GB) — subsequent launches are fast.{C.RESET}")
    print()


def _scan_hardware(cfg: dict) -> dict:
    """Step 1: show GPU, RAM and disk; pick the Whisper model, device and precision."""
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

    rocm = bool(gpu.get("has_amd")) and not IS_WIN
    if gpu["has_nvidia"]:
        success(f"GPU:  {gpu['name']} ({gpu['vram_mb']} MB)")
    elif rocm:
        success(f"GPU:  {gpu['name']} ({gpu.get('vram_mb', '?')} MB, ROCm)")
    else:
        warn(f"GPU:  {gpu['name'] or 'None'} -- CPU mode")
        info(f"{C.DIM}No compatible GPU found. Yume will use your CPU for transcription.{C.RESET}")
        info(f"{C.DIM}This works fine but is slower. NVIDIA GPUs with 4+ GB VRAM give best performance.{C.RESET}")
    success(f"RAM:  {ram:.1f} GB")
    (success if disk >= 10 else warn)(f"Disk: {disk:.1f} GB free" + ("" if disk >= 10 else " -- need ~8 GB"))

    existing_model = cfg.get("whisper_model", "")
    if existing_model and (os.path.sep in existing_model or "/" in existing_model):
        info(f"Custom Whisper model preserved: {C.BOLD}{Path(existing_model).name}{C.RESET}")
        info(f"{C.DIM}{existing_model}{C.RESET}")
    else:
        rec_model, rec_reason = recommend_whisper_model(gpu)
        info(f"Recommendation: {rec_reason}")
        cfg["whisper_model"] = rec_model

    if gpu["has_nvidia"]:
        cfg["whisper_device"] = "cuda"
        cfg["whisper_compute_type"] = _compute_type(gpu["vram_mb"])
    elif rocm:
        cfg["whisper_device"] = "auto"
        cfg["whisper_compute_type"] = _compute_type(gpu.get("vram_mb", 0))
        info(f"AMD GPU detected — ROCm (Linux), compute: {cfg['whisper_compute_type']}")
    else:
        cfg["whisper_device"] = "cpu"
        cfg["whisper_compute_type"] = "int8"
    return gpu


def _translation_engine_ok(gpu: dict) -> bool:
    """Report the llama.cpp translation engine; False when it must be installed."""
    from yume import llama_server

    if llama_server.server_path():
        build = llama_server.installed_build()
        success(f"Translation engine: llama-server {build.get('tag', '')} ({build.get('variant', '?')})")
        return True
    # llama.cpp's prebuilt server is the engine Yume installs. An existing
    # llama-cpp-python is kept only when it already runs on the GPU (or
    # there is no GPU to use): CUDA wheels lag behind new Pythons, and the
    # source build needs the CUDA Toolkit and a C++ compiler.
    try:
        import llama_cpp.llama_cpp as _lib  # noqa: F401
        import uvicorn  # noqa: F401
        import fastapi  # noqa: F401
    except ImportError:
        warn("Translation engine: missing")
        return False
    has_gpu_hw = gpu.get("has_nvidia") or gpu.get("has_amd")
    if not has_gpu_hw or callable(getattr(_lib, "ggml_backend_cuda_reg", None)):
        success("Translation engine: llama-cpp-python")
        return True
    warn("Translation engine: llama-cpp-python runs on the CPU only — slow")
    return False


def _check_components(cfg: dict, gpu: dict) -> tuple[list[str], bool]:
    """Step 2: report what is installed.

    Returns the missing components (keys of _MISSING_LABELS) and whether a
    translation model or a running backend is already there.
    """
    _step(2, "Component Check")
    info(f"{C.DIM}Checking which tools and packages are already installed...{C.RESET}")
    print()
    missing = _missing_tools()
    gf = find_gguf_models()
    if cfg.get("translation_backend", "llamacpp") != "llamacpp":
        # Ollama / LM Studio / custom manage their own models
        info(f"Translation engine: not needed ({cfg.get('translation_backend')} backend)")
    else:
        missing += _missing_for_llamacpp(cfg, gpu, gf)

    print()
    detected = False
    for k, bi in BACKEND_INFO.items():
        if k != "custom" and check_server(bi["dh"], bi["dp"], bi["hp"])["up"]:
            detected = True
            success(f"Detected: {bi['name']} on port {bi['dp']}")
    return missing, detected or bool(gf)


def _missing_tools() -> list[str]:
    """Report yt-dlp, FFmpeg, Deno and faster-whisper; return what is missing."""
    missing = []
    yt = find_tool("yt-dlp")
    ff = find_tool("ffmpeg")
    dn = find_tool("deno")
    (success if yt else warn)(f"yt-dlp: {'installed — ' + yt if yt else 'missing'}")
    (success if ff else warn)(f"FFmpeg: {'installed — ' + ff if ff else 'missing'}")
    info(f"Deno:   {'installed — ' + dn if dn else 'not installed (optional)'}")
    missing += [] if yt else ["yt-dlp"]
    missing += [] if ff else ["ffmpeg"]
    try:
        import faster_whisper  # noqa: F401

        success("faster-whisper: installed")
    except ImportError:
        warn("faster-whisper: missing")
        missing.append("python_deps")
    return missing


def _missing_for_llamacpp(cfg: dict, gpu: dict, gf: list) -> list[str]:
    """Report llama.cpp and the GGUF models; pick a model if none is set."""
    missing = [] if _translation_engine_ok(gpu) else ["llama_server"]
    if not gf:
        warn("No GGUF translation model found")
        return [*missing, "gguf_model"]
    success(f"GGUF models: {len(gf)} found")
    # Keep the user's choice on a re-run; only fill in a missing/stale path
    if not cfg.get("gguf_model_path") or not Path(cfg["gguf_model_path"]).exists():
        cfg["gguf_model_path"] = str(gf[0])
        cfg["translation_model"] = gf[0].name
    return missing


def _choose_youtube_auth(cfg: dict) -> None:
    """Step 3: browser cookies or Deno, so YouTube lets yt-dlp download."""
    from yume.installers import install_deno

    _step(3, "YouTube Authentication")
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
        _install_with_retry("Deno", install_deno, f"Download Deno manually: {C.CYAN}https://deno.land{C.RESET}")
        cfg["youtube_auth_method"] = "deno"


def _install_missing(missing: list[str], ask_each: bool) -> None:
    """Step 4: install the missing tools and packages (all, or one by one)."""
    from yume import llama_server
    from yume.installers import install_ffmpeg, install_python_deps, install_ytdlp

    # key → (question, label, installer, manual hint)
    installs = {
        "yt-dlp": (
            "Install yt-dlp?",
            "yt-dlp",
            install_ytdlp,
            f"Download manually: {C.CYAN}https://github.com/yt-dlp/yt-dlp/releases/latest{C.RESET}\n"
            f"Place the binary in: {C.BOLD}{TOOLS_DIR}{C.RESET}",
        ),
        "ffmpeg": (
            "Install FFmpeg?",
            "FFmpeg",
            install_ffmpeg,
            f"Download manually: {C.CYAN}https://ffmpeg.org/download.html{C.RESET}\n"
            f"Place ffmpeg{EXE} in: {C.BOLD}{TOOLS_DIR}{C.RESET}",
        ),
        "python_deps": ("Install Python packages?", "Python packages", install_python_deps, ""),
        "llama_server": (
            "Install the translation engine (llama.cpp, prebuilt for your GPU)?",
            "llama.cpp server",
            llama_server.install,
            f"Download it manually from {C.CYAN}https://github.com/ggml-org/llama.cpp/releases{C.RESET}"
            f" and unzip it into {llama_server.LLAMA_DIR}",
        ),
    }
    _step(4, "Installing Components")
    for key, (question, label, install_fn, hint) in installs.items():
        if key in missing and (not ask_each or ask_yn(question)):
            _install_with_retry(label, install_fn, hint)


def _use_ollama(cfg: dict) -> None:
    """Switch translation to Ollama; offer to install it and pull a model."""

    from yume.installers import install_ollama, pull_ollama_model

    use_backend(cfg, "ollama")
    cfg["translation_model"] = "qwen2.5:7b"
    if not ask_yn("Install Ollama?"):
        return
    _install_with_retry("Ollama", install_ollama, f"Install manually: {C.CYAN}https://ollama.com{C.RESET}")
    time.sleep(2)
    if ask_yn(f"Download model ({cfg['translation_model']})?"):
        pull_ollama_model(cfg["translation_model"])  # starts Ollama if needed


def _choose_translation_model(cfg: dict, needs_model: bool) -> None:
    """Step 5: get a GGUF model from HuggingFace, or switch to Ollama."""
    from yume.menus import browse_hf

    _step(5, "Translation Model")
    if not needs_model:
        gf = find_gguf_models()
        gf_name = gf[0].name if gf else cfg.get("translation_model", "configured")
        success(f"Translation model already configured: {C.BOLD}{gf_name}{C.RESET}")
        return
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
        _use_ollama(cfg)


def _print_summary(cfg: dict) -> None:
    """What is installed now, per component the chosen backend needs."""
    from yume import llama_server

    print()
    section("Installation Summary")
    bk = cfg.get("translation_backend", "llamacpp")
    checks = [
        ("yt-dlp", find_tool("yt-dlp") is not None),
        ("ffmpeg", find_tool("ffmpeg") is not None),
        ("faster-whisper", _try_import("faster_whisper")),
    ]
    if bk == "llamacpp":
        checks.append(("Translation engine", llama_server.server_path() is not None or _try_import("llama_cpp")))
        checks.append(("GGUF model", len(find_gguf_models()) > 0))
    elif bk == "ollama":
        checks.append(
            ("Ollama", check_server("127.0.0.1", cfg.get("translation_port", 11434), HEALTH_PATH_OLLAMA)["up"])
        )
    for name, ok in checks:
        if ok:
            success(f"{name}")
        else:
            error(f"{name} — not installed")

    print()
    if all(ok for _, ok in checks):
        success(f"{C.BOLD}Setup complete! All components installed.{C.RESET}")
    else:
        warn("Setup finished with missing components.")
        info("Run 'python pocket_yume.py health' to see details.")
        info("Missing components can be installed from the Tools menu.")


def _mark_complete(cfg: dict) -> None:
    from config import save_config

    cfg["first_run_complete"] = True
    save_config(cfg)


def _closing_offers() -> None:
    """One-click start, vocal isolation and the extension guide, then wait."""
    _offer_one_click()
    _offer_vocal_isolation()
    _extension_guide()
    pause()


def setup_wizard(cfg: dict) -> dict:
    """Scan, check, then (only if something is missing) authenticate, install and pick a model."""
    from config import config_export, save_config

    _welcome()
    gpu = _scan_hardware(cfg)
    pause()
    missing, ready = _check_components(cfg, gpu)

    if not missing and ready:
        success(f"\n  {C.BOLD}Everything ready!{C.RESET}")
        _mark_complete(cfg)
        _closing_offers()
        return cfg

    if missing:
        labels = ", ".join(_MISSING_LABELS.get(m, m) for m in missing)
        print(f"\n  Needs installation: {C.BOLD}{labels}{C.RESET}")
        pause()
        _choose_youtube_auth(cfg)
        save_config(cfg)

        print(f"\n  Ready to install: {C.BOLD}{labels}{C.RESET}\n")
        mode = ask_arrow(
            "How to proceed?",
            [
                ("Install all", "Download everything needed automatically"),
                ("Choose each", "Ask before installing each component"),
                ("Skip", "Set up later from the Tools menu"),
            ],
            default=0,
            allow_back=False,
        )
        if mode == 2:
            _mark_complete(cfg)
            pause()
            return cfg
        _install_missing(missing, ask_each=mode == 1)
        _choose_translation_model(cfg, "gguf_model" in missing)
        save_config(cfg)

    _mark_complete(cfg)
    try:
        config_export(cfg, BASE_DIR / "yume_config_post_setup.json")
    except Exception as e:
        _log.debug("[setup_wizard] config-backup failed: %s", e)
    _print_summary(cfg)
    _closing_offers()
    return cfg


# ── Uninstall ─────────────────────────────────────────────────────────────────


# Other programs use these too: the uninstaller leaves them installed
_SHARED_LIBS = {"numpy", "av", "yt-dlp"}


def _yume_pip_packages() -> list[str]:
    """Exactly what Yume installs: the server's requirements, the llama-cpp-python
    engine and its server, the YouTube PO-token plugin, and demucs for vocal isolation."""
    from yume import vocals
    from yume.servers import LLAMA_SERVER_DEPS
    from yume.utils import requirement_name, server_requirements

    specs = [*server_requirements(), "llama-cpp-python", *LLAMA_SERVER_DEPS, "bgutil-ytdlp-pot-provider", vocals.DEMUCS]
    names = (requirement_name(s) for s in specs)
    return list(dict.fromkeys(n for n in names if n not in _SHARED_LIBS))


def _uninstall_targets() -> list[tuple[str, Path]]:
    from config import CONFIG_DIR
    from yume.utils import hf_hub_dir

    hf_hub = hf_hub_dir()
    whisper_caches = sorted(hf_hub.glob("models--*faster*whisper*")) if hf_hub.exists() else []
    return [
        ("Tools (yt-dlp, ffmpeg, deno)", TOOLS_DIR),
        ("Translation models (downloaded AI model files)", GGUF_DIR),
        ("Configuration, blacklist, saved subtitles", CONFIG_DIR),
        ("Logs", LOGS_DIR),
    ] + [(f"Whisper model {d.name.split('--')[-1]}", d) for d in whisper_caches]


def _folder_mb(d: Path) -> float:
    size = 0
    for f in d.rglob("*") if d.exists() else ():
        try:
            size += f.stat().st_size
        except OSError as e:
            _log.debug("[uninstall_yume] file-stat failed: %s", e)
    return size / MiB


def _remove_data(dirs: list[tuple[str, Path]]) -> None:
    """Stop Yume, unregister one-click start, delete Yume's folders."""
    from yume import native_host
    from yume.service import request_stop

    request_stop()
    native_host.unregister()
    for stray in (BASE_DIR / ".yume_token", BASE_DIR / "yume_config_post_setup.json"):
        stray.unlink(missing_ok=True)
    for label, d in dirs:
        if d.exists():
            try:
                shutil.rmtree(d)
                success(f"Removed: {label}")
            except Exception as e:
                error(f"Failed to remove {label}: {e}")
    shutil.rmtree(MODELS_DIR, ignore_errors=True)


def _remove_packages(packages: list[str]) -> None:
    info("Uninstalling pip packages...")
    for pkg in packages:
        try:
            if _run([sys.executable, "-m", "pip", "uninstall", "-y", pkg], timeout=30).returncode == 0:
                success(f"  Removed: {pkg}")
        except Exception as e:
            _log.debug("[uninstall_yume] pip-remove failed: %s", e)


def uninstall_yume() -> None:
    header("Uninstall Yume")
    warn("This will remove all Yume data from your system.")
    print()
    info("What will be removed:")
    dirs = _uninstall_targets()
    for label, d in dirs:
        marker = f"{C.GREEN}✓{C.RESET}" if d.exists() else f"{C.DIM}—{C.RESET}"
        print(f"    {marker} {label:40s} {_folder_mb(d):>8.1f} MB  {d}")
    print()
    packages = _yume_pip_packages()
    info("Pip packages that can be removed:")
    print(f"    {', '.join(packages)}")
    info(f"{C.DIM}PyTorch (vocal isolation, ~3 GB) is left installed — other programs may use it.{C.RESET}")
    print()

    ch = ask_arrow(
        "What to remove?",
        [
            ("Everything (data + pip packages)", "Full clean uninstall"),
            ("Data only (keep pip packages)", "Remove tools, models, config, logs"),
            ("Pip packages only", "Uninstall Python packages installed by Yume"),
            ("Cancel", None),
        ],
        default=3,
    )
    if ch not in (0, 1, 2) or not ask_yn("Are you sure? This cannot be undone.", False):
        info("Cancelled.")
        pause()
        return
    if ch in (0, 1):
        _remove_data(dirs)
    if ch in (0, 2):
        _remove_packages(packages)

    print()
    success("Uninstall complete!")
    info("You can delete pocket_yume.py and the extension/ folder manually.")
    info("To remove the browser extension: go to chrome://extensions and remove Yume.")
    pause()
