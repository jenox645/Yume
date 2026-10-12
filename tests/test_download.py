"""The full-audio download: yt-dlp auth/format strategies and the ffmpeg fallbacks.

`subprocess.run` is faked; each test scripts what yt-dlp and ffmpeg answer and
checks which commands ran and what came back.
"""

from __future__ import annotations

import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

import _audio  # noqa: E402
import _state  # noqa: E402

YT = "https://www.youtube.com/watch?v=abc"
DIRECT = "https://cdn.example.com/show/master.m3u8"


class Tools:
    """Fake yt-dlp / ffmpeg. `ytdlp` and `ffmpeg` get the command and return
    (returncode, stderr, write_output) or raise."""

    def __init__(self, monkeypatch, ytdlp=None, ffmpeg=None, stream_url="https://rr1.googlevideo.com/a"):
        self.calls: list[list[str]] = []
        self.ytdlp = ytdlp or (lambda cmd: (1, "ERROR: Video unavailable", False))
        self.ffmpeg = ffmpeg or (lambda cmd: (1, "ffmpeg: failed", False))
        monkeypatch.setattr(_audio.subprocess, "run", self.run)
        monkeypatch.setattr(_audio, "get_stream_url", lambda url: stream_url)
        monkeypatch.setattr(_audio, "_cookies_failed_at", 0.0)

    def run(self, cmd, **kw):
        self.calls.append(cmd)
        if cmd[0] == "ffmpeg":
            rc, err, write = self.ffmpeg(cmd)
            if write:
                Path(cmd[-1]).write_bytes(b"\0" * 20000)
        else:
            rc, err, write = self.ytdlp(cmd)
            if write:
                out = cmd[cmd.index("-o") + 1].replace("%(ext)s", "wav")
                Path(out).write_bytes(b"\0" * 20000)
        return types.SimpleNamespace(returncode=rc, stderr=err, stdout="")

    def ytdlp_calls(self):
        return [c for c in self.calls if c[0] != "ffmpeg"]

    def ffmpeg_calls(self):
        return [c for c in self.calls if c[0] == "ffmpeg"]


@pytest.fixture
def no_auth(monkeypatch):
    monkeypatch.setattr(_state, "youtube_auth_method", "none")
    monkeypatch.setattr(_audio, "resolve_browser_cookies", lambda: (_ for _ in ()).throw(RuntimeError("no browser")))


def _fmt(cmd):
    return "bestaudio" if "-f" in cmd else "nofmt"


def test_first_strategy_success_returns_the_wav(monkeypatch, no_auth):
    tools = Tools(monkeypatch, ytdlp=lambda cmd: (0, "", True))
    path, err = _audio.download_full_audio(YT)
    assert err is None
    assert Path(path).name == "full_audio.wav" and Path(path).exists()
    assert len(tools.calls) == 1
    cmd = tools.calls[0]
    assert cmd[cmd.index("--postprocessor-args") + 1] == "ffmpeg:-ar 16000 -ac 1"
    assert cmd[-2:] == ["--", YT]
    _audio.remove_temp(path)


def test_stereo_asks_for_44k_stereo(monkeypatch, no_auth):
    tools = Tools(monkeypatch, ytdlp=lambda cmd: (0, "", True))
    path, _ = _audio.download_full_audio(YT, stereo=True)
    cmd = tools.calls[0]
    assert cmd[cmd.index("--postprocessor-args") + 1] == "ffmpeg:-ar 44100 -ac 2"
    _audio.remove_temp(path)


def test_missing_audio_only_format_retries_without_a_format(monkeypatch, no_auth):
    def ytdlp(cmd):
        if "-f" in cmd:
            return 1, "ERROR: Requested format is not available", False
        return 0, "", True

    tools = Tools(monkeypatch, ytdlp=ytdlp)
    path, err = _audio.download_full_audio(YT)
    assert err is None
    assert [_fmt(c) for c in tools.calls] == ["bestaudio", "nofmt"]
    _audio.remove_temp(path)


def test_auth_errors_skip_to_the_next_strategy(monkeypatch):
    monkeypatch.setattr(_state, "youtube_auth_method", "cookies")
    monkeypatch.setattr(_audio, "resolve_browser_cookies", lambda: "firefox")
    tools = Tools(monkeypatch, ytdlp=lambda cmd: (1, "ERROR: Sign in to confirm you're not a bot", False))
    path, err = _audio.download_full_audio(YT)
    assert path is None
    # cookies+default, cookies+tv,web, cookies+mweb, no-auth: one format pass each
    assert [_fmt(c) for c in tools.ytdlp_calls()] == ["bestaudio"] * 4
    assert "sign-in" in err.lower()


def test_other_errors_try_both_format_passes(monkeypatch, no_auth):
    tools = Tools(monkeypatch, ytdlp=lambda cmd: (1, "ERROR: something odd", False))
    path, err = _audio.download_full_audio(YT)
    assert path is None
    assert [_fmt(c) for c in tools.ytdlp_calls()] == ["bestaudio", "nofmt"]
    assert err == "ERROR: something odd"


def test_a_timeout_stops_everything(monkeypatch, no_auth):
    def ytdlp(cmd):
        raise subprocess.TimeoutExpired(cmd, 900)

    tools = Tools(monkeypatch, ytdlp=ytdlp)
    path, err = _audio.download_full_audio(YT)
    assert path is None
    assert "timed out" in err
    assert len(tools.calls) == 1  # no second format pass, no ffmpeg fallback


def test_youtube_falls_back_to_ffmpeg_on_the_stream_url(monkeypatch, no_auth):
    tools = Tools(monkeypatch, ffmpeg=lambda cmd: (0, "", True))
    path, err = _audio.download_full_audio(YT)
    assert err is None
    assert Path(path).name == "full_audio_stream.wav"
    (ff,) = tools.ffmpeg_calls()
    assert ff[ff.index("-i") + 1] == "https://rr1.googlevideo.com/a"
    _audio.remove_temp(path)


def test_youtube_without_a_stream_url_reports_the_ytdlp_error(monkeypatch, no_auth):
    tools = Tools(monkeypatch, stream_url=None)
    path, err = _audio.download_full_audio(YT)
    assert path is None and err == "This video is unavailable or private."
    assert tools.ffmpeg_calls() == []


def test_other_sites_fall_back_to_ffmpeg_on_the_url(monkeypatch, no_auth):
    tools = Tools(monkeypatch, ffmpeg=lambda cmd: (0, "", True))
    path, err = _audio.download_full_audio(DIRECT)
    assert err is None
    assert Path(path).name == "full_audio_ffmpeg.wav"
    (ff,) = tools.ffmpeg_calls()
    assert ff[ff.index("-i") + 1] == DIRECT
    _audio.remove_temp(path)


def test_a_tiny_ffmpeg_output_is_a_failure(monkeypatch, no_auth):
    def ffmpeg(cmd):
        Path(cmd[-1]).write_bytes(b"\0" * 100)
        return 0, "", False

    Tools(monkeypatch, ffmpeg=ffmpeg)
    path, _err = _audio.download_full_audio(DIRECT)
    assert path is None


def test_failure_removes_the_temp_dir(monkeypatch, no_auth):
    made = []
    real_mkdtemp = _audio.tempfile.mkdtemp

    def mkdtemp(**kw):
        made.append(real_mkdtemp(**kw))
        return made[-1]

    monkeypatch.setattr(_audio.tempfile, "mkdtemp", mkdtemp)
    Tools(monkeypatch)
    path, _err = _audio.download_full_audio(DIRECT)
    assert path is None
    assert made and not os.path.exists(made[0])
