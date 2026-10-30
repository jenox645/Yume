"""The stream preview and vocal isolation limit, from a live run on real YouTube videos."""

from __future__ import annotations

import sys
import types
import urllib.error
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

import _audio  # noqa: E402
import _separate  # noqa: E402
import _state  # noqa: E402


def _stream_fetch(tmp_path, url, *, body=b"audio", http_error=None, ffmpeg_rc=0):
    """_fetch_stream_start with the network and ffmpeg faked: (result, URLs requested)."""
    asked = []

    class Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, n):
            return body

    def urlopen(req, timeout=0):
        asked.append(req.full_url)
        if http_error:
            raise http_error
        return Resp()

    def run(cmd, **kw):
        Path(cmd[-1]).write_bytes(b"RIFF")
        return types.SimpleNamespace(returncode=ffmpeg_rc, stderr=b"", stdout=b"")

    with patch.object(_audio.urllib.request, "urlopen", urlopen), patch.object(_audio.subprocess, "run", run):
        return _audio._fetch_stream_start(url, 30.0, str(tmp_path / "a.wav")), asked


GV = "https://rr3---sn-x.googlevideo.com/videoplayback?itag=251"


def test_the_preview_asks_youtube_for_a_range_it_accepts(tmp_path):
    ok, asked = _stream_fetch(tmp_path, GV)
    assert ok and len(asked) == 1
    end = int(asked[0].rsplit("range=0-", 1)[1])
    assert 30 * 20_000 <= end < 800_000  # 30 s at 160 kbps fits; YouTube answers 403 above ~800-950 KB
    assert not list(tmp_path.glob("*.part"))  # the partial download is gone


def test_the_preview_range_is_only_for_youtube_streams_and_failures_fall_through(tmp_path):
    ok, asked = _stream_fetch(tmp_path, "https://cdn.example.com/a.m4a")
    assert not ok and not asked
    forbidden = urllib.error.HTTPError(GV, 403, "Forbidden", None, None)
    assert _stream_fetch(tmp_path, GV, http_error=forbidden)[0] is False
    assert _stream_fetch(tmp_path, GV, ffmpeg_rc=1)[0] is False
    assert not list(tmp_path.glob("*.part"))


def test_a_stream_ffmpeg_cannot_read_falls_back_to_yt_dlp(tmp_path, monkeypatch):
    """The fallback ran only when no stream URL was found, never after ffmpeg failed."""
    monkeypatch.setattr(_audio, "get_stream_url", lambda url: "https://cdn.example.com/a.m4a")
    monkeypatch.setattr(
        _audio.subprocess, "run", lambda *a, **k: types.SimpleNamespace(returncode=1, stderr=b"403", stdout=b"")
    )
    with patch.object(_audio, "_download_audio_segment_fallback", return_value="seg.wav") as fb:
        assert _audio.download_audio_segment("https://www.youtube.com/watch?v=abc", 60, 25) == "seg.wav"
    fb.assert_called_once()


def test_talks_of_10_to_15_minutes_are_not_separated(monkeypatch):
    monkeypatch.setattr(_state, "vocal_isolation", True)
    monkeypatch.setattr(_separate, "available", lambda: (True, ""))
    assert _separate.wanted(9 * 60) and not _separate.wanted(11 * 60)
