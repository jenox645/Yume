"""Whisper calls, with a spy model."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

import server_stubs  # noqa: E402,F401  (flask and faster_whisper, before the server modules)
import _state  # noqa: E402
import _transcribe  # noqa: E402


class _FakeSegment:
    """Minimal Whisper segment stand-in."""

    def __init__(self, start, end, text, avg_logprob=-0.5):
        self.start = start
        self.end = end
        self.text = text
        self.avg_logprob = avg_logprob


class _FakeInfo:
    language = "ja"
    duration = 30.0


class _SpyModel:
    """Records kwargs passed to transcribe() and returns configurable segments."""

    def __init__(self, segments=None):
        self.segments = segments or []
        self.last_kwargs: dict = {}
        self.call_count = 0

    def transcribe(self, audio_path, **kwargs):
        self.last_kwargs = kwargs
        self.call_count += 1
        return iter(self.segments), _FakeInfo()


class TestTranscribeAudio(unittest.TestCase):
    """_transcribe.transcribe_audio(): music-tuned params, raw segments, offsets."""

    AUDIO = np.zeros(16000 * 5, dtype=np.float32)  # 5 s of silence

    def setUp(self):
        _state.model = None
        _state.server_stats["regions_transcribed"] = 0

    def tearDown(self):
        _state.model = None

    def _run(self, segs=None, **kw):
        spy = _SpyModel(segs)
        _state.model = spy
        out = _transcribe.transcribe_audio(self.AUDIO, kw.pop("language", "ja"), **kw)
        return spy, out

    def test_raises_when_model_is_none(self):
        with self.assertRaises(RuntimeError) as ctx:
            _transcribe.transcribe_audio(self.AUDIO, "ja")
        self.assertIn("loading", str(ctx.exception).lower())

    def test_too_short_audio_skips_model(self):
        spy = _SpyModel()
        _state.model = spy
        self.assertEqual(_transcribe.transcribe_audio(np.zeros(100, dtype=np.float32), "ja"), [])
        self.assertEqual(spy.call_count, 0)

    def test_first_region_uses_07_threshold(self):
        spy, _ = self._run(is_first_region=True)
        self.assertEqual(spy.last_kwargs["no_speech_threshold"], 0.7)

    def test_other_regions_use_06_threshold(self):
        spy, _ = self._run()
        self.assertEqual(spy.last_kwargs["no_speech_threshold"], 0.6)

    def test_music_tuned_parameters(self):
        spy, _ = self._run()
        kw = spy.last_kwargs
        self.assertFalse(kw["vad_filter"])  # Silero VAD drops singing
        self.assertFalse(kw["word_timestamps"])  # word mode drops segments
        self.assertFalse(kw["condition_on_previous_text"])
        self.assertEqual(kw["beam_size"], 5)
        self.assertEqual(kw["log_prob_threshold"], -2.0)

    def test_language_forwarded(self):
        spy, _ = self._run(language="zh")
        self.assertEqual(spy.last_kwargs["language"], "zh")
        spy, _ = self._run(language=None)
        self.assertIsNone(spy.last_kwargs["language"])

    def test_offset_applied_to_timestamps(self):
        _, out = self._run([_FakeSegment(2.0, 5.0, "こんにちは")], offset=25.0)
        self.assertAlmostEqual(out[0]["start"], 27.0, places=1)
        self.assertAlmostEqual(out[0]["end"], 30.0, places=1)

    def test_returns_raw_segments_unfiltered(self):
        """Filtering happens when serving (so blacklist edits apply to the cache)."""
        _, out = self._run([_FakeSegment(0.0, 3.0, "Thank you for watching"), _FakeSegment(3.0, 6.0, "  ")])
        self.assertEqual([s["text"] for s in out], ["Thank you for watching"])  # blank dropped, hallucination kept

    def test_confidence_and_stats(self):
        _, out = self._run([_FakeSegment(0.0, 2.0, "テスト", avg_logprob=-0.8)])
        self.assertAlmostEqual(out[0]["confidence"], -0.8, places=1)
        self.assertEqual(_state.server_stats["regions_transcribed"], 1)

    def test_transcribe_lock_released(self):
        self._run()
        self.assertTrue(_state.transcribe_lock.acquire(blocking=False), "transcribe_lock was not released")
        _state.transcribe_lock.release()
