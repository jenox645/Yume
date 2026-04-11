#!/usr/bin/env python3
"""
Pocket Yume CLI v0.0.8 -- Cross-platform installer & launcher for Yume AI Subtitles
Complete rewrite: smart port management, API token auth, Windows cp1252 fix
Supports: Windows, Linux, macOS
"""

import logging
import os
import platform
import sys
import threading
import traceback
from pathlib import Path

# Python version check — give a clear message instead of a cryptic TypeError
if sys.version_info < (3, 10):
    print("\n  Yume requires Python 3.10 or newer.")
    print(f"  You are running Python {sys.version.split()[0]}")
    print("  Download the latest Python from: https://www.python.org/downloads/")
    print("  On Windows, make sure to check 'Add Python to PATH' during installation.\n")
    sys.exit(1)

_log = logging.getLogger("pocket_yume")
_update_result = None  # Background update check result: [latest, url, checked] or None

# ── Config module ─────────────────────────────────────────────────────────────

from config import (  # noqa: E402
    CONFIG_DIR,
    DEFAULT_OLLAMA_PORT,
    DEFAULT_TRANSLATION_PORT,
    config_export,
    config_import,
    load_config,
    save_config,
)

# ── Version & constants ───────────────────────────────────────────────────────

# NOTE: We do NOT wrap sys.stdout here. All print output in this file is
# pure ASCII + ANSI escape codes, which works on any Windows codepage.
# The UTF-8 wrapper is only applied in faster_whisper_server.py where
# Japanese text may appear in logs. Wrapping stdout here breaks ANSI
# color rendering on Windows terminals.

VERSION = "0.0.8"

KiB = 1024
MiB = 1024**2
GiB = 1024**3

BASE_DIR = Path(__file__).parent.resolve()
TOOLS_DIR = BASE_DIR / "tools"
SERVER_DIR = BASE_DIR / "server"
MODELS_DIR = BASE_DIR / "models"
GGUF_DIR = BASE_DIR / "models" / "translation"
LOGS_DIR = BASE_DIR / "logs"
EXT_DIR = BASE_DIR / "extension"

PLAT = platform.system()
IS_WIN = PLAT == "Windows"
IS_MAC = PLAT == "Darwin"
EXE = ".exe" if IS_WIN else ""
ARCH = platform.machine().lower()

# ── Download URLs ─────────────────────────────────────────────────────────────


def _get_download_urls() -> dict:
    urls: dict = {}

    if IS_WIN:
        urls["yt-dlp"] = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe"
    elif IS_MAC:
        urls["yt-dlp"] = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_macos"
    else:
        urls["yt-dlp"] = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_linux"

    if IS_WIN:
        urls["ffmpeg"] = (
            "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"
        )
    elif IS_MAC:
        if "arm" in ARCH or "aarch" in ARCH:
            urls["ffmpeg"] = "https://www.osxexperts.net/ffmpeg7arm.zip"
        else:
            urls["ffmpeg"] = "https://evermeet.cx/ffmpeg/getrelease/zip"
    else:
        urls["ffmpeg"] = (
            "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz"
        )

    if IS_WIN:
        urls["deno"] = "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip"
    elif IS_MAC:
        if "arm" in ARCH or "aarch" in ARCH:
            urls["deno"] = "https://github.com/denoland/deno/releases/latest/download/deno-aarch64-apple-darwin.zip"
        else:
            urls["deno"] = "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-apple-darwin.zip"
    else:
        urls["deno"] = "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip"

    if IS_WIN:
        urls["ollama"] = "https://ollama.com/download/OllamaSetup.exe"
    elif IS_MAC:
        urls["ollama"] = "https://ollama.com/download/Ollama-darwin.zip"
    else:
        urls["ollama"] = "https://ollama.com/install.sh"

    return urls


DOWNLOAD_URLS = _get_download_urls()

# ── API endpoint constants ────────────────────────────────────────────────────

HEALTH_PATH_OPENAI = "/v1/models"
HEALTH_PATH_OLLAMA = "/api/tags"
CHAT_PATH = "/v1/chat/completions"

# ── Backend definitions ───────────────────────────────────────────────────────

BACKEND_INFO = {
    "llamacpp": {
        "name": "llama.cpp (built-in)",
        "desc": "DEFAULT. Drop a .gguf in models/translation/, Yume loads it directly.",
        "dh": "127.0.0.1",
        "dp": DEFAULT_TRANSLATION_PORT,
        "hp": HEALTH_PATH_OPENAI,
        "ap": CHAT_PATH,
        "inst": (
            "pip install llama-cpp-python\n"
            '  GPU (CUDA):  CMAKE_ARGS="-DGGML_CUDA=on" pip install llama-cpp-python\n'
            "  Win prebuilt: pip install llama-cpp-python --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cu124"
        ),
    },
    "ollama": {
        "name": "Ollama",
        "desc": "One-click install, auto GPU, runs as service.",
        "dh": "127.0.0.1",
        "dp": DEFAULT_OLLAMA_PORT,
        "hp": HEALTH_PATH_OLLAMA,
        "ap": CHAT_PATH,
        "inst": "https://ollama.com -- or auto-install via Pocket Yume",
    },
    "lmstudio": {
        "name": "LM Studio",
        "desc": "GUI app with model browser.",
        "dh": "127.0.0.1",
        "dp": 1234,
        "hp": HEALTH_PATH_OPENAI,
        "ap": CHAT_PATH,
        "inst": "Download from: https://lmstudio.ai",
    },
    "textgenwebui": {
        "name": "text-generation-webui",
        "desc": "Feature-rich web UI by oobabooga.",
        "dh": "127.0.0.1",
        "dp": DEFAULT_TRANSLATION_PORT,
        "hp": HEALTH_PATH_OPENAI,
        "ap": CHAT_PATH,
        "inst": "https://github.com/oobabooga/text-generation-webui",
    },
    "custom": {
        "name": "Custom (OpenAI-compatible)",
        "desc": "Any server with /v1/chat/completions endpoint.",
        "dh": "127.0.0.1",
        "dp": DEFAULT_TRANSLATION_PORT,
        "hp": HEALTH_PATH_OPENAI,
        "ap": CHAT_PATH,
        "inst": "Provide your own endpoint.",
    },
}

# ── Yume sub-package imports ──────────────────────────────────────────────────

from yume.benchmark import benchmark_whisper  # noqa: E402
from yume.hardware import _detect_cpu_name, detect_gpu  # noqa: E402
from yume.health import detect_fonts, health_check, set_backend_info as _health_set_bi, show_status  # noqa: E402
from yume.installers import set_download_urls  # noqa: E402
from yume.launch import launch_services, set_launch_context  # noqa: E402
from yume.menus import (  # noqa: E402
    cli_blacklist,
    cli_model,
    cli_server_stats,
    set_backend_info as _menus_set_bi,
    settings_menu,
    tools_menu,
)
from yume.network import set_version as _network_set_version  # noqa: E402
from yume.ports import show_ports_status  # noqa: E402
from yume.setup import set_setup_context, setup_wizard, uninstall_yume  # noqa: E402
from yume.ui import C, enable_ansi, error, header, info, panel, pause  # noqa: E402
from yume.utils import check_for_updates  # noqa: E402

# ── Wire up injected context ──────────────────────────────────────────────────


def _init_modules() -> None:
    """Inject shared state (BACKEND_INFO, URLs, VERSION) into all submodules."""
    _menus_set_bi(BACKEND_INFO)
    _health_set_bi(BACKEND_INFO)
    _network_set_version(VERSION)
    set_launch_context(BACKEND_INFO, VERSION)
    set_setup_context(BACKEND_INFO, MODELS_DIR, GGUF_DIR, VERSION)
    set_download_urls(DOWNLOAD_URLS)


_init_modules()

# ── Main menu ─────────────────────────────────────────────────────────────────


def main_menu() -> None:
    cfg = load_config()
    if not cfg.get("first_run_complete"):
        cfg = setup_wizard(cfg)

    while True:
        header()
        gpu = detect_gpu()
        if gpu["has_nvidia"]:
            gs = f"{C.GREEN}{gpu['name']}{C.RESET}"
        elif gpu["has_amd"]:
            gs = f"{C.RED}{gpu['name']} (AMD){C.RESET}"
        else:
            cpu_name = _detect_cpu_name()
            if "intel" in cpu_name.lower():
                gs = f"{C.BLUE}CPU: {cpu_name}{C.RESET}"
            elif "amd" in cpu_name.lower():
                gs = f"{C.RED}CPU: {cpu_name}{C.RESET}"
            else:
                gs = f"{C.YELLOW}CPU: {cpu_name}{C.RESET}"

        bn = BACKEND_INFO.get(cfg.get("translation_backend", "llamacpp"), {}).get("name", "?")
        panel(
            f"{C.DIM}Hardware   {C.RESET}  {gs}\n"
            f"{C.DIM}Speech AI  {C.RESET}  {C.CYAN}{cfg['whisper_model']}{C.RESET}  on  {cfg['whisper_host']}:{cfg['whisper_port']}\n"
            f"{C.DIM}Translator {C.RESET}  {C.MAGENTA}{bn}{C.RESET}  on  {cfg['translation_host']}:{cfg['translation_port']}",
            style=C.DIM,
        )

        # Background update check — fires once, result shown on next render
        global _update_result
        if _update_result is None:
            _update_result = [None, None, False]

            def _bg_check() -> None:
                global _update_result
                try:
                    latest, url = check_for_updates(VERSION)
                    _update_result = [latest, url, True]
                except Exception:
                    _update_result = [None, None, True]

            threading.Thread(target=_bg_check, daemon=True).start()
        if _update_result[2] and _update_result[0]:
            print(
                f"  {C.YELLOW}⬆{C.RESET}  Update available: v{_update_result[0]}  {C.DIM}{_update_result[1]}{C.RESET}"
            )

        from yume.ui import ask_choice

        ch = ask_choice(
            "What would you like to do?",
            [
                ("Launch Yume", "Start servers + runtime menu"),
                ("System Status", "Hardware, tools, packages, ports"),
                ("Health Check", "Test every component end-to-end"),
                ("Settings", "Whisper, translation, addresses, subtitles"),
                ("Tools & Fonts", "Install, update, manage components, detect fonts"),
                ("Re-run Setup", "Guided first-time configuration"),
                ("Uninstall", "Remove Yume and all data"),
                ("Exit", None),
            ],
            default=0,
            allow_back=False,
        )

        if ch == 0:
            launch_services(cfg)
        elif ch == 1:
            show_status(cfg)
        elif ch == 2:
            health_check(cfg)
        elif ch == 3:
            settings_menu(cfg)
        elif ch == 4:
            tools_menu(cfg)
        elif ch == 5:
            cfg["first_run_complete"] = False
            save_config(cfg)
            cfg = setup_wizard(cfg)
        elif ch == 6:
            uninstall_yume()
        elif ch == 7:
            print(f"\n  {C.GOLD}Goodbye!{C.RESET}\n")
            break


# ── Entry point ───────────────────────────────────────────────────────────────


def main() -> None:
    verbose = "--verbose" in sys.argv or "-v" in sys.argv
    env_level = os.environ.get("LOG_LEVEL", "").upper()
    log_level = (
        logging.DEBUG
        if (verbose or env_level == "DEBUG")
        else max(getattr(logging, env_level, logging.WARNING), logging.DEBUG)
    )
    logging.basicConfig(level=log_level, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s", stream=sys.stderr)
    sys.argv = [a for a in sys.argv if a not in ("--verbose", "-v")]

    if "--no-color" in sys.argv or os.environ.get("NO_COLOR"):
        C.disable()
        sys.argv = [a for a in sys.argv if a != "--no-color"]
    else:
        enable_ansi()

    if "--version" in sys.argv:
        print(f"Yume v{VERSION} (Pocket Yume CLI)")
        return

    for d in [TOOLS_DIR, SERVER_DIR, CONFIG_DIR, MODELS_DIR, GGUF_DIR, LOGS_DIR]:
        d.mkdir(parents=True, exist_ok=True)

    if len(sys.argv) > 1:
        cmd = sys.argv[1].lower()
        cfg = load_config()

        if cmd == "launch":
            launch_services(cfg)
        elif cmd == "status":
            show_status(cfg)
        elif cmd == "health":
            health_check(cfg)
        elif cmd == "stats":
            cli_server_stats(cfg)
        elif cmd == "ports":
            show_ports_status(cfg)
        elif cmd == "blacklist":
            cli_blacklist(cfg, sys.argv[2:])
        elif cmd == "model":
            cli_model(cfg, sys.argv[2:])
        elif cmd == "export":
            config_export(cfg, sys.argv[2] if len(sys.argv) > 2 else None)
        elif cmd == "import" and len(sys.argv) > 2:
            imported = config_import(sys.argv[2])
            if imported:
                cfg.update(imported)
        elif cmd == "recommend":
            from yume.hardware import recommend_whisper_model

            model, reason = recommend_whisper_model()
            info(f"Recommended: {model}")
            info(f"Reason: {reason}")
        elif cmd == "fonts":
            detect_fonts()
        elif cmd == "benchmark":
            benchmark_whisper(cfg)
        elif cmd == "setup":
            cfg["first_run_complete"] = False
            save_config(cfg)
            setup_wizard(cfg)
        elif cmd == "help":
            print(f"\n  {C.BOLD}Pocket Yume v{VERSION}{C.RESET}")
            print("  Cross-platform installer for Yume AI Subtitles\n")
            print(f"  {C.BOLD}Usage:{C.RESET} python pocket_yume.py [command]\n")
            print(f"  {C.GOLD}Commands:{C.RESET}")
            print("    (none)              Interactive menu")
            print("    launch              Start servers (interactive runtime menu)")
            print("    status              Check components")
            print("    health              Run full health check")
            print("    stats               Live server statistics (GPU, session)")
            print("    model               Show current Whisper model")
            print("    model list          List available models with VRAM")
            print("    model switch <n>    Hot-swap Whisper model")
            print("    blacklist list      Show server blacklist")
            print("    blacklist add <t>   Block a phrase")
            print("    blacklist remove <t> Unblock a phrase")
            print("    blacklist clear     Clear all entries")
            print("    ports               Show port availability")
            print("    setup               Run setup wizard")
            print("    export [path]       Export config to backup file")
            print("    import <path>       Import config from backup file")
            print("    recommend           Suggest best Whisper model for your hardware")
            print("    fonts               Detect installed CJK and system fonts")
            print("    benchmark           Test Whisper model speeds on your hardware")
            print("    help                This message")
            print("    --verbose / -v      Enable debug logging to stderr")
            print("    --version           Print version and exit")
            print("    --no-color          Disable colored output\n")
        else:
            print(f"  Unknown: {cmd}. Try: python pocket_yume.py help")
        return

    try:
        main_menu()
    except KeyboardInterrupt:
        print(f"\n\n  {C.GOLD}Goodbye!{C.RESET}\n")
    except Exception as e:
        error(f"Error: {e}")
        traceback.print_exc()
        pause()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n\n  {C.GOLD}Goodbye!{C.RESET}\n")
        sys.exit(0)
