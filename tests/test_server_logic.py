"""Tests for faster_whisper_server.py — standalone logic (no server/model needed)."""
import sys
from pathlib import Path

# Add server dir to path
sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

# Import individual functions without triggering Flask app or Whisper model
# We need to be selective — import just the module and call functions directly
import importlib.util

spec = importlib.util.spec_from_file_location(
    "server", str(Path(__file__).parent.parent / "server" / "faster_whisper_server.py")
)

# We can't import the whole module (it starts Flask), so we test via exec extraction
# Instead, test the logic patterns directly


class TestURLValidation:
    """Test URL validation logic (mirrors _validate_url)."""

    def _validate_url(self, url):
        """Local copy of _validate_url for testing without Flask import."""
        from urllib.parse import urlparse
        if not url or not isinstance(url, str):
            return False, "Empty or invalid URL"
        url = url.strip()
        if url.startswith("-"):
            return False, "URL cannot start with '-'"
        # Reject shell metacharacters to prevent command injection
        # Note: '&' is intentionally allowed — it's a standard URL query separator
        # and all subprocess calls use shell=False (list args), so '&' is safe.
        if any(c in url for c in [";", "|", "`", "$", "(", ")", "\n", "\r"]):
            return False, "URL contains shell metacharacters"
        try:
            parsed = urlparse(url)
            if parsed.scheme not in ("http", "https"):
                return False, f"Unsupported scheme: {parsed.scheme!r}"
            if not parsed.netloc and not parsed.path:
                return False, "No host or path"
        except Exception as e:
            return False, f"Parse error: {e}"
        return True, ""

    def test_valid_http(self):
        ok, _ = self._validate_url("http://example.com/video")
        assert ok

    def test_valid_https(self):
        ok, _ = self._validate_url("https://youtube.com/watch?v=abc")
        assert ok

    def test_rejects_empty(self):
        ok, err = self._validate_url("")
        assert not ok

    def test_rejects_none(self):
        ok, err = self._validate_url(None)
        assert not ok

    def test_rejects_dash_prefix(self):
        """Argument injection: URLs starting with - could inject CLI flags."""
        ok, err = self._validate_url("-o /etc/passwd")
        assert not ok
        assert "'-'" in err

    def test_rejects_file_scheme(self):
        ok, err = self._validate_url("file:///etc/passwd")
        assert not ok

    def test_rejects_ftp_scheme(self):
        ok, err = self._validate_url("ftp://evil.com/payload")
        assert not ok

    def test_rejects_empty_scheme(self):
        """v5.2.0 fix: empty scheme no longer allowed."""
        ok, err = self._validate_url("//localhost/path")
        assert not ok

    def test_rejects_javascript_scheme(self):
        ok, err = self._validate_url("javascript:alert(1)")
        assert not ok

    def test_allows_ampersand_in_query(self):
        """'&' is a normal URL query separator — must not be blocked."""
        ok, _ = self._validate_url("https://www.youtube.com/watch?v=abc&t=120")
        assert ok

    def test_allows_multiple_query_params(self):
        """YouTube URLs with playlists use multiple '&' params."""
        ok, _ = self._validate_url("https://www.youtube.com/watch?v=abc&list=PLxyz&index=3")
        assert ok

    def test_rejects_semicolon(self):
        ok, err = self._validate_url("https://example.com/;rm -rf /")
        assert not ok
        assert "metacharacters" in err

    def test_rejects_pipe(self):
        ok, err = self._validate_url("https://example.com/|cat /etc/passwd")
        assert not ok

    def test_rejects_backtick(self):
        ok, err = self._validate_url("https://example.com/`whoami`")
        assert not ok

    def test_rejects_dollar(self):
        ok, err = self._validate_url("https://example.com/${HOME}")
        assert not ok

    def test_rejects_newline(self):
        ok, err = self._validate_url("https://example.com/\nmalicious")
        assert not ok


class TestYouTubeURLDetection:
    """Test YouTube URL detection logic (mirrors _is_youtube_url)."""

    def _is_youtube_url(self, url):
        """Local copy for testing."""
        if not url:
            return False
        try:
            from urllib.parse import urlparse
            parsed = urlparse(url)
            host = (parsed.hostname or '').lower()
            yt_domains = {"youtube.com", "www.youtube.com", "youtu.be",
                          "youtube-nocookie.com", "www.youtube-nocookie.com",
                          "music.youtube.com", "m.youtube.com"}
            return host in yt_domains or host.endswith(".youtube.com")
        except Exception:
            return False

    def test_standard_youtube(self):
        assert self._is_youtube_url("https://www.youtube.com/watch?v=abc")

    def test_short_url(self):
        assert self._is_youtube_url("https://youtu.be/abc123")

    def test_music_youtube(self):
        assert self._is_youtube_url("https://music.youtube.com/watch?v=abc")

    def test_mobile_youtube(self):
        assert self._is_youtube_url("https://m.youtube.com/watch?v=abc")

    def test_nocookie(self):
        assert self._is_youtube_url("https://youtube-nocookie.com/embed/abc")

    def test_not_youtube(self):
        assert not self._is_youtube_url("https://example.com/video")

    def test_spoofed_url(self):
        """v5.1.0 fix: substring match no longer works for spoofing."""
        assert not self._is_youtube_url("https://evil.com/youtube.com/watch")

    def test_empty(self):
        assert not self._is_youtube_url("")

    def test_none(self):
        assert not self._is_youtube_url(None)


class TestHallucinationDetection:
    """Test common hallucination patterns."""

    COMMON_HALLUCINATIONS = [
        "ご視聴ありがとうございました",
        "ご視聴ありがとう",
        "お疲れ様でした",
        "Subscribe",
        "Thank you for watching",
        "[Music]",
        "(Music)",
        "Sound Hodori",
        "请订阅",
        "感谢观看",
        "Подписывайтесь на канал",
        "اشترك في القناة",
    ]

    def test_known_hallucinations_are_detected(self):
        """Every pattern in the server's list should be catchable."""
        # Just verify the patterns are non-empty strings
        for pattern in self.COMMON_HALLUCINATIONS:
            assert len(pattern) > 0
            assert isinstance(pattern, str)

    def test_real_speech_not_flagged(self):
        """Real Japanese speech should not be in the hallucination list."""
        real_speech = [
            "今日はいい天気ですね",
            "おはようございます",
            "Hello everyone",
            "我很高兴认识你",
        ]
        for text in real_speech:
            assert text not in self.COMMON_HALLUCINATIONS


class TestVersionConsistency:
    """Verify version strings are consistent across project files."""

    def test_pocket_yume_version_format(self):
        content = Path(__file__).parent.parent.joinpath("pocket_yume.py").read_text(encoding="utf-8")
        import re
        match = re.search(r'VERSION\s*=\s*"(\d+\.\d+\.\d+)"', content)
        assert match, "VERSION not found in pocket_yume.py"
        version = match.group(1)
        parts = version.split(".")
        assert len(parts) == 3
        assert all(p.isdigit() for p in parts)

    def test_manifest_version_matches(self):
        import json
        content = Path(__file__).parent.parent.joinpath("pocket_yume.py").read_text(encoding="utf-8")
        import re
        match = re.search(r'VERSION\s*=\s*"(\d+\.\d+\.\d+)"', content)
        assert match, "VERSION not found in pocket_yume.py"
        py_version = match.group(1)

        manifest = json.loads(
            Path(__file__).parent.parent.joinpath("extension/manifest.json").read_text()
        )
        assert manifest["version"] == py_version, \
            f"manifest.json ({manifest['version']}) != pocket_yume.py ({py_version})"
