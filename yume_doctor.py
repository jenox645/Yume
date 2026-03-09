#!/usr/bin/env python3
"""
╔══════════════════════════════════════════════════════════════════════════════╗
║                        YUME DOCTOR v3.8.0                                  ║
║              Cross-platform diagnostic tool for PocketYume                 ║
║                  Linux / Windows / macOS                                   ║
╚══════════════════════════════════════════════════════════════════════════════╝

Run:  python yume_doctor.py
      python yume_doctor.py --fix          (auto-fix what it can)
      python yume_doctor.py --json         (machine-readable output)
      python yume_doctor.py --test-video URL  (test full pipeline with a video)
"""

import os
import sys
import json
import time
import socket
import shutil
import platform
import subprocess
import tempfile
import argparse
from pathlib import Path
from datetime import datetime

# ============================================================================
# COLORS & OUTPUT
# ============================================================================

IS_WIN = platform.system() == "Windows"

class C:
    if IS_WIN:
        try:
            os.system(""); _ok = True  # enable ANSI on Windows 10+
        except: _ok = False
    else:
        _ok = True

    RESET  = "\033[0m"   if _ok else ""
    BOLD   = "\033[1m"   if _ok else ""
    DIM    = "\033[2m"   if _ok else ""
    RED    = "\033[91m"  if _ok else ""
    GREEN  = "\033[92m"  if _ok else ""
    YELLOW = "\033[93m"  if _ok else ""
    BLUE   = "\033[94m"  if _ok else ""
    CYAN   = "\033[96m"  if _ok else ""
    WHITE  = "\033[97m"  if _ok else ""
    BG_RED = "\033[41m"  if _ok else ""
    BG_GRN = "\033[42m"  if _ok else ""
    BG_YLW = "\033[43m"  if _ok else ""

PASS = f"{C.GREEN}✓ PASS{C.RESET}"
FAIL = f"{C.RED}✗ FAIL{C.RESET}"
WARN = f"{C.YELLOW}⚠ WARN{C.RESET}"
SKIP = f"{C.DIM}○ SKIP{C.RESET}"
INFO = f"{C.CYAN}ℹ INFO{C.RESET}"

results = []  # for JSON export

def section(title):
    print(f"\n{C.BOLD}{C.CYAN}{'─'*60}{C.RESET}")
    print(f"{C.BOLD}{C.CYAN}  {title}{C.RESET}")
    print(f"{C.BOLD}{C.CYAN}{'─'*60}{C.RESET}")

def log(status, check, detail="", fix=""):
    tag = {"pass": PASS, "fail": FAIL, "warn": WARN, "skip": SKIP, "info": INFO}[status]
    print(f"  {tag}  {check}")
    if detail:
        print(f"         {C.DIM}{detail}{C.RESET}")
    if fix:
        print(f"         {C.YELLOW}Fix: {fix}{C.RESET}")
    results.append({"status": status, "check": check, "detail": detail, "fix": fix})

def run(cmd, timeout=15, shell=False):
    """Run a command, return (returncode, stdout, stderr)."""
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout,
            shell=shell, env=os.environ.copy()
        )
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except FileNotFoundError:
        return -1, "", f"Command not found: {cmd[0] if isinstance(cmd, list) else cmd}"
    except subprocess.TimeoutExpired:
        return -2, "", "Timed out"
    except Exception as e:
        return -3, "", str(e)

def check_port(host, port, timeout=3):
    """Check if a TCP port is listening."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except:
        return False

def http_get(url, timeout=5):
    """Simple HTTP GET using urllib (no deps)."""
    try:
        import urllib.request
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        return 0, str(e)

def http_post_json(url, data, timeout=10):
    """Simple HTTP POST JSON."""
    try:
        import urllib.request
        body = json.dumps(data).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return 0, str(e)


# ============================================================================
# 1. SYSTEM INFO
# ============================================================================

def check_system():
    section("System Information")
    
    os_name = platform.system()
    os_ver = platform.version()
    arch = platform.machine()
    py_ver = platform.python_version()
    
    log("info", f"OS: {os_name} {platform.release()} ({arch})", os_ver)
    log("info", f"Python: {py_ver}", sys.executable)
    
    # Python version check
    major, minor = sys.version_info[:2]
    if major < 3 or (major == 3 and minor < 8):
        log("fail", "Python >= 3.8 required", f"You have {py_ver}", "Install Python 3.8+")
    else:
        log("pass", f"Python {py_ver} meets minimum (3.8+)")
    
    # RAM
    try:
        if os_name == "Linux":
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal"):
                        kb = int(line.split()[1])
                        gb = kb / 1024 / 1024
                        s = "pass" if gb >= 8 else "warn"
                        log(s, f"RAM: {gb:.1f} GB", "8+ GB recommended" if gb < 8 else "")
                        break
        elif os_name == "Darwin":
            rc, out, _ = run(["sysctl", "-n", "hw.memsize"])
            if rc == 0:
                gb = int(out) / 1024**3
                s = "pass" if gb >= 8 else "warn"
                log(s, f"RAM: {gb:.1f} GB")
        elif os_name == "Windows":
            rc, out, _ = run(["wmic", "computersystem", "get", "TotalPhysicalMemory", "/value"], shell=True)
            if rc == 0 and "=" in out:
                gb = int(out.split("=")[1].strip()) / 1024**3
                s = "pass" if gb >= 8 else "warn"
                log(s, f"RAM: {gb:.1f} GB")
    except:
        log("skip", "Could not detect RAM")


# ============================================================================
# 2. GPU DETECTION
# ============================================================================

def check_gpu():
    section("GPU Detection")
    
    gpu_info = {"nvidia": False, "amd": False, "apple": False, "name": "Unknown", "vram": 0}
    os_name = platform.system()
    
    # --- NVIDIA ---
    rc, out, _ = run(["nvidia-smi", "--query-gpu=name,memory.total,driver_version,cuda_version",
                       "--format=csv,noheader,nounits"])
    if rc == 0 and out:
        parts = out.split(",")
        name = parts[0].strip()
        vram = int(parts[1].strip()) if len(parts) > 1 else 0
        driver = parts[2].strip() if len(parts) > 2 else "?"
        cuda = parts[3].strip() if len(parts) > 3 else "?"
        gpu_info.update(nvidia=True, name=name, vram=vram)
        log("pass", f"NVIDIA GPU: {name}", f"VRAM: {vram} MB | Driver: {driver} | CUDA: {cuda}")
        
        if vram < 4000:
            log("warn", "Low VRAM (<4 GB)", "Whisper large-v3 needs ~4 GB, translation model needs ~4 GB",
                "Use smaller Whisper model (medium/small) or CPU mode")
        elif vram < 8000:
            log("warn", "Moderate VRAM (4-8 GB)", "May not fit both Whisper + translation model simultaneously")
        else:
            log("pass", f"VRAM: {vram} MB — sufficient for Whisper + translation")
    else:
        log("info", "No NVIDIA GPU detected (nvidia-smi not found or failed)")
    
    # --- AMD / ROCm ---
    rc, out, _ = run(["rocminfo"])
    
    # Fallback: try with full path (venvs don't have /opt/rocm/bin in PATH)
    if rc != 0:
        for rocm_path in ["/opt/rocm/bin/rocminfo", "/opt/rocm-7.2.0/bin/rocminfo",
                          "/opt/rocm-7.1.0/bin/rocminfo"]:
            if os.path.exists(rocm_path):
                rc, out, _ = run([rocm_path])
                if rc == 0:
                    log("info", f"rocminfo found at {rocm_path} (not in venv PATH)")
                    break
    
    if rc == 0 and "gfx" in out.lower():
        gpu_info["amd"] = True
        
        # Extract GPU name
        name = "AMD GPU"
        for line in out.splitlines():
            if "marketing name" in line.lower() or "Marketing Name" in line:
                name = line.split(":")[-1].strip()
                if name and len(name) > 3:
                    break
        
        # Extract arch
        import re
        arches = list(set(re.findall(r'gfx(\d+)', out)))
        arch_str = ", ".join(f"gfx{a}" for a in arches) if arches else "unknown"
        gpu_info["name"] = name
        
        log("pass", f"AMD GPU: {name}", f"Architecture: {arch_str}")
        
        # Check ROCm version
        rc2, ver, _ = run(["rocm-smi", "--version"])
        if rc2 == 0:
            log("info", f"ROCm: {ver.strip()[:60]}")
        
        # Check TensileLibrary support
        rdna1_arches = [a for a in arches if a in ("1010", "1011", "1012")]
        if rdna1_arches:
            hsa = os.environ.get("HSA_OVERRIDE_GFX_VERSION", "")
            if hsa:
                log("pass", f"RDNA1 GPU (gfx{rdna1_arches[0]}) with HSA_OVERRIDE_GFX_VERSION={hsa}")
            else:
                log("fail", f"RDNA1 GPU (gfx{rdna1_arches[0]}) — HSA_OVERRIDE_GFX_VERSION not set!",
                    "rocBLAS will crash with 'Cannot read TensileLibrary.dat' and translation exits with code -6",
                    "export HSA_OVERRIDE_GFX_VERSION=10.3.0  (add to ~/.bashrc)")
        
        # Check supported TensileLibrary files
        tensile_dir = "/opt/rocm/lib/rocblas/library"
        alt_dirs = ["/opt/rocm-7.2.0/lib/rocblas/library", "/opt/rocm-7.1.0/lib/rocblas/library"]
        for d in [tensile_dir] + alt_dirs:
            if os.path.isdir(d):
                files = [f for f in os.listdir(d) if "TensileLibrary" in f and f.endswith(".dat")]
                supported = [re.search(r'gfx(\d+)', f).group(0) for f in files if re.search(r'gfx(\d+)', f)]
                if supported:
                    log("info", f"TensileLibrary supported archs: {', '.join(sorted(set(supported)))}", d)
                break

        # Check VRAM via rocm-smi
        rc3, vout, _ = run(["rocm-smi", "--showmeminfo", "vram", "--csv"])
        if rc3 == 0:
            for line in vout.splitlines():
                if "vram total" in line.lower() or (line.replace(",", "").replace(".", "").strip().isdigit()):
                    try:
                        # Try to extract MB value
                        nums = re.findall(r'(\d+)', line)
                        if nums:
                            mb = int(nums[0])
                            if mb > 100000:  # bytes → MB
                                mb = mb // (1024*1024)
                            elif mb > 10000:  # KB → MB
                                mb = mb // 1024
                            gpu_info["vram"] = mb
                            log("info", f"VRAM: ~{mb} MB")
                    except:
                        pass
    else:
        log("info", "No AMD/ROCm GPU detected via rocminfo")
        # Fallback: detect AMD GPU hardware via lspci even without ROCm
        if os_name == "Linux":
            rc_lspci, lspci_out, _ = run(["lspci"])
            if rc_lspci == 0:
                amd_lines = [l for l in lspci_out.splitlines()
                             if 'vga' in l.lower() or 'display' in l.lower() or '3d' in l.lower()]
                amd_gpus = [l for l in amd_lines if 'amd' in l.lower() or 'radeon' in l.lower() or 'ati' in l.lower()]
                if amd_gpus:
                    gpu_name = amd_gpus[0].split(":")[-1].strip()[:80]
                    gpu_info["amd"] = True
                    gpu_info["name"] = gpu_name
                    log("warn", f"AMD GPU detected via lspci: {gpu_name}",
                        "ROCm not installed or not in PATH — GPU acceleration unavailable",
                        "Install ROCm, or add /opt/rocm/bin to PATH, or run outside venv for GPU detection")
    
    # --- Apple Silicon ---
    if os_name == "Darwin":
        rc, out, _ = run(["sysctl", "-n", "machdep.cpu.brand_string"])
        if rc == 0 and "apple" in out.lower():
            gpu_info["apple"] = True
            gpu_info["name"] = out.strip()
            log("pass", f"Apple Silicon: {out.strip()}", "Metal acceleration not yet supported by CTranslate2 — CPU mode used")
        else:
            log("info", f"CPU: {out.strip()}" if rc == 0 else "Could not detect Apple Silicon")
    
    if not gpu_info["nvidia"] and not gpu_info["amd"] and not gpu_info["apple"]:
        log("warn", "No supported GPU detected", "Yume will use CPU mode (5-10x slower)")
    
    return gpu_info


# ============================================================================
# 3. DEPENDENCIES
# ============================================================================

def check_dependencies(do_fix=False):
    section("Dependencies")
    
    deps = {
        "ffmpeg":  {"cmd": ["ffmpeg", "-version"], "fix": "apt install ffmpeg / brew install ffmpeg / winget install ffmpeg"},
        "yt-dlp":  {"cmd": ["yt-dlp", "--version"], "fix": "pip install -U yt-dlp"},
    }
    
    for name, info in deps.items():
        rc, out, err = run(info["cmd"])
        if rc == 0:
            ver = out.split("\n")[0][:80]
            log("pass", f"{name}: {ver}")
        elif rc == -1:
            log("fail", f"{name}: not found", "", info["fix"])
            if do_fix and name == "yt-dlp":
                print(f"         {C.CYAN}Attempting auto-fix...{C.RESET}")
                run([sys.executable, "-m", "pip", "install", "-U", "yt-dlp"], timeout=120)
        else:
            log("fail", f"{name}: error (rc={rc})", err[:120], info["fix"])
    
    # Python packages
    py_packages = [
        ("faster_whisper", "faster-whisper", "pip install faster-whisper"),
        ("flask", "Flask", "pip install flask flask-cors"),
        ("flask_cors", "Flask-CORS", "pip install flask-cors"),
    ]
    
    for module, display, fix in py_packages:
        try:
            m = __import__(module)
            try:
                from importlib.metadata import version as pkg_version
                ver = pkg_version(display)
            except Exception:
                ver = getattr(m, "__version__", getattr(m, "VERSION", "installed"))
            log("pass", f"{display}: {ver}")
        except ImportError:
            log("fail", f"{display}: not installed", "", fix)
    
    # Check llama-cpp-python (optional — only for llamacpp backend)
    try:
        import llama_cpp
        ver = getattr(llama_cpp, "__version__", "installed")
        log("pass", f"llama-cpp-python: {ver}")
        
        # Check if server module exists
        try:
            import llama_cpp.server
            log("pass", "llama-cpp-python[server]: available")
        except ImportError:
            log("warn", "llama-cpp-python[server]: missing", "", 
                "pip install 'llama-cpp-python[server]'")
    except ImportError:
        log("info", "llama-cpp-python: not installed (needed for llamacpp backend only)")
    
    # yt-dlp freshness check
    rc, out, _ = run(["yt-dlp", "--version"])
    if rc == 0:
        try:
            from datetime import datetime
            # yt-dlp version format: YYYY.MM.DD
            parts = out.strip().split(".")
            if len(parts) >= 3 and parts[0].isdigit():
                yt_date = datetime(int(parts[0]), int(parts[1]), int(parts[2]))
                age_days = (datetime.now() - yt_date).days
                if age_days > 60:
                    log("warn", f"yt-dlp is {age_days} days old", 
                        "YouTube frequently changes their API, old yt-dlp may fail",
                        "pip install -U yt-dlp")
                else:
                    log("pass", f"yt-dlp is recent ({age_days} days old)")
        except:
            pass


# ============================================================================
# 3b. BROWSER COOKIES (critical for YouTube auth)
# ============================================================================

def check_browser_cookies(cfg=None):
    section("Browser Cookie Access")
    
    auth_method = "deno"
    browser = "chrome"
    if cfg:
        auth_method = cfg.get("youtube_auth_method", "deno")
        browser = cfg.get("cookies_browser", "chrome")
    
    if auth_method != "cookies":
        log("info", f"YouTube auth method: {auth_method} (not cookies)")
        log("info", "Cookie checks skipped — set youtube_auth_method to 'cookies' in config to use cookie auth")
        return
    
    log("info", f"YouTube auth: cookies from '{browser}'")
    os_name = platform.system()
    
    # Find browser profile directory
    browser_lower = browser.lower().split(":")[0]  # handle "firefox:/path" format
    profile_dirs = []
    
    if browser_lower == "firefox":
        if os_name == "Linux":
            profile_dirs = [
                os.path.expanduser("~/.mozilla/firefox"),                                  # dnf/apt
                os.path.expanduser("~/.var/app/org.mozilla.firefox/.mozilla/firefox"),     # Flatpak
                os.path.expanduser("~/snap/firefox/common/.mozilla/firefox"),              # Snap
            ]
        elif os_name == "Darwin":
            profile_dirs = [os.path.expanduser("~/Library/Application Support/Firefox/Profiles")]
        elif os_name == "Windows":
            profile_dirs = [os.path.expandvars(r"%APPDATA%\Mozilla\Firefox\Profiles")]
    elif browser_lower in ("chrome", "chromium"):
        if os_name == "Linux":
            profile_dirs = [
                os.path.expanduser("~/.config/google-chrome"),
                os.path.expanduser("~/.config/chromium"),
                os.path.expanduser("~/.var/app/com.google.Chrome/config/google-chrome"),  # Flatpak
            ]
        elif os_name == "Darwin":
            profile_dirs = [os.path.expanduser("~/Library/Application Support/Google/Chrome")]
        elif os_name == "Windows":
            profile_dirs = [os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data")]
    
    found_profile = None
    cookie_file = None
    for pd in profile_dirs:
        if os.path.isdir(pd):
            found_profile = pd
            # Search for cookies database
            for root, dirs, files in os.walk(pd):
                for f in files:
                    if f == "cookies.sqlite" or f == "Cookies":
                        cookie_file = os.path.join(root, f)
                        break
                if cookie_file:
                    break
            break
    
    if not found_profile:
        log("fail", f"Browser profile not found for '{browser}'",
            f"Searched: {', '.join(profile_dirs)}",
            f"Is {browser} installed? Check that cookies_browser matches your actual browser.")
        # Hint about Flatpak/Snap/dnf
        if browser_lower == "firefox" and os_name == "Linux":
            log("info", "Firefox install methods on Linux:", 
                "dnf/apt → ~/.mozilla/firefox  |  Flatpak → ~/.var/app/org.mozilla.firefox  |  Snap → ~/snap/firefox")
        return
    
    log("pass", f"Browser profile found: {found_profile}")
    
    if cookie_file:
        size = os.path.getsize(cookie_file)
        readable = os.access(cookie_file, os.R_OK)
        if readable and size > 0:
            log("pass", f"Cookie database: {cookie_file} ({size:,} bytes, readable)")
        elif not readable:
            log("fail", f"Cookie database not readable: {cookie_file}",
                "Permission denied — is the browser running with a lock?",
                f"Try: chmod 644 {cookie_file}")
        else:
            log("warn", f"Cookie database empty: {cookie_file}")
    else:
        log("warn", f"No cookie database found in {found_profile}",
            "Browser may not have been used yet, or cookies are in an unexpected location")
    
    # Test yt-dlp can actually use the cookies
    log("info", "Testing yt-dlp cookie extraction...")
    
    # Build the cookie argument the same way the server would
    cookie_arg = browser
    if browser_lower == "firefox" and os_name == "Linux":
        # Check Flatpak vs native (same logic as server)
        flatpak = os.path.expanduser("~/.var/app/org.mozilla.firefox/.mozilla/firefox")
        native = os.path.expanduser("~/.mozilla/firefox")
        if os.path.isdir(flatpak) and not os.path.isdir(native):
            cookie_arg = f"firefox:{flatpak}"
            log("info", f"Using Flatpak Firefox path: {flatpak}")
        elif os.path.isdir(flatpak) and os.path.isdir(native):
            # Check which is more recent
            flat_ini = os.path.join(flatpak, "profiles.ini")
            native_ini = os.path.join(native, "profiles.ini")
            if os.path.exists(flat_ini) and (not os.path.exists(native_ini) or
                os.path.getmtime(flat_ini) > os.path.getmtime(native_ini)):
                cookie_arg = f"firefox:{flatpak}"
                log("info", f"Using Flatpak Firefox (more recent)")
    
    rc, out, err = run([
        "yt-dlp", "--cookies-from-browser", cookie_arg,
        "--dump-json", "--no-download", "--no-playlist",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    ], timeout=30)
    
    if rc == 0:
        log("pass", "yt-dlp cookie auth works (test video accessible)")
    else:
        err_lower = (err or '').lower()
        if "could not find" in err_lower or "no cookies" in err_lower or "not found" in err_lower:
            log("fail", f"yt-dlp can't find cookies for '{cookie_arg}'",
                err.strip()[-200:] if err else "",
                f"Check cookies_browser in config. Try: yt-dlp --cookies-from-browser {cookie_arg} --dump-json 'https://youtube.com/watch?v=dQw4w9WgXcQ'")
        elif "permission" in err_lower or "locked" in err_lower:
            log("fail", "Cookie database locked/permission error",
                err.strip()[-200:] if err else "",
                "Try closing the browser, or copy the cookies database")
        else:
            # yt-dlp succeeded at reading cookies but failed for another reason — ok
            log("info", f"yt-dlp cookie extraction test: {err.strip()[-100:]}" if err else "Unknown result")


# ============================================================================
# 3c. DISK SPACE & TEMP DIRECTORY
# ============================================================================

def check_disk():
    section("Disk & Temp Directory")
    
    os_name = platform.system()
    
    # Check temp directory
    tmp = tempfile.gettempdir()
    try:
        test_file = os.path.join(tmp, "yume_test_write")
        with open(test_file, "w") as f:
            f.write("test")
        os.unlink(test_file)
        log("pass", f"Temp directory writable: {tmp}")
    except Exception as e:
        log("fail", f"Temp directory not writable: {tmp}", str(e))
    
    # Check disk space on temp dir and working dir
    for path, label in [(tmp, "Temp"), (os.getcwd(), "Working dir")]:
        try:
            if os_name == "Windows":
                import ctypes
                free_bytes = ctypes.c_ulonglong(0)
                ctypes.windll.kernel32.GetDiskFreeSpaceExW(path, None, None, ctypes.byref(free_bytes))
                free_gb = free_bytes.value / 1024**3
            else:
                stat = os.statvfs(path)
                free_gb = (stat.f_bavail * stat.f_frsize) / 1024**3
            
            s = "pass" if free_gb > 5 else ("warn" if free_gb > 1 else "fail")
            log(s, f"{label} free space: {free_gb:.1f} GB ({path})",
                "Yume needs ~500MB for audio temp files" if free_gb < 5 else "")
        except:
            log("skip", f"Could not check disk space for {label}")
    
    # Count existing yume temp files
    try:
        yume_temps = [d for d in os.listdir(tmp) if d.startswith("yume_")]
        if yume_temps:
            total_size = 0
            for d in yume_temps:
                dpath = os.path.join(tmp, d)
                if os.path.isdir(dpath):
                    for f in os.listdir(dpath):
                        total_size += os.path.getsize(os.path.join(dpath, f))
            mb = total_size / 1024 / 1024
            if mb > 100:
                log("warn", f"Leftover Yume temp files: {len(yume_temps)} dirs ({mb:.0f} MB)",
                    "These accumulate over time", "Clear cache in popup, or delete /tmp/yume_* manually")
            else:
                log("info", f"Yume temp files: {len(yume_temps)} dirs ({mb:.0f} MB)")
    except:
        pass


# ============================================================================
# 4. SERVER CONNECTIVITY
# ============================================================================

def check_servers(cfg=None):
    section("Server Connectivity")
    
    whisper_host = "127.0.0.1"
    whisper_port = 5001
    trans_host = "127.0.0.1"
    trans_port = 5000
    
    # Try to read from config
    if cfg:
        whisper_host = cfg.get("whisper_host", whisper_host)
        whisper_port = cfg.get("whisper_port", whisper_port)
        trans_host = cfg.get("translation_host", trans_host)
        trans_port = cfg.get("translation_port", trans_port)
    
    # --- Whisper server ---
    whisper_url = f"http://{whisper_host}:{whisper_port}"
    if check_port(whisper_host, whisper_port):
        log("pass", f"Whisper server: port {whisper_port} open")
        
        # Health check
        code, body = http_get(f"{whisper_url}/health")
        if code == 200:
            try:
                data = json.loads(body)
                model = data.get("model", "?")
                device = data.get("device", "?")
                log("pass", f"Whisper /health: model={model}, device={device}")
            except:
                log("pass", "Whisper /health: responded (non-JSON)")
        else:
            log("warn", f"Whisper /health: HTTP {code}", body[:120])
        
        # Stats
        code, body = http_get(f"{whisper_url}/stats")
        if code == 200:
            try:
                data = json.loads(body)
                reqs = data.get("total_requests", 0)
                uptime = data.get("uptime_seconds", 0)
                cache = data.get("cache_entries", 0)
                gpu_mem = data.get("gpu", {}).get("memory_used_mb", "?")
                log("info", f"Whisper stats: {reqs} requests, {cache} cached chunks, uptime {uptime//60}m",
                    f"GPU memory: {gpu_mem} MB" if gpu_mem != "?" else "")
            except:
                pass
        
        # Cache status
        code, body = http_get(f"{whisper_url}/cache/status")
        if code == 200:
            try:
                data = json.loads(body)
                log("info", f"Subtitle cache: {data.get('chunks_cached', 0)} chunks")
            except:
                pass
    else:
        log("fail", f"Whisper server: port {whisper_port} not listening",
            f"Expected at {whisper_url}",
            "Run: python pocket_yume.py → Launch Yume")
    
    # --- Translation server ---
    if check_port(trans_host, trans_port):
        log("pass", f"Translation server: port {trans_port} open")
        
        # Health check
        code, body = http_get(f"http://{trans_host}:{trans_port}/health")
        if code == 200:
            log("pass", "Translation /health: OK")
        elif code == 0:
            # Try /v1/models (llama.cpp style)
            code2, body2 = http_get(f"http://{trans_host}:{trans_port}/v1/models")
            if code2 == 200:
                try:
                    data = json.loads(body2)
                    models = [m.get("id", "?") for m in data.get("data", [])]
                    log("pass", f"Translation /v1/models: {', '.join(models)}")
                except:
                    log("pass", "Translation /v1/models: responded")
            else:
                log("warn", "Translation: port open but health/models check failed", body[:120])
        
        # Quick translation test
        code, resp = http_post_json(f"http://{trans_host}:{trans_port}/v1/chat/completions", {
            "messages": [{"role": "user", "content": "Say OK"}],
            "max_tokens": 5, "temperature": 0
        })
        if code == 200 and isinstance(resp, dict):
            text = resp.get("choices", [{}])[0].get("message", {}).get("content", "")
            log("pass", f"Translation inference test: '{text[:40]}'")
        else:
            log("warn", "Translation inference test failed", str(resp)[:120])
    else:
        log("fail", f"Translation server: port {trans_port} not listening",
            f"Expected at http://{trans_host}:{trans_port}",
            "Run: python pocket_yume.py → Launch Yume")
    
    return whisper_url


# ============================================================================
# 5. YT-DLP & AUDIO PIPELINE
# ============================================================================

def check_ytdlp_pipeline(test_url=None, cfg=None):
    section("Audio Pipeline")
    
    if not test_url:
        test_url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"  # short, always available
    
    # Test yt-dlp can extract info
    log("info", f"Testing yt-dlp with: {test_url[:80]}")
    rc, out, err = run(["yt-dlp", "--dump-json", "--no-download", "--no-playlist", test_url], timeout=30)
    if rc == 0:
        try:
            info = json.loads(out)
            title = info.get("title", "?")[:50]
            duration = info.get("duration", 0)
            formats = len(info.get("formats", []))
            log("pass", f"yt-dlp info extract: '{title}' ({duration}s, {formats} formats)")
            
            # Show available audio formats
            audio_fmts = [f for f in info.get("formats", []) 
                          if f.get("acodec", "none") != "none" and f.get("vcodec", "none") == "none"]
            if audio_fmts:
                best = audio_fmts[-1]
                log("info", f"Audio formats available: {len(audio_fmts)} " +
                    f"(best: {best.get('ext','?')} {best.get('abr','?')}kbps {best.get('acodec','?')})")
            else:
                mixed = [f for f in info.get("formats", []) if f.get("acodec", "none") != "none"]
                log("warn", f"No audio-only formats! {len(mixed)} mixed formats available",
                    "yt-dlp will need to extract audio from a video stream (slower)")
        except:
            log("pass", "yt-dlp info extract: OK (non-JSON)")
    else:
        log("fail", "yt-dlp info extract failed", err[:200])
        if "Sign in" in err or "bot" in err.lower():
            log("warn", "YouTube is blocking unauthenticated requests",
                "", "Set up cookie auth in yume_config.json (see README)")
        if "Requested format" in err:
            log("warn", "Format issue — yt-dlp can't find a compatible audio stream",
                "", "pip install -U yt-dlp (update to latest)")
        return
    
    # Test yt-dlp can get a stream URL
    rc, out, err = run(["yt-dlp", "--get-url", "--format", "bestaudio*/bestaudio/best",
                         "--no-playlist", test_url], timeout=30)
    if rc == 0 and out.startswith("http"):
        log("pass", f"yt-dlp stream URL: {out[:80]}...")
    else:
        # Try without format
        rc2, out2, _ = run(["yt-dlp", "--get-url", "--no-playlist", test_url], timeout=30)
        if rc2 == 0 and out2.startswith("http"):
            log("warn", "yt-dlp stream URL: OK (but only without format filter)",
                "bestaudio format selector failed, server will use fallback")
        else:
            log("fail", "yt-dlp --get-url failed", err[:200], "pip install -U yt-dlp")
    
    # Test with auth if configured
    auth_method = cfg.get("youtube_auth_method", "deno") if cfg else "deno"
    cookies_browser_cfg = cfg.get("cookies_browser", "") if cfg else ""
    if auth_method == "cookies" and cookies_browser_cfg:
        log("info", f"Testing yt-dlp with cookie auth ({cookies_browser_cfg})...")
        
        # Resolve cookie path same as server
        cookie_arg = cookies_browser_cfg
        if cookies_browser_cfg.lower() == "firefox" and platform.system() == "Linux":
            flatpak = os.path.expanduser("~/.var/app/org.mozilla.firefox/.mozilla/firefox")
            native = os.path.expanduser("~/.mozilla/firefox")
            if os.path.isdir(flatpak) and not os.path.isdir(native):
                cookie_arg = f"firefox:{flatpak}"
        
        rc, out, err = run([
            "yt-dlp", "--cookies-from-browser", cookie_arg,
            "--dump-json", "--no-download", "--no-playlist", test_url
        ], timeout=30)
        if rc == 0:
            log("pass", f"yt-dlp with cookie auth: OK")
        else:
            log("fail", f"yt-dlp with cookie auth failed",
                (err or '')[-200:],
                f"Test manually: yt-dlp --cookies-from-browser {cookie_arg} --dump-json '{test_url}'")
    
    # Test ffmpeg can process audio
    rc, _, _ = run(["ffmpeg", "-version"])
    if rc != 0:
        log("skip", "Skipping ffmpeg test (not installed)")
        return
    
    # Quick ffmpeg decode test with a tiny generated wav
    try:
        tmp = tempfile.mktemp(suffix=".wav")
        rc, _, err = run([
            "ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-ar", "16000", "-ac", "1", tmp
        ], timeout=10)
        if rc == 0 and os.path.exists(tmp):
            size = os.path.getsize(tmp)
            log("pass", f"ffmpeg audio encode: OK ({size} bytes)")
            os.unlink(tmp)
        else:
            log("fail", "ffmpeg audio encode failed", err[:120])
    except Exception as e:
        log("warn", f"ffmpeg test error: {e}")


# ============================================================================
# 6. EXTENSION FILES
# ============================================================================

def check_extension():
    section("Extension Files")
    
    # Find extension directory
    ext_dir = None
    for candidate in [
        Path(__file__).parent / "extension",
        Path.cwd() / "extension",
    ]:
        if candidate.is_dir() and (candidate / "manifest.json").exists():
            ext_dir = candidate
            break
    
    if not ext_dir:
        log("skip", "Extension directory not found (not in project root?)")
        return
    
    log("info", f"Extension dir: {ext_dir}")
    
    # Check manifest
    try:
        with open(ext_dir / "manifest.json") as f:
            manifest = json.load(f)
        ver = manifest.get("version", "?")
        mv = manifest.get("manifest_version", "?")
        log("pass", f"manifest.json: v{ver} (Manifest V{mv})")
        
        # Check permissions
        perms = manifest.get("permissions", [])
        log("info", f"Permissions: {', '.join(perms)}")
        if "tabCapture" in perms:
            log("warn", "tabCapture permission is still present (dead code)", "",
                "Remove from manifest.json")
    except Exception as e:
        log("fail", f"manifest.json: {e}")
    
    # Check required files
    required = {
        "js/background.js": "Service worker (message proxy, translation cache)",
        "js/content.js": "Content script (main orchestrator)",
        "js/audio-capture.js": "Pipeline engine (chunk scheduling, transcription, translation)",
        "js/subtitle-window.js": "Subtitle overlay DOM",
        "js/debug-system.js": "Debug logging (must load first)",
        "popup.html": "Popup UI",
        "popup.js": "Popup logic",
        "css/content.css": "Subtitle window styles",
        "css/popup.css": "Popup styles",
    }
    
    dead_files = ["js/cache-manager.js", "js/transcription-manager.js"]
    
    for rel, desc in required.items():
        fp = ext_dir / rel
        if fp.exists():
            size = fp.stat().st_size
            log("pass", f"{rel} ({size:,} bytes)", desc)
        else:
            log("fail", f"{rel}: MISSING", desc)
    
    for rel in dead_files:
        fp = ext_dir / rel
        if fp.exists():
            log("warn", f"{rel}: dead code still present ({fp.stat().st_size:,} bytes)",
                "Not loaded by manifest but adds confusion",
                "Delete this file")
    
    # Check icons
    for size in [16, 48, 128]:
        icon = ext_dir / "icons" / f"icon{size}.png"
        if not icon.exists():
            log("warn", f"icons/icon{size}.png: missing")
    
    # Check fonts
    font_dir = ext_dir / "fonts"
    if font_dir.exists():
        fonts = list(font_dir.glob("*"))
        log("info", f"Bundled fonts: {len(fonts)} files")
    else:
        log("info", "No bundled fonts directory")


# ============================================================================
# 7. CONFIG
# ============================================================================

def check_config():
    section("Configuration")
    
    cfg = {}
    config_paths = [
        Path(__file__).parent / "yume_config.json",
        Path.cwd() / "yume_config.json",
        Path.home() / ".config" / "yume" / "yume_config.json",
    ]
    
    config_path = None
    for cp in config_paths:
        if cp.exists():
            config_path = cp
            break
    
    if not config_path:
        log("info", "No yume_config.json found (using defaults)")
        return cfg
    
    try:
        with open(config_path) as f:
            cfg = json.load(f)
        log("pass", f"Config: {config_path}", f"{len(cfg)} keys")
    except Exception as e:
        log("fail", f"Config parse error: {e}", str(config_path))
        return cfg
    
    # Check backend
    backend = cfg.get("translation_backend", "llamacpp")
    log("info", f"Translation backend: {backend}")
    
    if backend == "llamacpp":
        model_path = cfg.get("gguf_model_path", "")
        if model_path:
            if Path(model_path).exists():
                size_mb = Path(model_path).stat().st_size / 1024 / 1024
                log("pass", f"GGUF model: {Path(model_path).name} ({size_mb:.0f} MB)")
            else:
                log("fail", f"GGUF model not found: {model_path}", "",
                    "Download a model or update gguf_model_path in config")
        else:
            log("warn", "No GGUF model path configured", "",
                "Run pocket_yume.py → Setup to download a model")
    
    # Check YouTube auth
    auth = cfg.get("youtube_auth_method", "")
    if auth:
        log("info", f"YouTube auth: {auth}")
        if auth == "cookies":
            browser = cfg.get("cookies_browser", "")
            log("info", f"Cookie browser: {browser or 'not set'}")
    
    # Check ports
    wp = cfg.get("whisper_port", 5001)
    tp = cfg.get("translation_port", 5000)
    log("info", f"Ports: Whisper={wp}, Translation={tp}")
    
    return cfg


# ============================================================================
# 8. LOG ANALYSIS
# ============================================================================

def check_logs():
    section("Log Analysis")
    
    log_dir = Path(__file__).parent / "logs"
    if not log_dir.exists():
        log_dir = Path.cwd() / "logs"
    
    if not log_dir.exists():
        log("info", "No logs directory found")
        return
    
    log_files = {
        "whisper_server.log": "Whisper server log",
        "translation_server.log": "Translation server log",
    }
    
    for name, desc in log_files.items():
        path = log_dir / name
        if not path.exists():
            log("info", f"{name}: not found")
            continue
        
        size = path.stat().st_size
        log("info", f"{name}: {size:,} bytes")
        
        # Read last 50 lines and scan for errors
        try:
            with open(path, "r", errors="replace") as f:
                lines = f.readlines()
            
            tail = lines[-50:] if len(lines) > 50 else lines
            errors = []
            warnings = []
            
            for line in tail:
                ll = line.lower()
                if "error" in ll or "exception" in ll or "traceback" in ll or "failed" in ll:
                    errors.append(line.strip()[:120])
                elif "warning" in ll or "warn" in ll:
                    warnings.append(line.strip()[:120])
            
            if errors:
                log("warn", f"{name}: {len(errors)} recent errors")
                for e in errors[-5:]:  # show last 5
                    print(f"         {C.RED}{e}{C.RESET}")
                
                # Specific pattern matching
                full_tail = "\n".join(l.strip() for l in tail)
                if "TensileLibrary" in full_tail:
                    log("fail", "ROCm TensileLibrary error detected!",
                        "Your GPU arch isn't supported by installed ROCm",
                        "export HSA_OVERRIDE_GFX_VERSION=10.3.0 (for RDNA1) then restart")
                if "CUDA" in full_tail and "out of memory" in full_tail.lower():
                    log("fail", "CUDA out of memory", "",
                        "Use a smaller Whisper model or close other GPU applications")
                if "Could not load" in full_tail and "model" in full_tail.lower():
                    log("fail", "Model loading error", "",
                        "Check model path in yume_config.json")
                if "code -6" in full_tail or "SIGABRT" in full_tail:
                    log("fail", "Process crash (SIGABRT / exit code -6)",
                        "Usually means ROCm/GPU library issue",
                        "Check HSA_OVERRIDE_GFX_VERSION or try CPU mode")
                if "cookies" in full_tail.lower() and ("not found" in full_tail.lower() or "error" in full_tail.lower()):
                    log("warn", "Cookie-related error detected",
                        "yt-dlp may not be finding your browser cookies",
                        "Run yume_doctor.py to check cookie accessibility")
            else:
                log("pass", f"{name}: no recent errors (last {len(tail)} lines clean)")
            
            if warnings:
                log("info", f"{name}: {len(warnings)} recent warnings")
                for w in warnings[-3:]:
                    print(f"         {C.YELLOW}{w}{C.RESET}")
        except Exception as e:
            log("warn", f"Could not read {name}: {e}")


# ============================================================================
# 9. NETWORK / FIREWALL
# ============================================================================

def check_network():
    section("Network & Ports")
    
    ports_to_check = [
        (5001, "Whisper server"),
        (5000, "Translation server (llama.cpp)"),
        (11434, "Ollama (if used)"),
    ]
    
    for port, desc in ports_to_check:
        if check_port("127.0.0.1", port, timeout=2):
            log("pass", f"Port {port} ({desc}): listening")
        else:
            log("info", f"Port {port} ({desc}): not in use")
    
    # Check if ports are blocked by another process
    if IS_WIN:
        rc, out, _ = run(["netstat", "-ano", "-p", "TCP"], shell=True, timeout=10)
        if rc == 0:
            for port, desc in ports_to_check:
                for line in out.splitlines():
                    if f":{port}" in line and "LISTENING" in line:
                        pid = line.strip().split()[-1]
                        log("info", f"Port {port}: PID {pid}")
    else:
        for port, desc in ports_to_check:
            rc, out, _ = run(["lsof", "-i", f":{port}", "-t"])
            if rc == 0 and out:
                pids = out.strip().split()
                log("info", f"Port {port}: PID(s) {', '.join(pids)}")


# ============================================================================
# 10. FULL PIPELINE TEST
# ============================================================================

def test_full_pipeline(whisper_url, video_url):
    section("Full Pipeline Test")
    
    if not video_url:
        log("skip", "No test video URL provided", "", "Run with --test-video URL")
        return
    
    log("info", f"Testing: {video_url[:80]}")
    
    # Step 1: Prepare
    log("info", "Step 1/4: Preparing video (downloading audio)...")
    t0 = time.time()
    code, resp = http_post_json(f"{whisper_url}/prepare", {
        "url": video_url,
        "video_id": "doctor_test"
    }, timeout=120)
    
    if code == 200 and isinstance(resp, dict) and resp.get("status") == "ready":
        dur = resp.get("duration", 0)
        log("pass", f"Prepare: OK ({dur:.0f}s audio, took {time.time()-t0:.1f}s)")
    else:
        log("fail", f"Prepare failed: HTTP {code}", str(resp)[:200])
        return
    
    # Step 2: Transcribe first chunk
    log("info", "Step 2/4: Transcribing chunk 0 (0-30s)...")
    t1 = time.time()
    code, resp = http_post_json(f"{whisper_url}/transcribe_url", {
        "url": video_url,
        "video_id": "doctor_test",
        "start_time": 0,
        "duration": 30,
        "step_size": 25,
        "chunk_index": 0
    }, timeout=120)
    
    if code == 200 and isinstance(resp, dict):
        segments = resp.get("segments", [])
        text_preview = ""
        if segments:
            text_preview = segments[0].get("text", "")[:60]
        log("pass", f"Transcribe: {len(segments)} segments ({time.time()-t1:.1f}s)",
            f"Preview: '{text_preview}'" if text_preview else "")
    else:
        log("fail", f"Transcribe failed: HTTP {code}", str(resp)[:200])
        return
    
    # Step 3: Translation test (if translation server is up)
    if check_port("127.0.0.1", 5000):
        if segments:
            test_text = segments[0].get("text", "テスト")
            log("info", f"Step 3/4: Translating: '{test_text[:40]}'...")
            t2 = time.time()
            code, resp = http_post_json("http://127.0.0.1:5000/v1/chat/completions", {
                "messages": [
                    {"role": "system", "content": "Translate Japanese to English. Output ONLY the translation."},
                    {"role": "user", "content": test_text}
                ],
                "max_tokens": 200, "temperature": 0.1
            }, timeout=30)
            
            if code == 200 and isinstance(resp, dict):
                trans = resp.get("choices", [{}])[0].get("message", {}).get("content", "")
                log("pass", f"Translate: '{trans[:60]}' ({time.time()-t2:.1f}s)")
            else:
                log("fail", f"Translate failed: HTTP {code}", str(resp)[:120])
        else:
            log("skip", "Step 3/4: No segments to translate")
    else:
        log("skip", "Step 3/4: Translation server not running")
    
    # Step 4: Cache verification
    log("info", "Step 4/4: Checking cache...")
    code, body = http_get(f"{whisper_url}/cache/status")
    if code == 200:
        try:
            data = json.loads(body)
            log("pass", f"Cache: {data.get('chunks_cached', 0)} chunks stored")
        except:
            log("pass", "Cache check: OK")
    
    log("pass", f"Full pipeline test completed in {time.time()-t0:.1f}s total")


# ============================================================================
# SUMMARY
# ============================================================================

def print_summary():
    section("Summary")
    
    counts = {"pass": 0, "fail": 0, "warn": 0, "skip": 0, "info": 0}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    
    fails = [r for r in results if r["status"] == "fail"]
    warns = [r for r in results if r["status"] == "warn"]
    
    bar = (f"  {C.GREEN}{counts['pass']} passed{C.RESET}  "
           f"{C.RED}{counts['fail']} failed{C.RESET}  "
           f"{C.YELLOW}{counts['warn']} warnings{C.RESET}  "
           f"{C.DIM}{counts['info']} info  {counts['skip']} skipped{C.RESET}")
    print(bar)
    
    if fails:
        print(f"\n  {C.RED}{C.BOLD}Critical issues:{C.RESET}")
        for r in fails:
            print(f"    {C.RED}✗{C.RESET} {r['check']}")
            if r["fix"]:
                print(f"      {C.YELLOW}→ {r['fix']}{C.RESET}")
    
    if warns:
        print(f"\n  {C.YELLOW}{C.BOLD}Warnings:{C.RESET}")
        for r in warns:
            print(f"    {C.YELLOW}⚠{C.RESET} {r['check']}")
            if r["fix"]:
                print(f"      {C.YELLOW}→ {r['fix']}{C.RESET}")
    
    if not fails and not warns:
        print(f"\n  {C.GREEN}{C.BOLD}Everything looks good! 🎉{C.RESET}")
    
    print()
    return len(fails)


# ============================================================================
# MAIN
# ============================================================================

def main():
    parser = argparse.ArgumentParser(description="Yume Doctor — diagnostic tool for PocketYume")
    parser.add_argument("--fix", action="store_true", help="Auto-fix issues where possible")
    parser.add_argument("--json", action="store_true", help="Output results as JSON")
    parser.add_argument("--test-video", type=str, help="URL to test full pipeline with")
    parser.add_argument("--skip-pipeline", action="store_true", help="Skip yt-dlp/audio pipeline tests")
    parser.add_argument("--skip-servers", action="store_true", help="Skip server connectivity tests")
    args = parser.parse_args()
    
    print(f"""
{C.BOLD}{C.CYAN}╔══════════════════════════════════════════════════════════════╗
║                     YUME DOCTOR v3.8.0                       ║
║            Cross-platform diagnostics for PocketYume         ║
╚══════════════════════════════════════════════════════════════╝{C.RESET}
{C.DIM}  Date: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
  Platform: {platform.system()} {platform.release()} ({platform.machine()}){C.RESET}""")
    
    check_system()
    gpu_info = check_gpu()
    check_dependencies(do_fix=args.fix)
    cfg = check_config()
    check_browser_cookies(cfg)
    check_disk()
    check_extension()
    check_logs()
    
    whisper_url = None
    if not args.skip_servers:
        whisper_url = check_servers(cfg)
    
    if not args.skip_pipeline:
        check_ytdlp_pipeline(args.test_video, cfg)
    
    check_network()
    
    if args.test_video and whisper_url and check_port("127.0.0.1", int(cfg.get("whisper_port", 5001))):
        test_full_pipeline(whisper_url, args.test_video)
    
    fail_count = print_summary()
    
    # JSON export
    if args.json:
        report = {
            "timestamp": datetime.now().isoformat(),
            "platform": {"os": platform.system(), "release": platform.release(),
                         "arch": platform.machine(), "python": platform.python_version()},
            "results": results,
            "summary": {"pass": sum(1 for r in results if r["status"]=="pass"),
                        "fail": sum(1 for r in results if r["status"]=="fail"),
                        "warn": sum(1 for r in results if r["status"]=="warn")}
        }
        report_path = Path("yume_doctor_report.json")
        with open(report_path, "w") as f:
            json.dump(report, f, indent=2)
        print(f"  {C.CYAN}Report saved: {report_path}{C.RESET}\n")
    
    sys.exit(1 if fail_count > 0 else 0)


if __name__ == "__main__":
    main()
