"""The real Yume server (routes, security checks, job pipeline, cache, filter,
romanization) with only the outside world faked: no Whisper model, no LLM, no
download. The browser test (extension.e2e.mjs) runs the real extension against it.

Fake Whisper writes one line every 4 s, at known times: "第{k}行です" over
[4k+1, 4k+3.5) s, so the test knows which line belongs on screen at any time.
The fake LLM translates it as "Line {k}". Every request is logged as JSON on
stdout (`{"req": ...}`) for the test to check what the extension sent.

usage: python fake_server.py <port>   (prints {"ready": port} once listening)
"""

import json
import re
import sys
import tempfile
import types
import wave
from pathlib import Path
from unittest.mock import patch

import numpy as np

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "server"))
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 5091
sys.argv = sys.argv[:1]  # the server modules read their own options from argv
try:
    import faster_whisper  # noqa: F401
except ImportError:  # never used here: CI installs the server's packages without it
    sys.modules["faster_whisper"] = types.SimpleNamespace(WhisperModel=None)

import _api  # noqa: E402
import _audio  # noqa: E402
import _download  # noqa: E402
import _jobs  # noqa: E402
import _settings  # noqa: E402
import _state  # noqa: E402
import _store  # noqa: E402
import _stream  # noqa: E402
import _ytdlp  # noqa: E402

SR = 16000
DURATION_S = 120


def line_at(k):
    return {"start": 4.0 * k + 1, "end": 4.0 * k + 3.5, "text": f"第{k}行です", "confidence": -0.2}


def fake_transcribe(audio, language, offset=0.0, is_first_region=False):
    end = offset + len(audio) / SR
    return [line_at(k) for k in range(int(DURATION_S / 4)) if offset <= 4 * k + 1 < end]


class FakeTranslator:
    def model_id(self):
        return "fake:e2e"

    def health(self):
        return {"up": True, "backend": "fake-llm", "address": "127.0.0.1:0"}

    def translate_batch(self, texts, src, tgt, title="", context=None, readings=None):
        return [re.sub(r"第(\d+)行です", r"Line \1", t) for t in texts]

    def romanize_batch(self, texts, lang):
        return ["roma"] * len(texts)


def audio_file(folder):
    """Tones with a short gap every 4 s: quiet points to cut regions at."""
    t = np.arange(DURATION_S * SR) / SR
    sig = 0.2 * np.sin(2 * np.pi * 220 * t) * ((t % 4) > 0.4)
    path = Path(folder) / "audio.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((sig * 32767).astype(np.int16).tobytes())
    return str(path)


def main():
    tmp = tempfile.mkdtemp(prefix="yume_e2e_")
    wav = audio_file(tmp)
    _state.model = object()  # "loaded": /health says ready
    _state.model_name, _state.device, _state.compute_type = "fake-whisper", "cpu", "int8"
    _state.store = _store.Store(Path(tmp) / "cache.db")
    _state.user_blacklist = []
    _state.blacklist_file = str(Path(tmp) / "blacklist.json")  # never the user's own list
    _state.vocal_isolation = False
    _state.jobs = _jobs.JobManager(FakeTranslator())

    @_api.app.before_request
    def _log():
        from flask import request

        body = request.get_json(silent=True) if request.method == "POST" else None
        print(json.dumps({"req": request.path, "method": request.method, "body": body}), flush=True)

    patches = [
        patch.object(_jobs, "transcribe_audio", fake_transcribe),
        # Whisper hears Japanese: a job set to another language gets the notice
        patch.object(_jobs, "detect_language", lambda audio: ("ja", 0.99)),
        patch.object(_download, "download_full_audio", lambda *a, **k: (wav, None)),
        patch.object(_stream, "download_audio_segment", lambda *a, **k: None),
        patch.object(_stream, "get_stream_url", lambda *a, **k: None),
        patch.object(_audio, "remove_temp", lambda *a, **k: None),
        patch.object(_ytdlp, "check_ytdlp", lambda: True),
        patch.object(_settings, "reload_config", lambda: None),  # never the user's config
    ]
    for p in patches:
        p.start()
    from werkzeug.serving import make_server

    server = make_server("127.0.0.1", PORT, _api.app, threaded=True)
    print(json.dumps({"ready": PORT}), flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
