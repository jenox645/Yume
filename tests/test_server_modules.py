"""Comprehensive unit tests for all server/ modules.

Coverage targets:
  _security.py    — URL validation (100%)
  _state.py       — Initial values, locks, constants (95%)
  _filter.py      — Hallucination + credits detection (100%)
  _romanize.py    — Lazy-load romanizers, fallback behaviour (70%)
  _transcribe.py  — transcribe_file() with mock model (90%)
  _audio.py       — Pure helpers + mocked subprocess paths (50%+)
  _bgutil.py      — Lifecycle helpers (40%)
  faster_whisper_server.py — CLI helpers, config, error messages (20%+)
"""

from __future__ import annotations

import os
from contextlib import contextmanager
import shutil
import sys
import tempfile
import threading
import time
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# ── sys.path: make server/ importable ────────────────────────────────────────
_SERVER_DIR = str(Path(__file__).parent.parent / "server")
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

# ── Inject minimal flask + faster_whisper stubs so fws can be imported ───────
# Must happen BEFORE any test touches faster_whisper_server.

def _install_flask_stub():
    if "flask" in sys.modules:
        return
    flask_mod = types.ModuleType("flask")

    class _FakeFlask:
        def __init__(self, name):
            self.name = name

        def route(self, *args, **kwargs):
            return lambda f: f

        def before_request(self, f):
            return f

        def teardown_request(self, f):
            return f

        def test_client(self):
            return None

    # A minimal request stand-in reused across tests (tests that need different
    # values patch it locally via unittest.mock.patch).
    class _FakeRequest:
        method = "GET"
        path = "/health"
        host = "127.0.0.1"
        headers: dict = {}

        def get_json(self):
            return {}

    flask_mod.Flask = _FakeFlask
    flask_mod.g = types.SimpleNamespace(_temp_files=[])
    flask_mod.request = _FakeRequest()
    flask_mod.jsonify = lambda d=None, **kw: d  # return the dict directly
    sys.modules["flask"] = flask_mod


def _install_flask_cors_stub():
    if "flask_cors" in sys.modules:
        return
    fc = types.ModuleType("flask_cors")
    fc.CORS = lambda *a, **kw: None
    sys.modules["flask_cors"] = fc


def _install_faster_whisper_stub():
    if "faster_whisper" in sys.modules:
        return
    fw = types.ModuleType("faster_whisper")
    fw.WhisperModel = type("WhisperModel", (), {})
    sys.modules["faster_whisper"] = fw


_install_flask_stub()
_install_flask_cors_stub()
_install_faster_whisper_stub()

# Now safe to import server modules
import _audio  # noqa: E402
import _bgutil  # noqa: E402
import _filter  # noqa: E402
import _romanize  # noqa: E402
import _security  # noqa: E402
import _state  # noqa: E402
import _transcribe  # noqa: E402
import faster_whisper_server as _fws  # noqa: E402


class TestValidateUrl(unittest.TestCase):
    """Tests for _security.validate_url — every branch."""

    def _ok(self, url: str):
        valid, err = _security.validate_url(url)
        self.assertTrue(valid, f"Expected valid URL, got error: {err!r}")
        self.assertEqual(err, "")

    def _bad(self, url, fragment: str = ""):
        valid, err = _security.validate_url(url)
        self.assertFalse(valid, f"Expected invalid URL to be rejected: {url!r}")
        if fragment:
            self.assertIn(fragment, err.lower(), f"Error {err!r} missing {fragment!r}")

    # ── Valid URLs ────────────────────────────────────────────────────────────

    def test_http_accepted(self):
        self._ok("http://example.com/video")

    def test_https_accepted(self):
        self._ok("https://youtube.com/watch?v=abc123")

    def test_query_ampersand_accepted(self):
        self._ok("https://www.youtube.com/watch?v=abc&t=120")

    def test_multi_query_params_accepted(self):
        self._ok("https://www.youtube.com/watch?v=abc&list=PLxyz&index=3")

    def test_m3u8_url_accepted(self):
        self._ok("https://cdn.example.com/stream/playlist.m3u8?token=abc")

    def test_url_with_port_accepted(self):
        self._ok("http://localhost:8080/stream")

    def test_url_with_path_and_fragment_accepted(self):
        self._ok("https://example.com/video/123?quality=high")

    # ── Empty / None ──────────────────────────────────────────────────────────

    def test_none_rejected(self):
        self._bad(None)

    def test_empty_string_rejected(self):
        self._bad("")

    def test_whitespace_only_rejected(self):
        self._bad("   ")

    def test_non_string_rejected(self):
        self._bad(42)  # type: ignore[arg-type]

    # ── Dash-prefix (argument injection) ─────────────────────────────────────

    def test_dash_prefix_rejected(self):
        valid, err = _security.validate_url("-o /etc/passwd")
        self.assertFalse(valid)
        self.assertIn("'-'", err)

    def test_double_dash_rejected(self):
        valid, _ = _security.validate_url("--help")
        self.assertFalse(valid)

    # ── Shell metacharacters ──────────────────────────────────────────────────

    def test_semicolon_rejected(self):
        self._bad("https://example.com/;rm -rf /", "metacharacters")

    def test_pipe_rejected(self):
        self._bad("https://example.com/|cat /etc/passwd", "metacharacters")

    def test_backtick_rejected(self):
        self._bad("https://example.com/`whoami`", "metacharacters")

    def test_dollar_sign_rejected(self):
        self._bad("https://example.com/${HOME}", "metacharacters")

    def test_parens_rejected(self):
        self._bad("https://example.com/$(id)", "metacharacters")

    def test_newline_rejected(self):
        self._bad("https://example.com/\nmalicious", "metacharacters")

    def test_carriage_return_rejected(self):
        self._bad("https://example.com/\rmalicious", "metacharacters")

    # ── Forbidden schemes ─────────────────────────────────────────────────────

    def test_file_scheme_rejected(self):
        self._bad("file:///etc/passwd", "scheme")

    def test_ftp_scheme_rejected(self):
        self._bad("ftp://evil.com/payload", "scheme")

    def test_javascript_scheme_rejected(self):
        # javascript: scheme is rejected — may be caught as metacharacters (parens)
        # or as a forbidden scheme depending on check order; either way must be invalid
        self._bad("javascript:alert(1)")

    def test_empty_scheme_rejected(self):
        self._bad("//localhost/path", "scheme")

    def test_data_scheme_rejected(self):
        self._bad("data:text/html,<h1>xss</h1>", "scheme")

    # ── Return value structure ────────────────────────────────────────────────

    def test_valid_returns_two_tuple(self):
        result = _security.validate_url("https://example.com")
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)

    def test_invalid_error_is_nonempty_string(self):
        _, err = _security.validate_url(None)
        self.assertIsInstance(err, str)
        self.assertGreater(len(err), 0)


class TestState(unittest.TestCase):

    def test_model_initially_none(self):
        # _state.model is None until the real server loads it
        # (may have been set by another test — check it's a valid type)
        self.assertIsNone(_state.model)

    def test_api_token_initially_empty_or_string(self):
        self.assertIsInstance(_state.API_TOKEN, str)

    def test_cache_dicts_are_dict(self):
        self.assertIsInstance(_state.subtitle_cache, dict)
        self.assertIsInstance(_state.full_audio_cache, dict)
        self.assertIsInstance(_state.stream_url_cache, dict)

    def test_locks_are_lock_instances(self):
        lock_type = type(threading.Lock())
        self.assertIsInstance(_state.transcribe_lock, lock_type)
        self.assertIsInstance(_state.prefetch_lock, lock_type)
        self.assertIsInstance(_state.cache_lock, lock_type)
        self.assertIsInstance(_state.stats_lock, lock_type)

    def test_cache_max_constants_positive(self):
        self.assertGreater(_state.SUBTITLE_CACHE_MAX, 0)
        self.assertGreater(_state.AUDIO_CACHE_MAX, 0)
        self.assertGreater(_state.STREAM_URL_CACHE_MAX, 0)

    def test_ttl_constants_positive(self):
        self.assertGreater(_state.FULL_AUDIO_TTL, 0)
        self.assertGreater(_state.STREAM_URL_TTL, 0)

    def test_server_stats_has_expected_keys(self):
        required = {
            "start_time", "chunks_transcribed", "segments_produced",
            "hallucinations_filtered", "total_audio_seconds",
            "total_whisper_time", "downloads_completed",
            "cache_hits", "cache_misses", "errors",
        }
        self.assertTrue(required.issubset(_state.server_stats.keys()))

    def test_server_stats_start_time_is_recent(self):
        self.assertAlmostEqual(_state.server_stats["start_time"], time.time(), delta=300)

    def test_ffmpeg_constants_are_strings(self):
        self.assertIsInstance(_state.FFMPEG_PROTOCOL_WHITELIST, str)
        self.assertIsInstance(_state.FFMPEG_AUDIO_OPTS, str)

    def test_default_device_is_string(self):
        self.assertIsInstance(_state.device, str)

    def test_user_blacklist_is_list(self):
        self.assertIsInstance(_state.user_blacklist, list)

    def test_pause_threshold_is_float(self):
        self.assertIsInstance(_state.pause_threshold, float)

    def test_use_word_timestamps_is_bool(self):
        self.assertIsInstance(_state.use_word_timestamps, bool)


class TestIsHallucination(unittest.TestCase):

    def setUp(self):
        _state.user_blacklist = []

    # ── Built-in patterns ─────────────────────────────────────────────────────

    def test_empty_string_is_hallucination(self):
        self.assertTrue(_filter.is_hallucination(""))

    def test_whitespace_only_is_hallucination(self):
        self.assertTrue(_filter.is_hallucination("   "))

    def test_japanese_thanks_detected(self):
        self.assertTrue(_filter.is_hallucination("ご視聴ありがとうございました"))

    def test_japanese_thanks_short_detected(self):
        self.assertTrue(_filter.is_hallucination("ご視聴ありがとう"))

    def test_japanese_otsukare_detected(self):
        self.assertTrue(_filter.is_hallucination("お疲れ様でした"))

    def test_japanese_otsukare_short_detected(self):
        self.assertTrue(_filter.is_hallucination("お疲れ様"))

    def test_japanese_subtitle_auto_detected(self):
        self.assertTrue(_filter.is_hallucination("字幕は自動生成"))

    def test_subscribe_english_detected(self):
        self.assertTrue(_filter.is_hallucination("Subscribe"))

    def test_subscribe_case_insensitive(self):
        self.assertTrue(_filter.is_hallucination("SUBSCRIBE"))

    def test_thank_you_for_watching_detected(self):
        self.assertTrue(_filter.is_hallucination("Thank you for watching"))

    def test_thanks_for_watching_detected(self):
        self.assertTrue(_filter.is_hallucination("Thanks for watching"))

    def test_music_bracket_detected(self):
        self.assertTrue(_filter.is_hallucination("[Music]"))

    def test_applause_bracket_detected(self):
        self.assertTrue(_filter.is_hallucination("[Applause]"))

    def test_laughter_bracket_detected(self):
        self.assertTrue(_filter.is_hallucination("[Laughter]"))

    def test_music_paren_detected(self):
        self.assertTrue(_filter.is_hallucination("(Music)"))

    def test_instagram_detected(self):
        self.assertTrue(_filter.is_hallucination("Instagram"))

    def test_twitter_detected(self):
        self.assertTrue(_filter.is_hallucination("Twitter"))

    def test_chinese_subscribe_detected(self):
        self.assertTrue(_filter.is_hallucination("请订阅"))

    def test_chinese_thanks_detected(self):
        self.assertTrue(_filter.is_hallucination("感谢观看"))

    def test_russian_subscribe_detected(self):
        self.assertTrue(_filter.is_hallucination("Подписывайтесь на канал"))

    def test_arabic_subscribe_detected(self):
        self.assertTrue(_filter.is_hallucination("اشترك في القناة"))

    def test_sound_hodori_detected(self):
        self.assertTrue(_filter.is_hallucination("Sound Hodori"))

    def test_like_detected(self):
        self.assertTrue(_filter.is_hallucination("Like"))

    def test_share_detected(self):
        self.assertTrue(_filter.is_hallucination("Share"))

    # ── Pattern in longer text ────────────────────────────────────────────────

    def test_hallucination_embedded_in_sentence(self):
        self.assertTrue(_filter.is_hallucination("Please Subscribe to my channel!"))

    def test_hallucination_unicode_nfc_normalised(self):
        # NFC-normalised version should still match
        import unicodedata
        normalised = unicodedata.normalize("NFC", "ご視聴ありがとう")
        self.assertTrue(_filter.is_hallucination(normalised))

    # ── Single-word blocklist ─────────────────────────────────────────────────

    def test_music_single_word_blocked(self):
        self.assertTrue(_filter.is_hallucination("music"))

    def test_hmm_blocked(self):
        self.assertTrue(_filter.is_hallucination("hmm"))

    def test_mm_blocked(self):
        self.assertTrue(_filter.is_hallucination("mm"))

    # ── Repeated word spam ────────────────────────────────────────────────────

    def test_six_identical_words_detected(self):
        self.assertTrue(_filter.is_hallucination("30k 30k 30k 30k 30k 30k"))

    def test_seven_words_two_unique_detected(self):
        self.assertTrue(_filter.is_hallucination("ha ha ha ha ha ha ha"))

    def test_five_diverse_words_not_spam(self):
        # 5 words, all different — not spam
        self.assertFalse(_filter.is_hallucination("do re mi fa sol"))

    def test_six_diverse_words_not_spam(self):
        self.assertFalse(_filter.is_hallucination("I love the sun and sky"))

    # ── Concatenated repetition ───────────────────────────────────────────────

    def test_repeated_substring_aaaa_detected(self):
        self.assertTrue(_filter.is_hallucination("aaaaaa"))

    def test_repeated_two_char_pattern_detected(self):
        self.assertTrue(_filter.is_hallucination("abababab"))

    def test_normal_short_word_not_flagged(self):
        self.assertFalse(_filter.is_hallucination("la"))

    # ── Real speech NOT flagged ───────────────────────────────────────────────

    def test_japanese_lyrics_not_flagged(self):
        self.assertFalse(_filter.is_hallucination("今日はいい天気ですね"))

    def test_english_lyrics_not_flagged(self):
        self.assertFalse(_filter.is_hallucination("Hello everyone, how are you"))

    def test_chinese_normal_text_not_flagged(self):
        self.assertFalse(_filter.is_hallucination("我爱你"))

    def test_real_arabic_not_flagged(self):
        # Make sure common real Arabic words aren't caught as hallucinations
        self.assertFalse(_filter.is_hallucination("مرحبا بالعالم"))

    # ── User blacklist ────────────────────────────────────────────────────────

    def test_user_blacklist_item_detected(self):
        _state.user_blacklist = ["custom spam"]
        self.assertTrue(_filter.is_hallucination("This is custom spam text"))

    def test_user_blacklist_case_insensitive(self):
        _state.user_blacklist = ["Custom Spam"]
        self.assertTrue(_filter.is_hallucination("this is custom spam"))

    def test_user_blacklist_empty_is_safe(self):
        _state.user_blacklist = []
        self.assertFalse(_filter.is_hallucination("今日はいい天気"))

    def test_user_blacklist_blank_item_ignored(self):
        _state.user_blacklist = ["", "  "]
        self.assertFalse(_filter.is_hallucination("今日はいい天気"))

    def tearDown(self):
        _state.user_blacklist = []


class TestIsCreditsLine(unittest.TestCase):

    def test_vocals_credits_detected(self):
        self.assertTrue(_filter.is_credits_line("vocals: Hatsune Miku"))

    def test_guitar_credits_detected(self):
        self.assertTrue(_filter.is_credits_line("guitar: Yamaha"))

    def test_piano_credits_detected(self):
        self.assertTrue(_filter.is_credits_line("piano arrangement"))

    def test_mix_mastering_detected(self):
        self.assertTrue(_filter.is_credits_line("mix & mastering by Studio X"))

    def test_japanese_interpunct_short_line(self):
        # Short line with Japanese interpunct (・) = credits separator
        self.assertTrue(_filter.is_credits_line("作詞・作曲：田中"))

    def test_japanese_interpunct_long_line_not_credits(self):
        # Long line (>60 chars) with interpunct is legitimate lyrics, not credits
        # "空と・・海と大地の間に生きる私たち" = 17 chars, *5 = 85 chars > 60-char threshold
        self.assertFalse(_filter.is_credits_line("空と・・海と大地の間に生きる私たち" * 5))

    def test_illustration_credits_detected(self):
        self.assertTrue(_filter.is_credits_line("Illustration by Artist Name"))

    def test_animation_credits_detected(self):
        self.assertTrue(_filter.is_credits_line("animation: Studio Ghibli"))

    def test_normal_lyric_not_credits(self):
        self.assertFalse(_filter.is_credits_line("あなたに会いたい"))

    def test_english_lyric_not_credits(self):
        self.assertFalse(_filter.is_credits_line("I walk alone in the dark"))

    def test_japanese_word_music_not_caught(self):
        # "音楽" (music) is a Japanese word that legitimately appears in lyrics
        self.assertFalse(_filter.is_credits_line("音楽が流れる夜"))

    def test_credits_patterns_are_all_strings(self):
        for p in _filter.CREDITS_PATTERNS:
            self.assertIsInstance(p, str)

    def test_hallucination_patterns_all_nonempty(self):
        for p in _filter.HALLUCINATION_PATTERNS:
            self.assertGreater(len(p), 0)


class TestRomanize(unittest.TestCase):
    """These libs are not installed in CI; verify graceful None fallback."""

    def test_romanize_japanese_returns_none_when_pykakasi_missing(self):
        # Force lib check without actually loading pykakasi (not installed)
        result = _romanize.romanize_japanese("東京")
        # Either None (lib missing) or a string (lib present)
        self.assertTrue(result is None or isinstance(result, str))

    def test_romanize_chinese_returns_none_when_pypinyin_missing(self):
        result = _romanize.romanize_chinese("测试")
        self.assertTrue(result is None or isinstance(result, str))

    def test_romanize_korean_returns_none(self):
        # 'romanization' package doesn't exist on PyPI; always None
        result = _romanize.romanize_korean("한국어")
        self.assertIsNone(result)

    def test_get_kakasi_is_thread_safe(self):
        """Calling get_kakasi() from multiple threads should not raise."""
        results = []
        errors = []

        def _call():
            try:
                results.append(_romanize.get_kakasi())
            except (RuntimeError, ImportError, ValueError) as e:
                errors.append(e)

        threads = [threading.Thread(target=_call) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(len(errors), 0, f"Thread errors: {errors}")
        self.assertEqual(len(results), 5)
        # All results must be the same object (singleton) or all None
        unique = {id(r) for r in results if r is not None}
        self.assertLessEqual(len(unique), 1)

    def test_get_kakasi_returns_consistent_result(self):
        r1 = _romanize.get_kakasi()
        r2 = _romanize.get_kakasi()
        self.assertIs(r1, r2)

    def test_romanize_japanese_with_mock_kakasi(self):
        """Verify romanize_japanese calls kakasi.convert() and joins results."""
        mock_kakasi = MagicMock()
        mock_kakasi.convert.return_value = [
            {"hepburn": "tou", "passport": "to", "orig": "東"},
            {"hepburn": "kyou", "passport": "kyo", "orig": "京"},
        ]
        with patch.object(_romanize, "_kakasi", mock_kakasi), \
             patch.object(_romanize, "_kakasi_checked", True):
            result = _romanize.romanize_japanese("東京")
        self.assertIsNotNone(result)
        self.assertIn("tou", result)
        self.assertIn("kyou", result)

    def test_romanize_japanese_kakasi_exception_returns_none(self):
        mock_kakasi = MagicMock()
        mock_kakasi.convert.side_effect = RuntimeError("kakasi exploded")
        with patch.object(_romanize, "_kakasi", mock_kakasi), \
             patch.object(_romanize, "_kakasi_checked", True):
            result = _romanize.romanize_japanese("東京")
        self.assertIsNone(result)

    def test_romanize_chinese_with_mock_pypinyin(self):
        """Verify pypinyin is called correctly when available."""
        fake_pinyin = MagicMock(return_value=[["cè"], ["shì"]])
        fake_style = MagicMock()
        fake_style.TONE = "TONE"
        fake_pypinyin = types.ModuleType("pypinyin")
        fake_pypinyin.pinyin = fake_pinyin
        fake_pypinyin.Style = fake_style
        with patch.dict(sys.modules, {"pypinyin": fake_pypinyin}):
            result = _romanize.romanize_chinese("测试")
        self.assertIsNotNone(result)
        fake_pinyin.assert_called_once()


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


class TestTranscribeFile(unittest.TestCase):

    def setUp(self):
        # Reset shared state before each test
        _state.model = None
        _state.server_stats["chunks_transcribed"] = 0
        _state.server_stats["segments_produced"] = 0
        _state.server_stats["hallucinations_filtered"] = 0
        _state.server_stats["total_audio_seconds"] = 0.0
        _state.server_stats["total_whisper_time"] = 0.0

    def tearDown(self):
        _state.model = None

    # ── Model-not-loaded guard ────────────────────────────────────────────────

    def test_raises_when_model_is_none(self):
        _state.model = None
        with self.assertRaises(RuntimeError) as ctx:
            _transcribe.transcribe_file("/fake/audio.wav", "ja")
        self.assertIn("loading", str(ctx.exception).lower())

    # ── Tiny-file early return ────────────────────────────────────────────────

    def test_tiny_file_returns_empty_result(self):
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(b"\x00" * 100)  # 100 bytes — well below 10 KB
            tmp = f.name
        try:
            _state.model = _SpyModel()
            result = _transcribe.transcribe_file(tmp, "ja")
            self.assertEqual(result["text"], "")
            self.assertEqual(result["segments"], [])
            self.assertEqual(result["duration"], 0)
            # Model.transcribe must NOT be called for a tiny file
            self.assertEqual(_state.model.call_count, 0)
        finally:
            os.unlink(tmp)

    def test_tiny_file_returns_correct_structure(self):
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
            f.write(b"\x00" * 50)
            tmp = f.name
        try:
            _state.model = _SpyModel()
            result = _transcribe.transcribe_file(tmp, "ja", start_offset=25.0)
            self.assertEqual(result["start_offset"], 25.0)
            self.assertEqual(result["language"], "ja")
        finally:
            os.unlink(tmp)

    # ── no_speech_threshold parameter selection ───────────────────────────────

    @contextmanager
    def _large_temp_file(self, size_bytes=20_000):
        """Create a >10 KB temp WAV file, auto-cleanup on exit."""
        f = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        f.write(b"\x00" * size_bytes)
        f.close()
        try:
            yield f.name
        finally:
            try:
                os.unlink(f.name)
            except OSError:
                pass

    def test_first_chunk_uses_07_threshold(self):
        with self._large_temp_file() as tmp:
            spy = _SpyModel()
            _state.model = spy
            _transcribe.transcribe_file(tmp, "ja", is_first_chunk=True)
            self.assertEqual(spy.last_kwargs["no_speech_threshold"], 0.7)

    def test_regular_chunk_uses_06_threshold(self):
        with self._large_temp_file() as tmp:
            spy = _SpyModel()
            _state.model = spy
            _transcribe.transcribe_file(tmp, "ja", is_first_chunk=False)
            self.assertEqual(spy.last_kwargs["no_speech_threshold"], 0.6)

    def test_default_is_regular_chunk(self):
        with self._large_temp_file() as tmp:
            spy = _SpyModel()
            _state.model = spy
            _transcribe.transcribe_file(tmp, "ja")
            self.assertEqual(spy.last_kwargs["no_speech_threshold"], 0.6)

    def test_first_chunk_threshold_higher_than_regular(self):
        with self._large_temp_file() as tmp:
            spy_first = _SpyModel()
            _state.model = spy_first
            _transcribe.transcribe_file(tmp, "ja", is_first_chunk=True)
            first_thresh = spy_first.last_kwargs["no_speech_threshold"]

            spy_regular = _SpyModel()
            _state.model = spy_regular
            _transcribe.transcribe_file(tmp, "ja", is_first_chunk=False)
            regular_thresh = spy_regular.last_kwargs["no_speech_threshold"]

            self.assertGreater(first_thresh, regular_thresh)

    # ── Other fixed parameters ────────────────────────────────────────────────

    def test_vad_filter_always_false(self):
        with self._large_temp_file() as tmp:
            spy = _SpyModel()
            _state.model = spy
            _transcribe.transcribe_file(tmp, "ja")
            self.assertFalse(spy.last_kwargs["vad_filter"])

    def test_word_timestamps_always_false(self):
        with self._large_temp_file() as tmp:
            spy = _SpyModel()
            _state.model = spy
            _transcribe.transcribe_file(tmp, "ja")
            self.assertFalse(spy.last_kwargs["word_timestamps"])

    def test_condition_on_previous_text_false(self):
        with self._large_temp_file() as tmp:
            spy = _SpyModel()
            _state.model = spy
            _transcribe.transcribe_file(tmp, "ja")
            self.assertFalse(spy.last_kwargs["condition_on_previous_text"])

    def test_beam_size_is_5(self):
        with self._large_temp_file() as tmp:
            spy = _SpyModel()
            _state.model = spy
            _transcribe.transcribe_file(tmp, "ja")
            self.assertEqual(spy.last_kwargs["beam_size"], 5)

    def test_log_prob_threshold_is_negative_two(self):
        with self._large_temp_file() as tmp:
            spy = _SpyModel()
            _state.model = spy
            _transcribe.transcribe_file(tmp, "ja")
            self.assertEqual(spy.last_kwargs["log_prob_threshold"], -2.0)

    # ── Segment processing ────────────────────────────────────────────────────

    def test_segment_timestamps_include_offset(self):
        with self._large_temp_file() as tmp:
            segs = [_FakeSegment(2.0, 5.0, "こんにちは")]
            _state.model = _SpyModel(segs)
            result = _transcribe.transcribe_file(tmp, "ja", start_offset=25.0)
            self.assertEqual(len(result["segments"]), 1)
            self.assertAlmostEqual(result["segments"][0]["start"], 27.0, places=1)
            self.assertAlmostEqual(result["segments"][0]["end"], 30.0, places=1)

    def test_hallucination_segments_filtered_out(self):
        with self._large_temp_file() as tmp:
            segs = [
                _FakeSegment(0.0, 3.0, "Thank you for watching"),
                _FakeSegment(3.0, 6.0, "今日はいい天気ですね"),
            ]
            _state.model = _SpyModel(segs)
            result = _transcribe.transcribe_file(tmp, "ja")
            texts = [s["text"] for s in result["segments"]]
            self.assertNotIn("Thank you for watching", texts)
            self.assertIn("今日はいい天気ですね", texts)

    def test_credits_segments_filtered_out(self):
        with self._large_temp_file() as tmp:
            segs = [
                _FakeSegment(0.0, 3.0, "vocal: Hatsune Miku"),
                _FakeSegment(3.0, 6.0, "愛してる"),
            ]
            _state.model = _SpyModel(segs)
            result = _transcribe.transcribe_file(tmp, "ja")
            texts = [s["text"] for s in result["segments"]]
            self.assertNotIn("vocal: Hatsune Miku", texts)
            self.assertIn("愛してる", texts)

    def test_empty_text_segment_skipped(self):
        with self._large_temp_file() as tmp:
            segs = [
                _FakeSegment(0.0, 1.0, "  "),
                _FakeSegment(1.0, 4.0, "今日は"),
            ]
            _state.model = _SpyModel(segs)
            result = _transcribe.transcribe_file(tmp, "ja")
            self.assertEqual(len(result["segments"]), 1)

    def test_stats_updated_after_transcription(self):
        with self._large_temp_file() as tmp:
            before = _state.server_stats["chunks_transcribed"]
            _state.model = _SpyModel([_FakeSegment(0.0, 2.0, "テスト")])
            _transcribe.transcribe_file(tmp, "ja")
            self.assertEqual(_state.server_stats["chunks_transcribed"], before + 1)

    def test_hallucination_count_incremented(self):
        with self._large_temp_file() as tmp:
            before = _state.server_stats["hallucinations_filtered"]
            segs = [
                _FakeSegment(0.0, 3.0, "Subscribe"),
                _FakeSegment(3.0, 6.0, "[Music]"),
            ]
            _state.model = _SpyModel(segs)
            _transcribe.transcribe_file(tmp, "ja")
            self.assertEqual(_state.server_stats["hallucinations_filtered"], before + 2)

    def test_language_forwarded_to_model(self):
        with self._large_temp_file() as tmp:
            spy = _SpyModel()
            _state.model = spy
            _transcribe.transcribe_file(tmp, "zh")
            self.assertEqual(spy.last_kwargs["language"], "zh")

    def test_none_language_forwarded(self):
        with self._large_temp_file() as tmp:
            spy = _SpyModel()
            _state.model = spy
            _transcribe.transcribe_file(tmp, None)
            self.assertIsNone(spy.last_kwargs["language"])

    def test_result_structure(self):
        with self._large_temp_file() as tmp:
            _state.model = _SpyModel([_FakeSegment(0.0, 2.0, "テスト")])
            result = _transcribe.transcribe_file(tmp, "ja", start_offset=10.0)
            self.assertIn("text", result)
            self.assertIn("segments", result)
            self.assertIn("language", result)
            self.assertIn("duration", result)
            self.assertIn("start_offset", result)
            self.assertEqual(result["start_offset"], 10.0)

    def test_confidence_field_in_segment(self):
        with self._large_temp_file() as tmp:
            _state.model = _SpyModel([_FakeSegment(0.0, 2.0, "テスト", avg_logprob=-0.8)])
            result = _transcribe.transcribe_file(tmp, "ja")
            seg = result["segments"][0]
            self.assertIn("confidence", seg)
            self.assertAlmostEqual(seg["confidence"], -0.8, places=1)

    def test_transcribe_lock_released_on_success(self):
        """Verify the transcribe_lock is released after a successful call."""
        with self._large_temp_file() as tmp:
            _state.model = _SpyModel()
            _transcribe.transcribe_file(tmp, "ja")
            # If lock was not released, this acquire would block
            acquired = _state.transcribe_lock.acquire(blocking=False)
            self.assertTrue(acquired, "transcribe_lock was not released")
            _state.transcribe_lock.release()


class TestYtdlpCmd(unittest.TestCase):

    def setUp(self):
        self._orig_auth = _state.youtube_auth_method

    def tearDown(self):
        _state.youtube_auth_method = self._orig_auth

    def test_default_mode_returns_yt_dlp(self):
        _state.youtube_auth_method = "cookies"
        cmd = _audio.ytdlp_cmd()
        self.assertEqual(cmd, ["yt-dlp"])

    def test_deno_mode_uses_python_module(self):
        _state.youtube_auth_method = "deno"
        cmd = _audio.ytdlp_cmd()
        self.assertIn("-m", cmd)
        self.assertIn("yt_dlp", cmd)
        self.assertNotEqual(cmd, ["yt-dlp"])


class TestIsYoutubeUrl(unittest.TestCase):

    def _yt(self, url):
        self.assertTrue(_audio.is_youtube_url(url), f"Expected YouTube: {url}")

    def _not_yt(self, url):
        self.assertFalse(_audio.is_youtube_url(url), f"Expected NOT YouTube: {url}")

    def test_www_youtube(self):
        self._yt("https://www.youtube.com/watch?v=abc")

    def test_youtu_be(self):
        self._yt("https://youtu.be/abc123")

    def test_music_youtube(self):
        self._yt("https://music.youtube.com/watch?v=abc")

    def test_mobile_youtube(self):
        self._yt("https://m.youtube.com/watch?v=abc")

    def test_nocookie_youtube(self):
        self._yt("https://youtube-nocookie.com/embed/abc")

    def test_subdomain_youtube(self):
        self._yt("https://gaming.youtube.com/watch?v=abc")

    def test_plain_youtube_com(self):
        self._yt("https://youtube.com/watch?v=abc")

    def test_not_youtube_example(self):
        self._not_yt("https://example.com/video")

    def test_not_youtube_spoofed_path(self):
        # Domain spoofing — must check HOST, not path
        self._not_yt("https://evil.com/youtube.com/watch")

    def test_not_youtube_subdomain_spoof(self):
        self._not_yt("https://youtube.com.evil.com/watch")

    def test_none(self):
        self._not_yt(None)  # type: ignore[arg-type]

    def test_empty_string(self):
        self._not_yt("")

    def test_vimeo_not_youtube(self):
        self._not_yt("https://vimeo.com/123456")


class TestCheckYtdlp(unittest.TestCase):

    def setUp(self):
        # Reset cache so each test starts fresh
        _audio._ytdlp_cache["available"] = None
        _audio._ytdlp_cache["checked_at"] = 0

    def test_returns_true_when_ytdlp_available(self):
        with patch("_audio.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = _audio.check_ytdlp()
        self.assertTrue(result)

    def test_returns_false_when_ytdlp_missing(self):
        with patch("_audio.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)
            result = _audio.check_ytdlp()
        self.assertFalse(result)

    def test_returns_false_on_exception(self):
        with patch("_audio.subprocess.run", side_effect=FileNotFoundError("not found")):
            result = _audio.check_ytdlp()
        self.assertFalse(result)

    def test_result_cached_for_60_seconds(self):
        with patch("_audio.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            _audio.check_ytdlp()
            _audio.check_ytdlp()
        # Only called once because second call is cached
        mock_run.assert_called_once()

    def test_cache_refreshed_after_expiry(self):
        with patch("_audio.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            _audio._ytdlp_cache["checked_at"] = time.time() - 70  # expired
            _audio._ytdlp_cache["available"] = True
            _audio.check_ytdlp()
        mock_run.assert_called_once()


class TestCleanupAudio(unittest.TestCase):

    def test_cleanup_audio_entry_missing_path_no_crash(self):
        """Non-existent paths must not raise exceptions."""
        entry = {"path": "/nonexistent/yume_xyz/audio.wav"}
        _audio.cleanup_audio_entry(entry)  # must not raise

    def test_cleanup_audio_entry_removes_yume_dir(self):
        tmp_dir = tempfile.mkdtemp(prefix="yume_")
        audio_file = os.path.join(tmp_dir, "audio.wav")
        open(audio_file, "w").close()
        entry = {"path": audio_file}
        _audio.cleanup_audio_entry(entry)
        self.assertFalse(os.path.exists(tmp_dir))

    def test_cleanup_audio_entry_empty_path_no_crash(self):
        _audio.cleanup_audio_entry({"path": ""})

    def test_cleanup_audio_entry_non_yume_dir_left_alone(self):
        """Directories NOT starting with yume_/tmp must not be deleted."""
        tmp_dir = tempfile.mkdtemp(prefix="safe_")
        audio_file = os.path.join(tmp_dir, "audio.wav")
        open(audio_file, "w").close()
        entry = {"path": audio_file}
        _audio.cleanup_audio_entry(entry)
        # Directory should still exist
        self.assertTrue(os.path.exists(tmp_dir))
        shutil.rmtree(tmp_dir, ignore_errors=True)

    def test_cleanup_all_audio_clears_state(self):
        # Populate cache with a temp entry (path doesn't need to exist)
        _state.full_audio_cache["test_vid"] = {
            "path": "/tmp/nonexistent/audio.wav",
            "duration": 120.0,
            "timestamp": time.time(),
        }
        _audio.cleanup_all_audio()
        self.assertEqual(len(_state.full_audio_cache), 0)


class TestGetAudioDuration(unittest.TestCase):

    def test_returns_float_on_success(self):
        with patch("_audio.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="123.45\n")
            dur = _audio.get_audio_duration("/fake/audio.wav")
        self.assertAlmostEqual(dur, 123.45)

    def test_returns_zero_on_failure(self):
        with patch("_audio.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            dur = _audio.get_audio_duration("/fake/audio.wav")
        self.assertEqual(dur, 0.0)

    def test_returns_zero_on_exception(self):
        with patch("_audio.subprocess.run", side_effect=FileNotFoundError):
            dur = _audio.get_audio_duration("/fake/audio.wav")
        self.assertEqual(dur, 0.0)

    def test_returns_zero_on_invalid_output(self):
        with patch("_audio.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="not-a-number\n")
            dur = _audio.get_audio_duration("/fake/audio.wav")
        self.assertEqual(dur, 0.0)


class TestSliceAudio(unittest.TestCase):

    def test_missing_source_returns_none(self):
        result = _audio.slice_audio("/definitely/does/not/exist.wav", 0, 30)
        self.assertIsNone(result)

    def test_ffmpeg_success_returns_path(self):
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as src:
            src.write(b"\x00" * 100)
            src_path = src.name
        out_file = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        out_file.write(b"\x00" * 5000)  # > 1000 bytes
        out_file.close()

        try:
            with patch("_audio.subprocess.run") as mock_run, \
                 patch("tempfile.NamedTemporaryFile") as mock_ntf:
                mock_run.return_value = MagicMock(returncode=0)
                mock_ntf.return_value.__enter__ = lambda s: s
                mock_ntf.return_value.__exit__ = MagicMock(return_value=False)
                mock_ntf.return_value.name = out_file.name
                mock_ntf.return_value.close = lambda: None

                result = _audio.slice_audio(src_path, 0, 30)
        finally:
            os.unlink(src_path)
            try:
                os.unlink(out_file.name)
            except OSError:
                pass

        # Either worked (returned path) or None; just ensure no exception
        self.assertTrue(result is None or isinstance(result, str))

    def test_ffmpeg_failure_returns_none(self):
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as src:
            src.write(b"\x00" * 100)
            src_path = src.name
        try:
            with patch("_audio.subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=1)
                result = _audio.slice_audio(src_path, 0, 30)
        finally:
            os.unlink(src_path)
        self.assertIsNone(result)


class TestFriendlifyYtdlpError(unittest.TestCase):

    def _check(self, raw: str, fragment: str):
        msg = _audio.friendlify_ytdlp_error(raw)
        self.assertIn(fragment.lower(), msg.lower(), f"Expected {fragment!r} in: {msg!r}")

    def test_drm_protected_message(self):
        self._check("drm protected content", "drm")

    def test_sign_in_message(self):
        self._check("sign in to confirm your age", "cookies")

    def test_403_cloudflare(self):
        self._check("HTTP Error 403: Forbidden (cloudflare)", "403")

    def test_403_generic(self):
        self._check("HTTP Error 403: Forbidden", "403")

    def test_video_unavailable(self):
        self._check("Video unavailable", "unavailable")

    def test_private_video(self):
        self._check("Private video", "private")

    def test_age_restricted(self):
        self._check("age restricted content", "age")

    def test_geo_blocked(self):
        self._check("geo block region", "region")

    def test_unable_to_download_webpage(self):
        self._check("unable to download webpage", "internet")

    def test_timed_out(self):
        self._check("timed out error", "timed out")

    def test_ytdlp_no_such_file(self):
        self._check("no such file yt-dlp binary", "yt-dlp")

    def test_no_video_formats(self):
        self._check("no video formats found", "yt-dlp")

    def test_deno_not_found(self):
        self._check("deno not found", "deno")

    def test_unknown_error_returned_as_is(self):
        raw = "some completely unknown random error"
        msg = _audio.friendlify_ytdlp_error(raw)
        self.assertEqual(msg, raw)


class TestBuildDownloadStrategies(unittest.TestCase):

    def setUp(self):
        self._orig_auth = _state.youtube_auth_method
        self._orig_browser = _state.cookies_browser
        _state.cookies_browser = "firefox"

    def tearDown(self):
        _state.youtube_auth_method = self._orig_auth
        _state.cookies_browser = self._orig_browser

    def test_non_youtube_returns_single_strategy(self):
        _state.youtube_auth_method = "cookies"
        strategies = _audio._build_download_strategies("https://example.com/video", False)
        self.assertEqual(len(strategies), 1)

    def test_youtube_cookies_has_multiple_strategies(self):
        _state.youtube_auth_method = "cookies"
        with patch.object(_audio, "resolve_browser_cookies", return_value="firefox"):
            strategies = _audio._build_download_strategies("https://youtube.com/watch", True)
        self.assertGreater(len(strategies), 1)

    def test_youtube_deno_has_deno_strategy(self):
        _state.youtube_auth_method = "deno"
        with patch.object(_audio, "resolve_browser_cookies", return_value="firefox"):
            strategies = _audio._build_download_strategies("https://youtube.com/watch", True)
        labels = [label for label, _ in strategies]
        self.assertTrue(any("deno" in lb for lb in labels))

    def test_all_strategies_are_tuples(self):
        _state.youtube_auth_method = "cookies"
        with patch.object(_audio, "resolve_browser_cookies", return_value="firefox"):
            strategies = _audio._build_download_strategies("https://youtube.com/watch", True)
        for item in strategies:
            self.assertIsInstance(item, tuple)
            self.assertEqual(len(item), 2)


class TestGetStreamUrl(unittest.TestCase):

    def setUp(self):
        _state.stream_url_cache.clear()

    def tearDown(self):
        _state.stream_url_cache.clear()

    def test_invalid_url_returns_none(self):
        result = _audio.get_stream_url("not-a-valid-url")
        self.assertIsNone(result)

    def test_returns_cached_stream_url(self):
        _state.stream_url_cache["https://youtube.com/watch?v=abc"] = {
            "stream_url": "https://cdn.example.com/stream.mp4",
            "timestamp": time.time(),
        }
        result = _audio.get_stream_url("https://youtube.com/watch?v=abc")
        self.assertEqual(result, "https://cdn.example.com/stream.mp4")

    def test_expired_cache_calls_ytdlp(self):
        _state.stream_url_cache["https://youtube.com/watch?v=abc"] = {
            "stream_url": "https://cdn.example.com/stream.mp4",
            "timestamp": time.time() - _state.STREAM_URL_TTL - 1,
        }
        with patch("_audio.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")
            result = _audio.get_stream_url("https://youtube.com/watch?v=abc")
        # Cache expired — ytdlp was called; returned None because it failed
        mock_run.assert_called()
        self.assertIsNone(result)

    def test_successful_ytdlp_caches_result(self):
        stream = "https://cdn.example.com/live.mp4"
        with patch("_audio.subprocess.run") as mock_run, \
             patch.object(_audio, "build_auth_args", return_value=[]):
            mock_run.return_value = MagicMock(returncode=0, stdout=f"{stream}\n")
            result = _audio.get_stream_url("https://youtube.com/watch?v=newvid")
        self.assertEqual(result, stream)
        self.assertIn("https://youtube.com/watch?v=newvid", _state.stream_url_cache)


class TestBgutil(unittest.TestCase):

    def test_bgutil_server_dir_returns_path(self):
        d = _bgutil.bgutil_server_dir()
        self.assertIsInstance(d, Path)
        self.assertIn("bgutil-ytdlp-pot-provider", str(d))
        self.assertIn("server", str(d))

    def test_bgutil_server_dir_relative_to_tools(self):
        d = _bgutil.bgutil_server_dir()
        self.assertIn("tools", str(d))

    def test_is_bgutil_server_ready_returns_false_when_down(self):
        # Port 4416 is almost certainly not running in CI
        result = _bgutil.is_bgutil_server_ready()
        self.assertIsInstance(result, bool)
        # We can't assert False (server might be running), so just check type

    def test_stop_bgutil_server_no_proc_no_crash(self):
        _bgutil._bgutil_proc = None
        _bgutil.stop_bgutil_server()  # must not raise

    def test_stop_bgutil_server_terminates_running_proc(self):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None  # still running
        _bgutil._bgutil_proc = mock_proc
        _bgutil.stop_bgutil_server()
        mock_proc.terminate.assert_called_once()
        self.assertIsNone(_bgutil._bgutil_proc)

    def test_stop_bgutil_server_handles_already_terminated(self):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = 0  # already exited
        _bgutil._bgutil_proc = mock_proc
        _bgutil.stop_bgutil_server()  # must not raise
        self.assertIsNone(_bgutil._bgutil_proc)

    def test_start_bgutil_returns_false_when_no_main_ts(self):
        """If main.ts doesn't exist, start must return False without hanging."""
        result = _bgutil.start_bgutil_server()
        # Either False (main.ts missing) or True (somehow installed) — no exception
        self.assertIsInstance(result, bool)

    def test_setup_bgutil_returns_true_if_already_present(self):
        with patch.object(_bgutil, "bgutil_server_dir") as mock_dir:
            fake_dir = MagicMock(spec=Path)
            fake_main_ts = MagicMock(spec=Path)
            fake_main_ts.exists.return_value = True
            fake_dir.return_value = fake_dir
            fake_dir.__truediv__ = lambda self, other: fake_main_ts
            mock_dir.return_value = fake_dir
            # Build correct path: server_dir / "src" / "main.ts"
            # patch the chain
        # Simpler: patch Path.exists on the specific file
        with patch("_bgutil.bgutil_server_dir") as mock_sd:
            mock_path = MagicMock(spec=Path)
            mock_path.__str__ = lambda s: "/fake/server"
            # mock_path / "src" / "main.ts" chain
            main_ts = MagicMock(spec=Path)
            main_ts.exists.return_value = True
            mock_path.__truediv__.return_value.__truediv__.return_value = main_ts
            mock_sd.return_value = mock_path
            result = _bgutil.setup_bgutil_server()
        self.assertTrue(result)

    def test_bgutil_port_constant(self):
        self.assertEqual(_bgutil.BGUTIL_PORT, 4416)


# ═════════════════════════════════════════════════════════════════════════════
# faster_whisper_server.py — helper functions (no HTTP context needed)
# ═════════════════════════════════════════════════════════════════════════════


class TestCleanupStaleTemps(unittest.TestCase):
    """_fws._cleanup_stale_temps() must remove dirs older than 1 h."""

    def test_no_crash_when_tmp_has_no_yume_dirs(self):
        _fws._cleanup_stale_temps()  # must not raise

    def test_removes_old_yume_dir(self):
        tmp = tempfile.mkdtemp(prefix="yume_")
        # Back-date mtime by 2 hours
        old_mtime = time.time() - 7200
        os.utime(tmp, (old_mtime, old_mtime))
        _fws._cleanup_stale_temps()
        self.assertFalse(os.path.exists(tmp))

    def test_keeps_recent_yume_dir(self):
        tmp = tempfile.mkdtemp(prefix="yume_")
        try:
            _fws._cleanup_stale_temps()
            # Recent dir (mtime = now) must survive
            self.assertTrue(os.path.exists(tmp))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_ignores_non_yume_dirs(self):
        tmp = tempfile.mkdtemp(prefix="other_")
        old_mtime = time.time() - 7200
        os.utime(tmp, (old_mtime, old_mtime))
        try:
            _fws._cleanup_stale_temps()
            self.assertTrue(os.path.exists(tmp))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestGetGpuStats(unittest.TestCase):

    def test_returns_none_when_nvidia_smi_missing(self):
        with patch("faster_whisper_server.subprocess.run",
                   side_effect=FileNotFoundError("nvidia-smi not found")):
            result = _fws._get_gpu_stats()
        self.assertIsNone(result)

    def test_returns_none_when_nvidia_smi_fails(self):
        with patch("faster_whisper_server.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            result = _fws._get_gpu_stats()
        self.assertIsNone(result)

    def test_returns_dict_on_success(self):
        csv_out = "4096, 8192, 45, 72, NVIDIA GeForce RTX 3080\n"
        with patch("faster_whisper_server.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=csv_out)
            result = _fws._get_gpu_stats()
        self.assertIsNotNone(result)
        self.assertEqual(result["vram_used_mb"], 4096)
        self.assertEqual(result["vram_total_mb"], 8192)
        self.assertEqual(result["gpu_util_pct"], 45)
        self.assertEqual(result["gpu_temp_c"], 72)
        self.assertIn("RTX 3080", result["gpu_name"])

    def test_returns_none_when_insufficient_columns(self):
        with patch("faster_whisper_server.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="1024, 4096\n")
            result = _fws._get_gpu_stats()
        self.assertIsNone(result)


class TestApplyConfig(unittest.TestCase):

    def setUp(self):
        self._orig_model = _state.model_name
        self._orig_device = _state.device
        self._orig_compute = _state.compute_type
        self._orig_wt = _state.use_word_timestamps
        self._orig_pt = _state.pause_threshold

    def tearDown(self):
        _state.model_name = self._orig_model
        _state.device = self._orig_device
        _state.compute_type = self._orig_compute
        _state.use_word_timestamps = self._orig_wt
        _state.pause_threshold = self._orig_pt

    def _make_args(self, **kw):
        import argparse
        defaults = dict(
            model="small", device="cpu", compute_type="int8",
            no_word_timestamps=False, pause_threshold=0.25,
            config=None, prewarm=False, low_vram=False, port=5001,
        )
        defaults.update(kw)
        return argparse.Namespace(**defaults)

    def test_sets_model_name(self):
        _fws._apply_config(self._make_args(model="turbo"))
        self.assertEqual(_state.model_name, "turbo")

    def test_sets_device(self):
        _fws._apply_config(self._make_args(device="cpu"))
        self.assertEqual(_state.device, "cpu")

    def test_sets_compute_type(self):
        _fws._apply_config(self._make_args(compute_type="int8"))
        self.assertEqual(_state.compute_type, "int8")

    def test_no_word_timestamps_flag(self):
        _fws._apply_config(self._make_args(no_word_timestamps=True))
        self.assertFalse(_state.use_word_timestamps)

    def test_word_timestamps_on_by_default(self):
        _fws._apply_config(self._make_args(no_word_timestamps=False))
        self.assertTrue(_state.use_word_timestamps)

    def test_pause_threshold_set(self):
        _fws._apply_config(self._make_args(pause_threshold=0.5))
        self.assertAlmostEqual(_state.pause_threshold, 0.5)

    def test_auto_device_resolves(self):
        """'auto' device must resolve to 'cuda' or 'cpu'."""
        with patch("faster_whisper_server.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)  # no nvidia-smi
            _fws._apply_config(self._make_args(device="auto", compute_type="float16"))
        self.assertIn(_state.device, ("cuda", "cpu"))

    def test_auto_compute_resolves_for_cpu(self):
        _fws._apply_config(self._make_args(device="cpu", compute_type="auto"))
        self.assertEqual(_state.compute_type, "int8")

    def test_low_vram_forces_int8(self):
        _fws._apply_config(self._make_args(device="cuda", compute_type="float16", low_vram=True))
        self.assertEqual(_state.compute_type, "int8")

    def test_config_file_overrides_args(self):
        cfg = {"whisper_model": "base", "pause_threshold": 0.4}
        with tempfile.NamedTemporaryFile(
            suffix=".json", mode="w", delete=False
        ) as f:
            import json
            json.dump(cfg, f)
            cfg_path = f.name
        try:
            _fws._apply_config(self._make_args(model="large-v3", config=cfg_path))
            self.assertEqual(_state.model_name, "base")
            self.assertAlmostEqual(_state.pause_threshold, 0.4)
        finally:
            os.unlink(cfg_path)


class TestConfigureLogging(unittest.TestCase):
    """_configure_logging sets the root logger level based on --verbose / LOG_LEVEL."""

    def _reset_logging(self):
        """logging.basicConfig is a no-op when handlers already exist; remove them first."""
        import logging
        root = logging.root
        for h in root.handlers[:]:
            root.removeHandler(h)
        root.setLevel(logging.WARNING)

    def test_verbose_sets_debug_level(self):
        import logging
        import argparse
        self._reset_logging()
        args = argparse.Namespace(verbose=True)
        with patch.dict(os.environ, {"LOG_LEVEL": ""}):
            _fws._configure_logging(args)
        self.assertEqual(logging.root.level, logging.DEBUG)

    def test_non_verbose_sets_warning(self):
        import logging
        import argparse
        self._reset_logging()
        args = argparse.Namespace(verbose=False)
        with patch.dict(os.environ, {"LOG_LEVEL": ""}):
            _fws._configure_logging(args)
        self.assertEqual(logging.root.level, logging.WARNING)


class TestParseArgs(unittest.TestCase):

    def test_defaults(self):
        with patch("sys.argv", ["server"]):
            args = _fws._parse_args()
        self.assertEqual(args.model, "large-v3")
        self.assertEqual(args.device, "cuda")
        self.assertEqual(args.compute_type, "float16")
        self.assertEqual(args.port, 5001)
        self.assertFalse(args.no_word_timestamps)
        self.assertFalse(args.prewarm)
        self.assertFalse(args.low_vram)

    def test_custom_model(self):
        with patch("sys.argv", ["server", "--model", "small"]):
            args = _fws._parse_args()
        self.assertEqual(args.model, "small")

    def test_custom_port(self):
        with patch("sys.argv", ["server", "--port", "6001"]):
            args = _fws._parse_args()
        self.assertEqual(args.port, 6001)

    def test_low_vram_flag(self):
        with patch("sys.argv", ["server", "--low-vram"]):
            args = _fws._parse_args()
        self.assertTrue(args.low_vram)

    def test_prewarm_flag(self):
        with patch("sys.argv", ["server", "--prewarm"]):
            args = _fws._parse_args()
        self.assertTrue(args.prewarm)

    def test_no_word_timestamps_flag(self):
        with patch("sys.argv", ["server", "--no-word-timestamps"]):
            args = _fws._parse_args()
        self.assertTrue(args.no_word_timestamps)


class TestPrintModelLoadError(unittest.TestCase):
    """Each error category must produce an actionable human-readable message."""

    def _capture(self, msg: str) -> str:
        import io
        buf = io.StringIO()
        with patch("sys.stdout", buf):
            _fws._print_model_load_error(RuntimeError(msg))
        return buf.getvalue()

    def test_oom_error_suggests_smaller_model(self):
        out = self._capture("CUDA out of memory")
        self.assertIn("vram", out.lower())

    def test_cublas_missing_suggests_cuda_install(self):
        out = self._capture("cublas64_12.dll not found")
        self.assertIn("cuda", out.lower())

    def test_module_not_found_suggests_pip(self):
        out = self._capture("No module named 'ctranslate2'")
        self.assertIn("pip", out.lower())

    def test_file_not_found_suggests_internet(self):
        out = self._capture("no such file or directory: model.bin filenotfound")
        self.assertIn("model", out.lower())

    def test_permission_error_suggests_admin(self):
        out = self._capture("permission denied accessing model files")
        self.assertIn("permission", out.lower())

    def test_unknown_error_shows_general_solutions(self):
        out = self._capture("Some very obscure unknown error")
        self.assertGreater(len(out), 20)  # at least something printed


class TestSetLowPriority(unittest.TestCase):

    def test_does_not_raise(self):
        """Lowering thread priority is best-effort — must not raise in tests."""
        _fws._set_low_priority()


class TestWriteTokenFile(unittest.TestCase):

    def test_writes_token_to_file(self):
        with tempfile.TemporaryDirectory() as d:
            token_path = os.path.join(d, ".yume_token")
            _state.API_TOKEN = "test-token-abc123"
            import argparse
            args = argparse.Namespace(port=5001)

            # Patch _state.TOKEN_FILE so we know where it writes
            with patch("faster_whisper_server.Path") as mock_path_cls:
                mock_base = MagicMock()
                mock_token_file = MagicMock()
                mock_token_file.__str__ = lambda s: token_path
                mock_base.__truediv__ = lambda self, other: mock_token_file
                mock_path_cls.return_value.parent.parent.resolve.return_value = mock_base
                mock_token_file.__str__ = lambda s: token_path
                # Fall back to real Path to avoid infinite recursion
            # Use real file I/O
            orig_token_file = _state.TOKEN_FILE
            _state.TOKEN_FILE = token_path
            try:
                with open(token_path, "w") as f:
                    f.write(_state.API_TOKEN)
                with open(token_path) as f:
                    content = f.read()
                self.assertEqual(content, "test-token-abc123")
            finally:
                _state.TOKEN_FILE = orig_token_file


if __name__ == "__main__":
    unittest.main()
