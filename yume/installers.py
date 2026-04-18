"""Tool and Python dependency installers."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import tarfile
import threading
import time
import zipfile

from yume.ui import C, error, info, success, warn
from yume.utils import BASE_DIR, TOOLS_DIR, _run, find_tool

_log = logging.getLogger("pocket_yume")

IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
IS_LIN = sys.platform.startswith("linux")
EXE = ".exe" if IS_WIN else ""
UNIX_EXEC_MODE = 0o750

_pip_venv_warned = False

# Download URLs are supplied by pocket_yume at runtime to avoid duplication
_DOWNLOAD_URLS: dict = {}


def _safe_extractall(zf: zipfile.ZipFile, target_dir: "os.PathLike[str]") -> None:
    """Extract zip archive with zip-slip path-traversal protection."""
    from pathlib import Path as _Path

    target_resolved = _Path(target_dir).resolve()
    for member in zf.namelist():
        member_resolved = (target_resolved / member).resolve()
        try:
            member_resolved.relative_to(target_resolved)
        except ValueError as exc:
            raise ValueError(f"Unsafe path in archive member: {member!r}") from exc
    zf.extractall(target_dir)


def set_download_urls(urls: dict) -> None:
    """Called by pocket_yume to inject platform-specific URLs."""
    global _DOWNLOAD_URLS
    _DOWNLOAD_URLS = urls


# ── Pip helpers ───────────────────────────────────────────────────────────────


def _is_venv() -> bool:
    return hasattr(sys, "real_prefix") or (hasattr(sys, "base_prefix") and sys.base_prefix != sys.prefix)


def _check_pip() -> bool:
    global _pip_venv_warned
    r = _run([sys.executable, "-m", "pip", "--version"], timeout=10)
    if r.returncode != 0:
        error("pip is not installed or not working!")
        if IS_WIN:
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
    if not _is_venv() and not _pip_venv_warned:
        _pip_venv_warned = True
        warn("Installing packages into your system Python.")
        info("Consider using a virtual environment:")
        info(f"  {sys.executable} -m venv yume-env")
        if IS_WIN:
            info("  yume-env\\Scripts\\activate")
        else:
            info("  source yume-env/bin/activate")
        print()
    return True


def _detect_package_manager() -> str | None:
    for cmd, name in [("dnf", "dnf"), ("apt-get", "apt"), ("brew", "brew"), ("pacman", "pacman"), ("zypper", "zypper")]:
        if shutil.which(cmd):
            return name
    return None


def _has_build_tools() -> bool:
    return bool(shutil.which("ninja") or shutil.which("ninja-build")) and bool(shutil.which("cmake"))


def _install_build_tools() -> bool:
    pm = _detect_package_manager()
    if not pm:
        warn("No package manager found. Please install manually: ninja cmake gcc (or g++)")
        return False
    info(f"Installing build tools via {pm}...")
    cmds = {
        "dnf": ["sudo", "dnf", "install", "-y", "ninja-build", "cmake", "gcc-c++", "gcc"],
        "apt": ["sudo", "apt-get", "install", "-y", "ninja-build", "cmake", "g++", "gcc"],
        "brew": ["brew", "install", "ninja", "cmake"],
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
        warn(f"Package manager returned code {r.returncode}")
        info("Trying pip install ninja cmake as fallback...")
        _run([sys.executable, "-m", "pip", "install", "ninja", "cmake", "-q"], timeout=120)
        return True
    except Exception as e:
        warn(f"System install failed: {e}")
        try:
            _run([sys.executable, "-m", "pip", "install", "ninja", "cmake", "-q"], timeout=120)
            return True
        except Exception:
            return False


# ── Tool installers ────────────────────────────────────────────────────────────


def install_ytdlp() -> bool:
    from yume.network import download_file

    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    dest = TOOLS_DIR / ("yt-dlp.exe" if IS_WIN else "yt-dlp")
    ok = download_file(_DOWNLOAD_URLS.get("yt-dlp", ""), dest, "yt-dlp")
    if ok and not IS_WIN:
        os.chmod(dest, UNIX_EXEC_MODE)
    return ok


def install_ffmpeg() -> bool:
    from yume.network import download_file

    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    if IS_WIN:
        zp = TOOLS_DIR / "ffmpeg.zip"
        if not download_file(_DOWNLOAD_URLS.get("ffmpeg", ""), zp, "FFmpeg"):
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
        if not download_file(_DOWNLOAD_URLS.get("ffmpeg", ""), tp, "FFmpeg"):
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
        if shutil.which("brew"):
            info("Installing FFmpeg via Homebrew...")
            r = _run(["brew", "install", "ffmpeg"], timeout=600)
            return r.returncode == 0
        zp = TOOLS_DIR / "ffmpeg.zip"
        if not download_file(_DOWNLOAD_URLS.get("ffmpeg", ""), zp, "FFmpeg"):
            return False
        try:
            with zipfile.ZipFile(zp) as zf:
                _safe_extractall(zf, TOOLS_DIR)
            zp.unlink(missing_ok=True)
            for b in (TOOLS_DIR / "ffmpeg", TOOLS_DIR / "ffprobe"):
                if b.exists():
                    os.chmod(b, UNIX_EXEC_MODE)
            return True
        except Exception as e:
            error(f"Failed: {e}")
            return False


def install_deno() -> bool:
    from yume.network import download_file

    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    zp = TOOLS_DIR / "deno.zip"
    if not download_file(_DOWNLOAD_URLS.get("deno", ""), zp, "Deno"):
        return False
    try:
        with zipfile.ZipFile(zp) as zf:
            _safe_extractall(zf, TOOLS_DIR)
        zp.unlink(missing_ok=True)
        de = TOOLS_DIR / f"deno{EXE}"
        if de.exists() and not IS_WIN:
            os.chmod(de, UNIX_EXEC_MODE)
        success("Deno extracted")
    except Exception as e:
        error(f"Failed: {e}")
        return False

    info("Installing YouTube PO token plugin (bgutil-ytdlp-pot-provider)...")
    info(f"{C.DIM}This plugin uses Deno to solve YouTube's bot-detection challenges.{C.RESET}")
    try:
        r = _run(
            [sys.executable, "-m", "pip", "install", "-q", "--no-warn-script-location", "bgutil-ytdlp-pot-provider"],
            timeout=120,
        )
        if r.returncode == 0:
            success("PO token plugin installed")
        else:
            warn("PO token plugin install failed — YouTube may require sign-in")
    except Exception as e:
        warn(f"PO token plugin install failed: {e}")

    info("Setting up bgutil PO token server...")
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
                    _safe_extractall(zf, TOOLS_DIR)
                zip_path.unlink(missing_ok=True)
                extracted = TOOLS_DIR / "bgutil-ytdlp-pot-provider-1.3.1"
                if extracted.exists():
                    if bgutil_dir.exists():
                        shutil.rmtree(bgutil_dir)
                    extracted.rename(bgutil_dir)
                success("bgutil server extracted")
                if main_ts.exists():
                    deno = find_tool("deno") or "deno"
                    info("Installing bgutil server dependencies (deno install)...")
                    try:
                        r = _run(
                            [deno, "install", "--allow-scripts=npm:canvas", "--frozen"],
                            cwd=str(server_dir),
                            timeout=180,
                        )
                        if r.returncode != 0:
                            r = _run([deno, "install", "--allow-scripts=npm:canvas"], cwd=str(server_dir), timeout=180)
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
    return True


def install_ollama() -> bool:
    from yume.network import check_server, download_file

    if IS_WIN:
        TOOLS_DIR.mkdir(parents=True, exist_ok=True)
        inst = TOOLS_DIR / "OllamaSetup.exe"
        if not download_file(_DOWNLOAD_URLS.get("ollama", ""), inst, "Ollama installer"):
            return False
        print(f"\n  {C.YELLOW}The Ollama installer will now open. Follow the prompts.{C.RESET}\n")
        from yume.ui import pause

        pause("Press Enter to launch installer...")
        try:
            subprocess.Popen([str(inst)])
            info("Waiting for Ollama...")
            for _ in range(90):
                time.sleep(2)
                if shutil.which("ollama") or check_server("127.0.0.1", 11434, "/api/tags")["up"]:
                    success("Ollama installed!")
                    inst.unlink(missing_ok=True)
                    return True
            warn("Timed out.")
            return False
        except Exception as e:
            error(f"Failed: {e}")
            return False
    elif IS_MAC:
        from yume.network import download_file

        zp = TOOLS_DIR / "Ollama.zip"
        if not download_file(_DOWNLOAD_URLS.get("ollama", ""), zp, "Ollama"):
            return False
        info("Extract and move to Applications manually.")
        return True
    else:
        info("Installing Ollama via official script...")
        script = TOOLS_DIR / "ollama_install.sh"
        TOOLS_DIR.mkdir(parents=True, exist_ok=True)
        try:
            from yume.network import download_file

            if not download_file(_DOWNLOAD_URLS.get("ollama", ""), script, "Ollama install script"):
                return False
            script.chmod(UNIX_EXEC_MODE)
            r = _run(["bash", str(script)], timeout=300)
            script.unlink(missing_ok=True)
            return r.returncode == 0
        except Exception as e:
            error(f"Failed: {e}")
            script.unlink(missing_ok=True)
            return False


def install_python_deps() -> bool:
    """Install flask + faster-whisper deps."""

    if not _check_pip():
        return False

    SERVER_DIR = BASE_DIR / "server"
    req = SERVER_DIR / "requirements.txt"
    if not req.exists():
        req = BASE_DIR / "requirements.txt"
    if not req.exists():
        SERVER_DIR.mkdir(parents=True, exist_ok=True)
        req.write_text(
            "faster-whisper==1.2.1\nflask==3.1.3\nflask-cors==6.0.2\nwaitress==3.0.2\nnumpy==2.4.3\n"
            "pykakasi==2.3.0\npypinyin==0.55.0\n"
        )

    if IS_WIN and not _has_build_tools():
        warn("C++ build tools not detected.")
        info("If the install fails, you may need Visual Studio Build Tools:")
        info("  https://visualstudio.microsoft.com/visual-cpp-build-tools/")

    info("Installing Whisper server Python packages...")
    try:
        r = _run(
            [sys.executable, "-m", "pip", "install", "-r", str(req), "-q", "--no-warn-script-location"], timeout=600
        )
        if r.returncode != 0:
            error("pip install failed. Error output:")
            stderr_text = (r.stderr or "").strip()
            if stderr_text:
                for line in stderr_text.splitlines()[-15:]:
                    print(f"    {C.DIM}{line}{C.RESET}")
            else:
                print(f"    {C.DIM}(no error output captured — try running with --verbose){C.RESET}")
            if IS_WIN and ("Microsoft Visual C++" in (r.stderr or "") or "cl.exe" in (r.stderr or "")):
                error("This looks like a missing C++ compiler.")
                info("Install Visual Studio Build Tools:")
                info("  https://visualstudio.microsoft.com/visual-cpp-build-tools/")
            return False
        success("Done!")
        return True
    except Exception as e:
        error(f"Failed: {e}")
        info(f"  Run manually: {C.BOLD}pip install -r server/requirements.txt{C.RESET}")
        return False


def install_llamacpp_python() -> bool:
    """Install llama-cpp-python with GPU support if NVIDIA detected."""
    from yume.hardware import detect_gpu

    if not _check_pip():
        return False

    gpu = detect_gpu()

    def _pip_install_with_progress(cmd, label, timeout=600, env=None):
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
            kw: dict = {"timeout": timeout}
            if env is not None:
                kw["env"] = env
            r = _run(cmd, **kw)
            stop_event.set()
            elapsed = int(time.time() - t0)
            m2, s2 = divmod(elapsed, 60)
            sys.stdout.write(f"\r{' ' * 40}\r")
            sys.stdout.flush()
            if r.returncode == 0:
                success(f"{label} — done in {m2}m{s2:02d}s")
                return True
            else:
                error(f"{label} — failed after {m2}m{s2:02d}s")
                stderr_text = (r.stderr or "").strip()
                if stderr_text:
                    for line in stderr_text.splitlines()[-10:]:
                        print(f"    {C.DIM}{line}{C.RESET}")
                return False
        except subprocess.TimeoutExpired:
            stop_event.set()
            error(f"{label} — timed out after {timeout // 60} minutes")
            return False
        except Exception as e:
            stop_event.set()
            error(f"{label} — error: {e}")
            return False

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

    installed = False
    if gpu["has_nvidia"]:
        if IS_WIN:
            installed = _pip_install_with_progress(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "llama-cpp-python",
                    "--extra-index-url",
                    "https://abetlen.github.io/llama-cpp-python/whl/cu124",
                    "-q",
                    "--no-warn-script-location",
                ],
                "Installing llama-cpp-python (CUDA prebuilt)",
            )
            if not installed:
                warn("Prebuilt failed, trying CPU version...")
        else:
            env = os.environ.copy()
            env["CMAKE_ARGS"] = "-DGGML_CUDA=on"
            installed = _pip_install_with_progress(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "llama-cpp-python",
                    "--force-reinstall",
                    "--no-cache-dir",
                    "-q",
                ],
                "Building llama-cpp-python (CUDA from source)",
                env=env,
            )
            if not installed:
                warn("CUDA build failed, trying CPU...")

    elif gpu["has_amd"]:
        info("AMD Radeon GPU detected. Installing llama-cpp-python with ROCm...")
        if IS_WIN:
            warn("ROCm is only supported on Linux for llama-cpp-python.")
            info("Falling back to CPU mode on Windows (AMD DirectML not yet supported).")
        else:
            env = os.environ.copy()
            env["CMAKE_ARGS"] = "-DGGML_HIP=on"
            installed = _pip_install_with_progress(
                [sys.executable, "-m", "pip", "install", "llama-cpp-python", "--force-reinstall", "--no-cache-dir"],
                "Building llama-cpp-python (ROCm/HIP from source)",
                timeout=900,
                env=env,
            )
            if not installed:
                warn("ROCm build failed. Check that ROCm is installed.")
                info("Falling back to CPU...")

    if not installed:
        installed = _pip_install_with_progress(
            [
                sys.executable,
                "-m",
                "pip",
                "install",
                "llama-cpp-python",
                "--extra-index-url",
                "https://abetlen.github.io/llama-cpp-python/whl/cpu",
                "-q",
                "--no-warn-script-location",
            ],
            "Installing llama-cpp-python (prebuilt CPU)",
            timeout=300,
        )
        if not installed:
            warn("Prebuilt wheel not available, building from source...")
            installed = _pip_install_with_progress(
                [sys.executable, "-m", "pip", "install", "llama-cpp-python", "--no-warn-script-location"],
                "Building llama-cpp-python (CPU from source)",
                timeout=900,
            )

    if installed:
        info("Installing server dependencies (uvicorn, fastapi)...")
        try:
            r = _run(
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
            if r.returncode == 0:
                success("Server dependencies installed!")
            else:
                warn("Some server deps may have failed — check manually")
        except Exception as e:
            warn(f"Server deps install issue: {e}")

    return installed


def pull_ollama_model(name: str) -> bool:
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
