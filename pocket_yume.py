#!/usr/bin/env python3
"""
Pocket Yume CLI v0.0.8 -- Cross-platform installer & launcher for Yume AI Subtitles
Complete rewrite: smart port management, API token auth, Windows cp1252 fix
Supports: Windows, Linux, macOS
"""

import json
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import traceback
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
import socket as _socket

# Python version check — give a clear message instead of a cryptic TypeError
if sys.version_info < (3, 10):
    print("\n  Yume requires Python 3.10 or newer.")
    print(f"  You are running Python {sys.version.split()[0]}")
    print("  Download the latest Python from: https://www.python.org/downloads/")
    print("  On Windows, make sure to check 'Add Python to PATH' during installation.\n")
    sys.exit(1)

_log = logging.getLogger('pocket_yume')
_update_result = None  # Background update check result: [latest, url, checked] or None

# Import extracted config module
from config import DEFAULT_CONFIG, CONFIG_FILE, CONFIG_DIR, load_config, save_config
from config import validate_port, validate_host, config_export, config_import
from config import DEFAULT_WHISPER_PORT, DEFAULT_TRANSLATION_PORT, DEFAULT_OLLAMA_PORT

_api_token = None

def _run(cmd, timeout=30, **kw):
    """subprocess.run wrapper: forces UTF-8 encoding to prevent Windows cp1252 crash."""
    kw.setdefault("capture_output", True)
    kw.setdefault("timeout", timeout)
    if kw.get("capture_output") or kw.get("stdout") == subprocess.PIPE:
        kw.setdefault("encoding", "utf-8")
        kw.setdefault("errors", "replace")
    if not cmd:
        return subprocess.CompletedProcess(cmd, returncode=127, stdout="", stderr="Empty command")
    try:
        return subprocess.run(cmd, **kw)
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, returncode=127, stdout="", stderr=f"Not found: {cmd[0] if cmd else '?'}")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, returncode=124, stdout="", stderr="Timed out")


# NOTE: We do NOT wrap sys.stdout here. All print output in this file is
# pure ASCII + ANSI escape codes, which works on any Windows codepage.
# The UTF-8 wrapper is only applied in faster_whisper_server.py where
# Japanese text may appear in logs. Wrapping stdout here breaks ANSI
# color rendering on Windows terminals.

VERSION = "0.0.8"

# Named constants
KiB = 1024
MiB = 1024 ** 2
GiB = 1024 ** 3
DOWNLOAD_CHUNK_SIZE = 64 * KiB
SERVER_STARTUP_TIMEOUT = 180
WHISPER_LOAD_TIMEOUT = 240
MAX_PORT = 65535
MIN_PORT = 1
LOG_MAX_SIZE_MB = 10
LOG_KEEP_COUNT = 3
UNIX_EXEC_MODE = 0o750
FIRST_UNPRIVILEGED_PORT = 1024
IANA_EPHEMERAL_START = 49152
WHISPER_SAMPLE_RATE = 16000

BASE_DIR = Path(__file__).parent.resolve()
TOOLS_DIR   = BASE_DIR / "tools"
SERVER_DIR  = BASE_DIR / "server"
MODELS_DIR  = BASE_DIR / "models"
GGUF_DIR    = BASE_DIR / "models" / "translation"
LOGS_DIR    = BASE_DIR / "logs"
EXT_DIR     = BASE_DIR / "extension"
# Backward compat: if config is in root (old layout), use that

PLAT = platform.system()  # "Windows", "Linux", "Darwin"
IS_WIN = (PLAT == "Windows")
IS_MAC = (PLAT == "Darwin")
IS_LIN = (PLAT == "Linux")
EXE = ".exe" if IS_WIN else ""
ARCH = platform.machine().lower()  # x86_64, amd64, arm64, aarch64

# ────
# DOWNLOAD URLS PER PLATFORM
# ────

def _get_download_urls():
    urls = {}

    # yt-dlp
    if IS_WIN:
        urls["yt-dlp"] = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe"
    elif IS_MAC:
        urls["yt-dlp"] = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_macos"
    else:
        # Linux
        urls["yt-dlp"] = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_linux"

    # FFmpeg
    if IS_WIN:
        urls["ffmpeg"] = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"
    elif IS_MAC:
        if "arm" in ARCH or "aarch" in ARCH:
            urls["ffmpeg"] = "https://www.osxexperts.net/ffmpeg7arm.zip"
        else:
            urls["ffmpeg"] = "https://evermeet.cx/ffmpeg/getrelease/zip"
    else:
        # Linux
        urls["ffmpeg"] = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz"

    # Deno
    if IS_WIN:
        urls["deno"] = "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip"
    elif IS_MAC:
        if "arm" in ARCH or "aarch" in ARCH:
            urls["deno"] = "https://github.com/denoland/deno/releases/latest/download/deno-aarch64-apple-darwin.zip"
        else:
            urls["deno"] = "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-apple-darwin.zip"
    else:
        # Linux
        urls["deno"] = "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip"

    # Ollama (optional external backend)
    if IS_WIN:
        urls["ollama"] = "https://ollama.com/download/OllamaSetup.exe"
    elif IS_MAC:
        urls["ollama"] = "https://ollama.com/download/Ollama-darwin.zip"
    else:
        urls["ollama"] = "https://ollama.com/install.sh"

    return urls


DOWNLOAD_URLS = _get_download_urls()

# ────
# API endpoint constants (avoid string duplication across backends + health checks)
HEALTH_PATH_OPENAI = "/v1/models"
HEALTH_PATH_OLLAMA = "/api/tags"
CHAT_PATH = "/v1/chat/completions"

# BACKEND DEFINITIONS  (llamacpp is DEFAULT -- no middleman)
# ────

BACKEND_INFO = {
    "llamacpp": {
        "name": "llama.cpp (built-in)",
        "desc": "DEFAULT. Drop a .gguf in models/translation/, Yume loads it directly.",
        "dh": "127.0.0.1", "dp": DEFAULT_TRANSLATION_PORT,
        "hp": HEALTH_PATH_OPENAI, "ap": CHAT_PATH,
        "inst": (
            "pip install llama-cpp-python\n"
            "  GPU (CUDA):  CMAKE_ARGS=\"-DGGML_CUDA=on\" pip install llama-cpp-python\n"
            "  Win prebuilt: pip install llama-cpp-python --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cu124"
        ),
    },
    "ollama": {
        "name": "Ollama",
        "desc": "One-click install, auto GPU, runs as service.",
        "dh": "127.0.0.1", "dp": DEFAULT_OLLAMA_PORT,
        "hp": HEALTH_PATH_OLLAMA, "ap": CHAT_PATH,
        "inst": "https://ollama.com -- or auto-install via Pocket Yume",
    },
    "lmstudio": {
        "name": "LM Studio",
        "desc": "GUI app with model browser.",
        "dh": "127.0.0.1", "dp": 1234,
        "hp": HEALTH_PATH_OPENAI, "ap": CHAT_PATH,
        "inst": "Download from: https://lmstudio.ai",
    },
    "textgenwebui": {
        "name": "text-generation-webui",
        "desc": "Feature-rich web UI by oobabooga.",
        "dh": "127.0.0.1", "dp": DEFAULT_TRANSLATION_PORT,
        "hp": HEALTH_PATH_OPENAI, "ap": CHAT_PATH,
        "inst": "https://github.com/oobabooga/text-generation-webui",
    },
    "custom": {
        "name": "Custom (OpenAI-compatible)",
        "desc": "Any server with /v1/chat/completions endpoint.",
        "dh": "127.0.0.1", "dp": DEFAULT_TRANSLATION_PORT,
        "hp": HEALTH_PATH_OPENAI, "ap": CHAT_PATH,
        "inst": "Provide your own endpoint.",
    },
}

# DEFAULT_CONFIG, load_config, save_config, validate_*, config_* -> config.py

#
# ────
# UI HELPERS  (ALL ASCII-SAFE)
# ────

class C:
    RESET  = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED    = "\033[91m"
    GREEN  = "\033[92m"
    YELLOW = "\033[93m"
    BLUE   = "\033[94m"
    MAGENTA= "\033[95m"
    CYAN   = "\033[96m"
    WHITE  = "\033[97m"
    GOLD   = "\033[38;5;220m"
    PURPLE = "\033[38;5;141m"

    @classmethod
    def disable(cls):
        """Strip all ANSI codes -- plain text fallback."""
        cls.RESET = cls.BOLD = cls.DIM = ""
        cls.RED = cls.GREEN = cls.YELLOW = ""
        cls.BLUE = cls.MAGENTA = cls.CYAN = ""
        cls.WHITE = cls.GOLD = cls.PURPLE = ""

# ────
# BOX DRAWING & PANELS (inspired by Rich library patterns)
# ────

BOX_CHARS = {
    "rounded": {"tl": "╭", "tr": "╮", "bl": "╰", "br": "╯", "h": "─", "v": "│"},
    "heavy":   {"tl": "┏", "tr": "┓", "bl": "┗", "br": "┛", "h": "━", "v": "┃"},
    "simple":  {"tl": "+", "tr": "+", "bl": "+", "br": "+", "h": "-", "v": "|"},
}

def _safe_box():
    """Pick box chars that work on the current terminal."""
    if IS_WIN:
        try:
            # Enable VT100 escape sequences on Windows consoles via kernel32 API
            import ctypes
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
            mode = ctypes.c_ulong()
            kernel32.GetConsoleMode(handle, ctypes.byref(mode))
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
            return BOX_CHARS["rounded"]
        except Exception:
            return BOX_CHARS["simple"]
    return BOX_CHARS["rounded"]

def panel(text, title="", style="", width=None, pad=1):
    """Draw a rounded panel/box around text. Like Rich's Panel."""
    b = _safe_box()
    w = width or min(tw() - 2, 80)
    inner = w - 2 - (pad * 2)
    lines = []
    for raw_line in text.split("\n"):
        # Word-wrap long lines
        while len(raw_line) > inner:
            lines.append(raw_line[:inner])
            raw_line = raw_line[inner:]
        lines.append(raw_line)

    # Title in top border
    if title:
        t = f" {title} "
        # Strip ANSI for width calculation (colored titles made border too short)
        t_visible = len(re.sub(r'\x1b\[[0-9;]*m', '', t))
        top = f"{b['tl']}{t}{b['h'] * max(0, w - 2 - t_visible)}{b['tr']}"
    else:
        top = f"{b['tl']}{b['h'] * (w - 2)}{b['tr']}"
    bot = f"{b['bl']}{b['h'] * (w - 2)}{b['br']}"

    color = style or C.DIM
    print(f"  {color}{top}{C.RESET}")
    for line in lines:
        # Strip ANSI for length calc
        clean = re.sub(r'\x1b\[[0-9;]*m', '', line)
        padding = inner - len(clean)
        print(f"  {color}{b['v']}{C.RESET}{' ' * pad}{line}{' ' * max(0, padding)}{' ' * pad}{color}{b['v']}{C.RESET}")
    print(f"  {color}{bot}{C.RESET}")

def table(headers, rows, col_styles=None, title=""):
    """Render a formatted table. Inspired by Rich Tables."""
    col_styles = col_styles or [C.RESET] * len(headers)
    # Calculate column widths
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            if i < len(widths):
                clean = re.sub(r'\x1b\[[0-9;]*m', '', str(cell))
                widths[i] = max(widths[i], len(clean))

    # Cap total width
    max_w = tw() - 6
    total = sum(widths) + (len(widths) - 1) * 3
    if total > max_w:
        scale = max_w / total
        widths = [max(4, int(w * scale)) for w in widths]

    def _row(cells, styles=None):
        parts = []
        for i, cell in enumerate(cells):
            s = (styles[i] if styles and i < len(styles) else C.RESET)
            clean = re.sub(r'\x1b\[[0-9;]*m', '', str(cell))
            pad = widths[i] - len(clean) if i < len(widths) else 0
            parts.append(f"{s}{cell}{C.RESET}{' ' * max(0, pad)}")
        return "   ".join(parts)

    sep = f"  {C.DIM}{'─' * (sum(widths) + (len(widths) - 1) * 3)}{C.RESET}"
    if title:
        print(f"\n  {C.BOLD}{title}{C.RESET}")
    print(sep)
    print(f"  {_row(headers, [C.BOLD + C.DIM] * len(headers))}")
    print(sep)
    for row in rows:
        print(f"  {_row(row, col_styles)}")
    print(sep)

# Spinner frames for animated status
SPINNERS = {
    "dots":   ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"],
    "line":   ["-", "\\", "|", "/"],
    "arrows": ["←", "↖", "↑", "↗", "→", "↘", "↓", "↙"],
    "simple": ["-", "\\", "|", "/"],
}

def _pick_spinner():
    """Pick spinner that works on terminal."""
    if IS_WIN:
        try:
            "⠋".encode(sys.stdout.encoding or "utf-8")
            return SPINNERS["dots"]
        except (UnicodeEncodeError, LookupError):
            return SPINNERS["simple"]
    return SPINNERS["dots"]

def spin_wait(check_fn, message, timeout=180, interval=0.5):
    """Spinner until check_fn() returns True."""
    frames = _pick_spinner()
    start = time.time()
    i = 0
    try:
        while time.time() - start < timeout:
            frame = frames[i % len(frames)]
            elapsed = int(time.time() - start)
            sys.stdout.write(f"\r  {C.CYAN}{frame}{C.RESET}  {message} {C.DIM}({elapsed}s){C.RESET}   ")
            sys.stdout.flush()
            if check_fn():
                sys.stdout.write(f"\r  {C.GREEN}✓{C.RESET}  {message} {C.DIM}({elapsed}s){C.RESET}   \n")
                sys.stdout.flush()
                return True
            time.sleep(interval)
            i += 1
        sys.stdout.write(f"\r  {C.YELLOW}⏱{C.RESET}  {message} {C.DIM}(timed out after {timeout}s){C.RESET}   \n")
        sys.stdout.flush()
        return False
    except KeyboardInterrupt:
        sys.stdout.write(f"\r  {C.RED}✗{C.RESET}  {message} {C.DIM}(cancelled){C.RESET}   \n")
        sys.stdout.flush()
        raise


def enable_ansi():
    """Try to enable ANSI escape sequences on Windows. Disable colors on failure."""
    if IS_WIN:
        ansi_ok = False
        try:
            import ctypes
            k32 = ctypes.windll.kernel32
            h = k32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
            # Get current console mode first
            mode = ctypes.c_ulong()
            if k32.GetConsoleMode(h, ctypes.byref(mode)):
                # Add ENABLE_VIRTUAL_TERMINAL_PROCESSING (0x0004)
                new_mode = mode.value | 0x0004
                if k32.SetConsoleMode(h, new_mode):
                    ansi_ok = True
        except Exception as e:
            _log.debug('[enable_ansi] ansi-init failed: %s', e)



        if not ansi_ok:
            # ANSI not supported -- strip all color codes for plain text output
            C.disable()

        # Set console codepage to UTF-8 so Japanese text (test translations etc.)
        # can be printed without cp932 crash
        try:
            import ctypes
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:
            subprocess.run(["cmd", "/c", "chcp", "65001"], capture_output=True)

def clear():
    if IS_WIN:
        subprocess.run(["cmd", "/c", "cls"])
    else:
        sys.stdout.write("\033[2J\033[H")
        sys.stdout.flush()

def tw():
    return shutil.get_terminal_size((80, 24)).columns

def center(t):
    return t.center(tw())

def gold_hr():
    print(f"{C.GOLD}{'='*tw()}{C.RESET}")

def header(sub=None):
    clear()
    print()
    # Cross-platform Unicode logo (works on Windows Terminal, macOS, Linux)
    try:
        logo = [
            f"{C.GOLD}╔═╗ ╔═╗ ╔═╗ ╦╔═ ╔═╗ ╔╦╗{C.RESET}",
            f"{C.GOLD}╠═╝ ║ ║ ║   ╠╩╗ ║╣   ║ {C.RESET}",
            f"{C.GOLD}╩   ╚═╝ ╚═╝ ╩ ╩ ╚═╝  ╩ {C.RESET}",
        ]
        for line in logo:
            print(center(line))
        print(center(f"{C.PURPLE}{C.BOLD}Y  U  M  E{C.RESET}"))
    except UnicodeEncodeError:
        # Absolute fallback for ancient terminals
        print(center(f"{C.GOLD}P O C K E T{C.RESET}"))
        print(center(f"{C.PURPLE}{C.BOLD}Y  U  M  E{C.RESET}"))
    ver_line = f"{C.DIM}v{VERSION}{C.RESET}"
    print(center(ver_line))
    if sub:
        print(center(f"{C.BOLD}{sub}{C.RESET}"))
    print()

def section(t):
    w = max(1, tw() - len(t) - 8)
    print(f"\n  {C.GOLD}--- {C.BOLD}{t} {C.GOLD}{'-'*w}{C.RESET}\n")

def info(m):     print(f"  {C.CYAN}i{C.RESET}  {m}")
def success(m):  print(f"  {C.GREEN}+{C.RESET}  {m}")
def warn(m):     print(f"  {C.YELLOW}!{C.RESET}  {C.YELLOW}{m}{C.RESET}")
def error(m):    print(f"  {C.RED}x{C.RESET}  {C.RED}{m}{C.RESET}")


def bullet(m, indent=2): print(f"{' '*indent}{C.DIM}-{C.RESET} {m}")

def pause(m="Press Enter to continue..."):
    try:
        input(f"\n  {C.DIM}{m}{C.RESET}")
    except (EOFError, KeyboardInterrupt):
        pass

def ask_yn(prompt, default=True):
    h = "[Y/n]" if default else "[y/N]"
    while True:
        try:
            r = input(f"  {C.GOLD}?{C.RESET}  {prompt} {C.DIM}{h}{C.RESET} ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return default
        if r == "":
            return default
        if r in ("y", "yes"):
            return True
        if r in ("n", "no"):
            return False

def ask_input(prompt, default=""):
    h = f" [{default}]" if default else ""
    try:
        r = input(f"  {C.GOLD}?{C.RESET}  {prompt}{C.DIM}{h}{C.RESET}: ").strip()
        return r if r else default
    except (EOFError, KeyboardInterrupt):
        print()
        return default

def ask_choice(prompt, options, default=0, allow_back=True):
    print(f"\n  {C.GOLD}?{C.RESET}  {prompt}\n")
    for i, (label, desc) in enumerate(options):
        mk = f"{C.GREEN}>{C.RESET}" if i == default else " "
        print(f"  {mk} {C.BOLD}{i+1}.{C.RESET} {label}")
        if desc:
            print(f"      {C.DIM}{desc}{C.RESET}")
    bh = ", b=back" if allow_back else ""
    print()
    while True:
        try:
            r = input(f"  {C.DIM}[1-{len(options)}{bh}, default={default+1}]: {C.RESET}").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return -1 if allow_back else default
        if r == "":
            return default
        if r == "b" and allow_back:
            return -1
        try:
            c = int(r) - 1
            if 0 <= c < len(options):
                return c
        except ValueError:
            pass

# ────
# FILE / NETWORK HELPERS
# ────

def download_file(url, dest, label="Downloading"):
    """Download a file with progress bar, speed, and ETA display."""
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": f"Yume/{VERSION}"})
        with urllib.request.urlopen(req, timeout=180) as resp:
            total = int(resp.headers.get("content-length", 0))
            dl = 0
            t0 = time.time()
            # Check disk space if content-length is known
            if total > 0:
                free = disk_free_gb(dest.parent) * GiB
                if free < total * 1.5:
                    # need 1.5x for download + extraction
                    print(f"\r  {C.RED}✗{C.RESET}  {label} — Not enough disk space " + " " * 20)
                    info(f"Need ~{total * 1.5 / MiB:.0f} MB, have {free / MiB:.0f} MB free")
                    return False
            with open(dest, "wb") as f:
                while True:
                    chunk = resp.read(DOWNLOAD_CHUNK_SIZE)  # 64KB chunks
                    if not chunk:
                        break
                    f.write(chunk)
                    dl += len(chunk)
                    elapsed = max(0.1, time.time() - t0)
                    speed = dl / elapsed  # bytes/sec
                    speed_mb = speed / MiB
                    if total > 0:
                        pct = min(100, dl * 100 // total)
                        remaining = (total - dl) / speed if speed > 0 else 0
                        eta_str = f"{int(remaining//60)}m{int(remaining%60):02d}s" if remaining > 60 else f"{int(remaining)}s"
                        mb = dl / MiB
                        tmb = total / MiB
                        bar_w = 20
                        filled = bar_w * pct // 100
                        bar = f"{C.GREEN}{'█' * filled}{C.DIM}{'░' * (bar_w - filled)}{C.RESET}"
                        sys.stdout.write(
                            f"\r  {C.CYAN}↓{C.RESET} {bar} {pct:3d}%  "
                            f"{mb:.1f}/{tmb:.1f} MB  "
                            f"{speed_mb:.1f} MB/s  "
                            f"ETA {eta_str}   "
                        )
                        sys.stdout.flush()
                    else:
                        mb = dl / MiB
                        sys.stdout.write(f"\r  {C.CYAN}↓{C.RESET}  {mb:.1f} MB  {speed_mb:.1f} MB/s  ")
                        sys.stdout.flush()
        elapsed = time.time() - t0
        print(f"\r  {C.GREEN}✓{C.RESET}  {label} — {total/MiB:.1f} MB in {elapsed:.0f}s" + " " * 30)
        return True
    except urllib.error.HTTPError as e:
        print(f"\r  {C.RED}✗{C.RESET}  {label} — HTTP {e.code}: {e.reason}" + " " * 20)
        if e.code == 404:
            info("The download URL may have changed. Try updating Yume.")
        _cleanup_partial(dest)
        return False
    except urllib.error.URLError as e:
        print(f"\r  {C.RED}✗{C.RESET}  {label} — Connection failed: {e.reason}" + " " * 20)
        info("Check your internet connection and try again")
        _cleanup_partial(dest)
        return False
    except Exception as e:
        print(f"\r  {C.RED}✗{C.RESET}  {label} — FAILED: {e}" + " " * 20)
        _cleanup_partial(dest)
        return False


def _cleanup_partial(path):
    """Remove a partially downloaded file so it doesn't corrupt future installs."""
    try:
        p = Path(path)
        if p.exists():
            p.unlink()
            _log.debug('[download] Cleaned up partial download: %s', p.name)
    except Exception:
        pass



def detect_gpu():
    r = {"has_nvidia": False, "has_amd": False, "name": None, "vram_mb": 0, "vendor": "none"}
    # Check NVIDIA
    try:
        out = _run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"], timeout=10)
        if out.returncode == 0 and out.stdout.strip():
            p = out.stdout.strip().split(",")
            r["has_nvidia"] = True
            r["name"] = p[0].strip()
            r["vendor"] = "nvidia"
            r["vram_mb"] = int(p[1].strip()) if len(p) > 1 else 0
            return r
    except Exception as e:
        _log.debug('[detect_gpu] nvidia-vram-parse failed: %s', e)



    # Check AMD/Radeon via rocm-smi (Linux ROCm)
    try:
        out = _run(["rocm-smi", "--showproductname", "--showmeminfo", "vram", "--csv"], timeout=10)
        if out.returncode == 0 and out.stdout.strip():
            lines = out.stdout.strip().split("\n")
            for line in lines[1:]:
                # skip header
                if line.strip():
                    r["has_amd"] = True
                    r["vendor"] = "amd"
                    r["name"] = line.split(",")[0].strip() if "," in line else "AMD GPU"
                    break
            # Try to get VRAM
            try:
                out2 = _run(["rocm-smi", "--showmeminfo", "vram"], timeout=10)
                for l2 in out2.stdout.split("\n"):
                    if "Total" in l2:
                        nums = [int(s) for s in l2.split() if s.isdigit()]
                        if nums:
                            r["vram_mb"] = nums[0] // MiB if nums[0] > 1_000_000 else nums[0]
            except Exception as e:
                _log.debug('[detect_gpu] nvidia-wmi-parse failed: %s', e)


            return r
    except Exception as e:
        _log.debug('[detect_gpu] nvidia-smi failed: %s', e)

    # Check AMD via rocminfo (fallback)
    try:
        out = _run(["rocminfo"], timeout=10)
        if out.returncode == 0 and "gfx" in out.stdout.lower():
            r["has_amd"] = True
            r["vendor"] = "amd"
            for line in out.stdout.split("\n"):
                if "Marketing Name" in line:
                    r["name"] = line.split(":")[-1].strip()
                    break
            if not r["name"]:
                r["name"] = "AMD GPU (ROCm)"
            return r
    except Exception as e:
        _log.debug('[detect_gpu] rocminfo failed: %s', e)

    # Windows AMD detection via WMI
    if IS_WIN:
        try:
            out = _run(["wmic", "path", "win32_videocontroller", "get", "name,adapterram", "/format:csv"], timeout=10)
            for line in out.stdout.strip().split("\n"):
                ll = line.lower()
                if "radeon" in ll or "amd" in ll:
                    parts = line.split(",")
                    r["has_amd"] = True
                    r["vendor"] = "amd"
                    r["name"] = parts[2].strip() if len(parts) > 2 else "AMD GPU"
                    try:
                        r["vram_mb"] = int(parts[1].strip()) // MiB if len(parts) > 1 and parts[1].strip().isdigit() else 0
                    except Exception as e:
                        _log.debug('[detect_gpu] wmic-amd-vram-parse failed: %s', e)

                    return r
        except Exception as e:
            _log.debug('[detect_gpu] wmic-amd failed: %s', e)

    return r

def _detect_cpu_name():
    """Get the CPU brand string (e.g. 'Intel Core i7-12700K' or 'AMD Ryzen 9 5900X')."""
    try:
        # Linux: /proc/cpuinfo
        cpuinfo = Path("/proc/cpuinfo")
        if cpuinfo.exists():
            for line in cpuinfo.read_text().splitlines():
                if line.startswith("model name"):
                    return line.split(":", 1)[1].strip()
        # Windows: wmic
        if IS_WIN:
            r = _run(["wmic", "cpu", "get", "name"], timeout=5)
            lines = [ln.strip() for ln in r.stdout.splitlines() if ln.strip() and ln.strip() != "Name"]
            if lines:
                return lines[0]
        # macOS: sysctl
        if IS_MAC:
            r = _run(["sysctl", "-n", "machdep.cpu.brand_string"], timeout=5)
            if r.stdout.strip():
                return r.stdout.strip()
    except Exception:
        pass
    return platform.processor() or "Unknown CPU"


def detect_ram_gb():
    try:
        if IS_WIN:
            import ctypes
            class MS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong)] + [("_"+str(i), ctypes.c_ulonglong) for i in range(6)]
            s = MS()
            s.dwLength = ctypes.sizeof(s)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(s))
            return s.ullTotalPhys / GiB
        else:
            with open("/proc/meminfo", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        return int(line.split()[1]) / MiB
    except Exception as e:
        _log.debug('[detect_ram_gb] int-parse failed: %s', e)


    return 0

def disk_free_gb(p=None):
    try:
        return shutil.disk_usage(p or BASE_DIR).free / GiB
    except Exception:
        return 0

def find_tool(name):
    # Check local tools/ first
    b = TOOLS_DIR / f"{name}{EXE}"
    if b.exists():
        return str(b)
    # Check without extension on Unix (yt-dlp_linux renamed to yt-dlp)
    if not IS_WIN:
        b2 = TOOLS_DIR / name
        if b2.exists():
            return str(b2)
    return shutil.which(name)

def _try_import(module_name):
    """Check if a Python module can be imported. Returns True/False."""
    try:
        __import__(module_name)
        return True
    except ImportError:
        return False

def check_server(host, port, path="/health"):
    """Check if a server is responding. Handles JSON and non-JSON responses."""
    try:
        url = f"http://{host}:{port}{path}"
        req = urllib.request.Request(url, headers={"User-Agent": "Yume"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            body = resp.read()
            try:
                return {"up": True, "data": json.loads(body)}
            except (json.JSONDecodeError, ValueError):
                # Server responded (HTTP 200) but body isn't JSON — still up
                return {"up": True, "data": {"raw": body.decode("utf-8", errors="replace")[:200]}}
    except Exception:
        return {"up": False, "data": {}}


def check_translation_server(host, port, backend_info=None):
    """Check translation server health.
    Only tries the backend's configured health endpoint, then falls back to
    a raw socket connect. This avoids flooding the server with 404s on
    endpoints it doesn't support (e.g., /health, /api/tags on llama.cpp)."""
    bi = backend_info or BACKEND_INFO.get("llamacpp", {})
    primary = bi.get("hp", HEALTH_PATH_OPENAI)

    # Try the configured endpoint
    st = check_server(host, port, primary)
    if st["up"]:
        return st

    # The LLM might be busy with inference (10-20s per request).
    # A raw socket connect proves the server is alive even when HTTP times out.
    try:
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        s.settimeout(2)
        s.connect((host, int(port)))
        s.close()
        return {"up": True, "data": {"status": "busy"}}
    except Exception:
        pass

    return {"up": False, "data": {}}

def check_ollama_models(host="127.0.0.1", port=DEFAULT_OLLAMA_PORT):
    try:
        req = urllib.request.Request(f"http://{host}:{port}/api/tags", headers={"User-Agent": "Yume"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            return [m["name"] for m in json.loads(resp.read()).get("models", [])]
    except Exception:
        return []

def find_gguf_models():
    GGUF_DIR.mkdir(parents=True, exist_ok=True)
    return list(GGUF_DIR.glob("*.gguf"))



# ────
# PORT MANAGEMENT
# ────

def is_port_free(port, host="127.0.0.1"):
    """Check if a port is available for binding."""
    if not isinstance(port, int) or port < 1 or port > 65535:
        return False
    try:
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        s.settimeout(1)
        s.bind((host, port))
        s.close()
        return True
    except (OSError, _socket.error):
        return False

def find_free_port(start=DEFAULT_TRANSLATION_PORT, exclude=None):
    """Find a free port starting from 'start', skipping any in exclude set."""
    exclude = exclude or set()
    for p in range(max(FIRST_UNPRIVILEGED_PORT, start), min(start + 200, MAX_PORT + 1)):
        if p not in exclude and is_port_free(p):
            return p
    for p in range(IANA_EPHEMERAL_START, IANA_EPHEMERAL_START + 100):
        if p not in exclude and is_port_free(p):
            return p
    return None

def get_port_process(port):
    """Find who owns a port. Returns (pid, name) or (None, None)."""
    try:
        if IS_WIN:
            r = _run(["netstat", "-ano"], timeout=10)
            if r.returncode != 0 or not r.stdout:
                return None, None
            for line in r.stdout.splitlines():
                if f":{port}" in line and "LISTENING" in line:
                    parts = line.split()
                    pid_str = parts[-1] if parts else ""
                    if pid_str.isdigit() and int(pid_str) != 0:
                        pid = int(pid_str)
                        r2 = _run(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"], timeout=5)
                        name = "unknown"
                        if r2.returncode == 0 and r2.stdout:
                            for row in r2.stdout.splitlines():
                                if str(pid) in row and "," in row:
                                    name = row.split(",")[0].strip('"')
                                    break
                        return pid, name
        else:
            r = _run(["lsof", "-ti", f":{port}"], timeout=10)
            if r.returncode == 0 and r.stdout and r.stdout.strip():
                pid_str = r.stdout.strip().split("\n")[0].strip()
                if pid_str.isdigit():
                    pid = int(pid_str)
                    r2 = _run(["ps", "-p", str(pid), "-o", "comm="], timeout=5)
                    name = (r2.stdout or "").strip() if r2.returncode == 0 else "unknown"
                    return pid, name
            # Fallback: ss (lsof may not be installed on all systems)
            r2 = _run(['ss', '-tlnp'], timeout=10)
            if r2.returncode == 0 and r2.stdout:
                for sline in r2.stdout.splitlines():
                    if f':{port}' in sline:
                        m = re.search(r'pid=(\d+)', sline)
                        if m:
                            pid = int(m.group(1))
                            n = _run(['ps', '-p', str(pid), '-o', 'comm='], timeout=5)
                            return pid, (n.stdout or '').strip() if n.returncode == 0 else 'unknown'
    except Exception as e:
        _log.debug('[get_port_process] port-lookup failed: %s', e)


    return None, None

def kill_port_process(port):
    """Kill whatever process is using port. Returns True if killed."""
    pid, name = get_port_process(port)
    if pid is None:
        # Try fuser on Linux as fallback
        if not IS_WIN:
            try:
                r = _run(["fuser", f"{port}/tcp"], timeout=5)
                if r.returncode == 0 and r.stdout.strip():
                    for p in r.stdout.strip().split():
                        p = p.strip()
                        if p.isdigit():
                            _run(["kill", "-9", p], timeout=5)
                    time.sleep(0.5)
                    if is_port_free(port):
                        success(f"Freed port {port}")
                        return True
            except Exception as e:
                _log.debug('[kill_port_process] fuser-kill failed: %s', e)


        return False
    try:
        if IS_WIN:
            _run(["taskkill", "/F", "/PID", str(pid)], timeout=10)
        else:
            _run(["kill", "-9", str(pid)], timeout=10)
        time.sleep(1)
        if is_port_free(port):
            info(f"Killed {name or 'process'} (PID {pid}) on port {port}")
            return True
        # Backup: fuser -k
        if not IS_WIN:
            _run(["fuser", "-k", f"{port}/tcp"], timeout=5)
            time.sleep(0.5)
        return is_port_free(port)
    except Exception as e:
        warn(f"Could not kill PID {pid}: {e}")
        return False

def ensure_port_free(port, cfg, key_prefix, exclude=None):
    """Free up a port interactively. Returns port or None."""
    if is_port_free(port):
        return port
    pid, name = get_port_process(port)
    warn(f"Port {port} is in use by {name or 'unknown'} (PID {pid or '?'})")
    # macOS Monterey+ uses port 5000 for AirPlay Receiver — common gotcha
    if IS_MAC and port == 5000:
        info(f"{C.DIM}macOS uses port 5000 for AirPlay Receiver.{C.RESET}")
        info(f"{C.DIM}Disable it in System Settings > General > AirDrop & Handoff > AirPlay Receiver,{C.RESET}")
        info(f"{C.DIM}or let Yume use a different port (recommended).{C.RESET}")
        print()
    ch = ask_choice("How to resolve?", [
        ("Kill the process", f"Terminate {name or 'PID '+str(pid)}"),
        ("Use a different port", "Auto-find a free port"),
        ("Enter port manually", None),
        ("Cancel", None),
    ], default=0)
    if ch == 0:
        if kill_port_process(port) and is_port_free(port):
            success(f"Port {port} is now free")
            return port
        error("Failed to free port")
        return None
    elif ch == 1:
        new_port = find_free_port(port + 1, exclude=exclude)
        if new_port:
            cfg[f"{key_prefix}_port"] = new_port
            save_config(cfg)
            success(f"Reassigned to port {new_port}")
            return new_port
        error("No free port found")
        return None
    elif ch == 2:
        np = ask_input("Port number", str(port + 1))
        try:
            np = int(np)
            if is_port_free(np):
                cfg[f"{key_prefix}_port"] = np
                save_config(cfg)
                return np
            else:
                error(f"Port {np} is also in use")
                return None
        except ValueError:
            error("Invalid port")
            return None
    return None

def show_ports_status(cfg):
    """Display port status overview."""
    section("Port Status")
    for label, key in [("Whisper", "whisper"), ("Translation", "translation")]:
        host = cfg.get(f"{key}_host", "127.0.0.1")
        port = cfg.get(f"{key}_port", DEFAULT_TRANSLATION_PORT)
        free = is_port_free(port, host)
        if free:
            info(f"{label:12s} {host}:{port}  -- {C.GREEN}free{C.RESET}")
        else:
            pid, name = get_port_process(port)
            who = f"{name} (PID {pid})" if pid else "unknown process"
            warn(f"{label:12s} {host}:{port}  -- {C.RED}in use{C.RESET} by {who}")

# ────
# API TOKEN DISCOVERY
# ────

def discover_api_token(host, port):
    """Discover API token from .yume_token file or server /health endpoint."""
    global _api_token
    if _api_token is not None:
        return _api_token
    token_file = BASE_DIR / ".yume_token"
    if token_file.exists():
        try:
            _api_token = token_file.read_text(encoding="utf-8").strip()
            if _api_token:
                return _api_token
        except Exception as e:
            _log.debug('[discover_api_token] health-check failed: %s', e)


    try:
        req = urllib.request.Request(f"http://{host}:{port}/health", headers={"User-Agent": "Yume"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read())
            token = data.get("api_token") or data.get("token")
            if token:
                _api_token = token
                return _api_token
    except Exception as e:
        _log.debug('[discover_api_token] token-file-read failed: %s', e)


    return None

# ────
# CLI SERVER INTERACTION  (used by CLI subcommands + interactive menus)
# ────

def _server_get(host, port, path, timeout=5):
    """GET request with API token auth."""
    try:
        headers = {"User-Agent": "Yume"}
        token = discover_api_token(host, port)
        if token:
            headers["X-API-Token"] = token
        req = urllib.request.Request(f"http://{host}:{port}{path}", headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def _server_post(host, port, path, data=None, timeout=30):
    """POST request with API token auth."""
    try:
        body = json.dumps(data or {}).encode("utf-8")
        headers = {"Content-Type": "application/json", "User-Agent": "Yume"}
        token = discover_api_token(host, port)
        if token:
            headers["X-API-Token"] = token
        req = urllib.request.Request(
            f"http://{host}:{port}{path}", data=body,
            headers=headers, method="POST"
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception as e:
        return {"error": str(e)}


def cli_server_stats(cfg):
    """Print server stats."""
    h, p = cfg["whisper_host"], cfg["whisper_port"]
    data = _server_get(h, p, "/stats")
    if not data:
        error(f"Whisper server not reachable at {h}:{p}")
        return

    header("Server Statistics")
    gpu = data.get("gpu")
    if gpu:
        pct = round(gpu["vram_used_mb"] / gpu["vram_total_mb"] * 100) if gpu["vram_total_mb"] else 0
        bar_w = 30
        filled = round(bar_w * pct / 100)
        bar = f"[{'#' * filled}{'-' * (bar_w - filled)}] {pct}%"
        success(f"GPU: {gpu['gpu_name']}")
        info(f"  VRAM: {gpu['vram_used_mb']}/{gpu['vram_total_mb']} MB  {bar}")
        info(f"  Util: {gpu['gpu_util_pct']}%  |  Temp: {gpu['gpu_temp_c']}C")
    else:
        info("GPU: N/A (CPU mode or nvidia-smi unavailable)")
    print()
    section("Whisper Engine")
    info(f"Model: {C.BOLD}{data.get('model', '?')}{C.RESET}  ({data.get('device', '?')}/{data.get('compute_type', '?')})")
    info(f"Uptime: {data.get('uptime_human', '?')}")
    section("Session")
    info(f"Chunks transcribed:      {data.get('chunks_transcribed', 0)}")
    info(f"Segments produced:       {data.get('segments_produced', 0)}")
    info(f"Hallucinations blocked:  {data.get('hallucinations_filtered', 0)}")
    info(f"Audio processed:         {data.get('total_audio_seconds', 0):.0f}s")
    info(f"Cache hits:              {data.get('cache_hits', 0)}")
    info(f"Avg time/chunk:          {data.get('avg_whisper_time', 0)}s")
    info(f"Last chunk:              {data.get('last_chunk_whisper_time', 0)}s ({data.get('last_chunk_segments', 0)} segs)")
    info(f"Subtitle cache:          {data.get('subtitle_cache_size', 0)} chunks")
    info(f"Blacklist size:          {data.get('blacklist_size', 0)} items")


def cli_blacklist(cfg, args):
    """CLI blacklist management."""
    h, p = cfg["whisper_host"], cfg["whisper_port"]
    if len(args) < 1:
        _menu_blacklist(cfg)
        return
    subcmd = args[0].lower()
    if subcmd == "list":
        data = _server_get(h, p, "/blacklist")
        if not data:
            error(f"Server not reachable at {h}:{p}")
            return
        bl = data.get("blacklist", [])
        if not bl:
            info("Blacklist is empty")
            return
        info(f"Server blacklist ({len(bl)} items):")
        for item in bl:
            bullet(item)
    elif subcmd == "add" and len(args) > 1:
        text = " ".join(args[1:])
        data = _server_get(h, p, "/blacklist")
        if not data:
            error("Server not reachable")
            return
        current = data.get("blacklist", [])
        if text in current:
            warn(f"Already blocked: {text}")
            return
        current.append(text)
        result = _server_post(h, p, "/blacklist/update", {"blacklist": current})
        if result and result.get("success"):
            success(f"Added: {text}")
        else:
            error(f"Failed: {result}")
    elif subcmd in ("remove", "rm") and len(args) > 1:
        text = " ".join(args[1:])
        data = _server_get(h, p, "/blacklist")
        if not data:
            error("Server not reachable")
            return
        current = data.get("blacklist", [])
        if text not in current:
            warn(f"Not in blacklist: {text}")
            return
        current.remove(text)
        result = _server_post(h, p, "/blacklist/update", {"blacklist": current})
        if result and result.get("success"):
            success(f"Removed: {text}")
        else:
            error(f"Failed: {result}")
    elif subcmd == "clear":
        result = _server_post(h, p, "/blacklist/update", {"blacklist": []})
        if result and result.get("success"):
            success("Blacklist cleared")
        else:
            error(f"Failed: {result}")
    else:
        print("  Usage: pocket_yume.py blacklist [list|add <text>|remove <text>|clear]")


def cli_model(cfg, args):
    """CLI model management."""
    h, p = cfg["whisper_host"], cfg["whisper_port"]
    if len(args) < 1:
        data = _server_get(h, p, "/stats")
        if not data:
            error(f"Server not reachable at {h}:{p}")
            info(f"Config model: {cfg.get('whisper_model', '?')}")
            return
        info(f"Active model: {C.BOLD}{data.get('model', '?')}{C.RESET}")
        info(f"Device: {data.get('device', '?')}  |  Compute: {data.get('compute_type', '?')}")
        if data.get("gpu"):
            g = data["gpu"]
            info(f"GPU: {g.get('gpu_name', '?')} ({g.get('vram_used_mb', '?')}/{g.get('vram_total_mb', '?')} MB)")
        return
    subcmd = args[0].lower()
    if subcmd == "switch" and len(args) > 1:
        new_model = args[1]
        info(f"Switching to {new_model}...")
        result = _server_post(h, p, "/model/switch", {"model": new_model}, timeout=120)
        if not result:
            error(f"Server not reachable at {h}:{p}")
            return
        if result.get("error"):
            error(result["error"])
            if result.get("valid"):
                info(f"Valid: {', '.join(result['valid'])}")
        elif result.get("status") == "already_loaded":
            info(f"Already using {new_model}")
        else:
            success(f"Switched to {result.get('model', new_model)}")
            cfg["whisper_model"] = result.get("model", new_model)
            save_config(cfg)
    elif subcmd == "list":
        models = [("tiny","~1GB","Fastest"),("base","~1GB","Fast"),("small","~2GB","Good"),
                  ("medium","~5GB","High"),("large-v2","~10GB","Very high"),("large-v3","~10GB","Best"),
                  ("large-v3-turbo","~4GB","Near-v3, 2x faster"),
                  ("distil-large-v2","~4GB","Fast+acc"),("distil-large-v3","~4GB","Fast+acc (new)")]
        cur = cfg.get("whisper_model", "large-v3")
        info("Available Whisper models:")
        for name, vram, desc in models:
            act = f" {C.GREEN}<- active{C.RESET}" if name == cur else ""
            cached = _is_whisper_model_cached(name)
            dl = f"{C.GREEN}[downloaded]{C.RESET}" if cached else f"{C.DIM}[not downloaded]{C.RESET}"
            bullet(f"{name:20s} {vram:8s} {desc:20s} {dl}{act}")
    else:
        print("  Usage: pocket_yume.py model [list|switch <name>]")


def _menu_blacklist(cfg):
    """Blacklist management menu."""
    while True:
        header("Subtitle Filter (Blacklist)")
        info("Whisper sometimes generates fake text that wasn't actually spoken —")
        info("things like 'Thank you for watching' or 'Subscribe'. These are called")
        info("'hallucinations'. Yume blocks known patterns automatically, but you can")
        info("add your own phrases to block here.")
        print()
        h, p = cfg["whisper_host"], cfg["whisper_port"]
        data = _server_get(h, p, "/blacklist")
        if not data:
            warn(f"Server not reachable at {h}:{p}")
            info("Start server first")
            pause()
            return
        bl = data.get("blacklist", [])
        info(f"Server blacklist: {len(bl)} items")
        if bl:
            for item in bl[:15]:
                bullet(item)
            if len(bl) > 15:
                info(f"  ... and {len(bl) - 15} more")
        ch = ask_choice("Options:", [
            ("Add entry", "Block a phrase from subtitles"),
            ("Remove entry", "Unblock a phrase"),
            ("Clear all", "Remove all entries"),
            ("Back", None)
        ], default=3)
        if ch == -1 or ch == 3:
            return
        elif ch == 0:
            text = ask_input("Phrase to block", "")
            if text:
                current = bl[:]
                if text in current:
                    warn("Already blocked")
                    pause()
                    continue
                current.append(text)
                r = _server_post(h, p, "/blacklist/update", {"blacklist": current})
                if r and r.get("success"):
                    success(f"Added: {text}")
                else:
                    error("Failed")
            pause()
        elif ch == 1:
            if not bl:
                info("Empty")
                pause()
                continue
            opts = [(item, None) for item in bl[:20]] + [("Back", None)]
            rc = ask_choice("Remove which?", opts, default=len(opts)-1)
            if 0 <= rc < len(bl):
                removed = bl[rc]
                current = bl[:]
                current.pop(rc)
                r = _server_post(h, p, "/blacklist/update", {"blacklist": current})
                if r and r.get("success"):
                    success(f"Removed: {removed}")
            pause()
        elif ch == 2:
            if ask_yn("Clear ALL?", False):
                r = _server_post(h, p, "/blacklist/update", {"blacklist": []})
                if r and r.get("success"):
                    success("Cleared")
            pause()


def _menu_whisper_model(cfg):
    """Interactive whisper model hot-swap."""
    header("Whisper Model")
    h, p = cfg["whisper_host"], cfg["whisper_port"]
    data = _server_get(h, p, "/stats")
    cur = data.get("model", cfg.get("whisper_model", "?")) if data else cfg.get("whisper_model", "large-v3")
    is_custom = os.path.sep in cur or "/" in cur
    friendly_name = cfg.get("whisper_model_name", "")
    if data:
        display = friendly_name or (Path(cur).name if is_custom else cur)
        info(f"Active: {C.BOLD}{display}{C.RESET}  ({data.get('device', '?')})")
        if is_custom:
            info(f"{C.DIM}Path: {cur}{C.RESET}")
        if data.get("gpu"):
            g = data["gpu"]
            info(f"GPU: {g['gpu_name']} ({g['vram_used_mb']}/{g['vram_total_mb']} MB)")
    else:
        display = friendly_name or (Path(cur).name if is_custom else cur)
        warn(f"Server not running. Config: {display}")
    models = ["tiny", "base", "small", "medium", "large-v2", "large-v3", "large-v3-turbo", "distil-large-v2", "distil-large-v3"]
    vram = {"tiny":"~1GB","base":"~1GB","small":"~2GB","medium":"~5GB",
            "large-v2":"~10GB","large-v3":"~10GB","large-v3-turbo":"~4GB","distil-large-v2":"~4GB","distil-large-v3":"~4GB"}
    opts = []
    for m in models:
        cached = _is_whisper_model_cached(m)
        tag = f" {C.GREEN}[downloaded]{C.RESET}" if cached else f" {C.DIM}[not downloaded]{C.RESET}"
        if m == cur:
            label = f"{m} ({vram.get(m,'?')}){tag}"
            opts.append((label, "active"))
        else:
            opts.append((f"{m} ({vram.get(m,'?')}){tag}", None))
    custom_tag = f" {C.GREEN}[active]{C.RESET}" if is_custom else ""
    opts.append((f"Custom model (local path){custom_tag}", "active" if is_custom else None))
    opts.append(("Back", None))
    di = models.index(cur) if cur in models else (len(models) if is_custom else len(models) + 1)
    ch = ask_choice("Switch to:", opts, default=di)
    if ch == -1 or ch == len(models) + 1:
        return  # Back

    if ch == len(models):
        # Custom model path
        print()
        info(f"{C.DIM}Paste the path to a CTranslate2 Whisper model directory.{C.RESET}")
        info(f"{C.DIM}The folder must contain: model.bin, config.json, tokenizer.json, vocabulary.txt{C.RESET}")
        info(f"{C.DIM}See README > Whisper Models > Using a custom or fine-tuned model for details.{C.RESET}")
        print()
        custom_path = input(f"  {C.CYAN}>{C.RESET} Path: ").strip().strip('"').strip("'")
        if not custom_path:
            info("Cancelled.")
            pause()
            return
        custom_path = str(Path(custom_path).resolve())
        if not Path(custom_path).is_dir():
            error(f"Directory not found: {custom_path}")
            pause()
            return
        # Verify it looks like a CT2 model
        required = ["model.bin", "config.json"]
        missing = [f for f in required if not (Path(custom_path) / f).exists()]
        if missing:
            error(f"Not a valid CTranslate2 model — missing: {', '.join(missing)}")
            info(f"{C.DIM}Convert your model first:{C.RESET}")
            info(f"{C.DIM}  ct2-openai-whisper-converter --model <checkpoint> --output_dir <output>{C.RESET}")
            info(f"{C.DIM}  ct2-transformers-converter --model <hf-model> --output_dir <output> --quantization float16{C.RESET}")
            pause()
            return
        new_model = custom_path
        # Ask for a friendly display name (default: directory name)
        dir_name = Path(custom_path).name
        print()
        info("Give this model a friendly name (shown in menus and popup).")
        friendly = input(f"  {C.CYAN}>{C.RESET} Name [{dir_name}]: ").strip()
        if not friendly:
            friendly = dir_name
        cfg["whisper_model_name"] = friendly
    else:
        new_model = models[ch]
        # Clear friendly name when switching to a standard model
        cfg["whisper_model_name"] = ""

    if new_model == cur:
        info("Already active")
        pause()
        return
    if not data:
        cfg["whisper_model"] = new_model
        save_config(cfg)
        display = Path(new_model).name if os.path.sep in new_model or "/" in new_model else new_model
        success(f"Config set to {display} (applies on next launch)")
        pause()
        return
    info(f"Switching to {Path(new_model).name if os.path.sep in new_model or '/' in new_model else new_model}...")
    result = _server_post(h, p, "/model/switch", {"model": new_model}, timeout=120)
    if result and not result.get("error"):
        success(f"Switched to {result.get('model', new_model)}")
        cfg["whisper_model"] = result.get("model", new_model)
        save_config(cfg)
    else:
        error(f"Failed: {result.get('error', 'unknown') if result else 'server unreachable'}")
    pause()


# ────
# TOOL INSTALLERS (cross-platform)
# ────

def install_ytdlp():
    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    dest = TOOLS_DIR / f"yt-dlp{EXE}"
    # On Linux/Mac the binary has a different name
    if not IS_WIN:
        dest = TOOLS_DIR / "yt-dlp"
    ok = download_file(DOWNLOAD_URLS["yt-dlp"], dest, "yt-dlp")
    if ok and not IS_WIN:
        os.chmod(dest, UNIX_EXEC_MODE)
    return ok

def install_ffmpeg():
    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    if IS_WIN:
        zp = TOOLS_DIR / "ffmpeg.zip"
        if not download_file(DOWNLOAD_URLS["ffmpeg"], zp, "FFmpeg"):
            return False
        print(f"  {C.CYAN}...{C.RESET}  Extracting...", end="", flush=True)
        try:
            with zipfile.ZipFile(zp) as zf:
                for n in zf.namelist():
                    bn = os.path.basename(n)
                    if bn in ("ffmpeg.exe", "ffprobe.exe"):
                        with zf.open(n) as s, open(TOOLS_DIR / bn, "wb") as d:
                            d.write(s.read())
            zp.unlink(missing_ok=True)
            print(f"\r  {C.GREEN}+{C.RESET}  Extracted" + " " * 30)
            # Verify the binary actually works
            ff = TOOLS_DIR / "ffmpeg.exe"
            if ff.exists():
                r = _run([str(ff), "-version"], timeout=5)
                if r.returncode != 0:
                    warn("ffmpeg extracted but fails to run — try downloading manually")
                    return False
            else:
                error("ffmpeg.exe not found after extraction — zip may have different structure")
                info("Download manually: https://ffmpeg.org/download.html")
                return False
            return True
        except Exception as e:
            print(f"\r  {C.RED}x{C.RESET}  Failed: {e}")
            return False

    elif IS_LIN:
        tp = TOOLS_DIR / "ffmpeg.tar.xz"
        if not download_file(DOWNLOAD_URLS["ffmpeg"], tp, "FFmpeg"):
            return False
        print(f"  {C.CYAN}...{C.RESET}  Extracting...", end="", flush=True)
        try:
            with tarfile.open(tp, "r:xz") as tf:
                for m in tf.getmembers():
                    bn = os.path.basename(m.name)
                    if bn in ("ffmpeg", "ffprobe") and m.isfile():
                        m.name = bn
                        tf.extract(m, TOOLS_DIR)
            tp.unlink(missing_ok=True)
            for b in ["ffmpeg", "ffprobe"]:
                bf = TOOLS_DIR / b
                if bf.exists():
                    os.chmod(bf, UNIX_EXEC_MODE)
            print(f"\r  {C.GREEN}+{C.RESET}  Extracted" + " " * 30)
            return True
        except Exception as e:
            print(f"\r  {C.RED}x{C.RESET}  Failed: {e}")
            return False

    else:
        # macOS
        # Try brew first
        if shutil.which("brew"):
            info("Installing FFmpeg via Homebrew...")
            r = _run(["brew", "install", "ffmpeg"], timeout=600)
            return r.returncode == 0
        zp = TOOLS_DIR / "ffmpeg.zip"
        if not download_file(DOWNLOAD_URLS["ffmpeg"], zp, "FFmpeg"):
            return False
        try:
            with zipfile.ZipFile(zp) as zf:
                zf.extractall(TOOLS_DIR)
            zp.unlink(missing_ok=True)
            for b in (TOOLS_DIR / "ffmpeg", ):
                if b.exists():
                    os.chmod(b, UNIX_EXEC_MODE)
            return True
        except Exception as e:
            error(f"Failed: {e}")
            return False

def install_deno():
    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    zp = TOOLS_DIR / "deno.zip"
    if not download_file(DOWNLOAD_URLS["deno"], zp, "Deno"):
        return False
    try:
        with zipfile.ZipFile(zp) as zf:
            zf.extractall(TOOLS_DIR)
        zp.unlink(missing_ok=True)
        # Make executable on Unix
        de = TOOLS_DIR / f"deno{EXE}"
        if de.exists() and not IS_WIN:
            os.chmod(de, UNIX_EXEC_MODE)
        success("Deno extracted")
    except Exception as e:
        error(f"Failed: {e}")
        return False

    # Install the PO token plugin — this is what actually uses deno for YouTube auth
    info("Installing YouTube PO token plugin (bgutil-ytdlp-pot-provider)...")
    info(f"{C.DIM}This plugin uses Deno to solve YouTube's bot-detection challenges.{C.RESET}")
    info(f"{C.DIM}Once installed, yt-dlp uses it automatically — no extra steps needed.{C.RESET}")
    try:
        r = _run([
            sys.executable, "-m", "pip", "install", "-q",
            "--no-warn-script-location", "bgutil-ytdlp-pot-provider"
        ], timeout=120)
        if r.returncode == 0:
            success("PO token plugin installed")
        else:
            warn("PO token plugin install failed — YouTube may require sign-in")
            info(f"{C.DIM}You can retry manually: pip install bgutil-ytdlp-pot-provider{C.RESET}")
    except Exception as e:
        warn(f"PO token plugin install failed: {e}")

    # Download and set up the bgutil PO token server
    # This is the actual server that generates PO tokens using deno
    info("Setting up bgutil PO token server...")
    info(f"{C.DIM}This downloads the server code from GitHub and installs its dependencies.{C.RESET}")
    bgutil_dir = TOOLS_DIR / "bgutil-ytdlp-pot-provider"
    server_dir = bgutil_dir / "server"
    main_ts = server_dir / "src" / "main.ts"

    if main_ts.exists():
        success("bgutil server already set up")
    else:
        zip_url = "https://github.com/Brainicism/bgutil-ytdlp-pot-provider/archive/refs/tags/1.3.1.zip"
        zip_path = TOOLS_DIR / "bgutil.zip"
        if download_file(zip_url, zip_path, "bgutil server"):
            try:
                with zipfile.ZipFile(zip_path) as zf:
                    zf.extractall(TOOLS_DIR)
                zip_path.unlink(missing_ok=True)
                # Rename extracted dir
                extracted = TOOLS_DIR / "bgutil-ytdlp-pot-provider-1.3.1"
                if extracted.exists():
                    if bgutil_dir.exists():
                        shutil.rmtree(bgutil_dir)
                    extracted.rename(bgutil_dir)
                success("bgutil server extracted")

                # Install dependencies with deno
                if main_ts.exists():
                    deno = find_tool("deno") or "deno"
                    info("Installing bgutil server dependencies (deno install)...")
                    try:
                        r = _run([deno, "install", "--allow-scripts=npm:canvas", "--frozen"],
                                 cwd=str(server_dir), timeout=180)
                        if r.returncode != 0:
                            r = _run([deno, "install", "--allow-scripts=npm:canvas"],
                                     cwd=str(server_dir), timeout=180)
                        if r.returncode == 0:
                            success("bgutil server dependencies installed")
                        else:
                            warn("deno install failed — server may not start")
                    except Exception as e:
                        warn(f"deno install failed: {e}")
                else:
                    warn("main.ts not found after extraction")
            except Exception as e:
                error(f"Extract failed: {e}")
                zip_path.unlink(missing_ok=True)
        else:
            warn("bgutil server download failed")
            info(f"{C.DIM}YouTube auth will fall back to browser cookies.{C.RESET}")

    return True

def install_ollama():
    if IS_WIN:
        TOOLS_DIR.mkdir(parents=True, exist_ok=True)
        inst = TOOLS_DIR / "OllamaSetup.exe"
        if not download_file(DOWNLOAD_URLS["ollama"], inst, "Ollama installer"):
            return False
        print(f"\n  {C.YELLOW}The Ollama installer will now open. Follow the prompts.{C.RESET}\n")
        pause("Press Enter to launch installer...")
        try:
            subprocess.Popen([str(inst)])
            info("Waiting for Ollama...")
            for _ in range(90):
                time.sleep(2)
                if shutil.which("ollama") or check_server("127.0.0.1", DEFAULT_OLLAMA_PORT, HEALTH_PATH_OLLAMA)["up"]:
                    success("Ollama installed!")
                    inst.unlink(missing_ok=True)
                    return True
            warn("Timed out.")
            return False
        except Exception as e:
            error(f"Failed: {e}")
            return False
    elif IS_MAC:
        zp = TOOLS_DIR / "Ollama.zip"
        if not download_file(DOWNLOAD_URLS["ollama"], zp, "Ollama"):
            return False
        info("Extract and move to Applications manually.")
        return True
    else:
        # Linux
        info("Installing Ollama via official script...")
        script = TOOLS_DIR / "ollama_install.sh"
        TOOLS_DIR.mkdir(parents=True, exist_ok=True)
        try:
            if not download_file(DOWNLOAD_URLS["ollama"], script, "Ollama install script"):
                return False
            script.chmod(UNIX_EXEC_MODE)
            r = _run(["bash", str(script)], timeout=300)
            script.unlink(missing_ok=True)
            return r.returncode == 0
        except Exception as e:
            error(f"Failed: {e}")
            script.unlink(missing_ok=True)
            return False

def _is_venv():
    """Check if running inside a virtualenv or venv."""
    return (hasattr(sys, 'real_prefix') or  # virtualenv
            (hasattr(sys, 'base_prefix') and sys.base_prefix != sys.prefix))  # venv

_pip_venv_warned = False

def _check_pip():
    """Verify pip is available. Returns True if pip works."""
    global _pip_venv_warned
    r = _run([sys.executable, "-m", "pip", "--version"], timeout=10)
    if r.returncode != 0:
        error("pip is not installed or not working!")
        if IS_WIN:
            info("If you installed Python from the Windows Store, pip may be missing.")
            info("Re-install Python from https://python.org and check 'Add to PATH'.")
        else:
            info("Install pip:")
            pm = _detect_package_manager()
            if pm == "apt":
                info("  sudo apt install python3-pip")
            elif pm == "dnf":
                info("  sudo dnf install python3-pip")
            else:
                info(f"  {sys.executable} -m ensurepip --upgrade")
        return False
    # Warn once if installing into system Python (not a venv)
    if not _is_venv() and not _pip_venv_warned:
        _pip_venv_warned = True
        warn("Installing packages into your system Python.")
        info("This is fine for most users, but if you share this Python with")
        info("other projects, consider using a virtual environment:")
        info(f"  {sys.executable} -m venv yume-env")
        if IS_WIN:
            info("  yume-env\\Scripts\\activate")
        else:
            info("  source yume-env/bin/activate")
        print()
    return True


def install_python_deps():
    """Install flask + faster-whisper deps."""
    if not _check_pip():
        return False

    req = SERVER_DIR / "requirements.txt"
    if not req.exists():
        req = BASE_DIR / "requirements.txt"  # backward compat
    if not req.exists():
        SERVER_DIR.mkdir(parents=True, exist_ok=True)
        req.write_text("faster-whisper==1.2.1\nflask==3.1.3\nflask-cors==6.0.2\nwaitress==3.0.2\nnumpy==2.4.3\npykakasi==2.3.0\npypinyin==0.55.0\nromanization==2.0.0\n")

    # On Windows, check for C++ build tools before attempting install
    # (faster-whisper's CTranslate2 dependency may need compilation)
    if IS_WIN and not _has_build_tools():
        warn("C++ build tools not detected.")
        info("If the install fails, you may need Visual Studio Build Tools:")
        info("  https://visualstudio.microsoft.com/visual-cpp-build-tools/")
        info("  Select 'Desktop development with C++' during installation.")
        print()

    info("Installing Whisper server Python packages...")
    try:
        r = _run(
            [sys.executable, "-m", "pip", "install", "-r", str(req), "-q", "--no-warn-script-location"],
            timeout=600
        )
        if r.returncode != 0:
            error("pip install failed. Error output:")
            # Show the actual error so the user knows what went wrong
            stderr_text = (r.stderr or "").strip()
            if stderr_text:
                for line in stderr_text.splitlines()[-15:]:
                    print(f"    {C.DIM}{line}{C.RESET}")
            else:
                print(f"    {C.DIM}(no error output captured — try running with --verbose){C.RESET}")
            if IS_WIN and ("Microsoft Visual C++" in (r.stderr or "") or "cl.exe" in (r.stderr or "")):
                print()
                error("This looks like a missing C++ compiler.")
                info("Install Visual Studio Build Tools:")
                info("  https://visualstudio.microsoft.com/visual-cpp-build-tools/")
                info("  Select 'Desktop development with C++' during installation.")
            return False
        success("Done!")
        return True
    except Exception as e:
        error(f"Failed: {e}")
        info("Troubleshooting tips:")
        info(f"  {C.CYAN}1.{C.RESET} Check your internet connection")
        info(f"  {C.CYAN}2.{C.RESET} Try running as administrator (Windows) or with sudo (Linux/Mac)")
        info(f"  {C.CYAN}3.{C.RESET} Run manually: {C.BOLD}pip install -r server/requirements.txt{C.RESET}")
        return False

def _detect_package_manager():
    """Detect the system package manager."""
    for cmd, name in [("dnf", "dnf"), ("apt-get", "apt"), ("brew", "brew"), ("pacman", "pacman"), ("zypper", "zypper")]:
        if shutil.which(cmd):
            return name
    return None

def _install_build_tools():
    """Install C++ build tools (ninja, cmake, gcc) needed to compile llama-cpp-python from source."""
    pm = _detect_package_manager()
    if not pm:
        warn("No package manager found. Please install manually: ninja cmake gcc (or g++)")
        return False

    info(f"Installing build tools via {pm}...")

    cmds = {
        "dnf":    ["sudo", "dnf", "install", "-y", "ninja-build", "cmake", "gcc-c++", "gcc"],
        "apt":    ["sudo", "apt-get", "install", "-y", "ninja-build", "cmake", "g++", "gcc"],
        "brew":   ["brew", "install", "ninja", "cmake"],
        "pacman": ["sudo", "pacman", "-S", "--noconfirm", "ninja", "cmake", "gcc"],
        "zypper": ["sudo", "zypper", "install", "-y", "ninja", "cmake", "gcc-c++"],
    }

    cmd = cmds.get(pm)
    if not cmd:
        warn(f"Unknown package manager: {pm}")
        return False

    try:
        r = _run(cmd, timeout=300)
        if r.returncode == 0:
            success("Build tools installed!")
            return True
        else:
            warn(f"Package manager returned code {r.returncode}")
            # Try pip fallback for ninja/cmake
            info("Trying pip install ninja cmake as fallback...")
            _run([sys.executable, "-m", "pip", "install", "ninja", "cmake", "-q"], timeout=120)
            return True
    except Exception as e:
        warn(f"System install failed: {e}")
        # Pip fallback
        info("Trying pip install ninja cmake as fallback...")
        try:
            _run([sys.executable, "-m", "pip", "install", "ninja", "cmake", "-q"], timeout=120)
            return True
        except Exception:
            return False

def _has_build_tools():
    """Check if ninja and cmake are available."""
    return bool(shutil.which("ninja") or shutil.which("ninja-build")) and bool(shutil.which("cmake"))

def install_llamacpp_python():
    """Install llama-cpp-python + server deps (uvicorn, fastapi), with GPU support if NVIDIA detected."""
    if not _check_pip():
        return False

    gpu = detect_gpu()

    def _pip_install_with_progress(cmd, label, timeout=600, env=None):
        """Run pip install showing elapsed time so user knows it's not frozen."""
        info(f"{label}...")
        info(f"{C.DIM}This may take several minutes for large downloads. Please wait.{C.RESET}")
        t0 = time.time()
        stop_event = threading.Event()
        def _timer():
            while not stop_event.is_set():
                elapsed = int(time.time() - t0)
                m, s = divmod(elapsed, 60)
                sys.stdout.write(f"\r  {C.CYAN}...{C.RESET} {m}m{s:02d}s elapsed   ")
                sys.stdout.flush()
                stop_event.wait(5)
        t = threading.Thread(target=_timer, daemon=True)
        t.start()
        try:
            kw = {"timeout": timeout}
            if env is not None:
                kw["env"] = env
            r = _run(cmd, **kw)
            stop_event.set()
            elapsed = int(time.time() - t0)
            m, s = divmod(elapsed, 60)
            sys.stdout.write(f"\r{' ' * 40}\r")
            sys.stdout.flush()
            if r.returncode == 0:
                success(f"{label} — done in {m}m{s:02d}s")
                return True
            else:
                error(f"{label} — failed after {m}m{s:02d}s")
                stderr_text = (r.stderr or "").strip()
                if stderr_text:
                    for line in stderr_text.splitlines()[-10:]:
                        print(f"    {C.DIM}{line}{C.RESET}")
                return False
        except subprocess.TimeoutExpired:
            stop_event.set()
            error(f"{label} — timed out after {timeout//60} minutes")
            return False
        except Exception as e:
            stop_event.set()
            error(f"{label} — error: {e}")
            return False

    # Step 0: On Linux/macOS, ensure build tools exist (ninja, cmake, gcc)
    # Windows uses prebuilt wheels, so this is only needed on Unix
    if not IS_WIN:
        if not _has_build_tools():
            info("Build tools (ninja, cmake) needed to compile llama-cpp-python...")
            if not _install_build_tools():
                error("Cannot install build tools. Please install manually:")
                if IS_LIN:
                    pm = _detect_package_manager()
                    if pm == "dnf":
                        print("    sudo dnf install ninja-build cmake gcc-c++")
                    elif pm == "apt":
                        print("    sudo apt install ninja-build cmake g++")
                    else:
                        print("    Install: ninja-build cmake g++ (or gcc-c++)")
                elif IS_MAC:
                    print("    brew install ninja cmake")
                return False

    # Step 1: Install llama-cpp-python
    installed = False
    if gpu["has_nvidia"]:
        if IS_WIN:
            installed = _pip_install_with_progress([
                sys.executable, "-m", "pip", "install", "llama-cpp-python",
                "--extra-index-url", "https://abetlen.github.io/llama-cpp-python/whl/cu124",
                "-q", "--no-warn-script-location"
            ], "Installing llama-cpp-python (CUDA prebuilt)", timeout=600)

            if not installed:
                warn("Prebuilt failed, trying CPU version...")
        else:
            env = os.environ.copy()
            env["CMAKE_ARGS"] = "-DGGML_CUDA=on"
            installed = _pip_install_with_progress([
                sys.executable, "-m", "pip", "install", "llama-cpp-python",
                "--force-reinstall", "--no-cache-dir", "-q"
            ], "Building llama-cpp-python (CUDA from source)", timeout=600, env=env)

            if not installed:
                warn("CUDA build failed, trying CPU...")

    elif gpu["has_amd"]:
        info("AMD Radeon GPU detected. Installing llama-cpp-python with ROCm...")
        if IS_WIN:
            warn("ROCm is only supported on Linux for llama-cpp-python.")
            info("Falling back to CPU mode on Windows (AMD DirectML not yet supported).")
        else:
            # ROCm / HIP build
            env = os.environ.copy()
            env["CMAKE_ARGS"] = "-DGGML_HIP=on"
            installed = _pip_install_with_progress([
                sys.executable, "-m", "pip", "install", "llama-cpp-python",
                "--force-reinstall", "--no-cache-dir"
            ], "Building llama-cpp-python (ROCm/HIP from source)", timeout=900, env=env)

            if not installed:
                warn("ROCm build failed. Check that ROCm is installed:")
                info("  Fedora: sudo dnf install rocm-hip-runtime rocm-hip-sdk")
                info("  Ubuntu: https://rocm.docs.amd.com/en/latest/deploy/linux/")
                info("Falling back to CPU...")

    if not installed:
        # Try prebuilt CPU wheel first (no ninja/cmake/gcc needed!)
        installed = _pip_install_with_progress([
            sys.executable, "-m", "pip", "install", "llama-cpp-python",
            "--extra-index-url", "https://abetlen.github.io/llama-cpp-python/whl/cpu",
            "--no-warn-script-location"
        ], "Installing llama-cpp-python (prebuilt CPU)", timeout=300)

        if not installed:
            warn("Prebuilt wheel not available, building from source...")
            installed = _pip_install_with_progress([
                sys.executable, "-m", "pip", "install", "llama-cpp-python",
                "--no-warn-script-location"
            ], "Building llama-cpp-python (CPU from source)", timeout=900)

    # Step 2: Install server dependencies (uvicorn, fastapi, etc.)
    if installed:
        info("Installing server dependencies (uvicorn, fastapi)...")
        try:
            r = _run([
                sys.executable, "-m", "pip", "install",
                "uvicorn==0.42.0", "fastapi==0.135.1", "sse-starlette==3.3.3",
                "starlette-context==0.5.1", "pydantic-settings==2.13.1",
                "-q", "--no-warn-script-location"
            ], timeout=300)
            if r.returncode == 0:
                success("Server dependencies installed!")
            else:
                warn("Some server deps may have failed -- check manually")
        except Exception as e:
            warn(f"Server deps install issue: {e}")

    return installed

def pull_ollama_model(name):
    info(f"Pulling {name}...\n")
    try:
        p = _run(["ollama", "pull", name], timeout=3600)
        if p.returncode == 0:
            success(f"{name} ready!")
            return True
        error(f"Failed (code {p.returncode})")
    except FileNotFoundError:
        error("ollama not found")
    except subprocess.TimeoutExpired:
        error("Timed out")
    return False

# ────
# HUGGINGFACE MODEL BROWSER
# ────

def hf_list_gguf(repo):
    try:
        req = urllib.request.Request(
            f"https://huggingface.co/api/models/{repo}/tree/main",
            headers={"User-Agent": f"Yume/{VERSION}"}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            files = json.loads(resp.read())
        out = []
        for f in files:
            if f.get("path", "").endswith(".gguf"):
                sb = f.get("size", 0)
                sg = sb / GiB
                out.append({
                    "name": f["path"], "bytes": sb,
                    "size": f"{sg:.2f} GB" if sg >= 1 else f"{sb/MiB:.0f} MB"
                })
        return out
    except urllib.error.HTTPError as e:
        error(f"HuggingFace error: {e.code}")
        return []
    except Exception as e:
        error(f"Failed: {e}")
        return []

def hf_download(repo, filename):
    GGUF_DIR.mkdir(parents=True, exist_ok=True)
    return download_file(
        f"https://huggingface.co/{repo}/resolve/main/{filename}",
        GGUF_DIR / filename,
        f"Downloading {filename}"
    )

def browse_hf(cfg):
    while True:
        header("Download GGUF Model from HuggingFace")
        gpu = detect_gpu()
        if gpu["has_nvidia"]:
            info(f"Your GPU: {gpu['name']} ({gpu['vram_mb']} MB VRAM)\n")
            info("Quantization guide:")
            bullet("Q4_K_M  ~4 bits -- best quality/speed balance")
            bullet("Q5_K_M  ~5 bits -- slightly better quality")
            bullet("Q8_0    ~8 bits -- near-original, 2x size")
            bullet("Q3_K_S  ~3 bits -- smallest, lower quality")
            print()
            bullet(f"7B Q4_K_M ~ 4.4 GB -> fits {C.GREEN}6 GB{C.RESET} VRAM")
            bullet(f"7B Q8_0 ~ 7.7 GB -> needs {C.YELLOW}8 GB{C.RESET} VRAM")
            bullet(f"14B Q4_K_M ~ 8.7 GB -> needs {C.YELLOW}10 GB{C.RESET} VRAM")

        print()
        info("Recommended repos for Japanese -> English:")
        print()
        bullet(f"{C.CYAN}Qwen/Qwen2.5-7B-Instruct-GGUF{C.RESET}       -- best JP->EN, 7B")
        bullet(f"{C.CYAN}Qwen/Qwen2.5-14B-Instruct-GGUF{C.RESET}      -- more accurate, 14B")
        bullet(f"{C.CYAN}Qwen/Qwen2.5-3B-Instruct-GGUF{C.RESET}       -- lighter, 3B")
        bullet(f"{C.CYAN}bartowski/gemma-2-9b-it-GGUF{C.RESET}         -- Google Gemma 2, 9B")
        print()
        repo = ask_input("HuggingFace repo (owner/model-name)", "")
        if not repo:
            return

        info(f"Fetching files from {repo}...")
        files = hf_list_gguf(repo)
        if not files:
            warn("No .gguf files found. Use a GGUF repo (usually has '-GGUF' suffix).")
            pause()
            continue

        print(f"\n  {C.BOLD}Files in {repo}:{C.RESET}\n")
        opts = []
        for f in files:
            sg = f["bytes"] / GiB
            fit = ""
            if gpu["has_nvidia"]:
                vg = gpu["vram_mb"] / KiB
                if sg * 1.15 < vg:
                    fit = f" {C.GREEN}+ fits GPU{C.RESET}"
                elif sg < vg:
                    fit = f" {C.YELLOW}~ tight{C.RESET}"
                else:
                    fit = f" {C.RED}x too large{C.RESET}"
            opts.append((f"{f['name']}  ({f['size']}){fit}", None))
        opts.append(("Back", None))

        ch = ask_choice("Select file:", opts, default=len(opts)-1)
        if ch == -1 or ch == len(files):
            continue

        sel = files[ch]
        print()
        info(f"File: {sel['name']}")
        info(f"Size: {sel['size']}")
        info(f"Dest: {GGUF_DIR / sel['name']}")
        print()

        if ask_yn(f"Download {sel['name']}?"):
            if hf_download(repo, sel["name"]):
                cfg["gguf_model_path"] = str(GGUF_DIR / sel["name"])
                cfg["translation_model"] = sel["name"]
                save_config(cfg)
                success(f"Saved to {GGUF_DIR / sel['name']}")
                if cfg["translation_backend"] != "llamacpp":
                    if ask_yn("Switch backend to llama.cpp to use this model?"):
                        bi = BACKEND_INFO["llamacpp"]
                        cfg["translation_backend"] = "llamacpp"
                        cfg["translation_host"] = bi["dh"]
                        cfg["translation_port"] = bi["dp"]
                        save_config(cfg)
        pause()
        return


# ────
# TOOLS MENU
# ────

def tools_menu(cfg):
    while True:
        header("Tools Management")
        yt = find_tool("yt-dlp")
        ff = find_tool("ffmpeg")
        dn = find_tool("deno")
        # Detect current browser from manifest
        _cur_browser = _detect_extension_browser()
        ch = ask_choice("Select a tool:", [
            (f"yt-dlp          {'OK' if yt else 'MISSING'}", "Downloads audio from YouTube and 1000+ video sites"),
            (f"FFmpeg          {'OK' if ff else 'MISSING'}", "Converts audio between formats (required)"),
            (f"Deno            {'OK' if dn else '--'}", "Helps bypass YouTube bot detection (optional)"),
            ("Translation Backend", "Choose how Yume translates: llama.cpp / Ollama / LM Studio / Custom"),
            ("Download Translation Model", "Browse and download GGUF models (the files your translator uses)"),
            ("Python Dependencies", "Install required Python packages + romanization libraries"),
            ("Test Translation", "Send a test sentence to check if translation is working"),
            ("Benchmark Whisper", "Measure how fast each speech recognition model runs on your hardware"),
            ("Detect Fonts", "Find subtitle-compatible fonts installed on your system"),
            (f"Browser Extension  [{_cur_browser}]", "Switch between Chrome and Firefox extension format"),
            ("Back", None),
        ], default=10)
        if ch == -1 or ch == 10:
            return
        elif ch == 0:
            _menu_ytdlp(cfg)
        elif ch == 1:
            _menu_ffmpeg()
        elif ch == 2:
            _menu_deno(cfg)
        elif ch == 3:
            _menu_backend(cfg)
        elif ch == 4:
            browse_hf(cfg)
        elif ch == 5:
            _menu_pydeps()
        elif ch == 6:
            _test_translation(cfg)
        elif ch == 7:
            benchmark_whisper(cfg)
        elif ch == 8:
            detect_fonts()
        elif ch == 9:
            _menu_browser_extension()

def _detect_extension_browser():
    """Detect whether the current manifest.json is Chrome or Firefox format."""
    manifest = EXT_DIR / "manifest.json"
    if not manifest.exists():
        return "unknown"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        if "browser_specific_settings" in data:
            return "Firefox"
        if data.get("background", {}).get("service_worker"):
            return "Chrome"
        if data.get("background", {}).get("scripts"):
            return "Firefox"
        return "Chrome"
    except Exception:
        return "unknown"


def _menu_browser_extension():
    """Switch extension manifest between Chrome (MV3 service worker) and Firefox (MV3 background scripts)."""
    header("Browser Extension Format")

    manifest_chrome = EXT_DIR / "manifest.json"
    manifest_firefox = EXT_DIR / "manifest_firefox.json"
    current = _detect_extension_browser()

    info(f"Current format: {C.BOLD}{current}{C.RESET}")
    print()
    info("Yume ships with two manifest files:")
    info(f"  {C.CYAN}manifest.json{C.RESET}         — Chrome / Brave / Edge (MV3 service worker)")
    info(f"  {C.CYAN}manifest_firefox.json{C.RESET}  — Firefox (MV3 background scripts)")
    print()
    info(f"{C.DIM}Switching replaces manifest.json with the right format for your browser.{C.RESET}")
    info(f"{C.DIM}A backup of the current manifest is saved as manifest_backup.json.{C.RESET}")
    print()

    if not manifest_firefox.exists():
        error("manifest_firefox.json not found in extension folder!")
        info("Re-extract Yume to get both manifest files.")
        pause()
        return

    ch = ask_choice("Switch to:", [
        ("Chrome / Brave / Edge", "MV3 with service_worker (default)"),
        ("Firefox", "MV3 with background scripts + gecko settings"),
        ("Back", None),
    ], default=0 if current == "Firefox" else 1)

    if ch == -1 or ch == 2:
        return

    if ch == 0 and current == "Chrome":
        info("Already using Chrome format.")
        pause()
        return
    if ch == 1 and current == "Firefox":
        info("Already using Firefox format.")
        pause()
        return

    # Backup current manifest
    backup = EXT_DIR / "manifest_backup.json"
    try:
        import shutil
        shutil.copy2(manifest_chrome, backup)
        info(f"Backup saved: {backup.name}")
    except Exception as e:
        warn(f"Backup failed: {e}")

    if ch == 1:
        # Switch to Firefox
        try:
            import shutil
            # Save current Chrome manifest as the chrome variant if it's currently Chrome
            if current == "Chrome":
                chrome_backup = EXT_DIR / "manifest_chrome.json"
                if not chrome_backup.exists():
                    shutil.copy2(manifest_chrome, chrome_backup)
            shutil.copy2(manifest_firefox, manifest_chrome)
            success("Switched to Firefox format!")
            info("Reload the extension in about:debugging to apply changes.")
        except Exception as e:
            error(f"Switch failed: {e}")
    elif ch == 0:
        # Switch to Chrome — restore from manifest_chrome.json if available
        chrome_backup = EXT_DIR / "manifest_chrome.json"
        if chrome_backup.exists():
            try:
                import shutil
                shutil.copy2(chrome_backup, manifest_chrome)
                success("Switched to Chrome format!")
                info("Reload the extension in chrome://extensions to apply changes.")
            except Exception as e:
                error(f"Switch failed: {e}")
        else:
            # No backup — generate Chrome manifest from Firefox by removing gecko bits
            try:
                data = json.loads(manifest_chrome.read_text(encoding="utf-8"))
                data.pop("browser_specific_settings", None)
                bg = data.get("background", {})
                if "scripts" in bg:
                    scripts = bg.pop("scripts")
                    bg["service_worker"] = scripts[0] if scripts else "js/background.js"
                manifest_chrome.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
                success("Switched to Chrome format!")
                info("Reload the extension in chrome://extensions to apply changes.")
            except Exception as e:
                error(f"Switch failed: {e}")

    pause()


def _menu_ytdlp(cfg):
    while True:
        header("yt-dlp")
        p = find_tool("yt-dlp")
        if p:
            success(f"Installed: {p}")
            try:
                v = _run([p, "--version"], timeout=5)
                info(f"Version: {v.stdout.strip()}")
            except Exception as e:
                _log.debug('[_menu_ytdlp] version-check failed: %s', e)

            info("yt-dlp supports 1000+ sites: YouTube, NicoNico, Bilibili, Twitch, etc.")
        else:
            warn("Not installed")
        ch = ask_choice("Options:", [
            ("Install / Update", "Download latest binary"),
            ("YouTube Auth", "Deno vs browser cookies"),
            ("Back", None)
        ], default=2)
        if ch == -1 or ch == 2:
            return
        elif ch == 0:
            install_ytdlp()
            pause()
        elif ch == 1:
            _menu_yt_auth(cfg)

def _menu_yt_auth(cfg):
    while True:
        header("YouTube Authentication")
        info("YouTube blocks automated downloads to prevent bots.")
        info("Yume needs a way to prove you're a real person.")
        print()
        info(f"{C.BOLD}Browser Cookies (recommended, default){C.RESET}")
        info(f"{C.DIM}  Borrows your YouTube login from Chrome/Firefox/Edge.{C.RESET}")
        info(f"{C.DIM}  Requirement: be logged into YouTube in your browser.{C.RESET}")
        info(f"{C.DIM}  No extra software needed. Works offline after login.{C.RESET}")
        print()
        info(f"{C.BOLD}Deno (advanced, no YouTube account needed){C.RESET}")
        info(f"{C.DIM}  Uses a small program (Deno) to solve YouTube's bot challenge.{C.RESET}")
        info(f"{C.DIM}  Generates a 'proof-of-origin' token without any login.{C.RESET}")
        info(f"{C.DIM}  Requires: internet connection + Deno installed (~35 MB).{C.RESET}")
        info(f"{C.DIM}  Yume runs a local server (port 4416) to generate tokens.{C.RESET}")
        print()
        cur = cfg.get("youtube_auth_method", "cookies")
        info(f"Current method: {C.BOLD}{cur}{C.RESET}")
        print()
        ch = ask_choice("Select method:", [
            ("Browser Cookies (recommended)", "Uses your browser's YouTube login. No extra software."),
            ("Deno (no account needed)", "Solves YouTube's bot challenge via a local server. Needs internet."),
            ("Back", None)
        ], default=0 if cur == "cookies" else 1)
        if ch == -1 or ch == 2:
            return
        elif ch == 0:
            cfg["youtube_auth_method"] = "cookies"
            save_config(cfg)
            browsers = ["chrome", "firefox", "edge", "brave", "opera", "chromium", "safari"]
            bc = ask_choice("Which browser are you logged into YouTube with?",
                [(b.capitalize(), None) for b in browsers] + [("Back", None)],
                default=0)
            if 0 <= bc < len(browsers):
                cfg["cookies_browser"] = browsers[bc]
                save_config(cfg)
                success(f"Using cookies from: {browsers[bc]}")
                info("Make sure you're logged into YouTube in that browser.")
            pause()
        elif ch == 1:
            cfg["youtube_auth_method"] = "deno"
            save_config(cfg)
            success("Set to Deno")
            if not find_tool("deno"):
                if ask_yn("Deno not installed. Download and set up now?"):
                    install_deno()
            else:
                # Check if bgutil server is set up
                bgutil_main = TOOLS_DIR / "bgutil-ytdlp-pot-provider" / "server" / "src" / "main.ts"
                if not bgutil_main.exists():
                    info("PO token server not set up yet.")
                    if ask_yn("Set up now? (downloads ~5 MB from GitHub)"):
                        install_deno()
            pause()

def _menu_ffmpeg():
    while True:
        header("FFmpeg")
        p = find_tool("ffmpeg")
        (success if p else warn)(f"{'Installed: '+p if p else 'Not installed'}")
        ch = ask_choice("Options:", [
            ("Install / Update", "Download latest static build"),
            ("Back", None)
        ], default=1)
        if ch == -1 or ch == 1:
            return
        elif ch == 0:
            install_ffmpeg()
            pause()

def _menu_deno(cfg):
    while True:
        header("Deno (YouTube Authentication)")
        p = find_tool("deno")
        (success if p else info)(f"{'Installed: '+p if p else 'Not installed'}")
        print()
        info("YouTube blocks automated downloads with a 'bot detection' challenge.")
        info("Deno is a JavaScript runtime that solves this challenge automatically.")
        info(f"{C.DIM}How it works: Deno runs YouTube's BotGuard script to generate a{C.RESET}")
        info(f"{C.DIM}'proof-of-origin' (PO) token that proves you're a real browser.{C.RESET}")
        info(f"{C.DIM}The bgutil-ytdlp-pot-provider plugin connects this to yt-dlp.{C.RESET}")
        print()

        # Check if bgutil plugin is installed (it's a yt-dlp plugin, not importable)
        bgutil_ok = False
        try:
            r = _run([sys.executable, "-m", "pip", "show", "bgutil-ytdlp-pot-provider"], timeout=10)
            bgutil_ok = r.returncode == 0
        except Exception:
            pass

        if p and bgutil_ok:
            success("PO token plugin: installed (YouTube auth fully working)")
        elif p:
            warn("Deno installed but PO token plugin missing")
            info(f"{C.DIM}Install it: pip install bgutil-ytdlp-pot-provider{C.RESET}")
        else:
            warn("Deno not installed — YouTube may block downloads")

        info(f"Current YouTube auth method: {C.BOLD}{cfg.get('youtube_auth_method', 'cookies')}{C.RESET}")
        ch = ask_choice("Options:", [
            ("Install Deno + PO token plugin", "Downloads Deno (~35 MB) and installs the YouTube auth plugin"),
            ("Install PO token plugin only", "If Deno is already installed, just add the yt-dlp plugin"),
            ("Switch to browser cookies", "Use your browser's YouTube login instead of Deno"),
            ("Back", None)
        ], default=3)
        if ch == -1 or ch == 3:
            return
        elif ch == 0:
            install_deno()
            cfg["youtube_auth_method"] = "deno"
            save_config(cfg)
            pause()
        elif ch == 1:
            info("Installing PO token plugin...")
            try:
                r = _run([
                    sys.executable, "-m", "pip", "install", "-q",
                    "--no-warn-script-location", "bgutil-ytdlp-pot-provider"
                ], timeout=120)
                if r.returncode == 0:
                    success("PO token plugin installed")
                    cfg["youtube_auth_method"] = "deno"
                    save_config(cfg)
                else:
                    error("Install failed")
            except Exception as e:
                error(f"Failed: {e}")
            pause()
        elif ch == 2:
            cfg["youtube_auth_method"] = "cookies"
            save_config(cfg)
            success("Switched to cookies")
            pause()

def _menu_backend(cfg):
    while True:
        header("Translation Backend")
        cur = cfg.get("translation_backend", "llamacpp")
        bi = BACKEND_INFO.get(cur, BACKEND_INFO["custom"])
        info(f"Current: {C.BOLD}{bi['name']}{C.RESET}")
        info(f"Address: {C.CYAN}{cfg.get('translation_host', '127.0.0.1')}:{cfg.get('translation_port', DEFAULT_TRANSLATION_PORT)}{C.RESET}")
        st = check_translation_server(cfg.get("translation_host", "127.0.0.1"), cfg.get("translation_port", DEFAULT_TRANSLATION_PORT), bi)
        (success if st["up"] else warn)(f"Status: {'RUNNING' if st['up'] else 'Not running'}")

        ch = ask_choice("Options:", [
            ("Change backend", "Switch between llama.cpp/Ollama/LM Studio/WebUI/Custom"),
            ("Change address", f"Currently {cfg.get('translation_host')}:{cfg.get('translation_port')}"),
            ("Install instructions", f"How to set up {bi['name']}"),
            ("Manage model", "Pull, change, browse, or download models"),
            ("Back", None)
        ], default=4)
        if ch == -1 or ch == 4:
            return
        elif ch == 0:
            _select_backend(cfg)
        elif ch == 1:
            _change_addr(cfg, "translation")
        elif ch == 2:
            header(f"Install {bi['name']}")
            print(f"\n  {C.BOLD}{bi['name']}{C.RESET}\n  {bi['desc']}\n\n  {C.WHITE}Installation:{C.RESET}")
            for line in bi["inst"].split("\n"):
                print(f"  {line}")
            if cur == "llamacpp":
                print()
                if ask_yn("Install llama-cpp-python now?"):
                    install_llamacpp_python()
            elif cur == "ollama":
                print()
                if ask_yn("Auto-install Ollama now?"):
                    install_ollama()
            pause()
        elif ch == 3:
            _manage_model(cfg)

def _select_backend(cfg):
    header("Select Backend")
    keys = list(BACKEND_INFO.keys())
    opts = [(BACKEND_INFO[k]["name"], BACKEND_INFO[k]["desc"]) for k in keys] + [("Back", None)]
    cur = cfg.get("translation_backend", "llamacpp")
    di = keys.index(cur) if cur in keys else 0
    ch = ask_choice("Choose:", opts, default=di)
    if ch == -1 or ch == len(keys):
        return
    k = keys[ch]
    bi = BACKEND_INFO[k]
    cfg["translation_backend"] = k
    cfg["translation_host"] = bi["dh"]
    cfg["translation_port"] = bi["dp"]
    save_config(cfg)
    success(f"Backend: {bi['name']}  ({bi['dh']}:{bi['dp']})")
    print(f"\n  {C.WHITE}Installation:{C.RESET}")
    for line in bi["inst"].split("\n"):
        print(f"  {line}")
    pause()

def _change_addr(cfg, prefix):
    ch = cfg.get(f"{prefix}_host", "127.0.0.1")
    cp = cfg.get(f"{prefix}_port", DEFAULT_TRANSLATION_PORT)
    print(f"\n  Current: {C.CYAN}{ch}:{cp}{C.RESET}\n")
    raw_host = ask_input("Host", ch)
    host = validate_host(raw_host)
    if host is None:
        warn(f"Keeping current host: {ch}")
        host = ch
    raw_port = ask_input("Port", str(cp))
    port = validate_port(raw_port, f"{prefix.title()} port")
    if port is None:
        warn(f"Keeping current port: {cp}")
        port = cp
    else:
        if not is_port_free(port, host):
            pid, name = get_port_process(port)
            warn(f"Port {port} is in use by {name or 'unknown'} (PID {pid or '?'})")
            if not ask_yn(f"Use port {port} anyway?", False):
                free = find_free_port(port + 1)
                if free:
                    info(f"Suggestion: port {free} is available")
                    if ask_yn(f"Use {free} instead?"):
                        port = free
                    else:
                        port = cp
                else:
                    port = cp
    cfg[f"{prefix}_host"] = host
    cfg[f"{prefix}_port"] = port
    save_config(cfg)
    success(f"Set to {host}:{port}")
    pause()

def _manage_model(cfg):
    while True:
        header("Manage Translation Model")
        bk = cfg.get("translation_backend", "llamacpp")
        mdl = cfg.get("translation_model", "")
        gp = cfg.get("gguf_model_path", "")
        bi = BACKEND_INFO.get(bk, {})
        info(f"Backend: {C.BOLD}{bi.get('name', bk)}{C.RESET}")
        if mdl:
            info(f"Model:   {C.BOLD}{mdl}{C.RESET}")
        if gp:
            info(f"GGUF:    {gp}")

        gf = find_gguf_models()
        if gf:
            print()
            info(f"GGUF files in {GGUF_DIR}:")
            for f in gf:
                sg = f.stat().st_size / GiB
                act = f" {C.GREEN}<- active{C.RESET}" if str(f) == gp else ""
                bullet(f"{f.name}  ({sg:.2f} GB){act}")

        if bk == "ollama":
            ms = check_ollama_models(cfg.get("translation_host", "127.0.0.1"), cfg.get("translation_port", DEFAULT_OLLAMA_PORT))
            if ms:
                print()
                info("Ollama models:")
                for m in ms:
                    act = f" {C.GREEN}<- active{C.RESET}" if m == mdl or m.startswith(mdl.split(":")[0] if ":" in mdl else mdl) else ""
                    bullet(f"{m}{act}")

        ch = ask_choice("Options:", [
            ("Change model name", "Enter model name manually"),
            ("Pull Ollama model", "Download via ollama pull"),
            ("Download GGUF from HuggingFace", "Browse repos and pick files"),
            ("Select local GGUF file", f"{len(gf)} file(s) in models/translation/"),
            ("Back", None)
        ], default=4)
        if ch == -1 or ch == 4:
            return
        elif ch == 0:
            nm = ask_input("Model name", mdl)
            if nm:
                cfg["translation_model"] = nm
                save_config(cfg)
                success(f"Model: {nm}")
            pause()
        elif ch == 1:
            mn = ask_input("Ollama model to pull", mdl or "qwen2.5:7b")
            if mn:
                if not check_server(cfg.get("translation_host", "127.0.0.1"), cfg.get("translation_port", DEFAULT_OLLAMA_PORT), HEALTH_PATH_OLLAMA)["up"]:
                    info("Starting Ollama...")
                    try:
                        subprocess.Popen(["ollama", "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        time.sleep(3)
                    except Exception:
                        error("Could not start Ollama")
                        pause()
                        continue
                pull_ollama_model(mn)
                cfg["translation_model"] = mn
                save_config(cfg)
            pause()
        elif ch == 2:
            browse_hf(cfg)
        elif ch == 3:
            if not gf:
                warn(f"No .gguf files in {GGUF_DIR}")
                pause()
                continue
            fo = [(f"{f.name} ({f.stat().st_size/GiB:.2f} GB)", None) for f in gf] + [("Back", None)]
            fc = ask_choice("Select:", fo, default=len(fo)-1)
            if 0 <= fc < len(gf):
                cfg["gguf_model_path"] = str(gf[fc])
                cfg["translation_model"] = gf[fc].stem
                save_config(cfg)
                success(f"Selected: {gf[fc].name}")
                if cfg["translation_backend"] != "llamacpp":
                    if ask_yn("Switch to llama.cpp backend?"):
                        bi2 = BACKEND_INFO["llamacpp"]
                        cfg["translation_backend"] = "llamacpp"
                        cfg["translation_host"] = bi2["dh"]
                        cfg["translation_port"] = bi2["dp"]
                        save_config(cfg)
            pause()

def _menu_pydeps():
    header("Python Dependencies")
    section("Core (required)")
    deps = {}
    for p in ["faster_whisper", "flask", "flask_cors", "llama_cpp", "uvicorn", "fastapi"]:
        try:
            __import__(p)
            deps[p] = True
        except ImportError:
            deps[p] = False
    for p, ok in deps.items():
        (success if ok else warn)(f"{p}: {'installed' if ok else 'NOT installed'}")
    if all(deps.values()):
        success("All core deps installed!")
    elif ask_yn("Install missing core deps?"):
        install_python_deps()
        if not deps.get("llama_cpp", False):
            install_llamacpp_python()

    # Romanization libraries
    print()
    section("Romanization (optional — faster romaji/pinyin)")
    roma_deps = {"pykakasi": False, "pypinyin": False, "romanization": False}
    roma_desc = {
        "pykakasi": "Japanese kanji → romaji (instant, no LLM needed)",
        "pypinyin": "Chinese hanzi → pinyin (instant, no LLM needed)",
        "romanization": "Korean hangul → romanization (instant, no LLM needed)",
    }
    for p in roma_deps:
        try:
            __import__(p)
            roma_deps[p] = True
        except ImportError:
            pass
    for p, ok in roma_deps.items():
        if ok:
            success(f"{p}: installed — {roma_desc[p]}")
        else:
            info(f"{p}: {C.DIM}not installed{C.RESET} — {roma_desc[p]}")

    if all(roma_deps.values()):
        success("All romanization libs installed — instant romaji/pinyin/romanization!")
    else:
        print()
        info(f"{C.DIM}Without these, Japanese kana still gets romaji via WanaKana (bundled),{C.RESET}")
        info(f"{C.DIM}but kanji readings, Chinese pinyin, and Korean need the LLM (slower).{C.RESET}")
        info(f"{C.DIM}Korean and Russian also have built-in client-side romanization.{C.RESET}")
        if ask_yn("Install romanization libraries? (recommended)"):
            _check_pip()
            r = _run([
                sys.executable, "-m", "pip", "install",
                "pykakasi==2.3.0", "pypinyin==0.55.0", "romanization==2.0.0",
                "-q", "--no-warn-script-location"
            ], timeout=120)
            if r.returncode == 0:
                success("Romanization libraries installed! Restart server to activate.")
            else:
                warn("Some libraries failed to install. Check pip output above.")
    pause()

def _test_translation(cfg):
    header("Test Translation")
    bk = cfg.get("translation_backend", "llamacpp")
    h = cfg.get("translation_host", "127.0.0.1")
    p = cfg.get("translation_port", DEFAULT_TRANSLATION_PORT)
    m = cfg.get("translation_model", "")
    bi = BACKEND_INFO.get(bk, BACKEND_INFO["custom"])

    info(f"Backend: {bi['name']} ({h}:{p})")
    if m:
        info(f"Model: {m}")
    print()

    info("Checking server connectivity...")
    st = check_translation_server(h, p, bi)
    if not st["up"]:
        error(f"Server not reachable at {h}:{p}")
        if bk == "llamacpp":
            warn("Make sure you launched Yume first (main menu → Launch Yume)")
            info(f"{C.DIM}The translation server starts automatically when you launch.{C.RESET}")
        pause()
        return
    success("Server reachable!")

    txt = ask_input("Test sentence (Japanese)", "\u4eca\u65e5\u306f\u3044\u3044\u5929\u6c17\u3067\u3059\u306d")
    info(f"Sending: {txt}")
    print()
    try:
        body = {
            "messages": [
                {"role": "system", "content": "You are a translation system. Output ONLY the English translation."},
                {"role": "user", "content": txt}
            ],
            "max_tokens": 200, "temperature": 0.1, "stream": False
        }
        if bk == "ollama":
            body["model"] = m
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            f"http://{h}:{p}{bi['ap']}", data=data,
            headers={"Content-Type": "application/json", "User-Agent": "Yume"},
            method="POST"
        )
        info("Waiting...")
        with urllib.request.urlopen(req, timeout=60) as resp:
            result = json.loads(resp.read())
        tr = ""
        if "choices" in result and result["choices"]:
            tr = result["choices"][0].get("message", {}).get("content", "")
        elif "message" in result:
            tr = result["message"].get("content", "")
        if tr:
            print()
            success(f"Translation: {C.BOLD}{tr.strip()}{C.RESET}")
            print()
            success("Pipeline working!")
        else:
            warn("Got response but no translation text")
    except Exception as e:
        error(f"Failed: {e}")
    pause()




def rotate_logs(max_size_mb=10, keep=3):
    """Rotate log files if they exceed max_size_mb. Keep N backups."""
    for name in ["whisper_server.log", "translation_server.log"]:
        lp = LOGS_DIR / name
        if not lp.exists():
            continue
        size_mb = lp.stat().st_size / MiB
        if size_mb < max_size_mb:
            continue
        # Rotate: .log.2 -> .log.3, .log.1 -> .log.2, .log -> .log.1
        for i in range(keep, 0, -1):
            old = LOGS_DIR / f"{name}.{i}"
            new = LOGS_DIR / f"{name}.{i+1}"
            if old.exists():
                if i == keep:
                    old.unlink()  # delete oldest
                else:
                    old.rename(new)
        lp.rename(LOGS_DIR / f"{name}.1")
        info(f"Rotated {name} ({size_mb:.1f} MB)")


def recommend_whisper_model(gpu_info=None):
    """Pick best model for available VRAM."""
    if gpu_info is None:
        gpu_info = detect_gpu()
    vram = gpu_info.get("vram_mb", 0)
    has_gpu = gpu_info.get("has_nvidia") or gpu_info.get("has_amd")
    ram = detect_ram_gb()

    if not has_gpu:
        # CPU mode
        if ram >= 16:
            return "small", "CPU with 16+ GB RAM → small model (best CPU balance)"
        elif ram >= 8:
            return "base", "CPU with 8-16 GB RAM → base model"
        else:
            return "tiny", "CPU with <8 GB RAM → tiny model (fastest)"
    else:
        # GPU mode — turbo sits between distil and large-v3
        if vram >= 10000:
            return "large-v3", f"GPU with {vram} MB VRAM → large-v3 (best accuracy)"
        elif vram >= 7000:
            return "large-v3-turbo", f"GPU with {vram} MB VRAM → turbo (near-v3 accuracy, 2x faster)"
        elif vram >= 5000:
            return "distil-large-v3", f"GPU with {vram} MB VRAM → distil-large-v3 (fast + accurate)"
        elif vram >= 4000:
            return "small", f"GPU with {vram} MB VRAM → small (recommended)"
        elif vram >= 2000:
            return "base", f"GPU with {vram} MB VRAM → base"
        else:
            return "tiny", f"GPU with {vram} MB VRAM → tiny"




def check_for_updates():
    """Check GitHub for newer Yume releases."""
    try:
        req = urllib.request.Request(
            "https://api.github.com/repos/jenox645/Yume/releases/latest",
            headers={"User-Agent": f"Yume/{VERSION}", "Accept": "application/vnd.github.v3+json"}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            latest = data.get("tag_name", "").lstrip("v")
            if latest and latest != VERSION:
                return latest, data.get("html_url", "")
            return None, None
    except Exception:
        return None, None


def discover_servers(cfg):
    """Check if servers are already running (e.g. user started Ollama externally)."""
    results = {}
    # Check whisper
    ws = check_server(cfg["whisper_host"], cfg["whisper_port"], "/health")
    results["whisper"] = ws["up"]

    # Check translation - try multiple health endpoints
    bk = cfg.get("translation_backend", "llamacpp")
    bi = BACKEND_INFO.get(bk, BACKEND_INFO["custom"])
    ts = check_server(cfg["translation_host"], cfg["translation_port"], bi["hp"])
    if not ts["up"]:
        ts = check_server(cfg["translation_host"], cfg["translation_port"], HEALTH_PATH_OPENAI)
    results["translation"] = ts["up"]

    # Check common ports for Ollama
    if not results["translation"] and bk == "ollama":
        for port in [DEFAULT_OLLAMA_PORT, DEFAULT_TRANSLATION_PORT, 8080]:
            if port != cfg["translation_port"]:
                ts2 = check_server("127.0.0.1", port, HEALTH_PATH_OLLAMA)
                if ts2["up"]:
                    results["ollama_found"] = port
                    break
    return results


def health_check(cfg):
    """Comprehensive health check — tests every component end-to-end."""
    header("Health Check")
    results = []

    # 1. System resources
    gpu = detect_gpu()
    ram = detect_ram_gb()
    disk = disk_free_gb()
    results.append(("GPU detection", True, gpu["name"] or "CPU mode"))
    results.append(("RAM ≥ 4 GB", ram >= 4, f"{ram:.1f} GB"))
    results.append(("Disk ≥ 5 GB", disk >= 5, f"{disk:.1f} GB free"))

    # 2. Tools
    for t in ["yt-dlp", "ffmpeg", "ffprobe"]:
        p = find_tool(t)
        if p:
            # Verify tool actually runs (not just exists)
            r = _run([p, "--version" if t == "yt-dlp" else "-version"], timeout=5)
            if r.returncode == 0:
                results.append((f"{t} installed", True, p))
            else:
                results.append((f"{t} installed", False, f"found at {p} but fails to run"))
        else:
            results.append((f"{t} installed", False, "not found"))
    dn = find_tool("deno")
    if cfg.get("youtube_auth_method") != "cookies":
        results.append(("deno installed", dn is not None, dn or "not found (needed for YouTube)"))

    # 3. Python packages
    # Check pip first
    pip_r = _run([sys.executable, "-m", "pip", "--version"], timeout=10)
    results.append(("pip available", pip_r.returncode == 0,
                     pip_r.stdout.split()[1] if pip_r.returncode == 0 else "not found — install python3-pip"))
    for pkg, name in [("faster_whisper", "faster-whisper"), ("flask", "Flask"),
                       ("flask_cors", "Flask-CORS"), ("llama_cpp", "llama-cpp-python")]:
        try:
            __import__(pkg)
            results.append((f"{name}", True, "installed"))
        except ImportError:
            results.append((f"{name}", False, "not installed"))

    # Romanization libraries (optional but recommended)
    roma_libs = [("pykakasi", "Japanese romaji (kanji→reading)"),
                 ("pypinyin", "Chinese pinyin"),
                 ("romanization", "Korean romanization")]
    roma_installed = 0
    for pkg, desc in roma_libs:
        try:
            __import__(pkg)
            results.append((f"{pkg}", True, f"installed — {desc}"))
            roma_installed += 1
        except ImportError:
            results.append((f"{pkg}", True, f"not installed (optional) — {desc}"))
    if roma_installed == 0:
        info(f"\n  {C.DIM}Tip: Install romanization libraries for instant romaji/pinyin:{C.RESET}")
        info(f"  {C.CYAN}pip install pykakasi pypinyin romanization{C.RESET}")
        info(f"  {C.DIM}Without them, Yume uses the LLM for romanization (slower).{C.RESET}")
        info(f"  {C.DIM}Japanese kana→romaji works without libraries (WanaKana, bundled).{C.RESET}")
        info(f"  {C.DIM}Only kanji readings need pykakasi.{C.RESET}\n")

    # 4. Config validity
    c = load_config()
    results.append(("Config loadable", bool(c), "OK" if c else "corrupted"))
    wp = c.get("whisper_port", DEFAULT_WHISPER_PORT)
    tp = c.get("translation_port", DEFAULT_TRANSLATION_PORT)
    results.append(("Ports different", wp != tp, f"whisper:{wp} translation:{tp}"))

    # 5. Port availability
    results.append((f"Whisper port {wp} free", is_port_free(wp), "available" if is_port_free(wp) else "in use"))
    results.append((f"Translation port {tp} free", is_port_free(tp), "available" if is_port_free(tp) else "in use"))

    # 6. Live server connectivity (only if ports are in use — servers may be running)
    if not is_port_free(wp):
        ws = check_server(c.get("whisper_host", "127.0.0.1"), wp, "/health")
        if ws["up"]:
            status = ws.get("data", {}).get("status", "unknown")
            results.append(("Whisper server responding", True, f"port {wp} — status: {status}"))
        else:
            results.append(("Whisper server responding", False,
                            f"port {wp} in use but /health failed — restart with: python pocket_yume.py launch"))
    if not is_port_free(tp):
        bi = BACKEND_INFO.get(c.get("translation_backend", "llamacpp"), BACKEND_INFO.get("custom", {"hp": "/health"}))
        ts = check_translation_server(c.get("translation_host", "127.0.0.1"), tp, bi)
        if ts["up"]:
            results.append(("Translation server responding", True, f"port {tp} — OK"))
        else:
            results.append(("Translation server responding", False,
                            f"port {tp} in use but not responding — check if your LLM backend is running"))

    # 7. Server files
    ss = SERVER_DIR / "faster_whisper_server.py"
    if not ss.exists():
        ss = BASE_DIR / "faster_whisper_server.py"  # backward compat
    results.append(("Whisper server script", ss.exists(), str(ss) if ss.exists() else "MISSING"))

    # 7. GGUF model (if llamacpp backend)
    if cfg.get("translation_backend") == "llamacpp":
        gf = find_gguf_models()
        results.append(("GGUF model present", len(gf) > 0,
                        gf[0].name if gf else "none in models/translation/"))

    # Display results
    ok = sum(1 for _, p, _ in results if p)
    total = len(results)
    table(
        ["Check", "Result", "Details"],
        [[name, f"{C.GREEN}PASS{C.RESET}" if passed else f"{C.RED}FAIL{C.RESET}", detail]
         for name, passed, detail in results],
        col_styles=[C.RESET, C.RESET, C.DIM],
        title=f"Health Check — {ok}/{total} passed"
    )

    if ok == total:
        print()
        success("All checks passed! Yume is ready to launch.")
    else:
        print()
        fails = [name for name, p, _ in results if not p]
        warn(f"{total - ok} issue(s) found: {', '.join(fails)}")
        print()
        # Actionable solutions for common failures
        {name: detail for name, passed, detail in results if not passed}
        for fname in fails:
            if "yt-dlp" in fname or "ffmpeg" in fname or "ffprobe" in fname:
                tool = fname.split(" ")[0]
                info(f"{C.BOLD}{fname}{C.RESET} -> Run: {C.CYAN}python pocket_yume.py setup{C.RESET}")
                info(f"  Or download manually: {C.CYAN}https://github.com/yt-dlp/yt-dlp/releases{C.RESET}" if "yt-dlp" in tool else
                     f"  Or download manually: {C.CYAN}https://ffmpeg.org/download.html{C.RESET}")
            elif "faster-whisper" in fname or "Flask" in fname or "llama-cpp" in fname:
                info(f"{C.BOLD}{fname}{C.RESET} -> Run: {C.CYAN}python pocket_yume.py setup{C.RESET}")
            elif "port" in fname.lower() and "free" in fname.lower():
                port_num = re.search(r'\d+', fname)
                pn = port_num.group() if port_num else "?"
                info(f"{C.BOLD}{fname}{C.RESET} -> Port {pn} in use. Another Yume instance may be running.")
                info(f"  Run {C.CYAN}python pocket_yume.py launch{C.RESET} to auto-detect a free port.")
            elif "Whisper server responding" in fname:
                info(f"{C.BOLD}{fname}{C.RESET} -> Restart: {C.CYAN}python pocket_yume.py launch{C.RESET}")
                info(f"  Or check logs: {C.CYAN}logs/whisper_server.log{C.RESET}")
            elif "Translation server responding" in fname:
                info(f"{C.BOLD}{fname}{C.RESET} -> Make sure your LLM backend is running:")
                bk = c.get("translation_backend", "llamacpp")
                if bk == "llamacpp":
                    info(f"  Restart everything: {C.CYAN}python pocket_yume.py launch{C.RESET}")
                elif bk == "ollama":
                    info(f"  Start Ollama: {C.CYAN}ollama serve{C.RESET}")
                else:
                    info(f"  Start your {bk} server, then re-run health check.")
            elif "Whisper server script" in fname:
                info(f"{C.BOLD}{fname}{C.RESET} -> Re-extract Yume or run: {C.CYAN}python pocket_yume.py setup{C.RESET}")
            elif "Config" in fname:
                info(f"{C.BOLD}{fname}{C.RESET} -> Delete {C.CYAN}{CONFIG_FILE}{C.RESET} and re-run setup.")
            elif "RAM" in fname:
                info(f"{C.BOLD}{fname}{C.RESET} -> Close other applications to free up memory.")
                info(f"  Or use a smaller Whisper model: {C.CYAN}python pocket_yume.py settings{C.RESET}")
            elif "Disk" in fname:
                info(f"{C.BOLD}{fname}{C.RESET} -> Free up disk space. Yume needs ~5-10 GB for models and tools.")
        print()

    # Quick perf estimate
    gpu = detect_gpu()
    rec_model, rec_reason = recommend_whisper_model(gpu)
    print()
    panel(f"Recommended model: {C.BOLD}{rec_model}{C.RESET}\n{C.DIM}{rec_reason}{C.RESET}",
          title="Performance Estimate", style=C.CYAN)
    pause()



def settings_menu(cfg):
    while True:
        header("Settings")
        bi = BACKEND_INFO.get(cfg.get("translation_backend", "llamacpp"), BACKEND_INFO["custom"])
        ym = cfg['youtube_auth_method']
        if ym == 'cookies':
            ym += f" ({cfg.get('cookies_browser', 'chrome')})"

        # Resolve auto device/precision for display
        dev_raw = cfg['whisper_device']
        comp_raw = cfg['whisper_compute_type']
        gpu = detect_gpu()
        if dev_raw == "auto":
            if gpu["has_nvidia"]:
                dev_display = f"{C.GREEN}cuda{C.RESET} (auto)"
            elif gpu.get("has_amd") and not IS_WIN:
                dev_display = f"{C.RED}cuda{C.RESET} (auto, ROCm)"
            else:
                dev_display = "cpu (auto)"
        else:
            dev_display = dev_raw
        if comp_raw == "auto":
            resolved_comp = "float16" if "cuda" in dev_display and gpu.get("vram_mb", 0) >= 8000 else (
                "int8_float16" if "cuda" in dev_display else "int8")
            comp_display = f"{resolved_comp} (auto)"
        else:
            comp_display = comp_raw

        table(
            ["Setting", "Value"],
            [
                [f"{C.GOLD}Whisper Model{C.RESET}", cfg['whisper_model']],
                [f"{C.GOLD}Device / Precision{C.RESET}", f"{dev_display} / {comp_display}"],
                [f"{C.GOLD}Whisper Address{C.RESET}", f"{cfg['whisper_host']}:{cfg['whisper_port']}"],
                ["", ""],
                [f"{C.MAGENTA}Translation{C.RESET}", bi['name']],
                [f"{C.MAGENTA}TL Address{C.RESET}", f"{cfg['translation_host']}:{cfg['translation_port']}"],
                [f"{C.MAGENTA}TL Model{C.RESET}", cfg.get('translation_model', '—')],
                ["", ""],
                [f"{C.CYAN}Chunk Duration{C.RESET}", f"{cfg['chunk_duration']}s (audio processed in this many seconds at a time)"],
                [f"{C.DIM}Word Timestamps{C.RESET}", f"{C.DIM}{'Yes' if cfg['word_timestamps'] else 'No'} (no effect — server overrides for music optimization){C.RESET}"],
                [f"{C.DIM}Pause Threshold{C.RESET}", f"{C.DIM}{cfg['pause_threshold']}s (no effect — requires word_timestamps which is forced off){C.RESET}"],
                ["", ""],
                [f"{C.YELLOW}YouTube Auth{C.RESET}", ym],
            ],
            col_styles=[C.RESET, C.CYAN],
            title="Current Settings"
        )

        ch = ask_choice("Change:", [
            ("Whisper settings", "Speech recognition model, device, precision"),
            ("Translation settings", "Translation backend, address, model"),
            ("Server addresses", "Host/port for Whisper and Translation servers"),
            ("Subtitle tuning", "Chunk size, pause detection, word splitting"),
            ("Translation prompt", "Customize how the AI translates (tone, style, rules)"),
            ("Romanization prompt", "Customize how the AI romanizes non-Latin text"),
            ("YouTube auth", "How Yume accesses YouTube (Deno bot bypass or browser cookies)"),
            ("Export config", "Save settings to a backup file"),
            ("Import config", "Load settings from a backup file"),
            ("Reset to defaults", None),
            ("Back", None)
        ], default=10)
        if ch == -1 or ch == 10:
            return
        elif ch == 0:
            _set_whisper(cfg)
        elif ch == 1:
            _menu_backend(cfg)
        elif ch == 2:
            _set_addrs(cfg)
        elif ch == 3:
            _set_subs(cfg)
        elif ch == 4:
            _menu_translation_prompt(cfg)
        elif ch == 5:
            _menu_romanization_prompt(cfg)
        elif ch == 6:
            _menu_yt_auth(cfg)
        elif ch == 7:
            config_export(cfg)
            pause()
        elif ch == 8:
            # Find backup files
            backups = sorted(BASE_DIR.glob("yume_config_backup_*.json"), reverse=True)
            if backups:
                info("Found backup files:")
                for i, b in enumerate(backups[:5]):
                    bullet(f"{i+1}. {b.name}")
                choice = ask_input("File number or path", "1")
                try:
                    idx = int(choice) - 1
                    if 0 <= idx < len(backups):
                        imported = config_import(backups[idx])
                        if imported:
                            cfg.update(imported)
                except ValueError:
                    imported = config_import(choice)
                    if imported:
                        cfg.update(imported)
            else:
                path = ask_input("Path to config file", "")
                if path:
                    imported = config_import(path)
                    if imported:
                        cfg.update(imported)
            pause()
        elif ch == 9:
            if ask_yn("Reset ALL settings?", False):
                n = dict(DEFAULT_CONFIG)
                n["first_run_complete"] = True
                save_config(n)
                cfg.update(n)
                success("Reset!")
            pause()

def _set_whisper(cfg):
    header("Whisper Settings")
    gpu = detect_gpu()
    rec_model, rec_reason = recommend_whisper_model(gpu)
    if gpu["has_nvidia"]:
        info(f"GPU: {gpu['name']} ({gpu['vram_mb']} MB VRAM)")
        info("  VRAM = Video RAM, the memory on your graphics card used by AI models")
    info(f"Current model: {C.BOLD}{cfg['whisper_model']}{C.RESET}")
    info(f"Recommendation: {rec_model} ({rec_reason})")

    ch = ask_choice("What to change:", [
        ("Whisper model", f"Currently: {cfg['whisper_model']} — the AI that converts speech to text"),
        ("Device (CPU/GPU)", f"Currently: {cfg['whisper_device']} — where the AI runs"),
        ("Precision", f"Currently: {cfg['whisper_compute_type']} — speed vs accuracy trade-off"),
        ("Back", None),
    ], default=3)
    if ch == -1 or ch == 3:
        return
    elif ch == 0:
        _menu_whisper_model(cfg)
    elif ch == 1:
        dc = ask_choice("Where should Whisper run?", [
            ("Auto-detect", "Uses GPU if available, falls back to CPU"),
            ("GPU (NVIDIA CUDA)", "Fastest — requires an NVIDIA graphics card"),
            ("CPU", "Works on any computer, but slower"),
            ("Keep current", f"{cfg['whisper_device']}"),
        ], default=3)
        if 0 <= dc < 3:
            cfg["whisper_device"] = ["auto", "cuda", "cpu"][dc]
        save_config(cfg)
        success("Saved!")
        pause()
    elif ch == 2:
        cc = ask_choice("Precision (lower = faster but slightly less accurate):", [
            ("Auto", "Let Yume decide based on your hardware"),
            ("float16", "Full precision — best accuracy, needs ~4.5 GB VRAM on GPU"),
            ("int8_float16", "Mixed — good balance, needs ~3 GB VRAM"),
            ("int8", "Most compressed — fastest, works well on CPU"),
            ("Keep current", f"{cfg['whisper_compute_type']}"),
        ], default=4)
        if 0 <= cc < 4:
            cfg["whisper_compute_type"] = ["auto", "float16", "int8_float16", "int8"][cc]
        save_config(cfg)
        success("Saved!")
        pause()

def _menu_translation_prompt(cfg):
    """Edit the system prompt that controls how the AI translates subtitles."""
    header("Translation Prompt Editor")

    default_prompt = (
        "You are a {src}-to-{tgt} translation system. "
        "Output ONLY the {tgt} translation. Do NOT respond to the content. "
        "Do NOT add explanations. Do NOT answer questions. "
        "ONLY translate {src} to {tgt}."
    )
    current = cfg.get("translation_prompt", "")

    info("The translation prompt is the instruction sent to the AI before every subtitle.")
    info(f"{C.DIM}It controls how the translator behaves — its tone, style, and rules.{C.RESET}")
    print()

    info(f"{C.BOLD}Why the default prompt is written this way:{C.RESET}")
    info(f"  {C.DIM}• 'Output ONLY the translation'{C.RESET}")
    info(f"    {C.DIM}  → Prevents the AI from adding commentary or notes{C.RESET}")
    info(f"  {C.DIM}• 'Do NOT respond to the content'{C.RESET}")
    info(f"    {C.DIM}  → Stops the AI from answering questions it hears in the audio{C.RESET}")
    info(f"    {C.DIM}  → e.g. if someone says 'What time is it?', it translates, not answers{C.RESET}")
    info(f"  {C.DIM}• 'Do NOT add explanations'{C.RESET}")
    info(f"    {C.DIM}  → Prevents output like 'This means: ...' or 'Note: ...'{C.RESET}")
    info(f"  {C.DIM}• Short, assertive rules{C.RESET}")
    info(f"    {C.DIM}  → Work best with small local AI models (7B-13B parameters){C.RESET}")
    info(f"  {C.DIM}• {{src}} and {{tgt}} are placeholders{C.RESET}")
    info(f"    {C.DIM}  → Replaced with actual language names (e.g. Japanese, English){C.RESET}")
    print()

    if current:
        info(f"{C.BOLD}Current custom prompt:{C.RESET}")
        info(f"  {C.CYAN}{current}{C.RESET}")
    else:
        info(f"{C.BOLD}Using default prompt:{C.RESET}")
        info(f"  {C.CYAN}{default_prompt}{C.RESET}")

    print()
    ch = ask_choice("Options:", [
        ("Edit prompt", "Write your own translation instruction"),
        ("Reset to default", "Restore the built-in prompt"),
        ("View example prompts", "See templates for different styles"),
        ("Back", None)
    ], default=3)

    if ch == -1 or ch == 3:
        return
    elif ch == 0:
        info("Use {src} for source language and {tgt} for target language.")
        info(f"{C.DIM}Example: 'Translate {{src}} to {{tgt}}. Keep it casual.'{C.RESET}")
        new_prompt = ask_input("New prompt", current or default_prompt)
        if new_prompt:
            cfg["translation_prompt"] = new_prompt
            save_config(cfg)
            success("Prompt saved! Will be used for all future translations.")
        pause()
    elif ch == 1:
        cfg.pop("translation_prompt", None)
        save_config(cfg)
        success("Reset to default prompt.")
        pause()
    elif ch == 2:
        section("Example Prompts")
        examples = [
            ("Casual / informal",
             "Translate {src} to casual {tgt}. Use everyday language, contractions, and slang where appropriate. Output ONLY the translation."),
            ("Formal / literary",
             "Translate {src} to formal {tgt}. Use proper grammar and literary vocabulary. Output ONLY the translation."),
            ("Song lyrics (poetic)",
             "Translate these {src} song lyrics to {tgt}. Preserve poetic rhythm and feeling. Output ONLY the translation."),
            ("Keep honorifics (anime)",
             "Translate {src} to {tgt}. Keep Japanese honorifics (-san, -kun, -chan, -sama, -sensei) untranslated. Output ONLY the translation."),
            ("Technical / precise",
             "Translate {src} to {tgt}. Preserve technical terms and proper nouns exactly. Output ONLY the translation."),
        ]
        for i, (name, prompt) in enumerate(examples):
            info(f"  {C.BOLD}{i+1}. {name}{C.RESET}")
            info(f"     {C.DIM}{prompt}{C.RESET}")
            print()
        choice = ask_input("Use which? (number, or press Enter to go back)", "")
        if choice.strip().isdigit():
            idx = int(choice.strip()) - 1
            if 0 <= idx < len(examples):
                cfg["translation_prompt"] = examples[idx][1]
                save_config(cfg)
                success(f"Prompt set to: {examples[idx][0]}")
        pause()


def _menu_romanization_prompt(cfg):
    """Edit the system prompt that controls how the AI romanizes text.

    Japanese, Chinese, and Korean CAN use deterministic (library-based) romanization
    (pykakasi, pypinyin, romanization package) which ignores this prompt. But if those
    libraries aren't installed, they also fall back to LLM — so this prompt applies.
    All other non-Latin languages (Russian, Arabic, etc.) always use LLM romanization.
    """
    header("Romanization Prompt Editor")

    info("This prompt controls how the AI converts non-Latin text to Latin characters.")
    print()
    info(f"{C.BOLD}How romanization works per language:{C.RESET}")
    info(f"  {C.DIM}Japanese  → pykakasi library (instant, ignores this prompt){C.RESET}")
    info(f"  {C.DIM}Chinese   → pypinyin library (instant, ignores this prompt){C.RESET}")
    info(f"  {C.DIM}Korean    → romanization library (instant, ignores this prompt){C.RESET}")
    info(f"  {C.DIM}Russian, Arabic, and all other non-Latin languages{C.RESET}")
    info(f"  {C.DIM}  → Uses your translation LLM with this prompt{C.RESET}")
    info(f"  {C.DIM}If the instant libraries aren't installed, those languages also use the LLM.{C.RESET}")
    print()

    info(f"{C.BOLD}Available placeholders:{C.RESET}")
    info(f"  {C.DIM}{{src}} → source language name (e.g. 'Russian', 'Japanese'){C.RESET}")
    info(f"  {C.DIM}{{sys}} → romanization system name (e.g. 'transliteration', 'romaji'){C.RESET}")
    print()

    defaults = {
        "ru": "You are a Russian to Latin transliteration converter. Output ONLY the Latin transliteration of the Russian input. Do NOT translate. Do NOT add explanations. ONLY transliteration.",
        "ar": "You are an Arabic to Latin transliteration converter. Output ONLY the Latin transliteration of the Arabic input. Do NOT translate. Do NOT add explanations. ONLY transliteration.",
    }

    current = cfg.get("romanization_prompt", "")
    if current:
        info(f"{C.BOLD}Current custom prompt:{C.RESET}")
        info(f"  {C.CYAN}{current}{C.RESET}")
    else:
        info(f"{C.BOLD}Using built-in per-language prompts:{C.RESET}")
        for lang, prompt in defaults.items():
            info(f"  {C.DIM}{lang}: {prompt[:70]}...{C.RESET}")

    print()
    ch = ask_choice("Options:", [
        ("Edit prompt", "Write your own romanization instruction"),
        ("Reset to default", "Restore the built-in per-language prompts"),
        ("View example prompts", "See templates for different styles"),
        ("Back", None)
    ], default=3)

    if ch == -1 or ch == 3:
        return
    elif ch == 0:
        info("Use {src} for source language and {sys} for the romanization system name.")
        info(f"{C.DIM}Example: 'Convert {{src}} to Latin script using {{sys}}. Output ONLY the result.'{C.RESET}")
        new_prompt = ask_input("New prompt", current or "")
        if new_prompt:
            cfg["romanization_prompt"] = new_prompt
            save_config(cfg)
            success("Romanization prompt saved!")
        pause()
    elif ch == 1:
        cfg.pop("romanization_prompt", None)
        save_config(cfg)
        success("Reset to built-in per-language prompts.")
        pause()
    elif ch == 2:
        section("Example Prompts")
        examples = [
            ("Standard transliteration",
             "Convert {src} text to Latin characters using {sys}. Output ONLY the result. No translation. No explanations."),
            ("Phonetic (pronunciation-focused)",
             "Convert {src} to how it sounds in English letters. Prioritize pronunciation over spelling rules. Output ONLY the result."),
            ("Academic (strict system)",
             "Transliterate {src} to Latin using the ISO 9 standard. Be precise. Output ONLY the transliteration."),
        ]
        for i, (name, prompt) in enumerate(examples):
            info(f"  {C.BOLD}{i+1}. {name}{C.RESET}")
            info(f"     {C.DIM}{prompt}{C.RESET}")
            print()
        choice = ask_input("Use which? (number, or press Enter to go back)", "")
        if choice.strip().isdigit():
            idx = int(choice.strip()) - 1
            if 0 <= idx < len(examples):
                cfg["romanization_prompt"] = examples[idx][1]
                save_config(cfg)
                success(f"Prompt set to: {examples[idx][0]}")
        pause()
def _set_addrs(cfg):
    while True:
        header("Server Addresses")
        info(f"Whisper:     {C.CYAN}{cfg['whisper_host']}:{cfg['whisper_port']}{C.RESET}")
        info(f"Translation: {C.CYAN}{cfg['translation_host']}:{cfg['translation_port']}{C.RESET}")
        ch = ask_choice("Change:", [
            ("Whisper address", None), ("Translation address", None), ("Back", None)
        ], default=2)
        if ch == -1 or ch == 2:
            return
        elif ch == 0:
            _change_addr(cfg, "whisper")
        elif ch == 1:
            _change_addr(cfg, "translation")

def _set_subs(cfg):
    header("Subtitle Tuning")
    print()
    info("These settings control how Yume processes audio into subtitles.")
    print()

    info(f"{C.BOLD}Word-level timestamps{C.RESET}: Break subtitles at individual words instead of sentences.")
    info(f"  Currently: {'ON' if cfg['word_timestamps'] else 'OFF'}")
    cfg["word_timestamps"] = ask_yn("Enable word-level timestamps? (OFF is better for music)", cfg["word_timestamps"])

    print()
    info(f"{C.BOLD}Pause threshold{C.RESET}: How long a silence (in seconds) before Yume splits")
    info("  a subtitle into two lines. Lower = more lines, higher = longer subtitles.")
    info("  0.25s works well for songs, 0.4s works well for speech.")
    info(f"  Currently: {cfg['pause_threshold']}s")
    pt = ask_input("Pause threshold (0.2-1.0s)", str(cfg["pause_threshold"]))
    try:
        cfg["pause_threshold"] = float(pt)
    except Exception as e:
        _log.debug('[_set_subs] float-parse failed: %s', e)

    print()
    info(f"{C.BOLD}Chunk duration{C.RESET}: Yume processes audio in chunks of this many seconds.")
    info("  Smaller chunks = subtitles appear faster, but may cut words at boundaries.")
    info("  Larger chunks = more context for the AI, but longer wait for first subtitle.")
    info("  30s is the default and works well for most content.")
    info(f"  Currently: {cfg['chunk_duration']}s")
    cd = ask_input("Chunk duration (10-60s)", str(cfg["chunk_duration"]))
    try:
        cfg["chunk_duration"] = int(cd)
    except Exception as e:
        _log.debug('[_set_subs] int-parse failed: %s', e)

    save_config(cfg)
    success("Saved!")
    pause()

# ────
# BENCHMARK
# ────

WHISPER_MODELS_INFO = [
    # (name, params, vram, user-friendly description)
    ("tiny",            "39M params",   "~1 GB",   "Fastest, low accuracy"),
    ("base",            "74M params",   "~1 GB",   "Fast, decent accuracy"),
    ("small",           "244M params",  "~2 GB",   "Good balance of speed and accuracy"),
    ("medium",          "769M params",  "~5 GB",   "High accuracy, slower"),
    ("large-v2",        "1550M params", "~10 GB",  "Very high accuracy"),
    ("large-v3",        "1550M params", "~10 GB",  "Best accuracy (recommended if VRAM allows)"),
    ("large-v3-turbo",  "809M params",  "~6 GB",   "Near-v3 accuracy at 2x speed (best mid-range)"),
    ("distil-large-v2", "756M params",  "~4 GB",   "Compressed v2 -- 2x faster, similar accuracy"),
    ("distil-large-v3", "756M params",  "~4 GB",   "Compressed v3 -- 2x faster, similar accuracy"),
]


def _is_whisper_model_cached(model_name):
    """Check if a Whisper model is already downloaded in the HuggingFace cache."""
    try:
        # faster-whisper uses HuggingFace hub cache: ~/.cache/huggingface/hub/
        cache_dir = Path.home() / ".cache" / "huggingface" / "hub"
        if not cache_dir.exists():
            return False
        # Models are stored as 'models--Systran--faster-whisper-{model_name}'
        patterns = [
            f"models--Systran--faster-whisper-{model_name}",
            f"models--guillaumekln--faster-whisper-{model_name}",
            f"models--openai--whisper-{model_name}",
            f"models--mobiuslabsgmbh--faster-whisper-{model_name}",
        ]
        for entry in cache_dir.iterdir():
            for pat in patterns:
                if pat.lower() in entry.name.lower():
                    return True
        # Also check CTranslate2 local cache
        ct2_cache = Path.home() / ".cache" / "faster_whisper"
        if ct2_cache.exists():
            for entry in ct2_cache.iterdir():
                if model_name in entry.name:
                    return True
        return False
    except Exception:
        return False  # Can't tell — assume not cached

def benchmark_whisper(cfg):
    """Compare Whisper model speeds on this hardware."""
    header("Whisper Benchmark")
    info("This measures how fast each speech recognition model runs on your hardware.")
    info(f"{C.DIM}Yume processes audio in chunks. Faster models = subtitles appear sooner.{C.RESET}")
    info(f"{C.DIM}A model running at '10x realtime' transcribes 1 minute of audio in 6 seconds.{C.RESET}")
    print()

    # Check faster-whisper is installed
    try:
        import faster_whisper # noqa: F401
        success("faster-whisper found")
    except ImportError:
        error("faster-whisper not installed. Run: pip install faster-whisper")
        pause()
        return

    gpu = detect_gpu()
    if gpu["has_nvidia"]:
        info(f"GPU: {gpu['name']} ({gpu['vram_mb']} MB VRAM)")
    elif gpu.get("has_amd"):
        info(f"GPU: {gpu['name']} ({gpu.get('vram_mb', '?')} MB VRAM, ROCm)")
    else:
        info("No GPU detected — benchmarking in CPU mode")

    rec, reason = recommend_whisper_model(gpu)
    info(f"Recommended: {rec} ({reason})")
    print()

    # Generate test audio using ffmpeg (5s of speech-like noise)
    ffmpeg = find_tool("ffmpeg")
    test_wav = LOGS_DIR / "_benchmark_test.wav"
    if ffmpeg:
        try:
            # Generate 5s of sine wave at speech frequency (300Hz) with harmonics
            _run([
                ffmpeg, "-y", "-f", "lavfi", "-i",
                "sine=frequency=300:duration=5",
                "-ar", str(WHISPER_SAMPLE_RATE), "-ac", "1",
                str(test_wav)
            ], timeout=10)
        except Exception as e:
            _log.debug('[benchmark_whisper] test-audio-gen failed: %s', e)



    if not test_wav.exists():
        # Fallback: create raw audio in Python
        try:
            import struct
            import math
            sr = WHISPER_SAMPLE_RATE
            dur = 5
            samples = []
            for i in range(sr * dur):
                t = i / sr
                v = 0.5 * math.sin(2 * math.pi * 300 * t) + 0.3 * math.sin(2 * math.pi * 600 * t)
                samples.append(int(v * 32767))  # 16-bit PCM full scale
            import wave
            with wave.open(str(test_wav), "w") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(sr)
                wf.writeframes(struct.pack(f"<{len(samples)}h", *samples))
            success("Generated test audio (5s)")
        except Exception as e:
            error(f"Could not create test audio: {e}")
            pause()
            return

    # Select models to benchmark
    gpu_has = gpu.get("has_nvidia") or gpu.get("has_amd")
    vram = gpu.get("vram_mb", 0) if gpu_has else 0
    available = []
    for name, params, vram_req, desc in WHISPER_MODELS_INFO:
        # Parse VRAM requirement
        try:
            req_mb = int(vram_req.replace("~", "").replace("GB", "").strip()) * KiB
        except (ValueError, AttributeError):
            req_mb = 0
        fits = True
        if gpu_has and vram > 0 and req_mb > vram:
            fits = False
        if not gpu_has and req_mb > 5120:
            fits = False  # Skip large models on CPU
        available.append((name, params, vram_req, desc, fits))

    info("Select models to benchmark:")
    info("  'distil' models = compressed versions (same accuracy, 2x faster, half the size)")
    info("  'turbo' = optimized large-v3 (near-full accuracy at much higher speed)")
    info("  VRAM = your GPU's video memory — models marked [fits] will work on your hardware")
    print()
    for i, (name, params, vr, desc, fits) in enumerate(available):
        tag = f"{C.GREEN}fits{C.RESET}" if fits else f"{C.RED}may OOM{C.RESET}"
        cached = _is_whisper_model_cached(name)
        dl_tag = f"{C.GREEN}downloaded{C.RESET}" if cached else f"{C.DIM}not downloaded{C.RESET}"
        cur = f" {C.GOLD}<- current{C.RESET}" if name == cfg.get("whisper_model") else ""
        print(f"    {i+1:2d}. {name:22s} {vr:8s} [{tag}] [{dl_tag}]{cur}")
        print(f"        {C.DIM}{desc}{C.RESET}")
    print()

    selection = ask_input(
        "Enter model numbers (comma-separated) or 'rec' for recommended, 'all' for all that fit",
        "rec"
    ).strip().lower()

    models_to_test = []
    if selection == "all":
        models_to_test = [name for name, _, _, _, fits in available if fits]
    elif selection == "rec":
        models_to_test = [rec]
    else:
        for part in selection.split(","):
            part = part.strip()
            if part.isdigit():
                idx = int(part) - 1
                if 0 <= idx < len(available):
                    models_to_test.append(available[idx][0])

    if not models_to_test:
        warn("No models selected")
        pause()
        return

    info(f"Benchmarking {len(models_to_test)} model(s): {', '.join(models_to_test)}")

    # Check which models need downloading vs already cached
    needs_download = []
    for m in models_to_test:
        if not _is_whisper_model_cached(m):
            needs_download.append(m)

    if needs_download:
        warn(f"These models will be downloaded first: {', '.join(needs_download)}")
        if not ask_yn("Download and benchmark?", True):
            return
    else:
        info("All selected models are already downloaded (cached)")
        if not ask_yn("Start benchmark?", True):
            return

    # Determine device/compute
    if gpu.get("has_nvidia"):
        device = "cuda"
    elif gpu.get("has_amd") and not IS_WIN:
        device = "auto"  # ROCm: torch.cuda.is_available() resolves via HIP
        info("AMD GPU detected — benchmarking via ROCm/auto")
    else:
        device = "cpu"
    vram_mb = gpu.get("vram_mb", 0)
    if device in ("cuda", "auto") and vram_mb >= 8000:
        compute = "float16"
    elif device in ("cuda", "auto") and vram_mb >= 4000:
        compute = "int8_float16"
    else:
        compute = "int8"

    # Run benchmarks
    results = []
    total = len(models_to_test)
    for idx, model_name in enumerate(models_to_test):
        section(f"Testing {idx+1}/{total}: {model_name}")
        cached = _is_whisper_model_cached(model_name)
        if not cached:
            info(f"{C.DIM}Downloading model... (this may take a few minutes the first time){C.RESET}")
        info(f"Device: {device} | Precision: {compute}")

        try:
            from faster_whisper import WhisperModel

            # Suppress HuggingFace Hub warnings during download
            # (unauthenticated request warnings, Windows symlink warnings)
            import warnings
            warnings.filterwarnings("ignore", message=".*huggingface.*")
            warnings.filterwarnings("ignore", message=".*symlinks.*")
            os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")
            os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
            # Suppress HF logging (WARNING level messages about tokens/symlinks)
            logging.getLogger("huggingface_hub").setLevel(logging.ERROR)

            # Measure load time
            t0 = time.time()
            model = WhisperModel(model_name, device=device, compute_type=compute)
            load_time = time.time() - t0
            success(f"Loaded in {load_time:.1f}s")

            # Measure transcription (3 runs, take median for stability)
            info(f"{C.DIM}Running 3 transcription passes (5s test audio each)...{C.RESET}")
            times = []
            segments_count = 0
            for run in range(3):
                t0 = time.time()
                segs, seg_info = model.transcribe(
                    str(test_wav),
                    language="ja",
                    vad_filter=False,
                    word_timestamps=False,
                )
                # Consume the generator
                seg_list = list(segs)
                elapsed = time.time() - t0
                times.append(elapsed)
                segments_count = len(seg_list)

            median_time = sorted(times)[len(times) // 2]
            rtf = median_time / 5.0  # Real-time factor (5s audio)
            speed = 5.0 / median_time if median_time > 0 else 0

            results.append({
                "model": model_name,
                "load_s": round(load_time, 1),
                "median_s": round(median_time, 3),
                "rtf": round(rtf, 3),
                "speed_x": round(speed, 1),
                "segments": segments_count,
                "status": "OK",
            })
            success(f"Median: {median_time:.3f}s for 5s audio ({speed:.1f}x realtime)")

            # Free memory
            del model
            try:
                import gc
                gc.collect()
                if device == "cuda":
                    import torch
                    torch.cuda.empty_cache()
            except Exception as e:
                _log.debug('[benchmark_whisper] cuda-cleanup failed: %s', e)

        except Exception as e:
            err_msg = str(e)
            short_err = err_msg[:80]
            error(f"Failed: {short_err}")

            # Detect missing CUDA libraries and offer CPU fallback
            is_cuda_lib_error = any(lib in err_msg.lower() for lib in [
                "cublas", "cudnn", "cudart", "cufft", "cusolver", "cusparse",
                "nvcuda", "is not found or cannot be loaded", "cuda",
            ])

            if is_cuda_lib_error and device in ("cuda", "auto"):
                print()
                warn("CUDA libraries are missing or incomplete.")
                info("This usually means the NVIDIA CUDA Toolkit isn't fully installed.")
                if IS_WIN:
                    info("Fix: Install CUDA Toolkit from https://developer.nvidia.com/cuda-downloads")
                    info("     Or install cuBLAS via: pip install nvidia-cublas-cu12")
                else:
                    info("Fix: Install CUDA Toolkit for your distro, or:")
                    info("     pip install nvidia-cublas-cu12")

                if ask_yn("Retry this model on CPU instead?", default=True):
                    try:
                        info(f"Retrying {model_name} on CPU...")
                        from faster_whisper import WhisperModel as WM2
                        t0 = time.time()
                        model = WM2(model_name, device="cpu", compute_type="int8")
                        load_time = time.time() - t0
                        success(f"Loaded on CPU in {load_time:.1f}s")

                        info(f"{C.DIM}Running 3 transcription passes (5s test audio each)...{C.RESET}")
                        times = []
                        for run in range(3):
                            t0 = time.time()
                            segs, _ = model.transcribe(str(test_wav), language="ja",
                                                       vad_filter=False, word_timestamps=False)
                            list(segs)
                            times.append(time.time() - t0)

                        median_time = sorted(times)[len(times) // 2]
                        speed = 5.0 / median_time if median_time > 0 else 0
                        results.append({
                            "model": f"{model_name} (CPU)",
                            "load_s": round(load_time, 1),
                            "median_s": round(median_time, 3),
                            "rtf": round(median_time / 5.0, 3),
                            "speed_x": round(speed, 1),
                            "segments": 0,
                            "status": "OK",
                        })
                        success(f"CPU median: {median_time:.3f}s for 5s audio ({speed:.1f}x realtime)")
                        del model
                    except Exception as e2:
                        error(f"CPU fallback also failed: {str(e2)[:60]}")
                        results.append({
                            "model": model_name, "load_s": 0, "median_s": 0, "rtf": 0,
                            "speed_x": 0, "segments": 0,
                            "status": f"FAIL: {short_err}",
                        })
                else:
                    results.append({
                        "model": model_name, "load_s": 0, "median_s": 0, "rtf": 0,
                        "speed_x": 0, "segments": 0,
                        "status": f"FAIL: {short_err}",
                    })
            else:
                results.append({
                    "model": model_name, "load_s": 0, "median_s": 0, "rtf": 0,
                    "speed_x": 0, "segments": 0,
                    "status": f"FAIL: {short_err}",
                })

    # Clean up test audio
    try:
        test_wav.unlink()
    except OSError:
        pass

    # Display results
    print()
    section("Benchmark Results")
    info(f"Device: {device} | Precision: {compute} | Test: 5s audio, 3 runs (median)")
    if gpu.get("has_nvidia"):
        info(f"GPU: {gpu['name']} ({gpu['vram_mb']} MB VRAM)")
    print()

    rows = []
    best_speed = max((r["speed_x"] for r in results if r["status"] == "OK"), default=0)
    for r in results:
        if r["status"] == "OK":
            bar_len = min(25, int(r["speed_x"] / max(best_speed, 1) * 25))
            speed_bar = f"{C.GREEN}{'█' * bar_len}{C.DIM}{'░' * (25 - bar_len)}{C.RESET}"
            is_best = f" {C.GOLD}★{C.RESET}" if r["speed_x"] == best_speed and len(results) > 1 else ""
            rows.append([
                r["model"],
                f"{r['load_s']}s",
                f"{r['median_s']:.3f}s",
                f"{r['speed_x']}x{is_best}",
                speed_bar,
            ])
        else:
            rows.append([r["model"], "-", "-", "-", f"{C.RED}{r['status']}{C.RESET}"])

    table(
        ["Model", "Load", "5s Audio", "Speed", ""],
        rows,
        col_styles=[C.CYAN, C.RESET, C.RESET, C.GREEN, C.RESET],
        title="Whisper Benchmark"
    )

    if results:
        ok_results = [r for r in results if r["status"] == "OK"]
        if ok_results:
            fastest = min(ok_results, key=lambda r: r["median_s"])
            print()
            info("How to read the results:")
            info(f"  {C.DIM}Speed = how many times faster than real-time. Higher is better.{C.RESET}")
            info(f"  {C.DIM}10x = 1 minute of audio transcribed in 6 seconds{C.RESET}")
            info(f"  {C.DIM}50x = 1 minute of audio transcribed in ~1 second{C.RESET}")
            print()
            success(f"Fastest: {C.BOLD}{fastest['model']}{C.RESET} at {C.GREEN}{fastest['speed_x']}x{C.RESET} realtime")

            # Show practical estimate
            if fastest["speed_x"] > 0:
                mins_per_min = 60.0 / fastest["speed_x"]
                info(f"  {C.DIM}→ A 4-minute song would be transcribed in ~{mins_per_min * 4:.1f} seconds{C.RESET}")

            if fastest["model"] != cfg.get("whisper_model"):
                print()
                if ask_yn(f"Switch to {fastest['model']}?", default=False):
                    cfg["whisper_model"] = fastest["model"]
                    save_config(cfg)
                    success(f"Config updated to {fastest['model']}")
    pause()

# ────
# STATUS
# ────

def show_status(cfg):
    header("System Status")
    gpu = detect_gpu()
    if gpu["has_nvidia"]:
        gpu_txt = f"{C.GREEN}{gpu['name']} ({gpu['vram_mb']} MB){C.RESET}"
    elif gpu["has_amd"]:
        vr = f" ({gpu['vram_mb']} MB)" if gpu["vram_mb"] else ""
        gpu_txt = f"{C.CYAN}{gpu['name']}{vr}{C.RESET}"
    else:
        gpu_txt = f"{C.YELLOW}None (CPU mode){C.RESET}"
    ram = detect_ram_gb()
    disk = disk_free_gb()
    table(
        ["Component", "Status"],
        [
            ["GPU", gpu_txt],
            ["RAM", f"{ram:.1f} GB" + (f"  {C.GREEN}OK{C.RESET}" if ram >= 8 else f"  {C.YELLOW}low{C.RESET}")],
            ["Disk", f"{disk:.1f} GB free" + (f"  {C.GREEN}OK{C.RESET}" if disk >= 10 else f"  {C.RED}low!{C.RESET}")],
            ["OS", f"{PLAT} ({ARCH})"],
        ],
        col_styles=[C.BOLD, C.RESET],
        title="Hardware"
    )

    tool_rows = []
    for t in ["yt-dlp", "ffmpeg", "deno"]:
        p = find_tool(t)
        if p:
            tool_rows.append([t, f"{C.GREEN}installed{C.RESET}", p])
        elif t == "deno" and cfg.get("youtube_auth_method") == "cookies":
            tool_rows.append([t, f"{C.DIM}skipped{C.RESET}", "using cookies"])
        else:
            tool_rows.append([t, f"{C.RED}MISSING{C.RESET}", "run Tools menu to install"])
    table(["Tool", "Status", "Path"], tool_rows,
          col_styles=[C.CYAN, C.RESET, C.DIM], title="Tools")

    pkg_rows = []
    for pkg, display in [("faster_whisper", "faster-whisper"), ("flask", "Flask"),
                          ("flask_cors", "Flask-CORS"), ("llama_cpp", "llama-cpp-python")]:
        try:
            __import__(pkg)
            pkg_rows.append([display, f"{C.GREEN}✓{C.RESET}"])
        except ImportError:
            pkg_rows.append([display, f"{C.RED}✗  not installed{C.RESET}"])
    table(["Package", "Status"], pkg_rows,
          col_styles=[C.CYAN, C.RESET], title="Python Packages")

    bk = cfg.get("translation_backend", "llamacpp")
    bi = BACKEND_INFO.get(bk, BACKEND_INFO["custom"])
    ws = check_server(cfg["whisper_host"], cfg["whisper_port"], "/health")
    ts = check_server(cfg["translation_host"], cfg["translation_port"], bi["hp"])
    if not ts["up"]:
        ts = check_server(cfg["translation_host"], cfg["translation_port"], HEALTH_PATH_OPENAI)
    ws_s = f"{C.GREEN}● running{C.RESET}" if ws["up"] else f"{C.DIM}○ stopped{C.RESET}"
    ts_s = f"{C.GREEN}● running{C.RESET}" if ts["up"] else f"{C.DIM}○ stopped{C.RESET}"
    table(
        ["Server", "Address", "Status"],
        [
            ["Whisper", f"{cfg['whisper_host']}:{cfg['whisper_port']}", ws_s],
            [bi["name"], f"{cfg['translation_host']}:{cfg['translation_port']}", ts_s],
        ],
        col_styles=[C.CYAN, C.DIM, C.RESET],
        title="Servers"
    )

    gf = find_gguf_models()

    # Models
    section("Models")
    wm = cfg.get("whisper_model", "large-v3")
    wm_path = Path(wm)
    if wm_path.is_dir():
        info(f"Whisper:      {C.BOLD}{wm_path.name}{C.RESET}  {C.DIM}(custom: {wm}){C.RESET}")
    else:
        info(f"Whisper:      {C.BOLD}{wm}{C.RESET}")
        # Show cache location
        hf_cache = Path.home() / ".cache" / "huggingface" / "hub"
        cached = _is_whisper_model_cached(wm)
        if cached:
            info(f"              {C.GREEN}downloaded{C.RESET}  {C.DIM}{hf_cache}{C.RESET}")
        else:
            info(f"              {C.YELLOW}not downloaded{C.RESET}  (will download on first use)")
            info(f"              {C.DIM}Cache: {hf_cache}{C.RESET}")

    gp = cfg.get("gguf_model_path", "")
    if gp and Path(gp).exists():
        info(f"Translation:  {C.BOLD}{Path(gp).name}{C.RESET}  {C.DIM}({gp}){C.RESET}")
    elif gf:
        info(f"Translation:  {len(gf)} GGUF file(s) in models/translation/")
        for f in gf:
            bullet(f"{f.name} ({f.stat().st_size/GiB:.2f} GB)")
    else:
        info(f"Translation:  {C.DIM}no GGUF models found{C.RESET}")

    section("Ports")
    for label, key in [("Whisper", "whisper"), ("Translation", "translation")]:
        host = cfg.get(f"{key}_host", "127.0.0.1")
        port = cfg.get(f"{key}_port", DEFAULT_TRANSLATION_PORT)
        free = is_port_free(port, host)
        if free:
            info(f"{label:12s} {host}:{port}  {C.GREEN}available{C.RESET}")
        else:
            pid, name = get_port_process(port)
            who = f"{name} (PID {pid})" if pid else "unknown process"
            warn(f"{label:12s} {host}:{port}  {C.RED}in use{C.RESET} by {who}")
    pause()


def detect_fonts():
    """Detect CJK and common fonts available on this system."""
    header("Font Detection")
    info("Scanning for subtitle-compatible fonts (Chinese, Japanese, Korean, Arabic)...")
    print()

    # Platform-specific font listing
    font_dirs = []
    if IS_WIN:
        winfonts = Path(os.environ.get("WINDIR", "C:\\Windows")) / "Fonts"
        font_dirs = [winfonts]
    elif IS_MAC:
        font_dirs = [Path("/Library/Fonts"), Path.home() / "Library/Fonts", Path("/System/Library/Fonts")]
    else:
        font_dirs = [Path("/usr/share/fonts"), Path.home() / ".local/share/fonts", Path("/usr/local/share/fonts")]

    # Scan for font files
    extensions = {".ttf", ".otf", ".ttc", ".woff", ".woff2"}
    found_files = []
    for d in font_dirs:
        if d.exists():
            for f in d.rglob("*"):
                if f.suffix.lower() in extensions:
                    found_files.append(f)

    info(f"Scanned {len(font_dirs)} font directories, found {len(found_files)} font files")
    print()

    # CJK font families to look for
    cjk_targets = {
        "Japanese": ["NotoSansJP", "NotoSerifJP", "Noto Sans JP", "MPLUS", "M PLUS", "Yu Gothic",
                      "Meiryo", "MS Gothic", "HiraginoSans", "Hiragino", "Kosugi", "ZenMaru"],
        "Chinese": ["NotoSansSC", "NotoSansTC", "Noto Sans SC", "Noto Sans TC", "SimHei",
                     "Microsoft YaHei", "PingFang", "STHeiti", "Source Han Sans"],
        "Korean": ["NotoSansKR", "Noto Sans KR", "Malgun", "NanumGothic", "AppleSD"],
        "Arabic": ["NotoNaskhArabic", "Noto Naskh Arabic", "Amiri", "Scheherazade"],
    }

    for lang, patterns in cjk_targets.items():
        matches = []
        for f in found_files:
            fname = f.stem
            for pat in patterns:
                if pat.lower().replace(" ", "") in fname.lower().replace(" ", ""):
                    matches.append(f.name)
                    break
        if matches:
            success(f"{lang}: {len(matches)} font(s) found")
            for m in sorted(set(matches))[:5]:
                bullet(m)
            if len(set(matches)) > 5:
                info(f"  ... and {len(set(matches)) - 5} more")
        else:
            warn(f"{lang}: No dedicated fonts found")
            info(f"  Install: Noto Sans {lang[:2].upper()} from Google Fonts")
    print()

    # Generic fonts
    generic = ["Arial", "Georgia", "Verdana", "Consolas", "Times New Roman", "Courier New"]
    found_generic = []
    for f in found_files:
        for g in generic:
            if g.lower().replace(" ", "") in f.stem.lower().replace(" ", ""):
                found_generic.append(g)
    found_generic = sorted(set(found_generic))
    info(f"Generic fonts: {', '.join(found_generic) if found_generic else 'none detected'}")

    # Extension fonts folder
    ext_fonts = EXT_DIR / "fonts"
    if ext_fonts.exists():
        bundled = [f.name for f in ext_fonts.iterdir() if f.suffix.lower() in extensions]
        if bundled:
            section("Bundled Fonts (extension/fonts/)")
            for f in bundled:
                bullet(f)
        else:
            info(f"Extension fonts folder: empty (add .ttf/.otf files to {ext_fonts})")
    else:
        warn(f"Extension fonts folder missing: {ext_fonts}")
        info("Create it and add .ttf/.otf files for custom subtitle fonts")

    print()
    info("How to use fonts in Yume:")
    info("  1. Yume automatically detects your system fonts")
    info("  2. Pick a font in the extension popup (Settings > Appearance)")
    info("")
    info("To add a custom font that isn't installed system-wide:")
    info("  1. Copy the .ttf or .otf file into the extension/fonts/ folder")
    info("  2. Open extension/popup.js, find the BUNDLED_FONTS list near the top")
    info("  3. Add your font: { file: 'MyFont.ttf', name: 'My Font' }")
    info("  4. Reload the extension in chrome://extensions")
    info("  The font will appear with '(bundled)' next to its name in the dropdown")
    pause()


# ────
# LAUNCH
# ────


def launch_services(cfg):
    header("Launching Yume")
    rotate_logs()  # Clean up old logs before starting
    procs = []
    lhs = []

    def _cleanup():
        for name, p in procs:
            try:
                p.terminate()
                p.wait(timeout=3)
            except Exception:
                try:
                    p.kill()
                    p.wait(timeout=2)
                except OSError:
                    pass
        for lh in lhs:
            try:
                lh.close()
            except OSError:
                pass
        # Verify ports are actually freed
        time.sleep(0.5)
        for key in ["whisper_port", "translation_port"]:
            port = cfg.get(key)
            if port and not is_port_free(port):
                kill_port_process(port)

    # Wrap everything so Ctrl+C during startup kills spawned processes
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


def _launch_inner(cfg, procs, lhs):
    """Actual server startup. Ctrl+C caught by launch_services()."""

    # --- Check for already-running servers ---
    existing = discover_servers(cfg)
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
                    arches = re.findall(r'gfx(\d+)', out.stdout)
                    # RDNA1 (gfx1010/1011/1012) needs override to 10.3.0
                    rdna1 = [a for a in arches if a in ("1010", "1011", "1012")]
                    if rdna1:
                        env["HSA_OVERRIDE_GFX_VERSION"] = "10.3.0"
                        warn(f"AMD RDNA1 GPU detected (gfx{rdna1[0]}). Setting HSA_OVERRIDE_GFX_VERSION=10.3.0")
                        info("Add 'export HSA_OVERRIDE_GFX_VERSION=10.3.0' to ~/.bashrc to make this permanent.")
            except Exception as e:
                _log.debug('[_launch_inner] rocm-detect failed: %s', e)



    bk = cfg.get("translation_backend", "llamacpp")
    bi = BACKEND_INFO.get(bk, BACKEND_INFO.get("custom", {"hp": "/health"}))

    # --- START TRANSLATION BACKEND ---
    if bk == "llamacpp":
        gp = cfg.get("gguf_model_path", "")
        if not gp or not Path(gp).exists():
            # Try to find any GGUF in the models dir
            gfs = find_gguf_models()
            if gfs:
                gp = str(gfs[0])
                cfg["gguf_model_path"] = gp
                save_config(cfg)
                info(f"Auto-selected GGUF: {gfs[0].name}")
            else:
                error("No .gguf model found in models/translation/")
                info("Fix: Main Menu → Tools → Download GGUF Model")
                info(f"Or: place a .gguf file in {GGUF_DIR}")
                pause()
                return

        # Check if llama-cpp-python is installed
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

        # Check if server deps (uvicorn, fastapi) are installed
        try:
            import uvicorn  # noqa: F401
            import fastapi  # noqa: F401
        except ImportError:
            warn("Server dependencies missing (uvicorn/fastapi)")
            info("Installing them now...")
            try:
                _run([
                    sys.executable, "-m", "pip", "install",
                    "uvicorn==0.42.0", "fastapi==0.135.1", "sse-starlette==3.3.3",
                    "starlette-context==0.5.1", "pydantic-settings==2.13.1",
                    "-q", "--no-warn-script-location"
                ], timeout=300, env=env)
                success("Server dependencies installed!")
            except Exception as e:
                error(f"Failed to install: {e}")
                pause()
                return

        port = cfg.get("translation_port", DEFAULT_TRANSLATION_PORT)
        st = check_translation_server(cfg["translation_host"], port, bi)
        if st["up"]:
            success("llama.cpp server already running!")
        else:
            # Kill any stale process occupying the port
            kill_port_process(port)
            info(f"Starting llama.cpp server with {Path(gp).name}...")
            gpu = detect_gpu()
            cmd = [
                sys.executable, "-m", "llama_cpp.server",
                "--model", gp,
                "--host", cfg.get("translation_host", "127.0.0.1"),
                "--port", str(port),
                "--n_ctx", "2048",
            ]
            if gpu["has_nvidia"]:
                cmd.extend(["--n_gpu_layers", "-1"])  # offload all layers to GPU
            elif gpu["has_amd"] and not IS_WIN:
                cmd.extend(["--n_gpu_layers", "-1"])  # ROCm/HIP offload on Linux

            LOGS_DIR.mkdir(parents=True, exist_ok=True)
            lp = LOGS_DIR / "translation_server.log"
            lh = open(lp, "w", encoding="utf-8", errors="replace")
            lhs.append(lh)
            # PYTHONUTF8=1 prevents UnicodeDecodeError in llama_cpp's log callback
            # when model metadata contains non-UTF-8 bytes
            tl_env = env.copy()
            tl_env["PYTHONUTF8"] = "1"
            p = subprocess.Popen(cmd, stdout=lh, stderr=subprocess.STDOUT, env=tl_env)
            procs.append(("Translation", p))
            info(f"Log: {lp}")
            def _trans_ready():
                if p.poll() is not None:
                    return None  # crashed
                # llama-cpp-python doesn't have /health — try /v1/models too
                return check_translation_server(cfg["translation_host"], port, bi)["up"]

            ready = spin_wait(
                lambda: _trans_ready() is True,
                "Loading translation model...",
                timeout=180, interval=2
            )
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
                    _log.debug('[_launch_inner] translation-crash-log-read failed: %s', e)
                # Diagnose common crash causes
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
                elif "address already in use" in cl or "port" in cl and "in use" in cl:
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

    elif bk == "ollama":
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
                return
        else:
            success("Ollama already running")
        ms = check_ollama_models(cfg["translation_host"], cfg["translation_port"])
        mb = cfg.get("translation_model", "qwen2.5:7b").split(":")[0]
        if not any(mb in m for m in ms):
            warn(f"Model {cfg['translation_model']} not found. Pulling...")
            pull_ollama_model(cfg.get("translation_model", "qwen2.5:7b"))
    else:
        bn = BACKEND_INFO.get(bk, {}).get("name", bk)
        info(f"{bn} -- make sure it's running at {cfg['translation_host']}:{cfg['translation_port']}")

    # --- START WHISPER SERVER ---
    ws = check_server(cfg["whisper_host"], cfg["whisper_port"], "/health")
    if ws["up"]:
        success("Whisper already running!")
    else:
        # Kill any stale process occupying the port
        kill_port_process(cfg["whisper_port"])
        info("Starting Whisper server...")
        ss = SERVER_DIR / "faster_whisper_server.py"
        if not ss.exists():
            ss = BASE_DIR / "faster_whisper_server.py"  # backward compat: root dir
        if not ss.exists():
            error("Whisper server script not found in server/ or root")
            info("Fix: Re-extract Yume or run Setup again")
            pause()
            return

        dev = cfg["whisper_device"]
        comp = cfg["whisper_compute_type"]
        gpu = detect_gpu()
        if dev == "auto":
            if gpu["has_nvidia"]:
                dev = "cuda"
            elif gpu.get("has_amd") and not IS_WIN:
                dev = "cuda"  # ROCm: HIP presents as CUDA to PyTorch
            else:
                dev = "cpu"
        if comp == "auto":
            comp = "float16" if dev == "cuda" and gpu.get("vram_mb", 0) >= 8000 else (
                "int8_float16" if dev == "cuda" else "int8"
            )

        cmd = [
            sys.executable, str(ss),
            "--model", cfg["whisper_model"],
            "--device", dev,
            "--compute-type", comp,
            "--port", str(cfg["whisper_port"]),
            "--pause-threshold", str(cfg["pause_threshold"]),
        ]
        if not cfg.get("word_timestamps"):
            cmd.append("--no-word-timestamps")
        if CONFIG_FILE.exists():
            cmd.extend(["--config", str(CONFIG_FILE)])

        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        lp = LOGS_DIR / "whisper_server.log"
        lh = open(lp, "w", encoding="utf-8", errors="replace")
        lhs.append(lh)
        info(f"Device: {dev} | Compute: {comp} | Port: {cfg['whisper_port']}")
        info(f"Log: {lp}")

        p = subprocess.Popen(cmd, stdout=lh, stderr=subprocess.STDOUT, env=env)
        procs.append(("Whisper", p))
        def _whisper_ready():
            if p.poll() is not None:
                return None  # crashed
            st = check_server(cfg["whisper_host"], cfg["whisper_port"], "/health")
            return st["up"] and st["data"].get("status") == "ready"

        ready = spin_wait(
            lambda: _whisper_ready() is True,
            f"Loading Whisper model ({cfg['whisper_model']})...",
            timeout=240, interval=2
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
                _log.debug('[_launch_inner] whisper-crash-log-read failed: %s', e)
            # Diagnose common crash causes with actionable solutions
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
            return
        if ready:
            success("Whisper server ready!")
        else:
            warn("Still loading — first run takes 30-60s while the Whisper model downloads.")
            info("This is a one-time download. Future launches will start much faster.")

    # --- READY ---
    clear()
    print()
    success(f"Yume is running!  {C.DIM}v{VERSION}{C.RESET}")
    tn = BACKEND_INFO.get(bk, {}).get("name", bk)
    print(f"  {C.CYAN}Whisper{C.RESET}      http://{cfg['whisper_host']}:{cfg['whisper_port']}")
    print(f"  {C.MAGENTA}Translation{C.RESET}  http://{cfg['translation_host']}:{cfg['translation_port']}  ({tn})")
    print()
    info(f"{C.DIM}Chrome -> Japanese video -> Yume extension -> Enable{C.RESET}")
    print()

    _runtime_menu(cfg, procs, lhs, bk)


def _runtime_menu(cfg, procs, lhs, bk):
    """Live menu: stats, blacklist, model swap, logs."""
    bi = BACKEND_INFO.get(bk, BACKEND_INFO.get("custom", {}))

    def _check_procs():
        """Check if any child process has died."""
        for n, p in procs:
            if p.poll() is not None:
                warn(f"{n} exited unexpectedly (code {p.returncode})")
                return False
        return True

    def _stop_all():
        print(f"\n  {C.YELLOW}Shutting down...{C.RESET}")
        for n, p in procs:
            try:
                p.terminate()
                p.wait(timeout=5)
                success(f"{n} stopped")
            except Exception:
                try:
                    p.kill()
                    p.wait(timeout=3)
                except Exception as e:
                    _log.debug('[_runtime_menu] process-cleanup failed: %s', e)

        for lh in lhs:
            try:
                lh.close()
            except OSError:
                pass
        # Double-check ports are freed
        time.sleep(0.5)
        for port_key in ["whisper_port", "translation_port"]:
            port = cfg.get(port_key)
            if port and not is_port_free(port):
                kill_port_process(port)

    def _show_logs():
        section("Recent Logs")
        for name in ["whisper_server.log", "translation_server.log"]:
            lp = LOGS_DIR / name
            if lp.exists():
                info(f"{C.BOLD}{name}{C.RESET}")
                try:
                    with open(lp, encoding="utf-8", errors="replace") as f:
                        lines = f.readlines()
                    # Show informative lines first (Yume pipeline output), then recent access logs
                    # Filter out noisy health check polling that drowns real info
                    yume_lines = [ln for ln in lines if "[Yume]" in ln or "error" in ln.lower() or "fail" in ln.lower() or "warn" in ln.lower()]
                    access_lines = [ln for ln in lines if "HTTP/" in ln and "/health" not in ln]
                    other_lines = [ln for ln in lines if ln not in yume_lines and ln not in access_lines and "GET /health" not in ln]
                    # Combine: last 10 Yume lines + last 5 non-health access lines
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
                    return  # returns to main menu, user can relaunch

            # Quick status check
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

            ch = ask_choice("Runtime:", [
                ("Server Stats", "GPU usage, memory, how many chunks have been processed"),
                ("Subtitle Filter", "Block phrases Whisper hallucinates (fake 'Subscribe' etc.)"),
                ("Whisper Model", "Switch speech recognition model without restarting"),
                ("Test Translation", "Send a test sentence to verify the translation pipeline"),
                ("Benchmark Whisper", "Measure how fast each model runs on your hardware"),
                ("View Logs", "Recent server output (for troubleshooting)"),
                ("Stop & Return", "Shut down all servers and go back to main menu"),
            ], default=6, allow_back=False)

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
                # All options loop back to top which calls clear()
            elif ch == 6:
                if ask_yn("Stop all servers?", default=True):
                    _stop_all()
                    return
    except KeyboardInterrupt:
        print()
        _stop_all()

# ────
# SETUP WIZARD
# ────

def setup_wizard(cfg):
    header("First-Time Setup")
    print(center(f"{C.BOLD}Welcome to Pocket Yume!{C.RESET}"))
    print(center(f"{C.DIM}Let's set up everything for Yume AI subtitles.{C.RESET}"))
    print(center(f"{C.DIM}Platform: {PLAT} ({ARCH}){C.RESET}"))
    print()
    info(f"{C.DIM}Note: First startup takes longer because the Whisper model needs to download (~1-3 GB).{C.RESET}")
    info(f"{C.DIM}Subsequent launches will be much faster.{C.RESET}")
    print()

    section("System Scan")
    gpu = detect_gpu()
    ram = detect_ram_gb()
    disk = disk_free_gb()

    # Warn about Python versions that may lack prebuilt wheels
    pyver = f"{sys.version_info.major}.{sys.version_info.minor}"
    if sys.version_info >= (3, 13):
        warn(f"Python {pyver} detected — some packages may need to build from source.")
        info("This requires C++ build tools. If installs fail, use Python 3.11 or 3.12.")
        print()

    has_gpu = gpu["has_nvidia"] or (gpu.get("has_amd") and not IS_WIN)
    (success if has_gpu else warn)(
        f"GPU:  {gpu['name'] or 'None'}" + (
            f" ({gpu['vram_mb']} MB)" if gpu["has_nvidia"]
            else f" ({gpu.get('vram_mb', '?')} MB, ROCm)" if gpu.get("has_amd") and not IS_WIN
            else " -- CPU mode"
        )
    )
    if not has_gpu:
        info(f"{C.DIM}No compatible GPU found. Yume will use your CPU for transcription.{C.RESET}")
        info(f"{C.DIM}This works fine but is slower. NVIDIA GPUs with 4+ GB VRAM give best performance.{C.RESET}")
    success(f"RAM:  {ram:.1f} GB")
    (success if disk >= 10 else warn)(f"Disk: {disk:.1f} GB free" + ("" if disk >= 10 else " -- need ~8 GB"))

    # Auto-configure Whisper using the model recommender
    # PRESERVE custom model paths — don't overwrite if user set a local directory
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
        cfg["whisper_device"] = "auto"  # ROCm resolves via HIP CUDA compat
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

    header("Component Check")
    missing = []
    yt = find_tool("yt-dlp")
    ff = find_tool("ffmpeg")
    dn = find_tool("deno")
    (success if yt else warn)(f"yt-dlp: {'OK '+yt if yt else 'MISSING'}")
    (success if ff else warn)(f"FFmpeg: {'OK '+ff if ff else 'MISSING'}")
    info(f"Deno:   {'OK '+dn if dn else '-- not installed (optional)'}")

    if not yt:
        missing.append("yt-dlp")
    if not ff:
        missing.append("ffmpeg")

    # Check Python packages
    try:
        import faster_whisper # noqa: F401
        success("faster-whisper: OK")  # noqa: F401
    except ImportError:
        warn("faster-whisper: MISSING")
        missing.append("python_deps")

    try:
        import llama_cpp # noqa: F401
        success("llama-cpp-python: OK")  # noqa: F401
    except ImportError:
        warn("llama-cpp-python: MISSING")
        missing.append("llama_cpp")

    try:
        import uvicorn # noqa: F401
        import fastapi # noqa: F401
        success("Server deps (uvicorn/fastapi): OK")  # noqa: F401
    except ImportError:
        warn("Server deps (uvicorn/fastapi): MISSING")
        missing.append("server_deps")

    # Check for GGUF models
    gf = find_gguf_models()
    if gf:
        success(f"GGUF models: {len(gf)} found")
        cfg["gguf_model_path"] = str(gf[0])
        cfg["translation_model"] = gf[0].name
    else:
        warn("No GGUF translation model found")
        missing.append("gguf_model")

    # Check for running backends
    print()
    detected = []
    for k, bi in BACKEND_INFO.items():
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
        mode = ask_choice("Proceed?", [
            ("Install all", "Download everything needed"),
            ("Choose each", "Ask per component"),
            ("Skip", "Set up later"),
        ], default=0, allow_back=False)

        if mode == 2:
            cfg["first_run_complete"] = True
            save_config(cfg)
            pause()
            return cfg

        ae = (mode == 1)  # ask-each
        header("Installing")

        if "yt-dlp" in missing:
            if not ae or ask_yn("Install yt-dlp?"):
                if not install_ytdlp():
                    info(f"Manual download: {C.CYAN}https://github.com/yt-dlp/yt-dlp/releases/latest{C.RESET}")
                    info(f"Place the binary in: {C.BOLD}{TOOLS_DIR}{C.RESET}")

        if "ffmpeg" in missing:
            if not ae or ask_yn("Install FFmpeg?"):
                if not install_ffmpeg():
                    info(f"Manual download: {C.CYAN}https://ffmpeg.org/download.html{C.RESET}")
                    info(f"Place ffmpeg{EXE} in: {C.BOLD}{TOOLS_DIR}{C.RESET}")

        # YouTube authentication — always ask during first run
        print()
        header("YouTube Authentication")
        info("YouTube blocks automated downloads to prevent bots.")
        info("Yume needs a way to prove you're a real person.")
        print()
        ch = ask_choice("How do you want to authenticate with YouTube?", [
            ("Browser Cookies (recommended)",
             "Uses your browser's YouTube login. Be logged into YouTube in your browser."),
            ("Deno (no account needed)",
             "Downloads Deno (~35 MB) + sets up a local server that solves YouTube's\n"
             "      bot challenge without any login. Requires internet connection."),
            ("Skip for now", "You can set this up later in Settings."),
        ], default=0, allow_back=False)
        if ch == 0:
            cfg["youtube_auth_method"] = "cookies"
            browsers = ["chrome", "firefox", "edge", "brave", "safari"]
            print()
            info("This only affects audio downloading — the extension itself works in any browser.")
            bc = ask_choice("Which browser are you logged into YouTube with?",
                            [(b.capitalize(), None) for b in browsers],
                            default=0, allow_back=False)
            cfg["cookies_browser"] = browsers[bc]
            success(f"Using cookies from: {browsers[bc]}")
        elif ch == 1:
            install_deno()
            cfg["youtube_auth_method"] = "deno"
        save_config(cfg)

        if "python_deps" in missing:
            if not ae or ask_yn("Install Python packages?"):
                install_python_deps()

        if "llama_cpp" in missing:
            if not ae or ask_yn("Install llama-cpp-python (translation engine)?"):
                install_llamacpp_python()

        if "server_deps" in missing and "llama_cpp" not in missing:
            # Only ask separately if llama-cpp was already installed
            # (install_llamacpp_python already installs server deps)
            if not ae or ask_yn("Install server dependencies (uvicorn, fastapi)?"):
                info("Installing server dependencies...")
                try:
                    _run([
                        sys.executable, "-m", "pip", "install",
                        "uvicorn==0.42.0", "fastapi==0.135.1", "sse-starlette==3.3.3",
                        "starlette-context==0.5.1", "pydantic-settings==2.13.1",
                        "-q", "--no-warn-script-location"
                    ], timeout=300)
                    success("Server dependencies installed!")
                except Exception as e:
                    error(f"Failed: {e}")
                    info("Try running as administrator, or install manually:")
                    info(f"  {C.CYAN}pip install uvicorn fastapi sse-starlette{C.RESET}")

        if "gguf_model" in missing:
            section("Translation Model")
            info("Yume needs a GGUF model file to translate Japanese -> English.")
            info("Default: llama.cpp loads it directly. No external servers needed.")
            print()
            ch = ask_choice("How to get a model?", [
                ("Download from HuggingFace", "Browse repos, pick quantization"),
                ("Use Ollama instead", "One-click, manages models for you"),
                ("Skip", "I'll add a .gguf file later"),
            ], default=0, allow_back=False)
            if ch == 0:
                browse_hf(cfg)
            elif ch == 1:
                bi = BACKEND_INFO["ollama"]
                cfg["translation_backend"] = "ollama"
                cfg["translation_host"] = bi["dh"]
                cfg["translation_port"] = bi["dp"]
                cfg["translation_model"] = "qwen2.5:7b"
                if ask_yn("Install Ollama?"):
                    install_ollama()
                    time.sleep(2)
                    if ask_yn(f"Download model ({cfg['translation_model']})?"):
                        if not check_server("127.0.0.1", DEFAULT_OLLAMA_PORT, HEALTH_PATH_OLLAMA)["up"]:
                            try:
                                subprocess.Popen(["ollama", "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                                time.sleep(3)
                            except Exception as e:
                                _log.debug('[setup_wizard] post-setup-backup failed: %s', e)

                        pull_ollama_model(cfg["translation_model"])
            save_config(cfg)

    cfg["first_run_complete"] = True
    save_config(cfg)
    try:
        config_export(cfg, BASE_DIR / "yume_config_post_setup.json")
    except Exception as e:
        _log.debug('[setup_wizard] config-backup failed: %s', e)

    # ── Installation summary ──
    print()
    section("Installation Summary")
    checks = [
        ("yt-dlp", find_tool("yt-dlp") is not None),
        ("ffmpeg", find_tool("ffmpeg") is not None),
        ("faster-whisper", _try_import("faster_whisper")),
    ]
    bk = cfg.get("translation_backend", "llamacpp")
    if bk == "llamacpp":
        checks.append(("llama-cpp-python", _try_import("llama_cpp")))
        checks.append(("GGUF model", len(find_gguf_models()) > 0))
    elif bk == "ollama":
        checks.append(("Ollama", check_server("127.0.0.1", cfg.get("translation_port", 11434), HEALTH_PATH_OLLAMA)["up"]))

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

# ────
# UNINSTALL
# ────

def uninstall_yume():
    header("Uninstall Yume")
    warn("This will remove all Yume data from your system.")
    print()
    info("What will be removed:")
    dirs = [
        ("Tools (yt-dlp, ffmpeg, deno)", TOOLS_DIR),
        ("Translation models (downloaded AI model files)", GGUF_DIR),
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
                    _log.debug('[uninstall_yume] file-stat failed: %s', e)

        mb = sz / MiB
        marker = f"{C.GREEN}✓{C.RESET}" if d.exists() else f"{C.DIM}—{C.RESET}"
        print(f"    {marker} {label:40s} {mb:>8.1f} MB  {d}")
    print()

    pip_pkgs = ["faster-whisper", "llama-cpp-python", "flask", "flask-cors",
                "uvicorn", "fastapi", "sse-starlette", "starlette-context", "pydantic-settings"]
    info("Pip packages that can be removed:")
    print(f"    {', '.join(pip_pkgs)}")
    print()

    ch = ask_choice("What to remove?", [
        ("Everything (data + pip packages)", "Full clean uninstall"),
        ("Data only (keep pip packages)", "Remove tools, models, config, logs"),
        ("Pip packages only", "Uninstall Python packages installed by Yume"),
        ("Cancel", None),
    ], default=3)

    if ch == 3 or ch == -1:
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

        # Also remove the models parent dir if empty
        if MODELS_DIR.exists():
            try:
                shutil.rmtree(MODELS_DIR)
            except Exception as e:
                _log.debug('[uninstall_yume] cleanup failed: %s', e)


    if remove_pip:
        info("Uninstalling pip packages...")
        for pkg in pip_pkgs:
            try:
                r = _run([sys.executable, "-m", "pip", "uninstall", "-y", pkg], timeout=30)
                if r.returncode == 0:
                    success(f"  Removed: {pkg}")
                else:
                    # Package wasn't installed, that's fine
                    pass
            except Exception as e:
                _log.debug('[uninstall_yume] dir-remove failed: %s', e)



    print()
    success("Uninstall complete!")
    info("You can delete pocket_yume.py and the extension/ folder manually.")
    info("To remove the browser extension: go to chrome://extensions and remove Yume.")
    pause()

# ────
# MAIN MENU
# ────

def main_menu():
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
            # Show CPU name instead of just "CPU only"
            cpu_name = _detect_cpu_name()
            if "intel" in cpu_name.lower():
                gs = f"{C.BLUE}CPU: {cpu_name}{C.RESET}"
            elif "amd" in cpu_name.lower():
                gs = f"{C.RED}CPU: {cpu_name}{C.RESET}"
            else:
                gs = f"{C.YELLOW}CPU: {cpu_name}{C.RESET}"

        # Show actual device the server will use
        bn = BACKEND_INFO.get(cfg.get("translation_backend", "llamacpp"), {}).get("name", "?")
        panel(
            f"{C.DIM}Hardware   {C.RESET}  {gs}\n"
            f"{C.DIM}Speech AI  {C.RESET}  {C.CYAN}{cfg['whisper_model']}{C.RESET}  on  {cfg['whisper_host']}:{cfg['whisper_port']}\n"
            f"{C.DIM}Translator {C.RESET}  {C.MAGENTA}{bn}{C.RESET}  on  {cfg['translation_host']}:{cfg['translation_port']}",
            style=C.DIM,
        )
        # Update check — truly non-blocking (runs in background thread)
        # Shows result on next menu render if available
        global _update_result
        if _update_result is None:
            _update_result = [None, None, False]  # [latest, url, checked]
            def _bg_check():
                global _update_result
                try:
                    latest, url = check_for_updates()
                    _update_result = [latest, url, True]
                except Exception:
                    _update_result = [None, None, True]
            threading.Thread(target=_bg_check, daemon=True).start()
        if _update_result[2] and _update_result[0]:
            print(f"  {C.YELLOW}⬆{C.RESET}  Update available: v{_update_result[0]}  {C.DIM}{_update_result[1]}{C.RESET}")



        ch = ask_choice("What would you like to do?", [
            ("Launch Yume", "Start servers + runtime menu"),
            ("System Status", "Hardware, tools, packages, ports"),
            ("Health Check", "Test every component end-to-end"),
            ("Settings", "Whisper, translation, addresses, subtitles"),
            ("Tools & Fonts", "Install, update, manage components, detect fonts"),
            ("Re-run Setup", "Guided first-time configuration"),
            ("Uninstall", "Remove Yume and all data"),
            ("Exit", None),
        ], default=0, allow_back=False)

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

def main():
    # Logging: --verbose or LOG_LEVEL env enables debug output
    verbose = "--verbose" in sys.argv or "-v" in sys.argv
    env_level = os.environ.get("LOG_LEVEL", "").upper()
    log_level = logging.DEBUG if (verbose or env_level == "DEBUG") else max(getattr(logging, env_level, logging.WARNING), logging.DEBUG)
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
