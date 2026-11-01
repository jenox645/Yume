"""Downloaded audio on disk: loaded for Whisper, then deleted."""

import os
import shutil
import subprocess


DECODE_TIMEOUT_S = 900


def remove_temp(path):
    """Delete a downloaded file and its yume_* temp directory."""
    if not path:
        return
    parent = os.path.dirname(path)
    try:
        if parent and os.path.basename(parent).startswith("yume_"):
            shutil.rmtree(parent, ignore_errors=True)
        elif os.path.isfile(path):
            os.unlink(path)
    except OSError:
        pass


def load_audio(path):
    """Load audio as float32 mono 16 kHz numpy array.

    Downloads are usually 16 kHz mono 16-bit WAV (yt-dlp/ffmpeg are told so), which
    the stdlib reads directly. Anything else — the 44.1 kHz stereo downloads for
    vocal isolation — is converted by ffmpeg. Not by faster-whisper's decode_audio:
    it passes PyAV an argument PyAV 19 removed (TypeError on every file).
    """
    import wave

    import numpy as np

    try:
        with wave.open(path, "rb") as wf:
            if wf.getframerate() == 16000 and wf.getnchannels() == 1 and wf.getsampwidth() == 2:
                pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
                return pcm.astype(np.float32) / 32768.0
    except (wave.Error, EOFError, OSError):
        pass
    r = subprocess.run(
        ["ffmpeg", "-v", "error", "-nostdin", "-i", path, "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "-"],
        capture_output=True,
        timeout=DECODE_TIMEOUT_S,
    )  # nosec B603 B607 — fixed argv, path from our own temp dir
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg could not read the audio: {r.stderr.decode(errors='replace').strip()[-200:]}")
    return np.frombuffer(r.stdout, dtype=np.int16).astype(np.float32) / 32768.0
