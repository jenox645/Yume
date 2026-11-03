"""First-run setup wizard and uninstall."""

from __future__ import annotations

import importlib.util
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
    IS_MAC,
    IS_WIN,
    EXE,
    MiB,
    HEALTH_PATH_OLLAMA,
    BACKEND_INFO,
    DEFAULT_OLLAMA_MODEL,
    use_backend,
)
from yume.hardware import (
    CUDA_ONLY_COMPUTE,
    detect_gpu,
    detect_ram_gb,
    disk_free_gb,
    TRANSLATORS,
    WHISPER_DOWNLOAD_MB,
    WHISPER_MODELS,
    recommend_translator,
    recommend_whisper_model,
    vulkan_whisper_model,
    whisper_compute_type,
)
from yume.network import check_server
from yume.ui import C, _width, ask_arrow, ask_yn, error, header, info, pause, section, success, warn
from yume.utils import _run, _try_import, find_gguf_models, find_tool

_log = logging.getLogger("pocket_yume")


# ── Setup wizard ──────────────────────────────────────────────────────────────


_TOTAL_STEPS = 5


def _step(n: int, name: str) -> None:
    """A step's title, numbered like the menus: 2/5  What's missing."""
    print(f"\n  {C.GOLD}{n}/{_TOTAL_STEPS}{C.RESET}  {C.BOLD}{name}{C.RESET}\n")


def _row(name: str, what: str = "", state: str = "", tone: str = "") -> None:
    """One aligned line, as in the status screen: what it is, what it is for, its state.
    Padded by what shows on screen, so a coloured cell keeps the columns."""
    gap = " " * max(1, 36 - _width(what))
    print(f"  {name:<19}{what}{gap}{tone}{state}{C.RESET}".rstrip())


def _ready(name: str, what: str, ok: bool, missing: str = "missing") -> None:
    _row(name, what, "ready" if ok else missing, C.GREEN if ok else C.YELLOW)


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
    section("The browser extension")
    info(f"{C.DIM}This is where the subtitles appear. One minute in your browser:{C.RESET}")
    print()
    steps = (
        f"Open {C.CYAN}chrome://extensions{C.RESET} in Chrome, Edge or Brave",
        f"Turn on {C.BOLD}Developer mode{C.RESET}, top right",
        f"Click {C.BOLD}Load unpacked{C.RESET} and pick this folder:",
        "Pin the Yume icon",
    )
    for n, step in enumerate(steps, 1):
        info(f"{C.GOLD}{n}{C.RESET}  {step}")
        if n == 3:
            info(f"   {C.BOLD}{EXT_DIR}{C.RESET}")
    print()
    info(
        f"{C.DIM}Firefox: about:debugging, This Firefox, Load Temporary Add-on, then extension/manifest.json.{C.RESET}"
    )
    print()
    if ask_yn("Open the extension folder now, ready for step 3?", True):
        if not _open_folder(EXT_DIR):
            info(f"Could not open it: the folder is {EXT_DIR}")


def _offer_one_click() -> None:
    """Offer to let the extension start Yume by itself (native messaging)."""
    from yume import native_host

    section("Auto start")
    info(f"{C.DIM}Pressing Turn subtitles on in the browser starts Yume by itself, with no terminal,{C.RESET}")
    info(f"{C.DIM}and it stops after 30 minutes without a video.{C.RESET}")
    print()
    if native_host.registered_browsers():
        _ready("Auto start", "", True)
        return
    if ask_yn("Turn on auto start?", True):
        try:
            done = native_host.register()
            _row(
                "Auto start",
                ", ".join(done) or "no browser found",
                "on" if done else "off",
                C.GREEN if done else C.YELLOW,
            )
        except Exception as e:
            warn(f"Could not turn it on ({e}). Try again from Auto start in the main menu.")


def _offer_vocal_isolation() -> None:
    """Offer vocal isolation on NVIDIA machines (optional, ~3.5 GB)."""
    from yume import vocals

    if not detect_gpu().get("has_nvidia") or vocals.status()[0]:
        return
    section("Vocal isolation, optional")
    info(f"{C.DIM}Separates the singer from the music before Whisper listens: fewer mistakes on songs.{C.RESET}")
    info(f"{C.DIM}About {vocals.DOWNLOAD_GB:.0f} GB to download (PyTorch).{C.RESET}")
    print()
    if ask_yn("Install vocal isolation?", True):
        vocals.install()


_MISSING_LABELS = {
    "yt-dlp": "yt-dlp",
    "ffmpeg": "FFmpeg",
    "python_deps": "Whisper",
    "llama_server": "the translator",
    "whisper_cpp": "whisper.cpp",
    "gguf_model": "a translation model",
}


def _welcome() -> None:
    # Also reached via "Re-run Setup", so not "First-Time"
    header("Setup")
    info("Yume needs a few tools and two AI models. Setup looks at this PC, installs")
    info("what is missing and picks the models. Most of the time goes to downloads.")
    info(f"{C.DIM}Running it again later only adds what is missing.{C.RESET}")


def _python_row() -> None:
    import platform

    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    where = f"{version} on {platform.system()}"
    if sys.version_info >= (3, 13):
        _row("Python", where, "new: if an install fails, use 3.12", C.YELLOW)
    else:
        _row("Python", where)


def _show_hardware(gpu: dict, ram: float, disk: float) -> None:
    name = (gpu.get("name") or "").replace("NVIDIA GeForce ", "")
    card = f"{name}, {gpu['vram_mb'] / 1024:.0f} GB" if gpu.get("vram_mb") else name
    if gpu["has_nvidia"]:
        _row("Graphics", card, "Whisper and the translator run on it", C.GREEN)
    elif gpu.get("has_amd"):
        _row("Graphics", card, "both models run on it, through Vulkan", C.GREEN)
    else:
        _row("Graphics", "no supported card", "the processor does the work, more slowly", C.YELLOW)
    _row("Memory", f"{ram:.0f} GB")
    if disk >= 10:
        _row("Disk", f"{disk:.0f} GB free", "enough", C.GREEN)
    else:
        _row("Disk", f"{disk:.0f} GB free", "Yume needs about 8 GB", C.YELLOW)
    _python_row()


def _pick_whisper_model(cfg: dict, gpu: dict, rerun: bool = False) -> None:
    existing_model = cfg.get("whisper_model", "")
    rec_model, _reason = recommend_whisper_model(gpu)
    if existing_model and (os.path.sep in existing_model or "/" in existing_model):
        info(f"Custom Whisper model preserved: {C.BOLD}{Path(existing_model).name}{C.RESET}")
        info(f"{C.DIM}{existing_model}{C.RESET}")
    elif rerun and existing_model:
        _row("Whisper model", existing_model, "kept", C.DIM)
        if existing_model != rec_model:
            info(f"{C.DIM}Recommended here: {rec_model}. Change it in Settings.{C.RESET}")
    else:  # a first setup: the models screen shows it
        cfg["whisper_model"] = rec_model


def _pick_whisper_device(cfg: dict, gpu: dict, rerun: bool = False) -> None:
    # A saved choice stays while it can run: without NVIDIA that rules out the
    # GPU device and the GPU precisions, which is what older setups saved on AMD
    gpu_precision = cfg.get("whisper_compute_type") in CUDA_ONLY_COMPUTE
    if rerun and cfg.get("whisper_device") == "cpu" and gpu["has_nvidia"]:
        if gpu_precision:  # the CPU stays the user's choice
            cfg["whisper_compute_type"] = "int8"
        return
    if rerun and (gpu["has_nvidia"] or not (cfg.get("whisper_device") == "cuda" or gpu_precision)):
        return
    if gpu["has_nvidia"]:
        cfg["whisper_device"] = "cuda"
        cfg["whisper_compute_type"] = whisper_compute_type(gpu["vram_mb"])
    elif gpu.get("has_amd"):  # whisper.cpp when it is installed, the CPU until then
        cfg["whisper_device"] = "auto"
        cfg["whisper_compute_type"] = "auto"
    else:
        cfg["whisper_device"] = "cpu"
        cfg["whisper_compute_type"] = "int8"


def _scan_hardware(cfg: dict, rerun: bool = False) -> dict:
    """Step 1: show GPU, RAM and disk; pick the Whisper model, device and precision,
    or keep the ones chosen before when Setup runs again."""
    _step(1, "This PC")
    gpu = detect_gpu()
    ram = detect_ram_gb()
    disk = disk_free_gb()
    _show_hardware(gpu, ram, disk)
    _pick_whisper_model(cfg, gpu, rerun)
    _pick_whisper_device(cfg, gpu, rerun)
    return gpu


def _translation_engine_ok(gpu: dict) -> bool:
    """Report the llama.cpp translation engine; False when it must be installed."""
    from yume import llama_server

    if llama_server.server_path():
        _ready("Translator", "runs the translation model", True)
        return True
    # llama.cpp's prebuilt server is the engine Yume installs. An existing
    # llama-cpp-python is kept only when it already runs on the GPU (or
    # there is no GPU to use): CUDA wheels lag behind new Pythons, and the
    # source build needs the CUDA Toolkit and a C++ compiler.
    on_gpu = llama_server.python_engine_on_gpu()
    if on_gpu is None or not _try_import("uvicorn") or not _try_import("fastapi"):
        _ready("Translator", "runs the translation model", False)
        return False
    if on_gpu or not (gpu.get("has_nvidia") or gpu.get("has_amd")):
        _ready("Translator", "llama-cpp-python", True)
        return True
    _row("Translator", "llama-cpp-python", "on the processor only: slow", C.YELLOW)
    return False


def _check_components(cfg: dict, gpu: dict) -> tuple[list[str], bool]:
    """Step 2: report what is installed.

    Returns the missing components (keys of _MISSING_LABELS) and whether a
    translation model or a running backend is already there.
    """
    _step(2, "What's missing")
    missing = _missing_tools()
    missing += _missing_for_amd(gpu)
    gf = find_gguf_models()
    bk = cfg.get("translation_backend", "llamacpp")
    if bk != "llamacpp":
        # Ollama / LM Studio / custom manage their own models
        address = f"{cfg.get('translation_host', '127.0.0.1')}:{cfg.get('translation_port', '')}"
        where = f"{BACKEND_INFO.get(bk, {}).get('name', bk)} at {address}"
        _row("Translator", where, "yours to run", C.DIM)
    else:
        missing += _missing_for_llamacpp(cfg, gpu, gf)

    detected = False
    for k, bi in BACKEND_INFO.items():
        if k != "custom" and check_server(bi["dh"], bi["dp"], bi["hp"])["up"]:
            detected = True
            _row(bi["name"], f"running on port {bi['dp']}", "found", C.GREEN)
    return missing, detected or bool(gf)


def _missing_for_amd(gpu: dict) -> list[str]:
    """whisper.cpp is how an AMD card hears; without it Whisper stays on the CPU."""
    from yume import whisper_cpp

    if not gpu.get("has_amd") or not whisper_cpp.available():
        return []
    ready = whisper_cpp.server_path() is not None
    _ready("whisper.cpp", "Whisper on your AMD card", ready, "missing: Whisper would use the CPU")
    return [] if ready else ["whisper_cpp"]


def _missing_tools() -> list[str]:
    """Report yt-dlp, FFmpeg, Deno and faster-whisper; return what is missing."""
    yt, ff, dn = find_tool("yt-dlp"), find_tool("ffmpeg"), find_tool("deno")
    _ready("yt-dlp", "downloads the audio", bool(yt))
    _ready("FFmpeg", "reads the audio", bool(ff))
    _row("Deno", "optional, for YouTube", "ready" if dn else "not installed", C.GREEN if dn else C.DIM)
    whisper = _try_import("faster_whisper")
    _ready("Whisper", "hears the video", whisper)
    return [k for k, ok in (("yt-dlp", yt), ("ffmpeg", ff), ("python_deps", whisper)) if not ok]


def _missing_for_llamacpp(cfg: dict, gpu: dict, gf: list) -> list[str]:
    """Report llama.cpp and the GGUF models; pick a model if none is set."""
    missing = [] if _translation_engine_ok(gpu) else ["llama_server"]
    if not gf:
        _ready("Translation model", "", False)
        return [*missing, "gguf_model"]
    _row("Translation model", f"{len(gf)} in models/translation", "ready", C.GREEN)
    # Keep the user's choice on a re-run; only fill in a missing/stale path
    if not cfg.get("gguf_model_path") or not Path(cfg["gguf_model_path"]).exists():
        cfg["gguf_model_path"] = str(gf[0])
        cfg["translation_model"] = gf[0].name
    return missing


def _choose_youtube_auth(cfg: dict) -> None:
    """Step 3: browser cookies or Deno, so YouTube lets yt-dlp download."""
    from yume.installers import install_deno

    _step(3, "YouTube")
    info("YouTube refuses downloads that look like a robot's. Yume can show it is you")
    info("with your browser's YouTube login, or run a small helper instead.")
    ch = ask_arrow(
        "How should Yume download from YouTube?",
        [
            ("Use my browser's login (recommended)", "Stay logged into YouTube in that browser"),
            ("Use the Deno helper", "No account needed, about 35 MB"),
            ("Not now", "Set it up later in Settings"),
        ],
        default=0,
        allow_back=False,
    )
    if ch == 0:
        cfg["youtube_auth_method"] = "cookies"
        browsers = ["chrome", "firefox", "edge", "brave", "safari"]
        info(f"{C.DIM}Only for downloading: the extension works in any browser.{C.RESET}")
        bc = ask_arrow(
            "Which browser are you logged into YouTube with?",
            [(b.capitalize(), None) for b in browsers],
            default=0,
            allow_back=False,
        )
        cfg["cookies_browser"] = browsers[bc]
        _row("YouTube", f"your {browsers[bc].capitalize()} login", "ready", C.GREEN)
    elif ch == 1:
        hint = f"Download Deno manually: {C.CYAN}https://deno.land{C.RESET}"
        if _install_with_retry("Deno", install_deno, hint):
            cfg["youtube_auth_method"] = "deno"
        else:  # the server would only switch back to cookies at every start
            warn(f"Keeping {cfg.get('youtube_auth_method', 'cookies')} for YouTube: change it in Settings.")


_CPU_MODELS = ("tiny", "base", "small")


def _install_whisper_cpp(cfg: dict) -> bool:
    """Saying yes to the GPU engine ends a saved CPU choice, which is what Setup wrote on AMD
    before, and a model picked for the CPU: the card can carry the recommended one."""
    from yume import whisper_cpp

    model = cfg.get("whisper_model", "")
    if whisper_cpp.model_path(model) and model in _CPU_MODELS:
        model = vulkan_whisper_model(detect_gpu().get("vram_mb", 0))[0]
    if not whisper_cpp.install(model):
        return False
    if whisper_cpp.model_path(model):
        cfg["whisper_model"] = model
    if cfg.get("whisper_device") == "cpu":
        cfg["whisper_device"], cfg["whisper_compute_type"] = "auto", "auto"
    return True


def _install_missing(cfg: dict, missing: list[str], ask_each: bool) -> None:
    """Step 4: install the missing tools and packages (all, or one by one)."""
    from yume import llama_server, whisper_cpp
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
        "whisper_cpp": (
            "Install whisper.cpp, so Whisper runs on your AMD GPU?",
            "whisper.cpp",
            lambda: _install_whisper_cpp(cfg),
            f"Unzip the build from {C.CYAN}{whisper_cpp.RELEASE_PAGE}{C.RESET} into {whisper_cpp.WHISPER_DIR}",
        ),
    }
    _step(4, "Installing")
    for key, (question, label, install_fn, hint) in installs.items():
        if key in missing and (not ask_each or ask_yn(question)):
            _install_with_retry(label, install_fn, hint)


def _use_ollama(cfg: dict) -> None:
    """Switch translation to Ollama; offer to install it and pull a model."""

    from yume.installers import install_ollama, pull_ollama_model

    use_backend(cfg, "ollama")
    cfg["translation_model"] = DEFAULT_OLLAMA_MODEL
    if not ask_yn("Install Ollama?"):
        return
    _install_with_retry("Ollama", install_ollama, f"Install manually: {C.CYAN}https://ollama.com{C.RESET}")
    time.sleep(2)
    if ask_yn(f"Download model ({cfg['translation_model']})?"):
        pull_ollama_model(cfg["translation_model"])  # starts Ollama if needed


def _present_models(cfg: dict, gpu: dict) -> bool:
    """A first setup: the two models for this PC, in plain words. True when the user takes
    them; otherwise they pick Whisper here and the translator in step 5."""
    whisper = cfg["whisper_model"]
    translator, why = recommend_translator(gpu)
    pick = TRANSLATORS[translator]
    whisper_mb = WHISPER_DOWNLOAD_MB.get(whisper, 0)
    section("Your models")
    info(f"{C.DIM}Two AI models do the work: one hears the video, one translates what it hears.{C.RESET}")
    print()
    hears = next((desc for name, _p, _v, desc, _mb in WHISPER_MODELS if name == whisper), "")
    _row("Hears", f"{C.BOLD}{whisper:<20}{C.RESET}", hears, C.DIM)
    _row("Translates", f"{C.BOLD}{translator:<20}{C.RESET}", why, C.DIM)
    print()
    info(f"About {(whisper_mb + pick['mb']) / 1024:.1f} GB to download. You can change either later, in Settings.")
    ch = ask_arrow(
        "Use these models?",
        [
            ("Use these (recommended)", "Picked for this PC"),
            ("Choose myself", "See every model and what it needs"),
        ],
        default=0,
        allow_back=False,
    )
    if ch == 0:
        return True
    options = [
        (name + ("  ★ recommended" if name == whisper else ""), f"{desc} — about {mb} MB")
        for name, _p, _v, desc, mb in WHISPER_MODELS
    ]
    names = [m[0] for m in WHISPER_MODELS]
    chosen = ask_arrow(
        "Which model should hear the video?",
        options,
        default=names.index(whisper) if whisper in names else 0,
        allow_back=False,
    )
    if 0 <= chosen < len(names):
        cfg["whisper_model"] = names[chosen]
    return False


def _download_translator(cfg: dict, key: str) -> bool:
    from yume.network import hf_download

    pick = TRANSLATORS[key]
    if not hf_download(pick["repo"], pick["file"], pick["sha256"]):
        return False
    cfg["gguf_model_path"] = str(GGUF_DIR / pick["file"])
    cfg["translation_model"] = pick["file"]
    _row("Translation model", key, "ready", C.GREEN)
    return True


def _fetch_whisper(cfg: dict) -> None:
    """Download the Whisper model now, with a progress bar: otherwise the first start
    downloads it silently and looks stuck for minutes."""
    from yume.servers import resolve_whisper_device

    name = cfg.get("whisper_model", "")
    if name not in WHISPER_DOWNLOAD_MB or resolve_whisper_device(cfg)[0] == "vulkan":
        return  # a custom model is on disk already; whisper.cpp got its own model at install
    try:
        from faster_whisper import download_model
    except ImportError:
        return
    info(f"Downloading the Whisper model {name} (about {WHISPER_DOWNLOAD_MB[name]} MB)...")
    try:
        download_model(name)
        _row("Whisper model", name, "ready", C.GREEN)
    except Exception as e:
        warn(f"The Whisper model will download when Yume first starts instead ({e}).")


def _choose_translation_model(cfg: dict, needs_model: bool, recommended: bool = False) -> None:
    """Step 5: the Whisper model, then a GGUF translator: the recommended one, from
    HuggingFace, or Ollama instead."""
    from yume.menus import browse_hf

    _step(5, "Models")
    _fetch_whisper(cfg)
    if not needs_model:
        gf = find_gguf_models()
        gf_name = gf[0].name if gf else cfg.get("translation_model", "configured")
        _row("Translation model", Path(cfg.get("gguf_model_path") or gf_name).name, "ready", C.GREEN)
        return
    if recommended and cfg.get("translation_backend", "llamacpp") == "llamacpp":
        if _download_translator(cfg, recommend_translator(detect_gpu())[0]):
            return
        warn("The download did not finish. Pick a translator below, or try again later from Settings.")
    info("Pick the model that translates the subtitles. It runs on this PC.")
    ch = ask_arrow(
        "How to get a model?",
        [
            ("Pick from a list", "Models chosen for Yume, with their sizes"),
            ("Use Ollama instead", "If Ollama already runs here"),
            ("Not now", "Add a .gguf file to models/translation/ later"),
        ],
        default=0,
        allow_back=False,
    )
    if ch == 0:
        browse_hf(cfg)
    elif ch == 1:
        _use_ollama(cfg)


def _print_summary(cfg: dict) -> bool:
    """What is installed now, per component the chosen backend needs. True when nothing is missing."""
    from yume import llama_server

    section("Installed")
    bk = cfg.get("translation_backend", "llamacpp")
    checks = [
        ("yt-dlp", "downloads the audio", find_tool("yt-dlp") is not None),
        ("FFmpeg", "reads the audio", find_tool("ffmpeg") is not None),
        ("Whisper", cfg.get("whisper_model", ""), _try_import("faster_whisper")),
    ]
    if bk == "llamacpp":
        engine = llama_server.server_path() is not None or _try_import("llama_cpp")
        checks.append(("Translator", "runs the translation model", engine))
        model = Path(cfg.get("gguf_model_path") or "").name
        checks.append(("Translation model", model, len(find_gguf_models()) > 0))
    elif bk == "ollama":
        up = check_server("127.0.0.1", cfg.get("translation_port", 11434), HEALTH_PATH_OLLAMA)["up"]
        checks.append(("Ollama", cfg.get("translation_model", ""), up))
    print()
    for name, what, ok in checks:
        _ready(name, what, ok)
    missing = [name for name, _what, ok in checks if not ok]
    print()
    if not missing:
        info(f"{C.BOLD}Everything is installed.{C.RESET}")
    else:
        warn(f"Still missing: {', '.join(missing)}.")
        info(f"{C.DIM}python pocket_yume.py health shows why; Tools in the main menu installs them.{C.RESET}")
    return not missing


def _mark_complete(cfg: dict) -> None:
    from config import save_config

    cfg["first_run_complete"] = True
    save_config(cfg)


def _closing_offers(complete: bool = True) -> None:
    """Auto start, vocal isolation and the extension guide, then what to do next."""
    _offer_one_click()
    _offer_vocal_isolation()
    _extension_guide()
    if complete:
        print(f"\n  {C.BOLD}Ready.{C.RESET} Open a video, click the Yume icon and press Turn subtitles on.")
        info(f"{C.DIM}The first start takes about 30 seconds while the models load.{C.RESET}")
    else:
        print(f"\n  {C.BOLD}Almost ready.{C.RESET} Install what is missing first: Tools, in the main menu.")
    pause()


def setup_wizard(cfg: dict, rerun: bool = False) -> dict:
    """Scan, check, then (only if something is missing) authenticate, install and pick a model."""
    from config import config_export, save_config

    _welcome()
    gpu = _scan_hardware(cfg, rerun)
    recommended = not rerun and _present_models(cfg, gpu)
    pause()
    missing, ready = _check_components(cfg, gpu)

    if not missing and ready:
        print(f"\n  {C.BOLD}Nothing to install.{C.RESET}")
        _mark_complete(cfg)
        _closing_offers()
        return cfg

    if missing:
        labels = ", ".join(_MISSING_LABELS.get(m, m) for m in missing)
        _choose_youtube_auth(cfg)
        save_config(cfg)

        print(f"\n  To install: {C.BOLD}{labels}{C.RESET}")
        mode = ask_arrow(
            "",
            [
                ("Install all (recommended)", "Everything listed as missing"),
                ("Ask about each one", "A yes or no for every item"),
                ("Not now", "Install later from Tools in the main menu"),
            ],
            default=0,
            allow_back=False,
        )
        if mode == 2:
            _mark_complete(cfg)
            pause()
            return cfg
        _install_missing(cfg, missing, ask_each=mode == 1)
        _choose_translation_model(cfg, "gguf_model" in missing, recommended)
        save_config(cfg)

    _mark_complete(cfg)
    try:
        config_export(cfg, BASE_DIR / "yume_config_post_setup.json")
    except Exception as e:
        _log.debug("[setup_wizard] config-backup failed: %s", e)
    _closing_offers(_print_summary(cfg))
    return cfg


# ── Uninstall ─────────────────────────────────────────────────────────────────


# Other programs use these too: the uninstaller leaves them installed
_SHARED_LIBS = {"numpy", "av", "yt-dlp"}
_TORCH_PACKAGES = ["torch", "torchaudio"]


def _yume_pip_packages() -> list[str]:
    """Exactly what Yume installs: the server's requirements, the llama-cpp-python
    engine and its server, the YouTube PO-token plugin, and demucs for vocal isolation."""
    from yume import vocals
    from yume.servers import LLAMA_SERVER_DEPS
    from yume.utils import requirement_name, server_requirements

    specs = [*server_requirements(), "llama-cpp-python", *LLAMA_SERVER_DEPS, "bgutil-ytdlp-pot-provider", vocals.DEMUCS]
    names = (requirement_name(s) for s in specs)
    return list(dict.fromkeys(n for n in names if n not in _SHARED_LIBS))


# The cached repos of the models Yume can load, as faster_whisper.utils names them.
# The cache is shared and does not say who downloaded what.
_WHISPER_REPOS = (
    *(
        f"models--Systran--faster-whisper-{m}"
        for m in ("tiny", "base", "small", "medium", "large-v1", "large-v2", "large-v3")
    ),
    "models--mobiuslabsgmbh--faster-whisper-large-v3-turbo",
)
_HTDEMUCS_WEIGHTS = "955717e8-8726e21a.th"


def _uninstall_targets() -> list[tuple[str, Path]]:
    """Yume's own folders, inside its install."""
    from config import CONFIG_DIR

    return [
        ("Tools (yt-dlp, ffmpeg, deno, whisper.cpp)", TOOLS_DIR),
        ("Translation models (downloaded AI model files)", GGUF_DIR),
        ("Whisper models for AMD GPUs", MODELS_DIR / "whisper"),
        ("Configuration, blacklist, saved subtitles", CONFIG_DIR),
        ("Logs", LOGS_DIR),
    ]


def _deno_cache() -> Path:
    if os.environ.get("DENO_DIR"):
        return Path(os.environ["DENO_DIR"])
    if IS_WIN:
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "deno"
    return Path.home() / ("Library/Caches/deno" if IS_MAC else ".cache/deno")


def _shared_targets() -> list[tuple[str, Path]]:
    """What Yume used outside its install and other programs may use too: asked one by one."""
    from yume.utils import hf_hub_dir

    cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    torch_hub = Path(os.environ["TORCH_HOME"]) / "hub" if os.environ.get("TORCH_HOME") else cache / "torch" / "hub"
    whisper = [(f"Whisper model {r.split('faster-whisper-')[-1]}", hf_hub_dir() / r) for r in _WHISPER_REPOS]
    return [
        *whisper,
        ("Demucs model (vocal isolation)", torch_hub / "checkpoints" / _HTDEMUCS_WEIGHTS),
        ("yt-dlp cache", cache / "yt-dlp"),
        ("Deno cache", _deno_cache()),
    ]


def _ask_shared(targets: list[tuple[str, Path]]) -> list[tuple[str, Path]]:
    found = [(label, p) for label, p in targets if p.exists()]
    if found:
        info("Other programs may use these too, so they stay unless you say otherwise:")
    return [t for t in found if ask_yn(f"Remove {t[0]} ({_folder_mb(t[1]):.0f} MB)?", False)]


def _in_own_venv() -> bool:
    """A venv inside Yume's folder goes with the folder, PyTorch included."""
    return sys.prefix != sys.base_prefix and Path(sys.prefix).resolve().is_relative_to(BASE_DIR.resolve())


def _ask_torch() -> list[str]:
    if _in_own_venv() or importlib.util.find_spec("torch") is None:
        return []
    return _TORCH_PACKAGES if ask_yn("Remove PyTorch (~3 GB)? Other programs may use it.", False) else []


def _folder_mb(d: Path) -> float:
    if d.is_file():
        return d.stat().st_size / MiB
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
                d.unlink() if d.is_file() else shutil.rmtree(d)
                success(f"Removed: {label}")
            except Exception as e:
                error(f"Failed to remove {label}: {e}")
    shutil.rmtree(MODELS_DIR, ignore_errors=True)


def _remove_packages(packages: list[str]) -> None:
    info("Uninstalling pip packages...")
    for pkg in packages:
        try:
            # PyTorch is thousands of files
            if _run([sys.executable, "-m", "pip", "uninstall", "-y", pkg], timeout=300).returncode == 0:
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
        dirs += _ask_shared(_shared_targets())
    if ch in (0, 2):
        packages += _ask_torch()
    if ch in (0, 1):
        _remove_data(dirs)
    if ch in (0, 2):
        _remove_packages(packages)

    print()
    success("Uninstall complete!")
    info("Two things Yume can't do from here:")
    info("  1. Remove Yume from your browser's extensions page.")
    info(f"  2. Delete this folder, which also holds any Python environment made for Yume: {C.CYAN}{BASE_DIR}{C.RESET}")
    info(f"{C.DIM}pip keeps its downloads, PyTorch included, for all your programs: python -m pip cache purge{C.RESET}")
    pause()
