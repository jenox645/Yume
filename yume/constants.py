"""The CLI's shared constants: version, paths, platform, translation backends, download URLs.

Each is defined once, here; modules import what they use. (They used to be
re-defined in several modules, or pushed in by pocket_yume at startup.)
"""

from __future__ import annotations

import platform
from pathlib import Path

from config import DEFAULT_OLLAMA_PORT, DEFAULT_TRANSLATION_PORT

# Must match server/_state.py SERVER_VERSION and extension/manifest.json (tests check it)
VERSION = "0.2.0"

BASE_DIR = Path(__file__).parent.parent.resolve()
TOOLS_DIR = BASE_DIR / "tools"
SERVER_DIR = BASE_DIR / "server"
MODELS_DIR = BASE_DIR / "models"
GGUF_DIR = MODELS_DIR / "translation"
LOGS_DIR = BASE_DIR / "logs"
EXT_DIR = BASE_DIR / "extension"

PLAT = platform.system()
IS_WIN = PLAT == "Windows"
IS_MAC = PLAT == "Darwin"
IS_LIN = PLAT == "Linux"
ARCH = platform.machine().lower()
EXE = ".exe" if IS_WIN else ""
CREATE_NO_WINDOW = 0x08000000  # Windows process creation flag

KiB = 1024
MiB = 1024**2
GiB = 1024**3

# ── Translation backends ──────────────────────────────────────────────────────

HEALTH_PATH_OPENAI = "/v1/models"
HEALTH_PATH_OLLAMA = "/api/tags"
CHAT_PATH = "/v1/chat/completions"

DEFAULT_OLLAMA_MODEL = "qwen2.5:7b"

# name, desc, default host (dh) / port (dp), health path (hp), chat path (ap), install hint
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


def backend_info(name: str | None) -> dict:
    """A backend's entry; unknown names are treated as a custom OpenAI-compatible server."""
    return BACKEND_INFO.get(name or "llamacpp", BACKEND_INFO["custom"])


def use_backend(cfg: dict, name: str) -> dict:
    """Switch translation to a backend at its default address; returns its entry (the caller saves)."""
    bi = BACKEND_INFO[name]
    cfg["translation_backend"] = name
    cfg["translation_host"] = bi["dh"]
    cfg["translation_port"] = bi["dp"]
    return bi


# ── Download URLs (per platform) ──────────────────────────────────────────────

_ARM = "arm" in ARCH or "aarch" in ARCH
_GH = "https://github.com"

if IS_WIN:
    DOWNLOAD_URLS = {
        "yt-dlp": f"{_GH}/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe",
        "ffmpeg": f"{_GH}/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip",
        "deno": f"{_GH}/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip",
        "ollama": "https://ollama.com/download/OllamaSetup.exe",
    }
elif IS_MAC:
    DOWNLOAD_URLS = {
        "yt-dlp": f"{_GH}/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_macos",
        "ffmpeg": "https://www.osxexperts.net/ffmpeg7arm.zip" if _ARM else "https://evermeet.cx/ffmpeg/getrelease/zip",
        "deno": f"{_GH}/denoland/deno/releases/latest/download/deno-{'aarch64' if _ARM else 'x86_64'}-apple-darwin.zip",
        "ollama": "https://ollama.com/download/Ollama-darwin.zip",
    }
else:
    DOWNLOAD_URLS = {
        "yt-dlp": f"{_GH}/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_linux",
        "ffmpeg": f"{_GH}/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz",
        "deno": f"{_GH}/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip",
        "ollama": "https://ollama.com/install.sh",
    }
