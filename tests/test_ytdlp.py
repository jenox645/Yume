"""yt-dlp commands, auth strategies and download errors."""

from __future__ import annotations

import sys
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

import _audio  # noqa: E402
import _download  # noqa: E402
import _state  # noqa: E402
import _stream  # noqa: E402
import _ytdlp  # noqa: E402


class TestYtdlpCmd(unittest.TestCase):
    def setUp(self):
        self._orig_auth = _state.youtube_auth_method

    def tearDown(self):
        _state.youtube_auth_method = self._orig_auth

    def test_default_mode_returns_yt_dlp(self):
        _state.youtube_auth_method = "cookies"
        cmd = _ytdlp.ytdlp_cmd()
        self.assertEqual(cmd, ["yt-dlp"])

    def test_deno_mode_uses_python_module(self):
        _state.youtube_auth_method = "deno"
        cmd = _ytdlp.ytdlp_cmd()
        self.assertIn("-m", cmd)
        self.assertIn("yt_dlp", cmd)
        self.assertNotEqual(cmd, ["yt-dlp"])


class TestIsYoutubeUrl(unittest.TestCase):
    def _yt(self, url):
        self.assertTrue(_ytdlp.is_youtube_url(url), f"Expected YouTube: {url}")

    def _not_yt(self, url):
        self.assertFalse(_ytdlp.is_youtube_url(url), f"Expected NOT YouTube: {url}")

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
        _ytdlp._ytdlp_cache["available"] = None
        _ytdlp._ytdlp_cache["checked_at"] = 0

    def test_returns_true_when_ytdlp_available(self):
        with patch("_ytdlp.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = _ytdlp.check_ytdlp()
        self.assertTrue(result)

    def test_returns_false_when_ytdlp_missing(self):
        with patch("_ytdlp.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)
            result = _ytdlp.check_ytdlp()
        self.assertFalse(result)

    def test_returns_false_on_exception(self):
        with patch("_ytdlp.subprocess.run", side_effect=FileNotFoundError("not found")):
            result = _ytdlp.check_ytdlp()
        self.assertFalse(result)

    def test_result_cached_for_60_seconds(self):
        with patch("_ytdlp.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            _ytdlp.check_ytdlp()
            _ytdlp.check_ytdlp()
        # Only called once because second call is cached
        mock_run.assert_called_once()

    def test_cache_refreshed_after_expiry(self):
        with patch("_ytdlp.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            _ytdlp._ytdlp_cache["checked_at"] = time.time() - 70  # expired
            _ytdlp._ytdlp_cache["available"] = True
            _ytdlp.check_ytdlp()
        mock_run.assert_called_once()


class TestAudioFiles(unittest.TestCase):
    """_audio.load_audio() / remove_temp()."""

    def test_load_audio_reads_16k_wav(self):
        import wave

        tmp = tempfile.mkdtemp(prefix="yume_")
        path = os.path.join(tmp, "a.wav")
        with wave.open(path, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes((np.ones(16000, dtype=np.int16) * 16384).tobytes())
        audio = _audio.load_audio(path)
        self.assertEqual(audio.dtype, np.float32)
        self.assertEqual(len(audio), 16000)
        self.assertAlmostEqual(float(audio[0]), 0.5, places=3)
        _audio.remove_temp(path)
        self.assertFalse(os.path.exists(tmp))

    def test_remove_temp_leaves_non_yume_dirs(self):
        tmp = tempfile.mkdtemp(prefix="keep_")
        path = os.path.join(tmp, "x.wav")
        open(path, "wb").close()
        _audio.remove_temp(path)
        self.assertTrue(os.path.isdir(tmp))  # only the file goes
        self.assertFalse(os.path.exists(path))
        shutil.rmtree(tmp, ignore_errors=True)

    def test_remove_temp_none_is_noop(self):
        _audio.remove_temp(None)
        _audio.remove_temp("")


class TestFriendlifyYtdlpError(unittest.TestCase):
    def _check(self, raw: str, fragment: str):
        msg = _ytdlp.friendlify_ytdlp_error(raw)
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
        msg = _ytdlp.friendlify_ytdlp_error(raw)
        self.assertEqual(msg, raw)


class TestBuildDownloadStrategies(unittest.TestCase):
    def setUp(self):
        self._orig_auth = _state.youtube_auth_method
        self._orig_browser = _state.cookies_browser
        _state.cookies_browser = "firefox"

    def tearDown(self):
        _state.youtube_auth_method = self._orig_auth
        _state.cookies_browser = self._orig_browser

    def test_non_youtube_has_no_auth_fallback(self):
        # Non-YouTube URLs try cookies first (login-gated sites) but must always
        # fall back to no-auth so a missing/locked cookie DB doesn't hard-fail a
        # URL that never needed cookies (direct media, cookie-less environments).
        _state.youtube_auth_method = "cookies"
        with patch.object(_ytdlp, "resolve_browser_cookies", return_value="firefox"):
            strategies = _download._build_download_strategies(False)
        labels = [label for label, _ in strategies]
        self.assertIn("no-auth", labels)
        self.assertEqual(labels[-1], "no-auth")

    def test_youtube_cookies_has_multiple_strategies(self):
        _state.youtube_auth_method = "cookies"
        with patch.object(_ytdlp, "resolve_browser_cookies", return_value="firefox"):
            strategies = _download._build_download_strategies(True)
        self.assertGreater(len(strategies), 1)

    def test_youtube_deno_has_deno_strategy(self):
        _state.youtube_auth_method = "deno"
        with patch.object(_ytdlp, "resolve_browser_cookies", return_value="firefox"):
            strategies = _download._build_download_strategies(True)
        labels = [label for label, _ in strategies]
        self.assertTrue(any("deno" in lb for lb in labels))

    def test_all_strategies_are_tuples(self):
        _state.youtube_auth_method = "cookies"
        with patch.object(_ytdlp, "resolve_browser_cookies", return_value="firefox"):
            strategies = _download._build_download_strategies(True)
        for item in strategies:
            self.assertIsInstance(item, tuple)
            self.assertEqual(len(item), 2)


class TestGetStreamUrl(unittest.TestCase):
    def setUp(self):
        _state.stream_url_cache.clear()

    def tearDown(self):
        _state.stream_url_cache.clear()

    def test_invalid_url_returns_none(self):
        result = _stream.get_stream_url("not-a-valid-url")
        self.assertIsNone(result)

    def test_returns_cached_stream_url(self):
        _state.stream_url_cache["https://youtube.com/watch?v=abc"] = {
            "stream_url": "https://cdn.example.com/stream.mp4",
            "timestamp": time.time(),
        }
        result = _stream.get_stream_url("https://youtube.com/watch?v=abc")
        self.assertEqual(result, "https://cdn.example.com/stream.mp4")

    def test_expired_cache_calls_ytdlp(self):
        _state.stream_url_cache["https://youtube.com/watch?v=abc"] = {
            "stream_url": "https://cdn.example.com/stream.mp4",
            "timestamp": time.time() - _state.STREAM_URL_TTL - 1,
        }
        with patch("_stream.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")
            result = _stream.get_stream_url("https://youtube.com/watch?v=abc")
        # Cache expired — ytdlp was called; returned None because it failed
        mock_run.assert_called()
        self.assertIsNone(result)

    def test_successful_ytdlp_caches_result(self):
        stream = "https://cdn.example.com/live.mp4"
        with patch("_stream.subprocess.run") as mock_run, patch.object(_ytdlp, "build_auth_args", return_value=[]):
            mock_run.return_value = MagicMock(returncode=0, stdout=f"{stream}\n")
            result = _stream.get_stream_url("https://youtube.com/watch?v=newvid")
        self.assertEqual(result, stream)
        self.assertIn("https://youtube.com/watch?v=newvid", _state.stream_url_cache)
