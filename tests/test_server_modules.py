"""URL validation and the server's shared state."""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

import server_stubs  # noqa: E402,F401  (flask and faster_whisper, before the server modules)
import _security  # noqa: E402
import _state  # noqa: E402


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

    def test_stream_url_cache_is_dict(self):
        self.assertIsInstance(_state.stream_url_cache, dict)

    def test_locks_are_lock_instances(self):
        lock_type = type(threading.Lock())
        self.assertIsInstance(_state.transcribe_lock, lock_type)
        self.assertIsInstance(_state.model_switch_lock, lock_type)
        self.assertIsInstance(_state.cache_lock, lock_type)
        self.assertIsInstance(_state.stats_lock, lock_type)

    def test_stream_url_constants_positive(self):
        self.assertGreater(_state.STREAM_URL_CACHE_MAX, 0)
        self.assertGreater(_state.STREAM_URL_TTL, 0)

    def test_server_stats_has_expected_keys(self):
        required = {
            "start_time",
            "regions_transcribed",
            "segments_produced",
            "hallucinations_filtered",
            "total_audio_seconds",
            "total_whisper_time",
            "downloads_completed",
            "lines_translated",
            "translation_cache_hits",
            "errors",
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

    def test_load_error_starts_empty(self):
        self.assertEqual(_state.load_error, "")
