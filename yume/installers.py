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
from pathlib import Path

from yume.constants import BASE_DIR, TOOLS_DIR, IS_WIN, IS_MAC, IS_LIN, EXE, HEALTH_PATH_OLLAMA, DOWNLOAD_URLS
from yume.ui import C, error, info, success, warn
from yume.utils import _run, find_ollama, find_tool

_log = logging.getLogger("pocket_yume")

UNIX_EXEC_MODE = 0o750

_pip_venv_warned = False

# Download URLs are supplied by pocket_yume at runtime to avoid duplication

# Published checksum files next to "latest" release assets. Verified when
# available; a mismatch aborts the install. (Same origin as the binary, so this
# guards against corrupted/truncated downloads, not a compromised release.)
_YTDLP_SUMS = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/SHA2-256SUMS"
_BTBN_SUMS = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/checksums.sha256"


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
    zf.extractall(target_dir)  # nosec B202 — every member validated above


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


BGUTIL_VERSION = "1.3.1"


def install_deno() -> bool:
    """Deno, the YouTube PO token plugin for yt-dlp, and the plugin's token
    server. Only Deno itself is required; the rest warns when it fails."""
    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    if not _install_deno_exe():
        return False
    _install_pot_plugin()
    _setup_bgutil_server()
    return True


def _install_deno_exe() -> bool:
    zp = TOOLS_DIR / "deno.zip"
    if not _download_verified("deno", zp, "Deno"):
        return False
    try:
        with zipfile.ZipFile(zp) as zf:
            _safe_extractall(zf, TOOLS_DIR)  # nosec B202 — zip-slip checked inside
        zp.unlink(missing_ok=True)
        de = TOOLS_DIR / f"deno{EXE}"
        if de.exists() and not IS_WIN:
            os.chmod(de, UNIX_EXEC_MODE)
        success("Deno extracted")
        return True
    except Exception as e:
        error(f"Failed: {e}")
        return False


def _install_pot_plugin() -> None:
    info("Installing YouTube PO token plugin (bgutil-ytdlp-pot-provider) + yt-dlp (pip)...")
    info(f"{C.DIM}This plugin uses Deno to solve YouTube's bot-detection challenges.{C.RESET}")
    info(f"{C.DIM}yt-dlp is left unpinned on purpose: YouTube changes break old versions within weeks.{C.RESET}")
    try:
        r = _run(
            [
                sys.executable, "-m", "pip", "install", "-q", "--no-warn-script-location", "-U",
                "yt-dlp", f"bgutil-ytdlp-pot-provider=={BGUTIL_VERSION}",
            ],
            timeout=180,
        )  # fmt: skip
        if r.returncode == 0:
            success("PO token plugin installed")
        else:
            warn("PO token plugin install failed — YouTube may require sign-in")
    except Exception as e:
        warn(f"PO token plugin install failed: {e}")


def _setup_bgutil_server() -> None:
    """The plugin's token server (run by Deno), from its release source."""
    from yume.network import download_file

    info("Setting up bgutil PO token server...")
    bgutil_dir = TOOLS_DIR / "bgutil-ytdlp-pot-provider"
    main_ts = bgutil_dir / "server" / "src" / "main.ts"
    if main_ts.exists():
        success("bgutil server already set up")
        return
    zip_url = f"https://github.com/Brainicism/bgutil-ytdlp-pot-provider/archive/refs/tags/{BGUTIL_VERSION}.zip"
    zip_path = TOOLS_DIR / "bgutil.zip"
    if not download_file(zip_url, zip_path, "bgutil server"):
        warn("bgutil server download failed")
        return
    try:
        with zipfile.ZipFile(zip_path) as zf:
            _safe_extractall(zf, TOOLS_DIR)  # nosec B202 — zip-slip checked inside
        zip_path.unlink(missing_ok=True)
        extracted = TOOLS_DIR / f"bgutil-ytdlp-pot-provider-{BGUTIL_VERSION}"
        if extracted.exists():
            if bgutil_dir.exists():
                shutil.rmtree(bgutil_dir)
            extracted.rename(bgutil_dir)
        success("bgutil server extracted")
    except Exception as e:
        error(f"Extract failed: {e}")
        zip_path.unlink(missing_ok=True)
        return
    if main_ts.exists():
        _deno_install_deps(bgutil_dir / "server")
    else:
        warn("main.ts not found after extraction")


def _deno_install_deps(server_dir: Path) -> None:
    deno = find_tool("deno") or "deno"
    info("Installing bgutil server dependencies (deno install)...")
    try:
        # The lockfile first; without it if the lockfile no longer resolves
        r = _run([deno, "install", "--allow-scripts=npm:canvas", "--frozen"], cwd=str(server_dir), timeout=180)
        if r.returncode != 0:
            r = _run([deno, "install", "--allow-scripts=npm:canvas"], cwd=str(server_dir), timeout=180)
        if r.returncode == 0:
            success("bgutil server dependencies installed")
        else:
            warn("deno install failed — server may not start")
    except Exception as e:
        warn(f"deno install failed: {e}")


def install_ollama() -> bool:
    from yume.network import check_server, download_file

    if IS_WIN:
        TOOLS_DIR.mkdir(parents=True, exist_ok=True)
        inst = TOOLS_DIR / "OllamaSetup.exe"
        if not download_file(DOWNLOAD_URLS.get("ollama", ""), inst, "Ollama installer"):
            return False
        print(f"\n  {C.YELLOW}The Ollama installer will now open. Follow the prompts.{C.RESET}\n")
        from yume.ui import pause

        pause("Press Enter to launch installer...")
        try:
            subprocess.Popen([str(inst)])
            info("Waiting for Ollama...")
            for _ in range(90):
                time.sleep(2)
                if find_ollama() or check_server("127.0.0.1", 11434, "/api/tags")["up"]:
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
        if not download_file(DOWNLOAD_URLS.get("ollama", ""), zp, "Ollama"):
            return False
        info("Extract and move to Applications manually.")
        return True
    else:
        info("Installing Ollama via official script...")
        script = TOOLS_DIR / "ollama_install.sh"
        TOOLS_DIR.mkdir(parents=True, exist_ok=True)
        try:
            from yume.network import download_file

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


def install_python_deps() -> bool:
    """Install flask + faster-whisper deps."""

    if not _check_pip():
        return False

    req = BASE_DIR / "server" / "requirements.txt"
    if not req.exists():
        error(f"{req} is missing — re-extract Yume.")
        return False

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


def _pip_with_progress(cmd: list, label: str, timeout: int = 600, env: dict | None = None) -> bool:
    """Run a long pip command with an elapsed-time ticker; show pip's last lines on failure."""
    info(f"{label}...")
    info(f"{C.DIM}This may take several minutes for large downloads. Please wait.{C.RESET}")
    t0 = time.time()
    stop = threading.Event()

    def _ticker():
        while not stop.is_set():
            m, s = divmod(int(time.time() - t0), 60)
            sys.stdout.write(f"\r  {C.CYAN}...{C.RESET} {m}m{s:02d}s elapsed   ")
            sys.stdout.flush()
            stop.wait(5)

    threading.Thread(target=_ticker, daemon=True).start()
    try:
        r = _run(cmd, timeout=timeout, **({"env": env} if env is not None else {}))
    except subprocess.TimeoutExpired:
        error(f"{label} — timed out after {timeout // 60} minutes")
        return False
    except Exception as e:
        error(f"{label} — error: {e}")
        return False
    finally:
        stop.set()
    m, s = divmod(int(time.time() - t0), 60)
    sys.stdout.write(f"\r{' ' * 40}\r")
    sys.stdout.flush()
    if r.returncode == 0:
        success(f"{label} — done in {m}m{s:02d}s")
        return True
    error(f"{label} — failed after {m}m{s:02d}s")
    for line in (r.stderr or "").strip().splitlines()[-10:]:
        print(f"    {C.DIM}{line}{C.RESET}")
    return False


_LLAMA_WHEELS = "https://abetlen.github.io/llama-cpp-python/whl"


def _pip_llama(label: str, *args: str, cmake: str | None = None, timeout: int = 600) -> bool:
    """pip install llama-cpp-python [args]; with `cmake`, built from source with those CMAKE_ARGS."""
    env = {**os.environ, "CMAKE_ARGS": cmake} if cmake else None
    cmd = [sys.executable, "-m", "pip", "install", "llama-cpp-python", *args]
    return _pip_with_progress(cmd, label, timeout=timeout, env=env)


def _llama_cuda_works() -> bool:
    r = _run(
        [sys.executable, "-c", "from llama_cpp import llama_cpp as l; print(hasattr(l, 'ggml_backend_cuda_reg'))"],
        timeout=15,
    )
    return r.returncode == 0 and r.stdout.strip() == "True"


def _ensure_build_tools() -> bool:
    """Linux/macOS build llama-cpp-python from source: ninja + cmake are needed."""
    if IS_WIN or _has_build_tools():
        return True
    info("Build tools (ninja, cmake) needed to compile llama-cpp-python...")
    if _install_build_tools():
        return True
    error("Cannot install build tools. Please install manually:")
    if IS_LIN:
        hint = {"dnf": "sudo dnf install ninja-build cmake gcc-c++", "apt": "sudo apt install ninja-build cmake g++"}
        print(f"    {hint.get(_detect_package_manager(), 'Install: ninja-build cmake g++ (or gcc-c++)')}")
    elif IS_MAC:
        print("    brew install ninja cmake")
    return False


def _install_llama_gpu(gpu: dict) -> bool:
    """A GPU build of llama-cpp-python; False = fall back to the CPU build."""
    if gpu["has_nvidia"] and IS_WIN:
        # pip --extra-index-url silently falls back to PyPI's CPU wheel when the
        # CUDA index has no wheel for this Python, so check CUDA after installing.
        # --only-binary: otherwise pip compiles PyPI's source for ~10 minutes
        # into a CPU-only build before we notice. --force-reinstall replaces one.
        _pip_llama(
            "Installing llama-cpp-python (CUDA prebuilt)",
            "--force-reinstall", "--only-binary=llama-cpp-python", "--extra-index-url", f"{_LLAMA_WHEELS}/cu124",
            "-q", "--no-warn-script-location",
        )  # fmt: skip
        if _llama_cuda_works():
            return True
        pyver = f"{sys.version_info.major}.{sys.version_info.minor}"
        warn(f"Prebuilt CUDA wheel not available for Python {pyver} — trying source build.")
        info(f"{C.DIM}Requires CUDA Toolkit (nvidia.com) + Visual Studio C++ tools.{C.RESET}")
        ok = _pip_llama(
            f"Building llama-cpp-python (CUDA from source, Python {pyver})",
            "--force-reinstall", "--no-cache-dir", "-q", cmake="-DGGML_CUDA=on", timeout=900,
        )  # fmt: skip
        if not ok:
            warn("CUDA install failed, falling back to CPU version...")
        return ok
    if gpu["has_nvidia"]:
        ok = _pip_llama(
            "Building llama-cpp-python (CUDA from source)",
            "--force-reinstall",
            "--no-cache-dir",
            "-q",
            cmake="-DGGML_CUDA=on",
        )
        if not ok:
            warn("CUDA build failed, trying CPU...")
        return ok
    if not gpu["has_amd"]:
        return False
    info("AMD Radeon GPU detected. Installing llama-cpp-python with ROCm...")
    if IS_WIN:
        warn("ROCm is only supported on Linux for llama-cpp-python.")
        info("Falling back to CPU mode on Windows (AMD DirectML not yet supported).")
        return False
    ok = _pip_llama(
        "Building llama-cpp-python (ROCm/HIP from source)",
        "--force-reinstall",
        "--no-cache-dir",
        cmake="-DGGML_HIP=on",
        timeout=900,
    )
    if not ok:
        warn("ROCm build failed. Check that ROCm is installed.")
        info("Falling back to CPU...")
    return ok


def _install_llama_cpu() -> bool:
    ok = _pip_llama(
        "Installing llama-cpp-python (prebuilt CPU)",
        "--only-binary=llama-cpp-python", "--extra-index-url", f"{_LLAMA_WHEELS}/cpu", "-q", "--no-warn-script-location",
        timeout=300,
    )  # fmt: skip
    if ok:
        return True
    warn("Prebuilt wheel not available, building from source...")
    return _pip_llama("Building llama-cpp-python (CPU from source)", "--no-warn-script-location", timeout=900)


def install_llamacpp_python() -> bool:
    """Install llama-cpp-python with GPU support if NVIDIA detected."""
    from yume.hardware import detect_gpu
    from yume.servers import LLAMA_SERVER_DEPS

    if not _check_pip() or not _ensure_build_tools():
        return False
    if not (_install_llama_gpu(detect_gpu()) or _install_llama_cpu()):
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
    from config import DEFAULT_OLLAMA_PORT
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
