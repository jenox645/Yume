"""llama-cpp-python: the pip builds per GPU and OS, and the tools they compile with.

llama.cpp's prebuilt server (yume/llama_server.py) is the engine Yume prefers;
this is the fallback, and only runs on the GPU when compiled for it.
"""

from __future__ import annotations

import os
import shutil
import sys
import threading
import time

from yume.constants import IS_LIN, IS_MAC, IS_WIN
from yume.ui import C, error, info, success, warn
from yume.utils import _run


def detect_package_manager() -> str | None:
    for cmd, name in [("dnf", "dnf"), ("apt-get", "apt"), ("brew", "brew"), ("pacman", "pacman"), ("zypper", "zypper")]:
        if shutil.which(cmd):
            return name
    return None


def has_build_tools() -> bool:
    return bool(shutil.which("ninja") or shutil.which("ninja-build")) and bool(shutil.which("cmake"))


def _install_build_tools() -> bool:
    pm = detect_package_manager()
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
    try:
        r = _run(cmds[pm], timeout=300)
        if r.returncode == 0:
            success("Build tools installed!")
            return True
        warn(f"Package manager returned code {r.returncode}")
        info("Trying pip install ninja cmake as fallback...")
    except Exception as e:
        warn(f"System install failed: {e}")
    try:
        _run([sys.executable, "-m", "pip", "install", "ninja", "cmake", "-q"], timeout=120)
        return True
    except Exception:
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
    if r.returncode == 124:  # _run's code for a timeout
        error(f"{label} — timed out after {timeout // 60} minutes")
        return False
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


def ensure_build_tools() -> bool:
    """Linux/macOS build llama-cpp-python from source: ninja + cmake are needed."""
    if IS_WIN or has_build_tools():
        return True
    info("Build tools (ninja, cmake) needed to compile llama-cpp-python...")
    if _install_build_tools():
        return True
    error("Cannot install build tools. Please install manually:")
    if IS_LIN:
        hint = {"dnf": "sudo dnf install ninja-build cmake gcc-c++", "apt": "sudo apt install ninja-build cmake g++"}
        print(f"    {hint.get(detect_package_manager(), 'Install: ninja-build cmake g++ (or gcc-c++)')}")
    elif IS_MAC:
        print("    brew install ninja cmake")
    return False


def install_gpu_build(gpu: dict) -> bool:
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


def install_cpu_build() -> bool:
    ok = _pip_llama(
        "Installing llama-cpp-python (prebuilt CPU)",
        "--only-binary=llama-cpp-python", "--extra-index-url", f"{_LLAMA_WHEELS}/cpu", "-q", "--no-warn-script-location",
        timeout=300,
    )  # fmt: skip
    if ok:
        return True
    warn("Prebuilt wheel not available, building from source...")
    return _pip_llama("Building llama-cpp-python (CPU from source)", "--no-warn-script-location", timeout=900)
