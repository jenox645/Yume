"""Stream URLs, stream segments and yt-dlp's auth arguments.

`subprocess.run` is faked; each test scripts what yt-dlp and ffmpeg answer.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

import _state  # noqa: E402
import _stream  # noqa: E402
import _ytdlp  # noqa: E402

YT = "https://www.youtube.com/watch?v=abc"
SITE = "https://example.com/watch/1"
STREAM = "https://cdn.example.com/a.m4a"
COOKIE_ERROR = "ERROR: Could not copy Chrome cookie database"


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    _state.stream_url_cache.clear()
    monkeypatch.setattr(_ytdlp, "_cookies_failed_at", 0.0)
    monkeypatch.setattr(_state, "youtube_auth_method", "cookies")
    monkeypatch.setattr(_state, "cookies_browser", "firefox")
    monkeypatch.setattr(_ytdlp.platform, "system", lambda: "Windows")
    yield
    _state.stream_url_cache.clear()


def _reply(returncode=0, stdout="", stderr=""):
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


def _fmt(cmd):
    return cmd[cmd.index("--format") + 1] if "--format" in cmd else None


# ── build_auth_args ───────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "url, method, cookies",
    [
        (YT, "cookies", True),
        (YT, "deno", True),  # backup for non-download calls
        (YT, "none", False),
        (SITE, "cookies", True),
        (SITE, "deno", False),
        (SITE, "none", False),
    ],
)
def test_auth_args_by_site_and_method(monkeypatch, url, method, cookies):
    monkeypatch.setattr(_state, "youtube_auth_method", method)
    expected = ["--cookies-from-browser", "firefox"] if cookies else []
    assert _ytdlp.build_auth_args(url) == expected


def test_auth_args_are_empty_while_cookies_are_unreadable(monkeypatch):
    monkeypatch.setattr(_ytdlp, "_cookies_failed_at", time.time())
    assert _ytdlp.build_auth_args(YT) == []


def test_cookies_that_cannot_be_resolved_only_matter_when_they_are_the_auth_method(monkeypatch):
    def broken():
        raise RuntimeError("no browser")

    monkeypatch.setattr(_ytdlp, "resolve_browser_cookies", broken)
    monkeypatch.setattr(_state, "youtube_auth_method", "deno")
    assert _ytdlp.build_auth_args(YT) == []  # only a backup in deno mode
    monkeypatch.setattr(_state, "youtube_auth_method", "cookies")
    with pytest.raises(RuntimeError):
        _ytdlp.build_auth_args(YT)


# ── resolve_browser_cookies ───────────────────────────────────────────────────


@pytest.fixture
def firefox_home(monkeypatch, tmp_path):
    """A Linux home; returns (flatpak dir, native dir), neither created yet."""
    monkeypatch.setattr(_ytdlp.platform, "system", lambda: "Linux")
    monkeypatch.setattr(_ytdlp.os.path, "expanduser", lambda p: str(tmp_path / p.lstrip("~/")))
    return tmp_path / ".var/app/org.mozilla.firefox/.mozilla/firefox", tmp_path / ".mozilla/firefox"


def test_cookies_browser_other_than_firefox_is_used_as_is(monkeypatch):
    monkeypatch.setattr(_state, "cookies_browser", "chrome")
    monkeypatch.setattr(_ytdlp.platform, "system", lambda: "Linux")
    assert _ytdlp.resolve_browser_cookies() == "chrome"
    monkeypatch.setattr(_state, "cookies_browser", "")
    assert _ytdlp.resolve_browser_cookies() == "firefox"  # the path hint below still applies on Linux


def test_flatpak_firefox_alone_gets_a_path_hint(firefox_home):
    flatpak, _native = firefox_home
    flatpak.mkdir(parents=True)
    assert _ytdlp.resolve_browser_cookies() == f"firefox:{flatpak}"


def test_native_firefox_needs_no_hint(firefox_home):
    _flatpak, native = firefox_home
    native.mkdir(parents=True)
    assert _ytdlp.resolve_browser_cookies() == "firefox"


def test_with_both_firefoxes_the_more_recent_one_wins(firefox_home):
    flatpak, native = firefox_home
    flatpak.mkdir(parents=True)
    native.mkdir(parents=True)
    assert _ytdlp.resolve_browser_cookies() == "firefox"  # no profiles.ini either side
    (flatpak / "profiles.ini").write_text("x")
    assert _ytdlp.resolve_browser_cookies() == f"firefox:{flatpak}"  # only the Flatpak one has profiles
    (native / "profiles.ini").write_text("x")
    os.utime(flatpak / "profiles.ini", (1000, 1000))
    os.utime(native / "profiles.ini", (2000, 2000))
    assert _ytdlp.resolve_browser_cookies() == "firefox"
    os.utime(flatpak / "profiles.ini", (3000, 3000))
    assert _ytdlp.resolve_browser_cookies() == f"firefox:{flatpak}"


# ── get_stream_url ────────────────────────────────────────────────────────────


def test_a_missing_format_tries_the_next_format_in_order(monkeypatch):
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        if len(calls) < 3:
            return _reply(1, stderr="ERROR: Requested format is not available")
        return _reply(0, stdout=f"{STREAM}\nMusic\n")

    monkeypatch.setattr(_stream.subprocess, "run", run)
    assert _stream.get_stream_url(YT) == STREAM
    assert [_fmt(c) for c in calls] == ["bestaudio/bestaudio*/best", "bestaudio/best", None]
    assert _stream.stream_category(YT) == "Music"


def test_any_other_error_stops_at_the_first_format(monkeypatch, capsys):
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return _reply(1, stderr="ERROR: Video unavailable")

    monkeypatch.setattr(_stream.subprocess, "run", run)
    assert _stream.get_stream_url(YT) is None
    assert len(calls) == 1
    assert "yt-dlp get-url failed: ERROR: Video unavailable" in capsys.readouterr().out


def test_a_get_url_timeout_gives_up(monkeypatch):
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        raise subprocess.TimeoutExpired(cmd, 30)

    monkeypatch.setattr(_stream.subprocess, "run", run)
    assert _stream.get_stream_url(YT) is None
    assert len(calls) == 1


def test_no_yt_dlp_means_no_stream_url(monkeypatch, capsys):
    """It raised: the job failed with "FileNotFoundError: [WinError 2]..." for a video with a duration."""

    def run(cmd, **kw):
        raise FileNotFoundError("yt-dlp")

    monkeypatch.setattr(_stream.subprocess, "run", run)
    assert _stream.get_stream_url(YT) is None
    assert "could not run" in capsys.readouterr().out


def test_a_full_url_cache_drops_the_oldest_entry(monkeypatch):
    monkeypatch.setattr(_state, "STREAM_URL_CACHE_MAX", 2)
    now = time.time()
    _state.stream_url_cache.update(
        {
            "https://old.example/a": {"stream_url": "x", "timestamp": now - 100},
            "https://new.example/b": {"stream_url": "y", "timestamp": now},
        }
    )
    monkeypatch.setattr(_stream.subprocess, "run", lambda cmd, **kw: _reply(0, stdout=f"{STREAM}\nMusic\n"))
    assert _stream.get_stream_url(YT) == STREAM
    assert set(_state.stream_url_cache) == {"https://new.example/b", YT}


def test_get_url_command_keeps_the_url_after_the_separator(monkeypatch):
    seen = []

    def run(cmd, **kw):
        seen.append(cmd)
        return _reply(0, stdout=f"{STREAM}\nMusic\n")

    monkeypatch.setattr(_stream.subprocess, "run", run)
    _stream.get_stream_url(YT)
    assert seen[0][-2:] == ["--", YT]
    assert seen[0].count("--print") == 2 and "--no-exec" in seen[0] and "--no-playlist" in seen[0]
    assert seen[0][seen[0].index("--cookies-from-browser") + 1] == "firefox"


# ── download_audio_segment ────────────────────────────────────────────────────


@pytest.fixture
def seg_dir(monkeypatch, tmp_path):
    """mkdtemp under tmp_path: yields the dirs made."""
    made = []
    real_mkdtemp = tempfile.mkdtemp

    def mkdtemp(prefix=""):
        made.append(real_mkdtemp(prefix=prefix, dir=str(tmp_path)))
        return made[-1]

    monkeypatch.setattr(_stream.tempfile, "mkdtemp", mkdtemp)
    return made


class _Download:
    """What Popen gives for yt-dlp."""

    def __init__(self):
        self.stdout = types.SimpleNamespace(close=lambda: None)
        self.stopped = False

    def terminate(self):
        self.stopped = True

    def wait(self, timeout=None):
        return 0


def _pipe(monkeypatch, returncode=0, audio=b"x" * 1000, error=None):
    """Fake the two processes of a segment; returns what they were asked."""
    seen = types.SimpleNamespace(ytdlp=None, ffmpeg=None, download=_Download())

    def popen(cmd, **kw):
        seen.ytdlp = cmd
        return seen.download

    def run(cmd, **kw):
        seen.ffmpeg = cmd
        if error:
            raise error
        Path(cmd[-1]).write_bytes(audio)
        return _reply(returncode=returncode, stderr=b"broken")

    monkeypatch.setattr(_stream.subprocess, "Popen", popen)
    monkeypatch.setattr(_stream.subprocess, "run", run)
    return seen


def test_an_invalid_url_never_makes_a_temp_dir(seg_dir):
    assert _stream.download_audio_segment("file:///etc/passwd", 0, 30) is None
    assert not any(Path(d).exists() for d in seg_dir)


def test_a_segment_is_read_from_one_ordinary_download(monkeypatch, seg_dir):
    seen = _pipe(monkeypatch)
    path = _stream.download_audio_segment("https://www.youtube.com/watch?v=abc", 600, 30)
    assert Path(path).parent.name.startswith("yume_")
    assert seen.ytdlp[seen.ytdlp.index("--output") + 1] == "-" and seen.ytdlp[-2:] == [
        "--",
        "https://www.youtube.com/watch?v=abc",
    ]
    assert not any("download-sections" in a or "range=" in a for a in seen.ytdlp)
    # ffmpeg decodes up to the start: -ss after the input, not a seek in the stream
    assert seen.ffmpeg.index("-i") < seen.ffmpeg.index("-ss") and seen.ffmpeg[seen.ffmpeg.index("-ss") + 1] == "600"
    assert seen.download.stopped  # the download ends with the segment


@pytest.mark.parametrize("kw", [{"returncode": 1}, {"audio": b"x" * 78}])
def test_a_segment_without_audio_cleans_up(monkeypatch, seg_dir, kw):
    seen = _pipe(monkeypatch, **kw)
    assert _stream.download_audio_segment("https://www.youtube.com/watch?v=abc", 0, 30) is None
    assert not any(Path(d).exists() for d in seg_dir) and seen.download.stopped


@pytest.mark.parametrize("error", [subprocess.TimeoutExpired("ffmpeg", 60), OSError("no ffmpeg")])
def test_a_segment_that_blows_up_cleans_up(monkeypatch, seg_dir, error):
    seen = _pipe(monkeypatch, error=error)
    assert _stream.download_audio_segment("https://www.youtube.com/watch?v=abc", 0, 30) is None
    assert not any(Path(d).exists() for d in seg_dir) and seen.download.stopped
