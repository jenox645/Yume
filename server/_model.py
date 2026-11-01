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


_CPU_SETTING = "       python pocket_yume.py settings   → Whisper settings → device 'cpu'"
_CPU_FLAGS = "    3. Or restart with: --device cpu --compute-type int8"
_NO_VRAM = [
    "  CAUSE: Your GPU doesn't have enough VRAM for this model.",
    "",
    "  SOLUTIONS (pick one):",
    "    1. Use a smaller model:",
    "       python pocket_yume.py settings   → Whisper settings → a smaller model ('small', 'base')",
    "    2. Use CPU instead (slower but works):",
    _CPU_SETTING,
    _CPU_FLAGS,
]
# (any of these in the error, what to print): the console's longer version of _ADVICE.
# {missing} is the quoted name in the error.
_LOAD_HELP = [
    (("out of memory", "oom"), _NO_VRAM),
    (
        ("cublas", "cudnn", "cudart"),
        [
            "  CAUSE: CUDA libraries are missing or incomplete.",
            "",
            "  SOLUTIONS (pick one):",
            "    1. Install CUDA Toolkit: https://developer.nvidia.com/cuda-toolkit",
            "    2. Or switch to CPU mode (no CUDA needed):",
            _CPU_SETTING,
            _CPU_FLAGS,
        ],
    ),
    (
        ("no module", "modulenotfound"),
        [
            "  CAUSE: Missing Python package: {missing}",
            "",
            "  SOLUTION: Run the setup wizard to install dependencies:",
            "    python pocket_yume.py setup",
            "    Or manually: pip install {missing}",
        ],
    ),
    (
        (_state.ERR_NO_SUCH_FILE, "filenotfound"),
        [
            "  CAUSE: Model files not found on disk.",
            "",
            "  SOLUTIONS:",
            "    1. The model will auto-download on first run (needs internet)",
            "    2. Check your internet connection and try again",
            "    3. Or choose a different model: python pocket_yume.py settings",
        ],
    ),
    (
        ("permission", "access"),
        [
            "  CAUSE: Permission denied — can't access model files or GPU.",
            "",
            "  SOLUTIONS:",
            "    1. Try running as Administrator (Windows) or with sudo (Linux/macOS)",
            "    2. Check that the model directory is not read-only",
        ],
    ),
]
_GENERAL_HELP = [
    "  GENERAL SOLUTIONS:",
    "    1. Switch to CPU:  --device cpu --compute-type int8",
    "    2. Use smaller model:  --model small  or  --model tiny",
    "    3. Re-run setup:  python pocket_yume.py setup",
    "    4. Check logs:  logs/whisper_server.log",
]


def _load_help(err_msg):
    if "cuda" in err_msg and "memory" in err_msg:
        return _NO_VRAM
    return next((lines for words, lines in _LOAD_HELP if any(w in err_msg for w in words)), _GENERAL_HELP)


def print_model_load_error(e):
    message = str(e)
    missing = message.split("'")[1] if "'" in message else "unknown"
    print("")
    print("=" * 70)
    print(f"  FAILED TO LOAD MODEL: {e}")
    print("=" * 70)
    print("")
    for line in _load_help(message.lower()):
        print(line.format(missing=missing))
    print("")
    print("  Need help? Run: python pocket_yume.py health")
    print("")
