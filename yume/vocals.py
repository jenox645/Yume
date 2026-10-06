"""Vocal isolation from the CLI: status, install, on/off.

The server separates a song's vocals from the music before Whisper hears them
(server/_separate.py, Demucs htdemucs): fewer transcription errors on
songs (14.0% -> 11.7% of characters wrong on one measured song). It needs PyTorch with CUDA (~3 GB) and the demucs package in
the Python that runs Yume; without them Yume transcribes the mix as before.
"""

from __future__ import annotations

import shutil
import sys

from yume.ui import C, ask_arrow, ask_yn, error, header, info, pause, success, warn
from yume.utils import BASE_DIR, _run

TORCH = ["torch==2.11.0", "torchaudio==2.11.0"]  # tested with demucs 4.0.1, Python 3.12-3.14
DEMUCS = "demucs==4.0.1"
DOWNLOAD_GB = 3.5  # torch + CUDA libraries + the htdemucs weights
# Newest first: a build newer than the driver's CUDA does not start
_TORCH_INDEXES = (((12, 8), "cu128"), ((12, 6), "cu126"))
_CHECK = "import torch, demucs; print(torch.__version__, torch.cuda.is_available())"


def torch_index(cuda: tuple[int, int] | None) -> str | None:
    """PyTorch wheel index for the driver's CUDA version (None: too old / no NVIDIA)."""
    if cuda is None:
        return None
    for version, tag in _TORCH_INDEXES:
        if cuda >= version:
            return f"https://download.pytorch.org/whl/{tag}"
    return None


def status() -> tuple[bool, str]:
    """(usable, detail). Imports torch in a child process: it takes seconds."""
    try:
        r = _run([sys.executable, "-c", _CHECK], timeout=180)
    except Exception as e:
        return False, f"check failed: {e}"
    out = (r.stdout or "").strip().split()
    if r.returncode != 0 or len(out) != 2:
        return False, "not installed"
    if out[1] != "True":
        return False, f"PyTorch {out[0]} has no CUDA — reinstall from Tools → Vocal isolation"
    return True, f"htdemucs, PyTorch {out[0]}"


def health_row(cfg: dict) -> tuple[str, bool, str]:
    if not cfg.get("vocal_isolation", True):
        return ("Vocal isolation", True, "off (Tools → Vocal isolation)")
    ok, detail = status()
    if ok:
        return ("Vocal isolation", True, f"on — {detail}")
    # Optional: never a failure, but say what it would bring
    return ("Vocal isolation", True, f"optional, {detail} — better lyrics on songs (Tools → Vocal isolation)")


def install() -> bool:
    from yume import llama_server
    from yume.hardware import detect_gpu

    if not detect_gpu().get("has_nvidia"):
        error("Vocal isolation needs an NVIDIA GPU (on a CPU it takes ~12 s per 25 s of audio).")
        return False
    cuda = llama_server.driver_cuda_version()
    index = torch_index(cuda)
    if not index:
        error(f"Your NVIDIA driver supports CUDA {cuda[0]}.{cuda[1] if cuda else '?'}; PyTorch needs 12.6 or newer.")
        info("Update the driver from https://www.nvidia.com/Download/index.aspx and try again.")
        return False
    free_gb = shutil.disk_usage(BASE_DIR).free / 2**30
    if free_gb < DOWNLOAD_GB + 2:
        warn(f"Only {free_gb:.1f} GB free; the install needs about {DOWNLOAD_GB:.0f} GB.")
        if not ask_yn("Install anyway?", False):
            return False

    pip = [sys.executable, "-m", "pip", "install", "-q", "--no-warn-script-location"]
    info(f"Installing PyTorch with CUDA ({index.rsplit('/', 1)[-1]}, ~3 GB — this takes a few minutes)...")
    r = _run([*pip, *TORCH, "--index-url", index], timeout=3600)
    if r.returncode != 0:
        return _failed(r)
    info("Installing demucs...")
    r = _run([*pip, DEMUCS], timeout=900)
    if r.returncode != 0:
        return _failed(r)
    info("Downloading the htdemucs model (~80 MB)...")
    r = _run([sys.executable, "-c", "from demucs.pretrained import get_model; get_model('htdemucs')"], timeout=900)
    if r.returncode != 0:
        return _failed(r)
    ok, detail = status()
    if ok:
        success(f"Vocal isolation installed ({detail}). Restart Yume to use it.")
    else:
        error(f"Installed, but not usable: {detail}")
    return ok


def _failed(r) -> bool:
    error("Install failed:")
    for line in (r.stderr or r.stdout or "").strip().splitlines()[-12:]:
        print(f"    {C.DIM}{line}{C.RESET}")
    return False


def menu(cfg: dict) -> None:
    from config import save_config

    while True:
        header("Vocal Isolation")
        info("Separates the singer from the music before Whisper listens (Demucs).")
        info("Measured on a song against its lyrics: 14.0% -> 11.7% of characters wrong.")
        info(f"{C.DIM}~8 s per 4-minute song on an NVIDIA GPU; videos over 15 min are not separated.{C.RESET}")
        ok, detail = status()
        on = cfg.get("vocal_isolation", True)
        print()
        info(f"Installed: {C.GREEN + '✓ ' + detail if ok else C.YELLOW + '– ' + detail}{C.RESET}")
        info(f"Setting:   {C.GREEN + '✓ on' if on else C.DIM + '– off'}{C.RESET}")
        ch = ask_arrow(
            "Vocal isolation:",
            [
                ("Reinstall" if ok else "Install", f"PyTorch with CUDA + demucs (~{DOWNLOAD_GB:.0f} GB download)"),
                ("Turn off" if on else "Turn on", "Takes effect for the next video"),
                ("Back", None),
            ],
            default=2,
        )
        if ch == 0:
            install()
            pause()
        elif ch == 1:
            cfg["vocal_isolation"] = not on
            save_config(cfg)
            success(f"Vocal isolation {'on' if cfg['vocal_isolation'] else 'off'}.")
        else:
            return
