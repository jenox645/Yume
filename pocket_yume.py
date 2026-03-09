#!/usr/bin/env python3
"""
Pocket Yume CLI v5.0.0 -- Cross-platform installer & launcher for Yume AI Subtitles
Complete rewrite: smart port management, API token auth, Windows cp1252 fix
Supports: Windows, Linux, macOS
"""

import os, sys, json, time, shutil, platform, subprocess, urllib.request, urllib.error, zipfile, threading, tarfile, re, traceback, logging
from pathlib import Path
import socket as _socket

_log = logging.getLogger('pocket_yume')

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

VERSION = "5.0.0"

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
UNIX_EXEC_MODE = 0o755
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
    else:  # Linux
        urls["yt-dlp"] = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_linux"

    # FFmpeg
    if IS_WIN:
        urls["ffmpeg"] = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"
    elif IS_MAC:
        if "arm" in ARCH or "aarch" in ARCH:
            urls["ffmpeg"] = "https://www.osxexperts.net/ffmpeg7arm.zip"
        else:
            urls["ffmpeg"] = "https://evermeet.cx/ffmpeg/getrelease/zip"
    else:  # Linux
        urls["ffmpeg"] = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz"

    # Deno
    if IS_WIN:
        urls["deno"] = "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip"
    elif IS_MAC:
        if "arm" in ARCH or "aarch" in ARCH:
            urls["deno"] = "https://github.com/denoland/deno/releases/latest/download/deno-aarch64-apple-darwin.zip"
        else:
            urls["deno"] = "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-apple-darwin.zip"
    else:  # Linux
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
# BACKEND DEFINITIONS  (llamacpp is DEFAULT -- no middleman)
# ────

BACKEND_INFO = {
    "llamacpp": {
        "name": "llama.cpp (built-in)",
        "desc": "DEFAULT. Drop a .gguf in models/translation/, Yume loads it directly.",
        "dh": "127.0.0.1", "dp": DEFAULT_TRANSLATION_PORT,
        "hp": "/health", "ap": "/v1/chat/completions",
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
        "hp": "/api/tags", "ap": "/v1/chat/completions",
        "inst": "https://ollama.com -- or auto-install via Pocket Yume",
    },
    "lmstudio": {
        "name": "LM Studio",
        "desc": "GUI app with model browser.",
        "dh": "127.0.0.1", "dp": 1234,
        "hp": "/v1/models", "ap": "/v1/chat/completions",
        "inst": "Download from: https://lmstudio.ai",
    },
    "textgenwebui": {
        "name": "text-generation-webui",
        "desc": "Feature-rich web UI by oobabooga.",
        "dh": "127.0.0.1", "dp": DEFAULT_TRANSLATION_PORT,
        "hp": "/v1/models", "ap": "/v1/chat/completions",
        "inst": "https://github.com/oobabooga/text-generation-webui",
    },
    "custom": {
        "name": "Custom (OpenAI-compatible)",
        "desc": "Any server with /v1/chat/completions endpoint.",
        "dh": "127.0.0.1", "dp": DEFAULT_TRANSLATION_PORT,
        "hp": "/v1/models", "ap": "/v1/chat/completions",
        "inst": "Provide your own endpoint.",
    },
}

# DEFAULT_CONFIG, load_config, save_config, validate_*, config_* -> config.py

# 
# ────
# UI HELPERS  (ALL ASCII-SAFE)
# ────

class C:
    RESET  = "\033[0m"; BOLD = "\033[1m"; DIM = "\033[2m"
    RED    = "\033[91m"; GREEN  = "\033[92m"; YELLOW = "\033[93m"
    BLUE   = "\033[94m"; MAGENTA= "\033[95m"; CYAN   = "\033[96m"
    WHITE  = "\033[97m"; GOLD   = "\033[38;5;220m"; PURPLE = "\033[38;5;141m"

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
            # Windows Terminal / modern consoles support Unicode
            os.system("")  # enable VT100
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
        top = f"{b['tl']}{t}{b['h'] * (w - 2 - len(t))}{b['tr']}"
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
        os.system("chcp 65001 >nul 2>&1")

def clear():
    os.system("cls" if IS_WIN else "clear")

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
        for l in logo:
            print(center(l))
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
            print(); return default
        if r == "": return default
        if r in ("y", "yes"): return True
        if r in ("n", "no"):  return False

def ask_input(prompt, default=""):
    h = f" [{default}]" if default else ""
    try:
        r = input(f"  {C.GOLD}?{C.RESET}  {prompt}{C.DIM}{h}{C.RESET}: ").strip()
        return r if r else default
    except (EOFError, KeyboardInterrupt):
        print(); return default

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
            print(); return -1 if allow_back else default
        if r == "": return default
        if r == "b" and allow_back: return -1
        try:
            c = int(r) - 1
            if 0 <= c < len(options): return c
        except ValueError:
            pass

# ────
# FILE / NETWORK HELPERS
# ────

def download_file(url, dest, label="Downloading"):
    """Download a file with progress bar, speed, and ETA display."""
    dest = Path(dest); dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "PocketYume/4.0"})
        with urllib.request.urlopen(req, timeout=180) as resp:
            total = int(resp.headers.get("content-length", 0)); dl = 0
            t0 = time.time()
            with open(dest, "wb") as f:
                while True:
                    chunk = resp.read(DOWNLOAD_CHUNK_SIZE)  # 64KB chunks
                    if not chunk: break
                    f.write(chunk); dl += len(chunk)
                    elapsed = max(0.1, time.time() - t0)
                    speed = dl / elapsed  # bytes/sec
                    speed_mb = speed / MiB
                    if total > 0:
                        pct = min(100, dl * 100 // total)
                        remaining = (total - dl) / speed if speed > 0 else 0
                        eta_str = f"{int(remaining//60)}m{int(remaining%60):02d}s" if remaining > 60 else f"{int(remaining)}s"
                        mb = dl / MiB; tmb = total / MiB
                        bar_w = 20; filled = bar_w * pct // 100
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
        return False
    except urllib.error.URLError as e:
        print(f"\r  {C.RED}✗{C.RESET}  {label} — Connection failed: {e.reason}" + " " * 20)
        info("Check your internet connection and try again")
        return False
    except Exception as e:
        print(f"\r  {C.RED}✗{C.RESET}  {label} — FAILED: {e}" + " " * 20)
        return False



def detect_gpu():
    r = {"has_nvidia": False, "has_amd": False, "name": None, "vram_mb": 0, "vendor": "none"}
    # Check NVIDIA
    try:
        out = _run(["nvidia-smi", "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"], timeout=10)
        if out.returncode == 0 and out.stdout.strip():
            p = out.stdout.strip().split(",")
            r["has_nvidia"] = True; r["name"] = p[0].strip(); r["vendor"] = "nvidia"
            r["vram_mb"] = int(p[1].strip()) if len(p) > 1 else 0
            return r
    except Exception as e:
        _log.debug('[detect_gpu] nvidia-vram-parse failed: %s', e)



    # Check AMD/Radeon via rocm-smi (Linux ROCm)
    try:
        out = _run(["rocm-smi", "--showproductname", "--showmeminfo", "vram", "--csv"], timeout=10)
        if out.returncode == 0 and out.stdout.strip():
            lines = out.stdout.strip().split("\n")
            for line in lines[1:]:  # skip header
                if line.strip():
                    r["has_amd"] = True; r["vendor"] = "amd"
                    r["name"] = line.split(",")[0].strip() if "," in line else "AMD GPU"
                    break
            # Try to get VRAM
            try:
                out2 = _run(["rocm-smi", "--showmeminfo", "vram"], timeout=10)
                for l2 in out2.stdout.split("\n"):
                    if "Total" in l2:
                        nums = [int(s) for s in l2.split() if s.isdigit()]
                        if nums: r["vram_mb"] = nums[0] // MiB if nums[0] > 1_000_000 else nums[0]
            except Exception as e:
                _log.debug('[detect_gpu] nvidia-wmi-parse failed: %s', e)


            return r
    except Exception as e:
        _log.debug('[detect_gpu] nvidia-smi failed: %s', e)

    # Check AMD via rocminfo (fallback)
    try:
        out = _run(["rocminfo"], timeout=10)
        if out.returncode == 0 and "gfx" in out.stdout.lower():
            r["has_amd"] = True; r["vendor"] = "amd"
            for line in out.stdout.split("\n"):
                if "Marketing Name" in line:
                    r["name"] = line.split(":")[-1].strip()
                    break
            if not r["name"]: r["name"] = "AMD GPU (ROCm)"
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
                    r["has_amd"] = True; r["vendor"] = "amd"
                    r["name"] = parts[2].strip() if len(parts) > 2 else "AMD GPU"
                    try:
                        r["vram_mb"] = int(parts[1].strip()) // MiB if len(parts) > 1 and parts[1].strip().isdigit() else 0
                    except Exception as e:
                        _log.debug('[detect_gpu] wmic-amd-vram-parse failed: %s', e)

                    return r
        except Exception as e:
            _log.debug('[detect_gpu] wmic-amd failed: %s', e)

    return r

def detect_ram_gb():
    try:
        if IS_WIN:
            import ctypes
            class MS(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong)] + [("_"+str(i), ctypes.c_ulonglong) for i in range(6)]
            s = MS(); s.dwLength = ctypes.sizeof(s)
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
    if b.exists(): return str(b)
    # Check without extension on Unix (yt-dlp_linux renamed to yt-dlp)
    if not IS_WIN:
        b2 = TOOLS_DIR / name
        if b2.exists(): return str(b2)
    return shutil.which(name)

def check_server(host, port, path="/health"):
    try:
        url = f"http://{host}:{port}{path}"
        req = urllib.request.Request(url, headers={"User-Agent": "PocketYume"})
        with urllib.request.urlopen(req, timeout=3) as resp:
            return {"up": True, "data": json.loads(resp.read())}
    except Exception:
        return {"up": False, "data": {}}

def check_ollama_models(host="127.0.0.1", port=DEFAULT_OLLAMA_PORT):
    try:
        req = urllib.request.Request(f"http://{host}:{port}/api/tags", headers={"User-Agent": "PocketYume"})
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
    ch = ask_choice("How to resolve?", [
        ("Kill the process", f"Terminate {name or 'PID '+str(pid)}"),
        ("Use a different port", "Auto-find a free port"),
        ("Enter port manually", None),
        ("Cancel", None),
    ], default=0)
    if ch == 0:
        if kill_port_process(port) and is_port_free(port):
            success(f"Port {port} is now free"); return port
        error("Failed to free port"); return None
    elif ch == 1:
        new_port = find_free_port(port + 1, exclude=exclude)
        if new_port:
            cfg[f"{key_prefix}_port"] = new_port; save_config(cfg)
            success(f"Reassigned to port {new_port}"); return new_port
        error("No free port found"); return None
    elif ch == 2:
        np = ask_input("Port number", str(port + 1))
        try:
            np = int(np)
            if is_port_free(np):
                cfg[f"{key_prefix}_port"] = np; save_config(cfg); return np
            else:
                error(f"Port {np} is also in use"); return None
        except ValueError:
            error("Invalid port"); return None
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
        req = urllib.request.Request(f"http://{host}:{port}/health", headers={"User-Agent": "PocketYume"})
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
        headers = {"User-Agent": "PocketYume"}
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
        headers = {"Content-Type": "application/json", "User-Agent": "PocketYume"}
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
        error(f"Whisper server not reachable at {h}:{p}"); return

    header("Server Statistics")
    gpu = data.get("gpu")
    if gpu:
        pct = round(gpu["vram_used_mb"] / gpu["vram_total_mb"] * 100) if gpu["vram_total_mb"] else 0
        bar_w = 30; filled = round(bar_w * pct / 100)
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
        _menu_blacklist(cfg); return
    subcmd = args[0].lower()
    if subcmd == "list":
        data = _server_get(h, p, "/blacklist")
        if not data: error(f"Server not reachable at {h}:{p}"); return
        bl = data.get("blacklist", [])
        if not bl: info("Blacklist is empty"); return
        info(f"Server blacklist ({len(bl)} items):")
        for item in bl: bullet(item)
    elif subcmd == "add" and len(args) > 1:
        text = " ".join(args[1:])
        data = _server_get(h, p, "/blacklist")
        if not data: error(f"Server not reachable"); return
        current = data.get("blacklist", [])
        if text in current: warn(f"Already blocked: {text}"); return
        current.append(text)
        result = _server_post(h, p, "/blacklist/update", {"blacklist": current})
        if result and result.get("success"): success(f"Added: {text}")
        else: error(f"Failed: {result}")
    elif subcmd in ("remove", "rm") and len(args) > 1:
        text = " ".join(args[1:])
        data = _server_get(h, p, "/blacklist")
        if not data: error(f"Server not reachable"); return
        current = data.get("blacklist", [])
        if text not in current: warn(f"Not in blacklist: {text}"); return
        current.remove(text)
        result = _server_post(h, p, "/blacklist/update", {"blacklist": current})
        if result and result.get("success"): success(f"Removed: {text}")
        else: error(f"Failed: {result}")
    elif subcmd == "clear":
        result = _server_post(h, p, "/blacklist/update", {"blacklist": []})
        if result and result.get("success"): success("Blacklist cleared")
        else: error(f"Failed: {result}")
    else:
        print(f"  Usage: pocket_yume.py blacklist [list|add <text>|remove <text>|clear]")


def cli_model(cfg, args):
    """CLI model management."""
    h, p = cfg["whisper_host"], cfg["whisper_port"]
    if len(args) < 1:
        data = _server_get(h, p, "/stats")
        if not data:
            error(f"Server not reachable at {h}:{p}")
            info(f"Config model: {cfg.get('whisper_model', '?')}"); return
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
        if not result: error(f"Server not reachable at {h}:{p}"); return
        if result.get("error"):
            error(result["error"])
            if result.get("valid"): info(f"Valid: {', '.join(result['valid'])}")
        elif result.get("status") == "already_loaded":
            info(f"Already using {new_model}")
        else:
            success(f"Switched to {result.get('model', new_model)}")
            cfg["whisper_model"] = result.get("model", new_model); save_config(cfg)
    elif subcmd == "list":
        models = [("tiny","~1GB","Fastest"),("base","~1GB","Fast"),("small","~2GB","Good"),
                  ("medium","~5GB","High"),("large-v2","~10GB","Very high"),("large-v3","~10GB","Best"),
                  ("distil-large-v2","~4GB","Fast+acc"),("distil-large-v3","~4GB","Fast+acc (new)")]
        cur = cfg.get("whisper_model", "large-v3")
        info("Available Whisper models:")
        for name, vram, desc in models:
            act = f" {C.GREEN}<- active{C.RESET}" if name == cur else ""
            bullet(f"{name:20s} {vram:8s} {desc}{act}")
    else:
        print(f"  Usage: pocket_yume.py model [list|switch <name>]")


def _menu_blacklist(cfg):
    """Blacklist management menu."""
    while True:
        header("Hallucination Blacklist")
        h, p = cfg["whisper_host"], cfg["whisper_port"]
        data = _server_get(h, p, "/blacklist")
        if not data:
            warn(f"Server not reachable at {h}:{p}"); info("Start server first"); pause(); return
        bl = data.get("blacklist", [])
        info(f"Server blacklist: {len(bl)} items")
        if bl:
            for item in bl[:15]: bullet(item)
            if len(bl) > 15: info(f"  ... and {len(bl) - 15} more")
        ch = ask_choice("Options:", [
            ("Add entry", "Block a phrase from subtitles"),
            ("Remove entry", "Unblock a phrase"),
            ("Clear all", "Remove all entries"),
            ("Back", None)
        ], default=3)
        if ch == -1 or ch == 3: return
        elif ch == 0:
            text = ask_input("Phrase to block", "")
            if text:
                current = bl[:]
                if text in current: warn(f"Already blocked"); pause(); continue
                current.append(text)
                r = _server_post(h, p, "/blacklist/update", {"blacklist": current})
                if r and r.get("success"): success(f"Added: {text}")
                else: error("Failed")
            pause()
        elif ch == 1:
            if not bl: info("Empty"); pause(); continue
            opts = [(item, None) for item in bl[:20]] + [("Back", None)]
            rc = ask_choice("Remove which?", opts, default=len(opts)-1)
            if 0 <= rc < len(bl):
                removed = bl[rc]; current = bl[:]; current.pop(rc)
                r = _server_post(h, p, "/blacklist/update", {"blacklist": current})
                if r and r.get("success"): success(f"Removed: {removed}")
            pause()
        elif ch == 2:
            if ask_yn("Clear ALL?", False):
                r = _server_post(h, p, "/blacklist/update", {"blacklist": []})
                if r and r.get("success"): success("Cleared")
            pause()


def _menu_whisper_model(cfg):
    """Interactive whisper model hot-swap."""
    header("Whisper Model")
    h, p = cfg["whisper_host"], cfg["whisper_port"]
    data = _server_get(h, p, "/stats")
    cur = data.get("model", cfg.get("whisper_model", "?")) if data else cfg.get("whisper_model", "large-v3")
    if data:
        info(f"Active: {C.BOLD}{cur}{C.RESET}  ({data.get('device', '?')})")
        if data.get("gpu"):
            g = data["gpu"]; info(f"GPU: {g['gpu_name']} ({g['vram_used_mb']}/{g['vram_total_mb']} MB)")
    else:
        warn(f"Server not running. Config: {cur}")
    models = ["tiny", "base", "small", "medium", "large-v2", "large-v3", "distil-large-v2", "distil-large-v3"]
    vram = {"tiny":"~1GB","base":"~1GB","small":"~2GB","medium":"~5GB",
            "large-v2":"~10GB","large-v3":"~10GB","distil-large-v2":"~4GB","distil-large-v3":"~4GB"}
    opts = [(f"{m} ({vram.get(m,'?')})", "active" if m == cur else None) for m in models] + [("Back", None)]
    di = models.index(cur) if cur in models else len(models)
    ch = ask_choice("Switch to:", opts, default=di)
    if ch == -1 or ch == len(models): return
    new_model = models[ch]
    if new_model == cur: info("Already active"); pause(); return
    if not data:
        cfg["whisper_model"] = new_model; save_config(cfg)
        success(f"Config set to {new_model} (applies on next launch)"); pause(); return
    info(f"Switching to {new_model}...")
    result = _server_post(h, p, "/model/switch", {"model": new_model}, timeout=120)
    if result and not result.get("error"):
        success(f"Switched to {result.get('model', new_model)}")
        cfg["whisper_model"] = result.get("model", new_model); save_config(cfg)
    else:
        error(f"Failed: {result.get('error', 'unknown')}")
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
        if not download_file(DOWNLOAD_URLS["ffmpeg"], zp, "FFmpeg"): return False
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
            return True
        except Exception as e:
            print(f"\r  {C.RED}x{C.RESET}  Failed: {e}")
            return False

    elif IS_LIN:
        tp = TOOLS_DIR / "ffmpeg.tar.xz"
        if not download_file(DOWNLOAD_URLS["ffmpeg"], tp, "FFmpeg"): return False
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
                if bf.exists(): os.chmod(bf, UNIX_EXEC_MODE)
            print(f"\r  {C.GREEN}+{C.RESET}  Extracted" + " " * 30)
            return True
        except Exception as e:
            print(f"\r  {C.RED}x{C.RESET}  Failed: {e}")
            return False

    else:  # macOS
        # Try brew first
        if shutil.which("brew"):
            info("Installing FFmpeg via Homebrew...")
            r = _run(["brew", "install", "ffmpeg"], timeout=600)
            return r.returncode == 0
        zp = TOOLS_DIR / "ffmpeg.zip"
        if not download_file(DOWNLOAD_URLS["ffmpeg"], zp, "FFmpeg"): return False
        try:
            with zipfile.ZipFile(zp) as zf:
                zf.extractall(TOOLS_DIR)
            zp.unlink(missing_ok=True)
            for b in (TOOLS_DIR / "ffmpeg", ):
                if b.exists(): os.chmod(b, UNIX_EXEC_MODE)
            return True
        except Exception as e:
            error(f"Failed: {e}"); return False

def install_deno():
    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    zp = TOOLS_DIR / "deno.zip"
    if not download_file(DOWNLOAD_URLS["deno"], zp, "Deno"): return False
    try:
        with zipfile.ZipFile(zp) as zf:
            zf.extractall(TOOLS_DIR)
        zp.unlink(missing_ok=True)
        # Make executable on Unix
        de = TOOLS_DIR / f"deno{EXE}"
        if de.exists() and not IS_WIN:
            os.chmod(de, UNIX_EXEC_MODE)
        success("Deno extracted")
        return True
    except Exception as e:
        error(f"Failed: {e}"); return False

def install_ollama():
    if IS_WIN:
        TOOLS_DIR.mkdir(parents=True, exist_ok=True)
        inst = TOOLS_DIR / "OllamaSetup.exe"
        if not download_file(DOWNLOAD_URLS["ollama"], inst, "Ollama installer"): return False
        print(f"\n  {C.YELLOW}The Ollama installer will now open. Follow the prompts.{C.RESET}\n")
        pause("Press Enter to launch installer...")
        try:
            subprocess.Popen([str(inst)], shell=True)
            info("Waiting for Ollama...")
            for _ in range(90):
                time.sleep(2)
                if shutil.which("ollama") or check_server("127.0.0.1", DEFAULT_OLLAMA_PORT, "/api/tags")["up"]:
                    success("Ollama installed!"); inst.unlink(missing_ok=True); return True
            warn("Timed out."); return False
        except Exception as e:
            error(f"Failed: {e}"); return False
    elif IS_MAC:
        zp = TOOLS_DIR / "Ollama.zip"
        if not download_file(DOWNLOAD_URLS["ollama"], zp, "Ollama"): return False
        info("Extract and move to Applications manually.")
        return True
    else:  # Linux
        info("Installing Ollama via official script...")
        try:
            r = _run(["bash", "-c", "curl -fsSL https://ollama.com/install.sh | sh"], timeout=300)
            return r.returncode == 0
        except Exception as e:
            error(f"Failed: {e}"); return False

def install_python_deps():
    """Install flask + faster-whisper deps."""
    req = SERVER_DIR / "requirements.txt"
    if not req.exists():
        req = BASE_DIR / "requirements.txt"  # backward compat
    if not req.exists():
        SERVER_DIR.mkdir(parents=True, exist_ok=True)
        req.write_text("faster-whisper>=1.0.0\nflask>=2.3.0\nflask-cors>=4.0.0\n")
    info("Installing Whisper server Python packages...")
    try:
        r = _run(
            [sys.executable, "-m", "pip", "install", "-r", str(req), "-q", "--no-warn-script-location"],
            timeout=600
        )
        if r.returncode != 0:
            raise subprocess.CalledProcessError(r.returncode, "pip install")
        success("Done!")
        return True
    except Exception as e:
        error(f"Failed: {e}"); return False

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
    gpu = detect_gpu()

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
                        print(f"    sudo dnf install ninja-build cmake gcc-c++")
                    elif pm == "apt":
                        print(f"    sudo apt install ninja-build cmake g++")
                    else:
                        print(f"    Install: ninja-build cmake g++ (or gcc-c++)")
                elif IS_MAC:
                    print(f"    brew install ninja cmake")
                return False

    # Step 1: Install llama-cpp-python
    installed = False
    if gpu["has_nvidia"]:
        info("NVIDIA GPU detected. Installing llama-cpp-python with CUDA...")
        if IS_WIN:
            try:
                r = _run([
                    sys.executable, "-m", "pip", "install", "llama-cpp-python",
                    "--extra-index-url", "https://abetlen.github.io/llama-cpp-python/whl/cu124",
                    "-q", "--no-warn-script-location"
                ], timeout=600)
                if r.returncode == 0:
                    success("llama-cpp-python (CUDA) installed!"); installed = True
            except Exception as e:
                _log.debug('[install_llamacpp_python] pip-cuda-prebuilt failed: %s', e)


            if not installed:
                warn("Prebuilt failed, trying CPU version...")
        else:
            env = os.environ.copy()
            env["CMAKE_ARGS"] = "-DGGML_CUDA=on"
            try:
                r = _run([
                    sys.executable, "-m", "pip", "install", "llama-cpp-python",
                    "--force-reinstall", "--no-cache-dir", "-q"
                ], env=env, timeout=600)
                if r.returncode == 0:
                    success("llama-cpp-python (CUDA) installed!"); installed = True
            except Exception as e:
                _log.debug('[install_llamacpp_python] pip-cuda-source failed: %s', e)


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
            try:
                r = _run([
                    sys.executable, "-m", "pip", "install", "llama-cpp-python",
                    "--force-reinstall", "--no-cache-dir"
                ], env=env, timeout=900)
                if r.returncode == 0:
                    success("llama-cpp-python (ROCm/HIP) installed!"); installed = True
            except Exception as e:
                _log.debug('[install_llamacpp_python] pip-rocm-hip failed: %s', e)


            if not installed:
                warn("ROCm build failed. Check that ROCm is installed:")
                info("  Fedora: sudo dnf install rocm-hip-runtime rocm-hip-sdk")
                info("  Ubuntu: https://rocm.docs.amd.com/en/latest/deploy/linux/")
                info("Falling back to CPU...")

    if not installed:
        info("Installing llama-cpp-python (CPU)...")

        # Try prebuilt CPU wheel first (no ninja/cmake/gcc needed!)
        info("Trying prebuilt CPU wheel...")
        try:
            r = _run([
                sys.executable, "-m", "pip", "install", "llama-cpp-python",
                "--extra-index-url", "https://abetlen.github.io/llama-cpp-python/whl/cpu",
                "--no-warn-script-location"
            ], timeout=300)
            if r.returncode == 0:
                success("llama-cpp-python (prebuilt CPU) installed!"); installed = True
        except Exception as e:
            _log.debug('[install_llamacpp_python] pip-cpu-prebuilt failed: %s', e)



        if not installed:
            warn("Prebuilt wheel not available, building from source...")
            # Show output on Linux so user can see compilation progress
            stdout_opt = None if IS_LIN else subprocess.DEVNULL
            try:
                r = _run([
                    sys.executable, "-m", "pip", "install", "llama-cpp-python",
                    "--no-warn-script-location"
                ], timeout=900, stdout=stdout_opt)
                if r.returncode == 0:
                    success("llama-cpp-python installed!"); installed = True
                else:
                    error("Build failed. Check error output above.")
            except subprocess.TimeoutExpired:
                error("Build timed out (15 min). The machine may be too slow for compilation.")
            except Exception as e:
                error(f"Failed: {e}")

    # Step 2: Install server dependencies (uvicorn, fastapi, etc.)
    if installed:
        info("Installing server dependencies (uvicorn, fastapi)...")
        try:
            r = _run([
                sys.executable, "-m", "pip", "install",
                "uvicorn>=0.20.0", "fastapi>=0.100.0", "sse-starlette>=1.6.0",
                "starlette-context>=0.3.0", "pydantic-settings>=2.0.0",
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
            success(f"{name} ready!"); return True
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
            headers={"User-Agent": "PocketYume/3.2"}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            files = json.loads(resp.read())
        out = []
        for f in files:
            if f.get("path", "").endswith(".gguf"):
                sb = f.get("size", 0); sg = sb / GiB
                out.append({
                    "name": f["path"], "bytes": sb,
                    "size": f"{sg:.2f} GB" if sg >= 1 else f"{sb/MiB:.0f} MB"
                })
        return out
    except urllib.error.HTTPError as e:
        error(f"HuggingFace error: {e.code}"); return []
    except Exception as e:
        error(f"Failed: {e}"); return []

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

        print(); info("Recommended repos for Japanese -> English:"); print()
        bullet(f"{C.CYAN}Qwen/Qwen2.5-7B-Instruct-GGUF{C.RESET}       -- best JP->EN, 7B")
        bullet(f"{C.CYAN}Qwen/Qwen2.5-14B-Instruct-GGUF{C.RESET}      -- more accurate, 14B")
        bullet(f"{C.CYAN}Qwen/Qwen2.5-3B-Instruct-GGUF{C.RESET}       -- lighter, 3B")
        bullet(f"{C.CYAN}bartowski/gemma-2-9b-it-GGUF{C.RESET}         -- Google Gemma 2, 9B")
        print()
        repo = ask_input("HuggingFace repo (owner/model-name)", "")
        if not repo: return

        info(f"Fetching files from {repo}...")
        files = hf_list_gguf(repo)
        if not files:
            warn("No .gguf files found. Use a GGUF repo (usually has '-GGUF' suffix).")
            pause(); continue

        print(f"\n  {C.BOLD}Files in {repo}:{C.RESET}\n")
        opts = []
        for f in files:
            sg = f["bytes"] / GiB; fit = ""
            if gpu["has_nvidia"]:
                vg = gpu["vram_mb"] / KiB
                if sg * 1.15 < vg:    fit = f" {C.GREEN}+ fits GPU{C.RESET}"
                elif sg < vg:         fit = f" {C.YELLOW}~ tight{C.RESET}"
                else:                 fit = f" {C.RED}x too large{C.RESET}"
            opts.append((f"{f['name']}  ({f['size']}){fit}", None))
        opts.append(("Back", None))

        ch = ask_choice("Select file:", opts, default=len(opts)-1)
        if ch == -1 or ch == len(files): continue

        sel = files[ch]; print()
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
        pause(); return


# ────
# TOOLS MENU
# ────

def tools_menu(cfg):
    while True:
        header("Tools Management")
        yt = find_tool("yt-dlp"); ff = find_tool("ffmpeg"); dn = find_tool("deno")
        ch = ask_choice("Select a tool:", [
            (f"yt-dlp          {'OK' if yt else 'MISSING'}", "Audio downloader (supports 1000+ sites)"),
            (f"FFmpeg          {'OK' if ff else 'MISSING'}", "Audio converter"),
            (f"Deno            {'OK' if dn else '--'}", "Optional -- YouTube JS solver"),
            ("Translation Backend", "llama.cpp / Ollama / LM Studio / WebUI / Custom"),
            ("Download GGUF Model", "Browse HuggingFace repos"),
            ("Python Dependencies", "faster-whisper, flask, llama-cpp-python"),
            ("Test Translation", "Verify pipeline works"),
            ("Benchmark Whisper", "Compare model speeds on your hardware"),
            ("Detect Fonts", "Find installed CJK and system fonts"),
            ("Back", None),
        ], default=9)
        if ch == -1 or ch == 9: return
        elif ch == 0: _menu_ytdlp(cfg)
        elif ch == 1: _menu_ffmpeg()
        elif ch == 2: _menu_deno(cfg)
        elif ch == 3: _menu_backend(cfg)
        elif ch == 4: browse_hf(cfg)
        elif ch == 5: _menu_pydeps()
        elif ch == 6: _test_translation(cfg)
        elif ch == 7: benchmark_whisper(cfg)
        elif ch == 8: detect_fonts()

def _menu_ytdlp(cfg):
    while True:
        header("yt-dlp"); p = find_tool("yt-dlp")
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
        if ch == -1 or ch == 2: return
        elif ch == 0: install_ytdlp(); pause()
        elif ch == 1: _menu_yt_auth(cfg)

def _menu_yt_auth(cfg):
    while True:
        header("YouTube Authentication")
        cur = cfg.get("youtube_auth_method", "deno")
        info(f"Current: {C.BOLD}{cur}{C.RESET}"); print()
        ch = ask_choice("Select method:", [
            ("Deno (JS runtime)", "Automatic, no login needed. Requires Deno (~35 MB)"),
            ("Browser Cookies", "No extra software. Must be logged into YouTube.\n      Uses: --cookies-from-browser <browser>"),
            ("Back", None)
        ], default=0 if cur == "deno" else 1)
        if ch == -1 or ch == 2: return
        elif ch == 0:
            cfg["youtube_auth_method"] = "deno"; save_config(cfg); success("Set to Deno")
            if not find_tool("deno"):
                if ask_yn("Deno not installed. Download now?"): install_deno()
            pause()
        elif ch == 1:
            cfg["youtube_auth_method"] = "cookies"; save_config(cfg)
            browsers = ["chrome", "firefox", "edge", "brave", "opera", "chromium", "safari"]
            bc = ask_choice("Which browser?",
                [(b.capitalize(), None) for b in browsers] + [("Back", None)],
                default=0)
            if 0 <= bc < len(browsers):
                cfg["cookies_browser"] = browsers[bc]; save_config(cfg)
                success(f"Cookies from: {browsers[bc]}")
                warn("Make sure you're logged into YouTube in that browser!")
            pause()

def _menu_ffmpeg():
    while True:
        header("FFmpeg"); p = find_tool("ffmpeg")
        (success if p else warn)(f"{'Installed: '+p if p else 'Not installed'}")
        ch = ask_choice("Options:", [
            ("Install / Update", "Download latest static build"),
            ("Back", None)
        ], default=1)
        if ch == -1 or ch == 1: return
        elif ch == 0: install_ffmpeg(); pause()

def _menu_deno(cfg):
    while True:
        header("Deno (Optional)"); p = find_tool("deno")
        (success if p else info)(f"{'Installed: '+p if p else 'Not installed'}")
        print()
        info(f"Deno is {C.GREEN}optional{C.RESET}. Alternative: browser cookies.")
        info(f"Current YouTube auth: {C.BOLD}{cfg.get('youtube_auth_method', 'deno')}{C.RESET}")
        ch = ask_choice("Options:", [
            ("Install Deno", "~35 MB download"),
            ("Switch to cookies", "Use browser cookies instead"),
            ("Back", None)
        ], default=2)
        if ch == -1 or ch == 2: return
        elif ch == 0:
            install_deno(); cfg["youtube_auth_method"] = "deno"; save_config(cfg); pause()
        elif ch == 1:
            cfg["youtube_auth_method"] = "cookies"; save_config(cfg)
            success("Switched to cookies"); pause()

def _menu_backend(cfg):
    while True:
        header("Translation Backend")
        cur = cfg.get("translation_backend", "llamacpp")
        bi = BACKEND_INFO.get(cur, BACKEND_INFO["custom"])
        info(f"Current: {C.BOLD}{bi['name']}{C.RESET}")
        info(f"Address: {C.CYAN}{cfg.get('translation_host', '127.0.0.1')}:{cfg.get('translation_port', DEFAULT_TRANSLATION_PORT)}{C.RESET}")
        st = check_server(cfg.get("translation_host", "127.0.0.1"), cfg.get("translation_port", DEFAULT_TRANSLATION_PORT), bi.get("hp", "/health"))
        (success if st["up"] else warn)(f"Status: {'RUNNING' if st['up'] else 'Not running'}")

        ch = ask_choice("Options:", [
            ("Change backend", "Switch between llama.cpp/Ollama/LM Studio/WebUI/Custom"),
            ("Change address", f"Currently {cfg.get('translation_host')}:{cfg.get('translation_port')}"),
            ("Install instructions", f"How to set up {bi['name']}"),
            ("Manage model", "Pull, change, browse, or download models"),
            ("Back", None)
        ], default=4)
        if ch == -1 or ch == 4: return
        elif ch == 0: _select_backend(cfg)
        elif ch == 1: _change_addr(cfg, "translation")
        elif ch == 2:
            header(f"Install {bi['name']}")
            print(f"\n  {C.BOLD}{bi['name']}{C.RESET}\n  {bi['desc']}\n\n  {C.WHITE}Installation:{C.RESET}")
            for l in bi["inst"].split("\n"): print(f"  {l}")
            if cur == "llamacpp":
                print()
                if ask_yn("Install llama-cpp-python now?"): install_llamacpp_python()
            elif cur == "ollama":
                print()
                if ask_yn("Auto-install Ollama now?"): install_ollama()
            pause()
        elif ch == 3: _manage_model(cfg)

def _select_backend(cfg):
    header("Select Backend")
    keys = list(BACKEND_INFO.keys())
    opts = [(BACKEND_INFO[k]["name"], BACKEND_INFO[k]["desc"]) for k in keys] + [("Back", None)]
    cur = cfg.get("translation_backend", "llamacpp")
    di = keys.index(cur) if cur in keys else 0
    ch = ask_choice("Choose:", opts, default=di)
    if ch == -1 or ch == len(keys): return
    k = keys[ch]; bi = BACKEND_INFO[k]
    cfg["translation_backend"] = k
    cfg["translation_host"] = bi["dh"]
    cfg["translation_port"] = bi["dp"]
    save_config(cfg)
    success(f"Backend: {bi['name']}  ({bi['dh']}:{bi['dp']})")
    print(f"\n  {C.WHITE}Installation:{C.RESET}")
    for l in bi["inst"].split("\n"): print(f"  {l}")
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
    cfg[f"{prefix}_host"] = host; cfg[f"{prefix}_port"] = port
    save_config(cfg); success(f"Set to {host}:{port}"); pause()

def _manage_model(cfg):
    while True:
        header("Manage Translation Model")
        bk = cfg.get("translation_backend", "llamacpp")
        mdl = cfg.get("translation_model", "")
        gp = cfg.get("gguf_model_path", "")
        bi = BACKEND_INFO.get(bk, {})
        info(f"Backend: {C.BOLD}{bi.get('name', bk)}{C.RESET}")
        if mdl: info(f"Model:   {C.BOLD}{mdl}{C.RESET}")
        if gp:  info(f"GGUF:    {gp}")

        gf = find_gguf_models()
        if gf:
            print(); info(f"GGUF files in {GGUF_DIR}:")
            for f in gf:
                sg = f.stat().st_size / GiB
                act = f" {C.GREEN}<- active{C.RESET}" if str(f) == gp else ""
                bullet(f"{f.name}  ({sg:.2f} GB){act}")

        if bk == "ollama":
            ms = check_ollama_models(cfg.get("translation_host", "127.0.0.1"), cfg.get("translation_port", DEFAULT_OLLAMA_PORT))
            if ms:
                print(); info("Ollama models:")
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
        if ch == -1 or ch == 4: return
        elif ch == 0:
            nm = ask_input("Model name", mdl)
            if nm: cfg["translation_model"] = nm; save_config(cfg); success(f"Model: {nm}")
            pause()
        elif ch == 1:
            mn = ask_input("Ollama model to pull", mdl or "qwen2.5:7b")
            if mn:
                if not check_server(cfg.get("translation_host", "127.0.0.1"), cfg.get("translation_port", DEFAULT_OLLAMA_PORT), "/api/tags")["up"]:
                    info("Starting Ollama...")
                    try:
                        subprocess.Popen(["ollama", "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                        time.sleep(3)
                    except Exception:
                        error("Could not start Ollama"); pause(); continue
                pull_ollama_model(mn); cfg["translation_model"] = mn; save_config(cfg)
            pause()
        elif ch == 2:
            browse_hf(cfg)
        elif ch == 3:
            if not gf: warn(f"No .gguf files in {GGUF_DIR}"); pause(); continue
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
                        cfg["translation_host"] = bi2["dh"]; cfg["translation_port"] = bi2["dp"]
                        save_config(cfg)
            pause()

def _menu_pydeps():
    header("Python Dependencies")
    deps = {}
    for p in ["faster_whisper", "flask", "flask_cors", "llama_cpp", "uvicorn", "fastapi"]:
        try: __import__(p); deps[p] = True
        except ImportError: deps[p] = False
    for p, ok in deps.items():
        (success if ok else warn)(f"{p}: {'installed' if ok else 'NOT installed'}")
    if all(deps.values()):
        success("All installed!")
    elif ask_yn("Install missing?"):
        install_python_deps()
        if not deps.get("llama_cpp", False):
            install_llamacpp_python()
    pause()

def _test_translation(cfg):
    header("Test Translation")
    bk = cfg.get("translation_backend", "llamacpp")
    h = cfg.get("translation_host", "127.0.0.1")
    p = cfg.get("translation_port", DEFAULT_TRANSLATION_PORT)
    m = cfg.get("translation_model", "")
    bi = BACKEND_INFO.get(bk, BACKEND_INFO["custom"])

    info(f"Backend: {bi['name']} ({h}:{p})")
    if m: info(f"Model: {m}")
    print()

    st = check_server(h, p, bi.get("hp", "/health"))
    if not st["up"]:
        error(f"Server not reachable at {h}:{p}")
        if bk == "llamacpp":
            warn("Start the server first: Launch Yume from main menu")
        pause(); return
    success("Server reachable!")

    txt = ask_input("Test sentence (Japanese)", "\u4eca\u65e5\u306f\u3044\u3044\u5929\u6c17\u3067\u3059\u306d")
    info(f"Sending: {txt}"); print()
    try:
        body = {
            "messages": [
                {"role": "system", "content": "You are a translation system. Output ONLY the English translation."},
                {"role": "user", "content": txt}
            ],
            "max_tokens": 200, "temperature": 0.1, "stream": False
        }
        if bk == "ollama": body["model"] = m
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            f"http://{h}:{p}{bi['ap']}", data=data,
            headers={"Content-Type": "application/json", "User-Agent": "PocketYume"},
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
            print(); success(f"Translation: {C.BOLD}{tr.strip()}{C.RESET}")
            print(); success("Pipeline working!")
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
        # GPU mode
        if vram >= 10000:
            return "large-v3", f"GPU with {vram} MB VRAM → large-v3 (best accuracy)"
        elif vram >= 6000:
            return "distil-large-v3", f"GPU with {vram} MB VRAM → distil-large-v3 (fast + accurate)"
        elif vram >= 4000:
            return "small", f"GPU with {vram} MB VRAM → small (recommended)"
        elif vram >= 2000:
            return "base", f"GPU with {vram} MB VRAM → base"
        else:
            return "tiny", f"GPU with {vram} MB VRAM → tiny"




def check_for_updates():
    """Check GitHub for newer PocketYume releases."""
    try:
        req = urllib.request.Request(
            "https://api.github.com/repos/PocketYume/PocketYume/releases/latest",
            headers={"User-Agent": "PocketYume/4.0", "Accept": "application/vnd.github.v3+json"}
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
        ts = check_server(cfg["translation_host"], cfg["translation_port"], "/v1/models")
    results["translation"] = ts["up"]
    
    # Check common ports for Ollama
    if not results["translation"] and bk == "ollama":
        for port in [DEFAULT_OLLAMA_PORT, DEFAULT_TRANSLATION_PORT, 8080]:
            if port != cfg["translation_port"]:
                ts2 = check_server("127.0.0.1", port, "/api/tags")
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
    for t in ["yt-dlp", "ffmpeg"]:
        p = find_tool(t)
        results.append((f"{t} installed", p is not None, p or "not found"))
    dn = find_tool("deno")
    if cfg.get("youtube_auth_method") != "cookies":
        results.append(("deno installed", dn is not None, dn or "not found (needed for YouTube)"))

    # 3. Python packages
    for pkg, name in [("faster_whisper", "faster-whisper"), ("flask", "Flask"),
                       ("flask_cors", "Flask-CORS"), ("llama_cpp", "llama-cpp-python")]:
        try:
            __import__(pkg)
            results.append((f"{name}", True, "installed"))
        except ImportError:
            results.append((f"{name}", False, "not installed"))

    # 4. Config validity
    c = load_config()
    results.append(("Config loadable", bool(c), "OK" if c else "corrupted"))
    wp = c.get("whisper_port", DEFAULT_WHISPER_PORT); tp = c.get("translation_port", DEFAULT_TRANSLATION_PORT)
    results.append(("Ports different", wp != tp, f"whisper:{wp} translation:{tp}"))

    # 5. Port availability
    results.append((f"Whisper port {wp} free", is_port_free(wp), "available" if is_port_free(wp) else "in use"))
    results.append((f"Translation port {tp} free", is_port_free(tp), "available" if is_port_free(tp) else "in use"))

    # 6. Server files
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
        info("Fix these before launching for best results.")

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
        if ym == 'cookies': ym += f" ({cfg.get('cookies_browser', 'chrome')})"
        table(
            ["Setting", "Value"],
            [
                [f"{C.GOLD}Whisper Model{C.RESET}", cfg['whisper_model']],
                [f"{C.GOLD}Device / Compute{C.RESET}", f"{cfg['whisper_device']} / {cfg['whisper_compute_type']}"],
                [f"{C.GOLD}Whisper Address{C.RESET}", f"{cfg['whisper_host']}:{cfg['whisper_port']}"],
                ["", ""],
                [f"{C.MAGENTA}Translation{C.RESET}", bi['name']],
                [f"{C.MAGENTA}TL Address{C.RESET}", f"{cfg['translation_host']}:{cfg['translation_port']}"],
                [f"{C.MAGENTA}TL Model{C.RESET}", cfg.get('translation_model', '—')],
                ["", ""],
                [f"{C.CYAN}Chunk Duration{C.RESET}", f"{cfg['chunk_duration']}s"],
                [f"{C.CYAN}Word Timestamps{C.RESET}", "Yes" if cfg['word_timestamps'] else "No"],
                [f"{C.CYAN}Pause Threshold{C.RESET}", f"{cfg['pause_threshold']}s"],
                ["", ""],
                [f"{C.YELLOW}YouTube Auth{C.RESET}", ym],
            ],
            col_styles=[C.RESET, C.CYAN],
            title="Current Settings"
        )

        ch = ask_choice("Change:", [
            ("Whisper settings", "Model, device, compute, batch"),
            ("Translation settings", "Backend, address, model"),
            ("Server addresses", "Host/port for Whisper and Translation"),
            ("Subtitle tuning", "Chunk, word timestamps, pause threshold"),
            ("YouTube auth", "Deno vs cookies"),
            ("Export config", "Save settings to a backup file"),
            ("Import config", "Load settings from a backup file"),
            ("Reset to defaults", None),
            ("Back", None)
        ], default=8)
        if ch == -1 or ch == 8: return
        elif ch == 0: _set_whisper(cfg)
        elif ch == 1: _menu_backend(cfg)
        elif ch == 2: _set_addrs(cfg)
        elif ch == 3: _set_subs(cfg)
        elif ch == 4: _menu_yt_auth(cfg)
        elif ch == 5:
            config_export(cfg); pause()
        elif ch == 6:
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
                        if imported: cfg.update(imported)
                except ValueError:
                    imported = config_import(choice)
                    if imported: cfg.update(imported)
            else:
                path = ask_input("Path to config file", "")
                if path:
                    imported = config_import(path)
                    if imported: cfg.update(imported)
            pause()
        elif ch == 7:
            if ask_yn("Reset ALL settings?", False):
                n = dict(DEFAULT_CONFIG); n["first_run_complete"] = True
                save_config(n); cfg.update(n); success("Reset!")
            pause()

def _set_whisper(cfg):
    header("Whisper Settings")
    gpu = detect_gpu()
    rec_model, rec_reason = recommend_whisper_model(gpu)
    if gpu["has_nvidia"]: info(f"GPU: {gpu['name']} ({gpu['vram_mb']} MB)")
    info(f"Current model: {C.BOLD}{cfg['whisper_model']}{C.RESET}")
    info(f"Recommendation: {rec_model} ({rec_reason})")

    ch = ask_choice("What to change:", [
        ("Whisper model", f"Currently: {cfg['whisper_model']}"),
        ("Device (CPU/GPU)", f"Currently: {cfg['whisper_device']}"),
        ("Compute type", f"Currently: {cfg['whisper_compute_type']}"),
        ("Back", None),
    ], default=3)
    if ch == -1 or ch == 3: return
    elif ch == 0:
        _menu_whisper_model(cfg)
    elif ch == 1:
        dc = ask_choice("Device:", [
            ("Auto", "GPU if available else CPU"), ("CUDA (GPU)", "Needs NVIDIA"),
            ("CPU", "Slower"), ("Keep current", f"{cfg['whisper_device']}")
        ], default=3)
        if 0 <= dc < 3: cfg["whisper_device"] = ["auto", "cuda", "cpu"][dc]
        save_config(cfg); success("Saved!"); pause()
    elif ch == 2:
        cc = ask_choice("Compute:", [
            ("Auto", None), ("float16", "~4.5 GB VRAM"), ("int8_float16", "~3 GB"),
            ("int8", "CPU-friendly"), ("Keep current", f"{cfg['whisper_compute_type']}")
        ], default=4)
        if 0 <= cc < 4: cfg["whisper_compute_type"] = ["auto", "float16", "int8_float16", "int8"][cc]
        save_config(cfg); success("Saved!"); pause()

def _set_addrs(cfg):
    while True:
        header("Server Addresses")
        info(f"Whisper:     {C.CYAN}{cfg['whisper_host']}:{cfg['whisper_port']}{C.RESET}")
        info(f"Translation: {C.CYAN}{cfg['translation_host']}:{cfg['translation_port']}{C.RESET}")
        ch = ask_choice("Change:", [
            ("Whisper address", None), ("Translation address", None), ("Back", None)
        ], default=2)
        if ch == -1 or ch == 2: return
        elif ch == 0: _change_addr(cfg, "whisper")
        elif ch == 1: _change_addr(cfg, "translation")

def _set_subs(cfg):
    header("Subtitle Tuning")
    cfg["word_timestamps"] = ask_yn("Word-level timestamps?", cfg["word_timestamps"])
    pt = ask_input("Pause threshold (0.2-1.0s)", str(cfg["pause_threshold"]))
    try: cfg["pause_threshold"] = float(pt)
    except Exception as e:
        _log.debug('[_set_subs] float-parse failed: %s', e)

    cd = ask_input("Chunk duration (4-60s, smaller = faster subtitles)", str(cfg["chunk_duration"]))
    try: cfg["chunk_duration"] = int(cd)
    except Exception as e:
        _log.debug('[_set_subs] int-parse failed: %s', e)

    save_config(cfg); success("Saved!"); pause()

# ────
# BENCHMARK
# ────

WHISPER_MODELS_INFO = [
    ("tiny",            "39M params",   "~1 GB",   "Fastest, low accuracy"),
    ("base",            "74M params",   "~1 GB",   "Fast, decent accuracy"),
    ("small",           "244M params",  "~2 GB",   "Good balance"),
    ("medium",          "769M params",  "~5 GB",   "High accuracy"),
    ("large-v2",        "1550M params", "~10 GB",  "Very high accuracy"),
    ("large-v3",        "1550M params", "~10 GB",  "Best accuracy"),
    ("distil-large-v2", "756M params",  "~4 GB",   "Fast + accurate"),
    ("distil-large-v3", "756M params",  "~4 GB",   "Fast + accurate (newer)"),
]

def benchmark_whisper(cfg):
    """Compare Whisper model speeds on this hardware."""
    header("Whisper Benchmark")

    # Check faster-whisper is installed
    try:
        import faster_whisper
        success("faster-whisper found")
    except ImportError:
        error("faster-whisper not installed. Run: pip install faster-whisper")
        pause(); return

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
            import struct, math
            sr = WHISPER_SAMPLE_RATE; dur = 5
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
            pause(); return

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

    info(f"Select models to benchmark:")
    print()
    for i, (name, params, vr, desc, fits) in enumerate(available):
        tag = f"{C.GREEN}fits{C.RESET}" if fits else f"{C.RED}may OOM{C.RESET}"
        cur = f" {C.GOLD}<- current{C.RESET}" if name == cfg.get("whisper_model") else ""
        print(f"    {i+1:2d}. {name:22s} {params:14s} {vr:8s} [{tag}]{cur}")
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
        warn("No models selected"); pause(); return

    info(f"Benchmarking {len(models_to_test)} model(s): {', '.join(models_to_test)}")
    if not ask_yn("This will download models you don't have yet. Continue?", True):
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
    for model_name in models_to_test:
        section(f"Testing: {model_name}")
        info(f"Device: {device} | Compute: {compute}")

        try:
            from faster_whisper import WhisperModel

            # Measure load time
            t0 = time.time()
            model = WhisperModel(model_name, device=device, compute_type=compute)
            load_time = time.time() - t0
            success(f"Loaded in {load_time:.1f}s")

            # Measure transcription (3 runs, take median)
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
                import gc; gc.collect()
                if device == "cuda":
                    import torch; torch.cuda.empty_cache()
            except Exception as e:
                _log.debug('[benchmark_whisper] cuda-cleanup failed: %s', e)



        except Exception as e:
            err_msg = str(e)[:60]
            error(f"Failed: {err_msg}")
            results.append({
                "model": model_name,
                "load_s": 0,
                "median_s": 0,
                "rtf": 0,
                "speed_x": 0,
                "segments": 0,
                "status": f"FAIL: {err_msg}",
            })

    # Clean up test audio
    try: test_wav.unlink()
    except OSError: pass

    # Display results
    print()
    section("Benchmark Results")
    info(f"Device: {device} | Compute: {compute} | Audio: 5s test tone")
    if gpu.get("has_nvidia"):
        info(f"GPU: {gpu['name']} ({gpu['vram_mb']} MB)")
    print()

    rows = []
    best_speed = max((r["speed_x"] for r in results if r["status"] == "OK"), default=0)
    for r in results:
        if r["status"] == "OK":
            speed_bar = "#" * min(30, int(r["speed_x"] / max(best_speed, 1) * 30))
            is_best = " *" if r["speed_x"] == best_speed and len(results) > 1 else ""
            rows.append([
                r["model"],
                f"{r['load_s']}s",
                f"{r['median_s']}s",
                f"{r['speed_x']}x{is_best}",
                speed_bar,
            ])
        else:
            rows.append([r["model"], "-", "-", "-", f"{C.RED}{r['status']}{C.RESET}"])

    table(
        ["Model", "Load", "5s Audio", "Speed", ""],
        rows,
        col_styles=[C.CYAN, C.RESET, C.RESET, C.GREEN, C.DIM],
        title="Whisper Benchmark"
    )

    if results:
        ok_results = [r for r in results if r["status"] == "OK"]
        if ok_results:
            fastest = min(ok_results, key=lambda r: r["median_s"])
            print()
            success(f"Fastest: {fastest['model']} at {fastest['speed_x']}x realtime")
            if fastest["model"] != cfg.get("whisper_model"):
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
        ts = check_server(cfg["translation_host"], cfg["translation_port"], "/v1/models")
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
    if gf:
        info(f"GGUF files: {len(gf)} in models/translation/")
        for f in gf:
            bullet(f"{f.name} ({f.stat().st_size/GiB:.2f} GB)")

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
    info("To use a font: select it in the extension popup under Appearance settings")
    info("To bundle a font: place .ttf/.otf in extension/fonts/ and list in popup.js BUNDLED_FONTS")
    pause()


# ────
# LAUNCH
# ────


def launch_services(cfg):
    header("Launching Yume")
    rotate_logs()  # Clean up old logs before starting
    procs = []; lhs = []

    def _cleanup():
        for name, p in procs:
            try:
                p.terminate()
                p.wait(timeout=3)
            except Exception:
                try: p.kill(); p.wait(timeout=2)
                except OSError: pass
        for lh in lhs:
            try: lh.close()
            except OSError: pass
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
            cfg["translation_port"] = tport; save_config(cfg)
            success(f"Auto-assigned translation to port {tport}")
        else:
            error("Could not find a free port"); pause(); return
    if not is_port_free(wport):
        wport = ensure_port_free(wport, cfg, "whisper", exclude={tport})
        if wport is None:
            error("Cannot proceed without a free whisper port"); pause(); return
    if not is_port_free(tport):
        tport = ensure_port_free(tport, cfg, "translation", exclude={wport})
        if tport is None:
            error("Cannot proceed without a free translation port"); pause(); return

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
                cfg["gguf_model_path"] = gp; save_config(cfg)
                info(f"Auto-selected GGUF: {gfs[0].name}")
            else:
                error("No .gguf model found in models/translation/")
                info(f"Fix: Main Menu → Tools → Download GGUF Model")
                info(f"Or: place a .gguf file in {GGUF_DIR}")
                pause(); return

        # Check if llama-cpp-python is installed
        try:
            import llama_cpp  # noqa: F401
        except ImportError:
            error("llama-cpp-python not installed!")
            if ask_yn("Install now?"):
                install_llamacpp_python()
            else:
                pause(); return

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
                    "uvicorn>=0.20.0", "fastapi>=0.100.0", "sse-starlette>=1.6.0",
                    "starlette-context>=0.3.0", "pydantic-settings>=2.0.0",
                    "-q", "--no-warn-script-location"
                ], timeout=300, env=env)
                success("Server dependencies installed!")
            except Exception as e:
                error(f"Failed to install: {e}")
                pause(); return

        port = cfg.get("translation_port", DEFAULT_TRANSLATION_PORT)
        hp = bi.get("hp", "/health")
        st = check_server(cfg["translation_host"], port, hp)
        # Also try /v1/models as fallback (llama-cpp-python serves OpenAI API)
        if not st["up"]:
            st = check_server(cfg["translation_host"], port, "/v1/models")
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
            lh = open(lp, "w", encoding="utf-8", errors="replace"); lhs.append(lh)
            p = subprocess.Popen(cmd, stdout=lh, stderr=subprocess.STDOUT, env=env)
            procs.append(("Translation", p))
            info(f"Log: {lp}")
            def _trans_ready():
                if p.poll() is not None:
                    return None  # crashed
                return check_server(cfg["translation_host"], port, "/health")["up"]

            ready = spin_wait(
                lambda: _trans_ready() is True,
                "Loading translation model...",
                timeout=180, interval=2
            )
            if p.poll() is not None:
                error("llama.cpp server crashed!")
                try:
                    with open(lp, encoding="utf-8", errors="replace") as f:
                        for l in f.readlines()[-10:]: print(f"  {C.DIM}{l.rstrip()}{C.RESET}")
                except Exception as e:
                    _log.debug('[_launch_inner] translation-crash-log-read failed: %s', e)

                info(f"Full log: {lp}")
                pause(); return
            if ready:
                success("Translation server ready!")
            else:
                warn("Still loading — large models may take a few minutes")

    elif bk == "ollama":
        st = check_server(cfg["translation_host"], cfg["translation_port"], "/api/tags")
        if not st["up"]:
            info("Starting Ollama...")
            try:
                p = subprocess.Popen(["ollama", "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
                procs.append(("Ollama", p)); time.sleep(3)
                if check_server(cfg["translation_host"], cfg["translation_port"], "/api/tags")["up"]:
                    success("Ollama running!")
                else:
                    warn("Ollama may still be starting...")
            except FileNotFoundError:
                error("Ollama not found"); pause(); return
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
            error(f"Whisper server script not found in server/ or root")
            info("Fix: Re-extract PocketYume or run Setup again")
            pause(); return

        dev = cfg["whisper_device"]; comp = cfg["whisper_compute_type"]
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
        if not cfg.get("word_timestamps"): cmd.append("--no-word-timestamps")
        if CONFIG_FILE.exists(): cmd.extend(["--config", str(CONFIG_FILE)])

        LOGS_DIR.mkdir(parents=True, exist_ok=True)
        lp = LOGS_DIR / "whisper_server.log"
        lh = open(lp, "w", encoding="utf-8", errors="replace"); lhs.append(lh)
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
            try:
                with open(lp, encoding="utf-8", errors="replace") as f:
                    for l in f.readlines()[-10:]: print(f"  {C.DIM}{l.rstrip()}{C.RESET}")
            except Exception as e:
                _log.debug('[_launch_inner] whisper-crash-log-read failed: %s', e)

            info(f"Full log: {lp}")
            pause(); return
        if ready:
            success("Whisper server ready!")
        else:
            warn("Still loading — first run takes 30-60s for model download")

    # --- READY ---
    clear()
    print(); gold_hr(); print()
    print(center(f"{C.GREEN}{C.BOLD}  Yume is running!  {C.RESET}"))
    print()
    tn = BACKEND_INFO.get(bk, {}).get("name", bk)
    print(f"  {C.CYAN}Whisper{C.RESET}      http://{cfg['whisper_host']}:{cfg['whisper_port']}")
    print(f"  {C.MAGENTA}Translation{C.RESET}  http://{cfg['translation_host']}:{cfg['translation_port']}  ({tn})")
    print()
    print(center(f"{C.DIM}Chrome -> Japanese video -> Yume extension -> Enable{C.RESET}"))
    print(); gold_hr()

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
            try: lh.close()
            except OSError: pass
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
                    for l in lines[-15:]:
                        print(f"    {C.DIM}{l.rstrip()}{C.RESET}")
                except Exception as e:
                    warn(f"Could not read: {e}")
                print()

    try:
        while True:
            clear()
            header("Runtime")
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
            ts_up = check_server(cfg["translation_host"], cfg["translation_port"], bi.get("hp", "/health"))["up"]
            if not ts_up:
                ts_up = check_server(cfg["translation_host"], cfg["translation_port"], "/v1/models")["up"]

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
                ("Server Stats", "GPU, VRAM, session metrics"),
                ("Blacklist", "Manage hallucination filter"),
                ("Whisper Model", "Hot-swap model while running"),
                ("View Logs", "Recent server output"),
                ("Stop & Return", "Shut down all servers"),
            ], default=4, allow_back=False)

            if ch == 0:
                cli_server_stats(cfg); pause()
            elif ch == 1:
                _menu_blacklist(cfg)
            elif ch == 2:
                _menu_whisper_model(cfg)
            elif ch == 3:
                _show_logs(); pause()
                # All options loop back to top which calls clear()
            elif ch == 4:
                if ask_yn("Stop all servers?", default=True):
                    _stop_all(); return
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

    section("System Scan")
    gpu = detect_gpu(); ram = detect_ram_gb(); disk = disk_free_gb()
    (success if gpu["has_nvidia"] or (gpu.get("has_amd") and not IS_WIN) else warn)(
        f"GPU:  {gpu['name'] or 'None'}" + (
            f" ({gpu['vram_mb']} MB)" if gpu["has_nvidia"]
            else f" ({gpu.get('vram_mb', '?')} MB, ROCm)" if gpu.get("has_amd") and not IS_WIN
            else " -- CPU mode"
        )
    )
    success(f"RAM:  {ram:.1f} GB")
    (success if disk >= 10 else warn)(f"Disk: {disk:.1f} GB free" + ("" if disk >= 10 else " -- need ~8 GB"))

    # Auto-configure Whisper using the model recommender
    rec_model, rec_reason = recommend_whisper_model(gpu)
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
        cfg["whisper_device"] = "cpu"; cfg["whisper_compute_type"] = "int8"

    pause()

    header("Component Check")
    missing = []
    yt = find_tool("yt-dlp"); ff = find_tool("ffmpeg"); dn = find_tool("deno")
    (success if yt else warn)(f"yt-dlp: {'OK '+yt if yt else 'MISSING'}")
    (success if ff else warn)(f"FFmpeg: {'OK '+ff if ff else 'MISSING'}")
    info(f"Deno:   {'OK '+dn if dn else '-- not installed (optional)'}")

    if not yt: missing.append("yt-dlp")
    if not ff: missing.append("ffmpeg")

    # Check Python packages
    try: import faster_whisper; success("faster-whisper: OK")  # noqa: F401
    except ImportError: warn("faster-whisper: MISSING"); missing.append("python_deps")

    try: import llama_cpp; success("llama-cpp-python: OK")  # noqa: F401
    except ImportError: warn("llama-cpp-python: MISSING"); missing.append("llama_cpp")

    try: import uvicorn; import fastapi; success("Server deps (uvicorn/fastapi): OK")  # noqa: F401
    except ImportError: warn("Server deps (uvicorn/fastapi): MISSING"); missing.append("server_deps")

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
        if k == "custom": continue
        if check_server(bi["dh"], bi["dp"], bi["hp"])["up"]:
            detected.append(k)
            success(f"Detected: {bi['name']} on port {bi['dp']}")

    if not missing and (detected or gf):
        success(f"\n  {C.BOLD}Everything ready!{C.RESET}")
        cfg["first_run_complete"] = True; save_config(cfg); pause(); return cfg

    if missing:
        print(f"\n  Missing: {C.BOLD}{', '.join(missing)}{C.RESET}\n")
        mode = ask_choice("Proceed?", [
            ("Install all", "Download everything needed"),
            ("Choose each", "Ask per component"),
            ("Skip", "Set up later"),
        ], default=0, allow_back=False)

        if mode == 2:
            cfg["first_run_complete"] = True; save_config(cfg); pause(); return cfg

        ae = (mode == 1)  # ask-each
        header("Installing")

        if "yt-dlp" in missing:
            if not ae or ask_yn("Install yt-dlp?"):
                install_ytdlp()

        if "ffmpeg" in missing:
            if not ae or ask_yn("Install FFmpeg?"):
                install_ffmpeg()

        if not find_tool("deno"):
            print(); info("Deno is optional -- handles YouTube JS challenges.")
            ch = ask_choice("YouTube auth:", [
                ("Install Deno", "Automatic, ~35 MB"),
                ("Browser cookies", "No download, must be logged in"),
                ("Skip", None),
            ], default=0, allow_back=False)
            if ch == 0:
                install_deno(); cfg["youtube_auth_method"] = "deno"
            elif ch == 1:
                cfg["youtube_auth_method"] = "cookies"
                browsers = ["chrome", "firefox", "edge", "brave", "safari"]
                bc = ask_choice("Browser?", [(b.capitalize(), None) for b in browsers], default=0, allow_back=False)
                cfg["cookies_browser"] = browsers[bc]
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
                        "uvicorn>=0.20.0", "fastapi>=0.100.0", "sse-starlette>=1.6.0",
                        "starlette-context>=0.3.0", "pydantic-settings>=2.0.0",
                        "-q", "--no-warn-script-location"
                    ], timeout=300)
                    success("Server dependencies installed!")
                except Exception as e:
                    error(f"Failed: {e}")

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
                cfg["translation_host"] = bi["dh"]; cfg["translation_port"] = bi["dp"]
                cfg["translation_model"] = "qwen2.5:7b"
                if ask_yn("Install Ollama?"):
                    install_ollama(); time.sleep(2)
                    if ask_yn(f"Download model ({cfg['translation_model']})?"):
                        if not check_server("127.0.0.1", DEFAULT_OLLAMA_PORT, "/api/tags")["up"]:
                            try:
                                subprocess.Popen(["ollama", "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                                time.sleep(3)
                            except Exception as e:
                                _log.debug('[setup_wizard] post-setup-backup failed: %s', e)

                        pull_ollama_model(cfg["translation_model"])
            save_config(cfg)

    cfg["first_run_complete"] = True; save_config(cfg)
    try:
        config_export(cfg, BASE_DIR / "yume_config_post_setup.json")
    except Exception as e:
        _log.debug('[setup_wizard] config-backup failed: %s', e)


    print(); success(f"{C.BOLD}Setup complete! Config backed up.{C.RESET}"); pause()
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
        ("GGUF models", GGUF_DIR),
        ("Server files", SERVER_DIR),
        ("Configuration", CONFIG_DIR),
        ("Logs", LOGS_DIR),
    ]
    for label, d in dirs:
        sz = 0
        if d.exists():
            for f in d.rglob("*"):
                try: sz += f.stat().st_size
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
        info("Cancelled."); pause(); return

    remove_data = ch in (0, 1)
    remove_pip = ch in (0, 2)

    if not ask_yn(f"Are you sure? This cannot be undone.", False):
        info("Cancelled."); pause(); return

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
            gs = f"{C.CYAN}{gpu['name']} (ROCm){C.RESET}"
        else:
            gs = f"{C.YELLOW}CPU only{C.RESET}"
        bn = BACKEND_INFO.get(cfg.get("translation_backend", "llamacpp"), {}).get("name", "?")
        panel(
            f"{C.DIM}GPU{C.RESET}  {gs}\n"
            f"{C.DIM}STT{C.RESET}  {C.CYAN}{cfg['whisper_model']}{C.RESET}  on  {cfg['whisper_host']}:{cfg['whisper_port']}\n"
            f"{C.DIM}TL {C.RESET}  {C.MAGENTA}{bn}{C.RESET}  on  {cfg['translation_host']}:{cfg['translation_port']}",
            style=C.DIM,
        )
        # Async update check (non-blocking)
        try:
            latest, url = check_for_updates()
            if latest:
                print(f"  {C.YELLOW}⬆{C.RESET}  Update available: v{latest}  {C.DIM}{url}{C.RESET}")
        except Exception as e:
            _log.debug('[main_menu] update-check failed: %s', e)



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

        if ch == 0: launch_services(cfg)
        elif ch == 1: show_status(cfg)
        elif ch == 2: health_check(cfg)
        elif ch == 3: settings_menu(cfg)
        elif ch == 4: tools_menu(cfg)
        elif ch == 5:
            cfg["first_run_complete"] = False; save_config(cfg); cfg = setup_wizard(cfg)
        elif ch == 6: uninstall_yume()
        elif ch == 7:
            print(f"\n  {C.GOLD}Goodbye!{C.RESET}\n"); break

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

    for d in [TOOLS_DIR, SERVER_DIR, CONFIG_DIR, MODELS_DIR, GGUF_DIR, LOGS_DIR]:
        d.mkdir(parents=True, exist_ok=True)

    if len(sys.argv) > 1:
        cmd = sys.argv[1].lower(); cfg = load_config()
        if cmd == "launch": launch_services(cfg)
        elif cmd == "status": show_status(cfg)
        elif cmd == "health": health_check(cfg)
        elif cmd == "stats": cli_server_stats(cfg)
        elif cmd == "ports": show_ports_status(cfg)
        elif cmd == "blacklist": cli_blacklist(cfg, sys.argv[2:])
        elif cmd == "model": cli_model(cfg, sys.argv[2:])
        elif cmd == "export":
            config_export(cfg, sys.argv[2] if len(sys.argv) > 2 else None)
        elif cmd == "import" and len(sys.argv) > 2:
            imported = config_import(sys.argv[2])
            if imported: cfg.update(imported)
        elif cmd == "recommend":
            model, reason = recommend_whisper_model()
            info(f"Recommended: {model}")
            info(f"Reason: {reason}")
        elif cmd == "fonts": detect_fonts()
        elif cmd == "benchmark":
            benchmark_whisper(cfg)
        elif cmd == "setup":
            cfg["first_run_complete"] = False; save_config(cfg); setup_wizard(cfg)
        elif cmd == "help":
            print(f"\n  {C.BOLD}Pocket Yume v{VERSION}{C.RESET}")
            print(f"  Cross-platform installer for Yume AI Subtitles\n")
            print(f"  {C.BOLD}Usage:{C.RESET} python pocket_yume.py [command]\n")
            print(f"  {C.GOLD}Commands:{C.RESET}")
            print(f"    (none)              Interactive menu")
            print(f"    launch              Start servers (interactive runtime menu)")
            print(f"    status              Check components")
            print(f"    health              Run full health check")
            print(f"    stats               Live server statistics (GPU, session)")
            print(f"    model               Show current Whisper model")
            print(f"    model list          List available models with VRAM")
            print(f"    model switch <n>    Hot-swap Whisper model")
            print(f"    blacklist list      Show server blacklist")
            print(f"    blacklist add <t>   Block a phrase")
            print(f"    blacklist remove <t> Unblock a phrase")
            print(f"    blacklist clear     Clear all entries")
            print(f"    ports               Show port availability")
            print(f"    setup               Run setup wizard")
            print(f"    export [path]       Export config to backup file")
            print(f"    import <path>       Import config from backup file")
            print(f"    recommend           Suggest best Whisper model for your hardware")
            print(f"    fonts               Detect installed CJK and system fonts")
            print(f"    benchmark           Test Whisper model speeds on your hardware")
            print(f"    help                This message")
            print(f"    --verbose / -v      Enable debug logging to stderr")
            print(f"    --no-color          Disable colored output\n")
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
