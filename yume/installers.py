"""Tool and Python dependency installers."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tarfile
import time
import zipfile
from pathlib import Path

from config import DEFAULT_OLLAMA_PORT
from yume import po_token
from yume.constants import BASE_DIR, TOOLS_DIR, IS_WIN, IS_MAC, IS_LIN, EXE, HEALTH_PATH_OLLAMA, DOWNLOAD_URLS
from yume.llama_python import (
    detect_package_manager,
    ensure_build_tools,
    has_build_tools,
    install_cpu_build,
    install_gpu_build,
)
from yume.ui import C, error, info, success, warn
from yume.utils import _run, find_ollama, safe_extractall

UNIX_EXEC_MODE = 0o750

_pip_venv_warned = False

# Published checksum files next to "latest" release assets. Verified when
# available; a mismatch aborts the install. (Same origin as the binary, so this
# guards against corrupted/truncated downloads, not a compromised release.)
_YTDLP_SUMS = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/SHA2-256SUMS"
_BTBN_SUMS = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/checksums.sha256"

_VS_BUILD_TOOLS = "https://visualstudio.microsoft.com/visual-cpp-build-tools/"


def _expected_sha256(url: str) -> str | None:
    """Published SHA-256 for a tool download URL, or None if none is published."""
    from yume.network import fetch_published_sha256

    name = url.rsplit("/", 1)[-1]
    if "github.com/yt-dlp/yt-dlp/" in url:
        sums = _YTDLP_SUMS
    elif "github.com/BtbN/FFmpeg-Builds/" in url:
        sums = _BTBN_SUMS
    elif "github.com/denoland/deno/" in url:
        sums = url + ".sha256sum"
    else:
        return None
    sha = fetch_published_sha256(sums, name)
    if sha is None:
        warn(f"Could not fetch the published checksum for {name} — downloading without verification.")
    return sha


def _download_verified(key: str, dest, label: str) -> bool:
    from yume.network import download_file

    url = DOWNLOAD_URLS.get(key, "")
    sha = _expected_sha256(url) if url else None
    if sha:
        info(f"{C.DIM}Verifying against the published SHA-256 checksum{C.RESET}")
    return download_file(url, dest, label, sha256=sha)


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
            pm = detect_package_manager()
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
        if IS_WIN and sys.version_info >= (3, 13):
            info(f"  {C.DIM}Python {sys.version_info.major}.{sys.version_info.minor} may lack prebuilt CUDA wheels.")
            info(f"  {C.DIM}Use Python 3.12 for guaranteed CUDA support:{C.RESET}")
            info(
                f"  {C.CYAN}py -3.12 -m venv yume-env{C.RESET}  {C.DIM}(install 3.12 from python.org if missing){C.RESET}"
            )
            info(f"  {C.CYAN}yume-env\\Scripts\\activate{C.RESET}")
        else:
            info(f"  {sys.executable} -m venv yume-env")
            if IS_WIN:
                info("  yume-env\\Scripts\\activate")
            else:
                info("  source yume-env/bin/activate")
        print()
    return True


# ── Tool installers ────────────────────────────────────────────────────────────


def install_ytdlp() -> bool:

    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    dest = TOOLS_DIR / ("yt-dlp.exe" if IS_WIN else "yt-dlp")
    ok = _download_verified("yt-dlp", dest, "yt-dlp")
    if ok and not IS_WIN:
        os.chmod(dest, UNIX_EXEC_MODE)
    return ok


def _ffmpeg_names() -> set[str]:
    return {f"{b}.exe" if IS_WIN else b for b in ("ffmpeg", "ffprobe")}


def _extract_ffmpeg(archive) -> None:
    """Copy ffmpeg and ffprobe, wherever they sit in the archive, into TOOLS_DIR."""
    names = _ffmpeg_names()
    if archive.suffix == ".zip":
        with zipfile.ZipFile(archive) as zf:
            for n in zf.namelist():
                if os.path.basename(n) in names:
                    with zf.open(n) as src, open(TOOLS_DIR / os.path.basename(n), "wb") as dst:
                        shutil.copyfileobj(src, dst)
    else:
        with tarfile.open(archive, "r:xz") as tf:
            for m in tf.getmembers():
                if m.isfile() and os.path.basename(m.name) in names:
                    m.name = os.path.basename(m.name)  # flat, and never outside TOOLS_DIR
                    tf.extract(m, TOOLS_DIR)
    archive.unlink(missing_ok=True)
    if not IS_WIN:
        for n in names:
            if (TOOLS_DIR / n).exists():
                os.chmod(TOOLS_DIR / n, UNIX_EXEC_MODE)


def _ffmpeg_runs() -> bool:
    ff = TOOLS_DIR / ("ffmpeg.exe" if IS_WIN else "ffmpeg")
    if not ff.exists():
        error(f"{ff.name} not found after extraction — the archive may have a different structure")
        info("Download manually: https://ffmpeg.org/download.html")
        return False
    if _run([str(ff), "-version"], timeout=5).returncode != 0:
        warn("ffmpeg extracted but fails to run — try downloading manually")
        return False
    return True


def install_ffmpeg() -> bool:
    """ffmpeg + ffprobe into tools/: Homebrew on a Mac that has it, else the platform's static build."""
    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    if not IS_WIN and not IS_LIN and shutil.which("brew"):
        info("Installing FFmpeg via Homebrew...")
        return _run(["brew", "install", "ffmpeg"], timeout=600).returncode == 0
    archive = TOOLS_DIR / ("ffmpeg.tar.xz" if IS_LIN else "ffmpeg.zip")
    if not _download_verified("ffmpeg", archive, "FFmpeg"):
        return False
    print(f"  {C.CYAN}...{C.RESET}  Extracting...", end="", flush=True)
    try:
        _extract_ffmpeg(archive)
    except Exception as e:
        print(f"\r  {C.RED}x{C.RESET}  Failed: {e}")
        return False
    print(f"\r  {C.GREEN}+{C.RESET}  Extracted" + " " * 30)
    return _ffmpeg_runs()


def install_deno() -> bool:
    """Deno, the YouTube PO token plugin for yt-dlp, and the plugin's token
    server. Only Deno itself is required; the rest warns when it fails."""
    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    if not _install_deno_exe():
        return False
    po_token.install_plugin()
    po_token.setup_server()
    return True


def _install_deno_exe() -> bool:
    zp = TOOLS_DIR / "deno.zip"
    if not _download_verified("deno", zp, "Deno"):
        return False
    try:
        with zipfile.ZipFile(zp) as zf:
            safe_extractall(zf, TOOLS_DIR)  # nosec B202 — zip-slip checked inside
        zp.unlink(missing_ok=True)
        de = TOOLS_DIR / f"deno{EXE}"
        if de.exists() and not IS_WIN:
            os.chmod(de, UNIX_EXEC_MODE)
        success("Deno extracted")
        return True
    except Exception as e:
        error(f"Failed: {e}")
        return False


def _remove_finished_setup(setup_exe: Path) -> None:
    try:
        setup_exe.unlink(missing_ok=True)
    except OSError:  # ollama.exe lands before the setup program exits, and Windows locks a running program
        pass


def _install_ollama_windows() -> bool:
    from yume.network import check_server, download_file
    from yume.ui import pause

    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    inst = TOOLS_DIR / "OllamaSetup.exe"
    if not download_file(DOWNLOAD_URLS.get("ollama", ""), inst, "Ollama installer"):
        return False
    print(f"\n  {C.YELLOW}The Ollama installer will now open. Follow the prompts.{C.RESET}\n")
    pause("Press Enter to launch installer...")
    try:
        subprocess.Popen([str(inst)])
        info("Waiting for Ollama...")
        for _ in range(90):
            time.sleep(2)
            if find_ollama() or check_server("127.0.0.1", DEFAULT_OLLAMA_PORT, HEALTH_PATH_OLLAMA)["up"]:
                success("Ollama installed!")
                _remove_finished_setup(inst)
                return True
        warn("Timed out.")
        return False
    except Exception as e:
        error(f"Failed: {e}")
        return False


def _install_ollama_mac() -> bool:
    from yume.network import download_file

    zp = TOOLS_DIR / "Ollama.zip"
    if not download_file(DOWNLOAD_URLS.get("ollama", ""), zp, "Ollama"):
        return False
    info("Extract and move to Applications manually.")
    return True


def _install_ollama_linux() -> bool:
    from yume.network import download_file

    info("Installing Ollama via official script...")
    script = TOOLS_DIR / "ollama_install.sh"
    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        if not download_file(DOWNLOAD_URLS.get("ollama", ""), script, "Ollama install script"):
            return False
        script.chmod(UNIX_EXEC_MODE)
        # Not captured: the script asks for the sudo password
        r = _run(["bash", str(script)], timeout=900, capture_output=False)
        script.unlink(missing_ok=True)
        return r.returncode == 0
    except Exception as e:
        error(f"Failed: {e}")
        script.unlink(missing_ok=True)
        return False


def install_ollama() -> bool:
    if IS_WIN:
        return _install_ollama_windows()
    return _install_ollama_mac() if IS_MAC else _install_ollama_linux()


def _show_pip_failure(stderr: str | None) -> None:
    error("pip install failed. Error output:")
    stderr_text = (stderr or "").strip()
    if stderr_text:
        for line in stderr_text.splitlines()[-15:]:
            print(f"    {C.DIM}{line}{C.RESET}")
    else:
        print(f"    {C.DIM}(no error output captured — try running with --verbose){C.RESET}")
    if IS_WIN and ("Microsoft Visual C++" in stderr_text or "cl.exe" in stderr_text):
        error("This looks like a missing C++ compiler.")
        info("Install Visual Studio Build Tools:")
        info(f"  {_VS_BUILD_TOOLS}")


def install_python_deps() -> bool:
    """Install flask + faster-whisper deps."""

    if not _check_pip():
        return False

    req = BASE_DIR / "server" / "requirements.txt"
    if not req.exists():
        error(f"{req} is missing — re-extract Yume.")
        return False

    if IS_WIN and not has_build_tools():
        warn("C++ build tools not detected.")
        info("If the install fails, you may need Visual Studio Build Tools:")
        info(f"  {_VS_BUILD_TOOLS}")

    info("Installing Whisper server Python packages...")
    try:
        r = _run(
            [sys.executable, "-m", "pip", "install", "-r", str(req), "-q", "--no-warn-script-location"], timeout=600
        )
        if r.returncode != 0:
            _show_pip_failure(r.stderr)
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
    from yume.servers import LLAMA_SERVER_DEPS

    if not _check_pip() or not ensure_build_tools():
        return False
    if not (install_gpu_build(detect_gpu()) or install_cpu_build()):
        return False
    info("Installing server dependencies (uvicorn, fastapi)...")
    try:
        r = _run(
            [sys.executable, "-m", "pip", "install", *LLAMA_SERVER_DEPS, "-q", "--no-warn-script-location"], timeout=300
        )
        if r.returncode == 0:
            success("Server dependencies installed!")
        else:
            warn("Some server deps may have failed — check manually")
    except Exception as e:
        warn(f"Server deps install issue: {e}")
    return True


def _ensure_ollama_serving(ollama: str) -> bool:
    """`ollama pull` talks to the Ollama server: start one (detached) when none answers."""
    from yume.network import check_server

    if check_server("127.0.0.1", DEFAULT_OLLAMA_PORT, HEALTH_PATH_OLLAMA)["up"]:
        return True
    info("Starting Ollama...")
    try:
        subprocess.Popen([ollama, "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # nosec B603
    except OSError as e:
        error(f"Could not start Ollama: {e}")
        return False
    time.sleep(3)
    return True


def pull_ollama_model(name: str) -> bool:
    ollama = find_ollama()
    if not ollama:
        error("Ollama is not installed — get it from https://ollama.com/download, then try again.")
        return False
    if not _ensure_ollama_serving(ollama):
        return False
    info(f"Pulling {name} (several GB on first download)...\n")
    # Not captured: the user needs ollama's progress bar for a multi-GB pull
    p = _run([ollama, "pull", name], timeout=3600, capture_output=False)
    if p.returncode == 0:
        success(f"{name} ready!")
        return True
    error("Timed out" if p.returncode == 124 else f"Failed (code {p.returncode})")
    return False
