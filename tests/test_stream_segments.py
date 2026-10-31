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
from unittest.mock import patch

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


def _ffmpeg_writes(monkeypatch, returncode=0, stderr=b"", calls=None):
    def run(cmd, **kw):
        if calls is not None:
            calls.append(cmd)
        if returncode == 0:
            Path(cmd[-1]).write_bytes(b"x" * 2048)
        return types.SimpleNamespace(returncode=returncode, stderr=stderr, stdout=b"")

    monkeypatch.setattr(_stream.subprocess, "run", run)


def test_an_invalid_url_never_makes_a_temp_dir(seg_dir):
    assert _stream.download_audio_segment("file:///etc/passwd", 0, 25) is None
    assert seg_dir == []


def test_no_stream_url_uses_the_fallback_in_a_yume_dir(monkeypatch, seg_dir):
    monkeypatch.setattr(_stream, "get_stream_url", lambda url: None)
    with patch.object(_stream, "_download_audio_segment_fallback", return_value="seg.wav") as fb:
        assert _stream.download_audio_segment(YT, 60, 25) == "seg.wav"
    url, start, duration, out = fb.call_args.args
    assert (url, start, duration) == (YT, 60, 25)
    assert Path(out).name == "segment.wav" and Path(out).parent.name.startswith("yume_")
    assert os.path.isdir(seg_dir[0])  # the caller's remove_temp deletes it


def test_a_fallback_that_fails_removes_the_temp_dir(monkeypatch, seg_dir):
    monkeypatch.setattr(_stream, "get_stream_url", lambda url: None)
    with patch.object(_stream, "_download_audio_segment_fallback", return_value=None):
        assert _stream.download_audio_segment(YT, 60, 25) is None
    assert not os.path.exists(seg_dir[0])


def test_an_invalid_stream_url_is_forgotten_and_the_fallback_runs(monkeypatch, seg_dir):
    _state.stream_url_cache[YT] = {"stream_url": "file:///x", "timestamp": time.time()}
    monkeypatch.setattr(_stream, "get_stream_url", lambda url: "file:///x")
    with patch.object(_stream, "_download_audio_segment_fallback", return_value=None) as fb:
        assert _stream.download_audio_segment(YT, 60, 25) is None
    fb.assert_called_once()
    assert YT not in _state.stream_url_cache
    assert not os.path.exists(seg_dir[0])


def test_ffmpeg_extracts_the_segment_from_the_stream(monkeypatch, seg_dir, capsys):
    monkeypatch.setattr(_stream, "get_stream_url", lambda url: STREAM)
    calls = []
    _ffmpeg_writes(monkeypatch, calls=calls)
    path = _stream.download_audio_segment(YT, 60, 25)
    assert Path(path).name == "segment.wav" and Path(path).exists()
    (cmd,) = calls
    assert cmd[cmd.index("-ss") + 1] == "60" and cmd[cmd.index("-t") + 1] == "25"
    assert cmd[cmd.index("-i") + 1] == STREAM
    assert cmd[cmd.index("-protocol_whitelist") + 1] == _state.FFMPEG_PROTOCOL_WHITELIST
    assert "Audio segment downloaded: 2.0 KB" in capsys.readouterr().out


def test_ffmpeg_failure_forgets_the_stream_url_and_cleans_up(monkeypatch, seg_dir, capsys):
    _state.stream_url_cache[YT] = {"stream_url": STREAM, "timestamp": time.time()}
    monkeypatch.setattr(_stream, "get_stream_url", lambda url: STREAM)
    _ffmpeg_writes(monkeypatch, returncode=1, stderr=b"403 Forbidden")
    with patch.object(_stream, "_download_audio_segment_fallback", return_value=None):
        assert _stream.download_audio_segment(YT, 60, 25) is None
    assert YT not in _state.stream_url_cache
    assert not os.path.exists(seg_dir[0])
    assert "ffmpeg failed: 403 Forbidden" in capsys.readouterr().out


def test_the_start_of_a_youtube_stream_is_fetched_by_range(monkeypatch, seg_dir):
    monkeypatch.setattr(_stream, "get_stream_url", lambda url: "https://rr1.googlevideo.com/a?x=1")
    calls = []
    _ffmpeg_writes(monkeypatch, calls=calls)
    with patch.object(_stream, "_fetch_stream_start", return_value=True) as start:
        path = _stream.download_audio_segment(YT, 0, 30.0)
    assert path == start.call_args.args[2]
    assert calls == []  # no ffmpeg on the whole URL


def test_only_the_very_start_goes_by_range(monkeypatch, seg_dir):
    monkeypatch.setattr(_stream, "get_stream_url", lambda url: "https://rr1.googlevideo.com/a?x=1")
    _ffmpeg_writes(monkeypatch)
    with patch.object(_stream, "_fetch_stream_start", return_value=True) as start:
        assert _stream.download_audio_segment(YT, 25, 30.0)
    start.assert_not_called()


@pytest.mark.parametrize("error", [subprocess.TimeoutExpired("ffmpeg", 60), RuntimeError("boom")])
def test_a_segment_that_blows_up_cleans_up(monkeypatch, seg_dir, error):
    monkeypatch.setattr(_stream, "get_stream_url", lambda url: STREAM)

    def run(cmd, **kw):
        raise error

    monkeypatch.setattr(_stream.subprocess, "run", run)
    assert _stream.download_audio_segment(YT, 60, 25) is None
    assert not os.path.exists(seg_dir[0])


# ── the yt-dlp fallback for one segment ───────────────────────────────────────


def test_fallback_cuts_the_section_with_yt_dlp(monkeypatch, tmp_path):
    out = str(tmp_path / "segment.wav")
    seen = []

    def run(cmd, **kw):
        seen.append(cmd)
        Path(out).write_bytes(b"x")
        return types.SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr(_stream.subprocess, "run", run)
    assert _stream._download_audio_segment_fallback(YT, 60, 25, out) == out
    (cmd,) = seen
    assert cmd[cmd.index("--download-sections") + 1] == "*60-85"
    assert cmd[cmd.index("-o") + 1] == out.replace(".wav", ".%(ext)s")
    assert cmd[cmd.index("--postprocessor-args") + 1] == _state.FFMPEG_AUDIO_OPTS
    assert cmd[-2:] == ["--", YT]


def test_fallback_retries_once_without_unreadable_cookies(monkeypatch, tmp_path):
    out = str(tmp_path / "segment.wav")
    seen = []

    def run(cmd, **kw):
        seen.append(cmd)
        return types.SimpleNamespace(returncode=1, stderr=COOKIE_ERROR.encode())

    monkeypatch.setattr(_stream.subprocess, "run", run)
    assert _stream._download_audio_segment_fallback(YT, 60, 25, out) is None
    assert ["--cookies-from-browser" in c for c in seen] == [True, False]


def test_fallback_reports_a_failure_and_survives_an_exception(monkeypatch, tmp_path, capsys):
    out = str(tmp_path / "segment.wav")
    monkeypatch.setattr(
        _stream.subprocess, "run", lambda cmd, **kw: types.SimpleNamespace(returncode=1, stderr=b"ERROR: nope")
    )
    assert _stream._download_audio_segment_fallback(YT, 60, 25, out) is None
    assert "Fallback also failed: ERROR: nope" in capsys.readouterr().out

    def run(cmd, **kw):
        raise OSError("no yt-dlp")

    monkeypatch.setattr(_stream.subprocess, "run", run)
    assert _stream._download_audio_segment_fallback(YT, 60, 25, out) is None
    assert "Fallback error: no yt-dlp" in capsys.readouterr().out
