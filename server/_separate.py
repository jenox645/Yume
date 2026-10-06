"""Vocal isolation: Whisper hears the singer, not the band.

Before transcription, Demucs (htdemucs) separates the vocals from the whole
song. Measured on two Japanese songs against their lyrics (character error
rate on the reading, large-v3-turbo), through the whole pipeline: 14.0% →
11.7% and 8.1% → 7.9% (an offline run of the same songs: 10.9% / 5.5%). It
needs the original stereo 44.1 kHz audio: separating Yume's 16 kHz mono made
things worse (14.7% / 16.2%), so downloads for isolation keep the stereo.

Optional: needs PyTorch with CUDA and the demucs package (CLI: Tools → Vocal
isolation). Without them, or on a CPU (~12 s per 25 s of audio), Yume
transcribes the original mix as before. ~8 s and 0.6 GB VRAM for a 4-minute
song on an RTX 3060. Videos longer than MAX_S are mostly talks, not songs:
they are not separated.
"""

import threading
import wave

import numpy as np

import _state

MODEL = "htdemucs"
MAX_S = 15 * 60
MODEL_SUFFIX = "+" + MODEL  # transcripts of separated vocals are cached apart

_lock = threading.Lock()
_model = None
_checked = False
_reason = ""  # why isolation is unavailable ("" = available)


def available():
    """(True, "") if vocals can be separated on this machine, else (False, why).
    Imports torch only once; never raises."""
    global _checked, _reason
    with _lock:
        if not _checked:
            _checked = True
            try:
                import torch

                import demucs.pretrained  # noqa: F401

                if not torch.cuda.is_available():
                    _reason = f"PyTorch {torch.__version__} has no CUDA (a CPU would take ~12 s per 25 s of audio)"
            except ImportError as e:
                _reason = f"not installed ({e.name or e})"
            except Exception as e:  # a broken torch install must not take the server down
                _reason = f"{type(e).__name__}: {e}"
        return not _reason, _reason


def wanted(duration):
    """Should a job for a video this long be separated?"""
    return bool(_state.vocal_isolation) and 0 < (duration or 0) <= MAX_S and available()[0]


def _get_model():
    global _model
    if _model is None:
        from demucs.pretrained import get_model

        _model = get_model(MODEL).to("cuda").eval()
        print(f"[Yume] Vocal isolation: {MODEL} loaded on CUDA")
    return _model


def read_stereo(path):
    """(2, n) float32 at 44.1 kHz from a 16-bit WAV, or None if it is not one."""
    try:
        with wave.open(path, "rb") as wf:
            if wf.getnchannels() != 2 or wf.getframerate() != 44100 or wf.getsampwidth() != 2:
                return None
            pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
    except (wave.Error, EOFError, OSError):
        return None
    return (pcm.astype(np.float32) / 32768.0).reshape(-1, 2).T.copy()


def isolate(stereo):
    """(2, n) 44.1 kHz mix → 16 kHz mono float32 vocals. Raises on failure."""
    import julius
    import torch
    from demucs.apply import apply_model

    with _lock:  # one separation at a time: the model is shared
        model = _get_model()
        wav = torch.from_numpy(stereo).to("cuda")[None]
        ref, std = wav.mean(), wav.std() + 1e-8
        with torch.inference_mode():
            sources = apply_model(model, (wav - ref) / std, device="cuda", progress=False, overlap=0.25)[0]
        vocals = (sources[model.sources.index("vocals")] * std + ref).mean(0)
        out = julius.resample_frac(vocals, model.samplerate, 16000).float().cpu().numpy()
        del wav, sources, vocals
        torch.cuda.empty_cache()  # leave the VRAM to Whisper and the LLM between songs
    return out
