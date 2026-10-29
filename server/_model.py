"""The Whisper model: loading at startup (in a background thread, with a CUDA
warm-up that falls back to the CPU when cuBLAS/cuDNN are missing), loading
another one for a hot swap, and advice when loading fails.
"""

import os
import sys

from faster_whisper import WhisperModel

import _state


def load_whisper(name):
    """A WhisperModel on the server's device (for a hot swap)."""
    return WhisperModel(name, device=_state.device, compute_type=_state.compute_type)


def set_low_priority():
    """Lower this thread's scheduling priority to avoid PC stutter during model load."""
    try:
        if sys.platform == "win32":
            import ctypes

            THREAD_PRIORITY_BELOW_NORMAL = -1
            ctypes.windll.kernel32.SetThreadPriority(
                ctypes.windll.kernel32.GetCurrentThread(), THREAD_PRIORITY_BELOW_NORMAL
            )
        else:
            os.nice(10)
    except Exception:
        pass


def load_model(args):
    """Load the Whisper model; handle errors with actionable messages."""
    set_low_priority()
    try:
        _state.model = WhisperModel(_state.model_name, device=_state.device, compute_type=_state.compute_type)

        # On CUDA a 1 s warm-up is cheap and is the only way to find out NOW that
        # cuBLAS/cuDNN are missing (the model itself loads fine without them).
        if not (args.prewarm or _state.device == "cuda"):
            print("  Prewarm:          skipped (use --prewarm to enable)")
        else:
            import numpy as np

            _dummy = np.zeros(16000, dtype=np.float32)
            try:
                list(_state.model.transcribe(_dummy, language="en"))
                print("  Prewarm:          done (CUDA kernels compiled)")
            except Exception as pw_err:
                pw_msg = str(pw_err)
                cuda_lib_missing = any(
                    lib in pw_msg.lower()
                    for lib in ["cublas", "cudnn", "cudart", "cufft", "cusolver", "is not found or cannot be loaded"]
                )
                if cuda_lib_missing and _state.device == "cuda":
                    print(f"  Prewarm:          CUDA library missing: {pw_msg[:80]}")
                    print("")
                    print("  WARNING: CUDA libraries are incomplete.")
                    print("  The model loaded but inference requires cuBLAS/cuDNN.")
                    print("  Falling back to CPU mode automatically.")
                    print("")
                    try:
                        # Assign, never `del`: other threads read _state.model concurrently
                        _state.model = None
                        _state.device = "cpu"
                        _state.compute_type = "int8"
                        _state.model = WhisperModel(_state.model_name, device="cpu", compute_type="int8")
                        list(_state.model.transcribe(_dummy, language="en"))
                        print("  Prewarm:          done (CPU fallback)")
                    except Exception as cpu_err:
                        print(f"  Prewarm:          CPU fallback also failed: {cpu_err}")
                        _state.model = None
                        _state.load_error = f"CUDA libraries missing and CPU fallback failed: {cpu_err}"
                        return
                else:
                    print(f"  Prewarm:          skipped ({pw_err})")

        print("=" * 70)
        print("  MODEL LOADED -- Server ready")
        print(f"  Listening on http://localhost:{args.port}")  # noqa: S5332 — display only, loopback
        print("=" * 70)
        print("")

    except Exception as e:
        print_model_load_error(e)
        # sys.exit() here would only end this loader thread and leave /health
        # reporting "loading" forever. Publish the failure instead: /health then
        # returns status "error" with the message, which the extension and the
        # CLI launcher both surface to the user.
        _state.load_error = with_advice(str(e) or type(e).__name__)


_WHISPER_SETTINGS = "python pocket_yume.py settings → Whisper settings"
# (any of these in the error, what to do): one line, shown in the overlay and the launcher
_ADVICE = [
    (("out of memory", "oom"), f"Not enough GPU memory: pick a smaller model or the CPU ({_WHISPER_SETTINGS})."),
    (
        ("cublas", "cudnn", "cudart"),
        f"CUDA libraries are missing: install the CUDA Toolkit, or pick the CPU ({_WHISPER_SETTINGS}).",
    ),
    (("no module", "modulenotfound"), "A Python package is missing: run python pocket_yume.py setup."),
    (
        ("hub", "connection", "resolve", "max retries", "offline", _state.ERR_NO_SUCH_FILE, "filenotfound"),
        "The model could not be downloaded (the first start needs the internet), or its files are missing: "
        f"check the connection, or pick another model ({_WHISPER_SETTINGS}).",
    ),
]


def with_advice(message):
    """The load error with what to do about it, when the cause is known."""
    low = message.lower()
    hint = next((h for words, h in _ADVICE if any(w in low for w in words)), "")
    return f"{message} — {hint}" if hint else message


def print_model_load_error(e):
    err_msg = str(e).lower()
    print("")
    print("=" * 70)
    print(f"  FAILED TO LOAD MODEL: {e}")
    print("=" * 70)

    if "out of memory" in err_msg or "oom" in err_msg or ("cuda" in err_msg and "memory" in err_msg):
        print("")
        print("  CAUSE: Your GPU doesn't have enough VRAM for this model.")
        print("")
        print("  SOLUTIONS (pick one):")
        print("    1. Use a smaller model:")
        print("       python pocket_yume.py settings   → Whisper settings → a smaller model ('small', 'base')")
        print("    2. Use CPU instead (slower but works):")
        print("       python pocket_yume.py settings   → Whisper settings → device 'cpu'")
        print("    3. Or restart with: --device cpu --compute-type int8")
    elif "cublas" in err_msg or "cudnn" in err_msg or "cudart" in err_msg:
        print("")
        print("  CAUSE: CUDA libraries are missing or incomplete.")
        print("")
        print("  SOLUTIONS (pick one):")
        print("    1. Install CUDA Toolkit: https://developer.nvidia.com/cuda-toolkit")
        print("    2. Or switch to CPU mode (no CUDA needed):")
        print("       python pocket_yume.py settings   → Whisper settings → device 'cpu'")
        print("    3. Or restart with: --device cpu --compute-type int8")
    elif "no module" in err_msg or "modulenotfound" in err_msg:
        missing = str(e).split("'")[1] if "'" in str(e) else "unknown"
        print("")
        print(f"  CAUSE: Missing Python package: {missing}")
        print("")
        print("  SOLUTION: Run the setup wizard to install dependencies:")
        print("    python pocket_yume.py setup")
        print(f"    Or manually: pip install {missing}")
    elif _state.ERR_NO_SUCH_FILE in err_msg or "filenotfound" in err_msg:
        print("")
        print("  CAUSE: Model files not found on disk.")
        print("")
        print("  SOLUTIONS:")
        print("    1. The model will auto-download on first run (needs internet)")
        print("    2. Check your internet connection and try again")
        print("    3. Or choose a different model: python pocket_yume.py settings")
    elif "permission" in err_msg or "access" in err_msg:
        print("")
        print("  CAUSE: Permission denied — can't access model files or GPU.")
        print("")
        print("  SOLUTIONS:")
        print("    1. Try running as Administrator (Windows) or with sudo (Linux/macOS)")
        print("    2. Check that the model directory is not read-only")
    else:
        print("")
        print("  GENERAL SOLUTIONS:")
        print("    1. Switch to CPU:  --device cpu --compute-type int8")
        print("    2. Use smaller model:  --model small  or  --model tiny")
        print("    3. Re-run setup:  python pocket_yume.py setup")
        print("    4. Check logs:  logs/whisper_server.log")

    print("")
    print("  Need help? Run: python pocket_yume.py health")
    print("")
