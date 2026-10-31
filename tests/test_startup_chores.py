"""What the server does around Whisper at startup: the yt-dlp self-update, Deno
auth and the bgutil server. Subprocesses and the clock are faked."""

from __future__ import annotations

import os
import subprocess
import sys
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

import _bgutil  # noqa: E402
import _startup  # noqa: E402
import _state  # noqa: E402

OLD = "2026.01.02"


def _reply(returncode=0, stdout="", stderr=""):
    return types.SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)


# ── yt-dlp self-update ────────────────────────────────────────────────────────


@pytest.fixture
def ytdlp_exe(monkeypatch, tmp_path):
    exe = tmp_path / "yt-dlp"
    exe.write_text("")
    monkeypatch.setattr(_startup.shutil, "which", lambda name: str(exe))
    monkeypatch.setattr(_state, "youtube_auth_method", "cookies")
    return exe


def _ytdlp_answers(monkeypatch, update, version=OLD):
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return update if cmd[-1] == "-U" else _reply(0, stdout=f"{version}\n")

    monkeypatch.setattr(_startup.subprocess, "run", run)
    return calls


def test_a_version_that_is_no_date_is_left_alone(monkeypatch):
    def run(cmd, **kw):
        raise AssertionError("no update for an unknown version")

    monkeypatch.setattr(_startup.subprocess, "run", run)
    assert _startup.maybe_update_ytdlp("?") == "?"


def test_a_pip_install_is_left_to_the_cli(monkeypatch, ytdlp_exe):
    calls = _ytdlp_answers(monkeypatch, _reply(0))
    monkeypatch.setattr(_state, "youtube_auth_method", "deno")
    assert _startup.maybe_update_ytdlp(OLD) == OLD
    assert calls == []


def test_no_standalone_yt_dlp_means_nothing_to_update(monkeypatch, ytdlp_exe):
    calls = _ytdlp_answers(monkeypatch, _reply(0))
    monkeypatch.setattr(_startup.shutil, "which", lambda name: None)
    assert _startup.maybe_update_ytdlp(OLD) == OLD
    assert calls == []


def test_an_update_reports_the_new_version_and_stamps_the_day(monkeypatch, ytdlp_exe):
    _ytdlp_answers(monkeypatch, _reply(0), version="2026.09.01")
    assert _startup.maybe_update_ytdlp(OLD) == f"2026.09.01 (updated from {OLD})"
    assert ytdlp_exe.with_name(".yt-dlp-update-check").exists()


def test_a_stamp_from_yesterday_allows_another_check(monkeypatch, ytdlp_exe):
    stamp = ytdlp_exe.with_name(".yt-dlp-update-check")
    stamp.touch()
    os.utime(stamp, (time.time() - 90000, time.time() - 90000))
    calls = _ytdlp_answers(monkeypatch, _reply(0))
    assert _startup.maybe_update_ytdlp(OLD) == f"{OLD} (latest)"
    assert [str(ytdlp_exe), "-U"] in calls


def test_a_failed_update_says_so_and_does_not_stamp(monkeypatch, ytdlp_exe, capsys):
    _ytdlp_answers(monkeypatch, _reply(1, stdout="line 1\n", stderr="ERROR: no permission\n"))
    assert _startup.maybe_update_ytdlp(OLD) == f"{OLD} (could not update — Tools → yt-dlp)"
    assert "update failed: ERROR: no permission" in capsys.readouterr().out
    assert not ytdlp_exe.with_name(".yt-dlp-update-check").exists()


def test_a_silent_failed_update_is_reported_too(monkeypatch, ytdlp_exe, capsys):
    _ytdlp_answers(monkeypatch, _reply(1))
    _startup.maybe_update_ytdlp(OLD)
    assert "update failed: no output" in capsys.readouterr().out


@pytest.mark.parametrize("error", [OSError("cannot run"), subprocess.TimeoutExpired("yt-dlp", 180)])
def test_an_update_that_blows_up_keeps_the_version(monkeypatch, ytdlp_exe, capsys, error):
    def run(cmd, **kw):
        raise error

    monkeypatch.setattr(_startup.subprocess, "run", run)
    assert _startup.maybe_update_ytdlp(OLD) == f"{OLD} (could not update — Tools → yt-dlp)"
    assert "update failed:" in capsys.readouterr().out


# ── Deno auth ─────────────────────────────────────────────────────────────────


@pytest.fixture
def deno_mode(monkeypatch):
    monkeypatch.setattr(_state, "youtube_auth_method", "deno")
    monkeypatch.setattr(_state, "cookies_browser", "chrome")
    registered = []
    monkeypatch.setattr(_startup.atexit, "register", registered.append)
    return registered


def test_other_auth_methods_never_look_for_deno(monkeypatch):
    def run(cmd, **kw):
        raise AssertionError("deno must not run")

    monkeypatch.setattr(_state, "youtube_auth_method", "cookies")
    monkeypatch.setattr(_startup.subprocess, "run", run)
    _startup.setup_deno_auth(None)


@pytest.mark.parametrize("system, browser", [("Linux", "firefox"), ("Windows", "chrome")])
def test_without_deno_auth_switches_to_cookies(monkeypatch, deno_mode, capsys, system, browser):
    def run(cmd, **kw):
        raise FileNotFoundError("deno")

    monkeypatch.setattr(_startup.subprocess, "run", run)
    monkeypatch.setattr(_startup.platform, "system", lambda: system)
    _startup.setup_deno_auth(None)
    assert _state.youtube_auth_method == "cookies" and _state.cookies_browser == browser
    assert f"Now using: cookies ({browser})" in capsys.readouterr().out


def test_a_deno_that_fails_counts_as_missing(monkeypatch, deno_mode):
    monkeypatch.setattr(_startup.subprocess, "run", lambda cmd, **kw: _reply(1))
    _startup.setup_deno_auth(None)
    assert _state.youtube_auth_method == "cookies"


def test_with_deno_the_bgutil_server_starts_and_stops_at_exit(monkeypatch, deno_mode, capsys):
    monkeypatch.setattr(_startup.subprocess, "run", lambda cmd, **kw: _reply(0, stdout="deno 2.1.4\nv8 12\n"))
    monkeypatch.setattr(_bgutil, "start_bgutil_server", lambda: True)
    _startup.setup_deno_auth(None)
    assert _state.youtube_auth_method == "deno"
    assert _bgutil.stop_bgutil_server in deno_mode
    assert "Deno:             deno 2.1.4\n" in capsys.readouterr().out


def test_missing_deno_packages_are_named(monkeypatch, deno_mode, capsys):
    monkeypatch.setattr(_startup.subprocess, "run", lambda cmd, **kw: _reply(0, stdout="deno 2.1.4\n"))
    monkeypatch.setattr(_bgutil, "start_bgutil_server", lambda: True)
    monkeypatch.setitem(sys.modules, "yt_dlp", None)  # import raises ImportError
    monkeypatch.setitem(sys.modules, "yt_dlp_plugins", None)
    _startup.setup_deno_auth(None)
    out = capsys.readouterr().out
    assert "Deno auth needs yt-dlp (pip), bgutil-ytdlp-pot-provider — run:" in out


# ── bgutil server ─────────────────────────────────────────────────────────────


class FakeProc:
    returncode = None

    def __init__(self, exits_with=None):
        self.exits_with = exits_with

    def poll(self):
        self.returncode = self.exits_with
        return self.exits_with


@pytest.fixture
def bgutil_dir(monkeypatch, tmp_path):
    """The bgutil checkout and logs under tmp_path, the clock not waiting."""
    monkeypatch.setattr(_bgutil, "__file__", str(tmp_path / "server" / "_bgutil.py"))
    monkeypatch.setattr(_bgutil, "_bgutil_proc", None)
    monkeypatch.setattr(_bgutil.time, "sleep", lambda s: None)
    monkeypatch.setattr(_bgutil, "is_bgutil_server_ready", lambda: False)
    main_ts = _bgutil.bgutil_server_dir() / "src" / "main.ts"
    main_ts.parent.mkdir(parents=True)
    main_ts.write_text("")
    return tmp_path


def _spawns(monkeypatch, proc, log_text=""):
    spawned = []

    def popen(cmd, **kw):
        spawned.append((cmd, kw))
        kw["stdout"].write(log_text)
        return proc

    monkeypatch.setattr(_bgutil.subprocess, "Popen", popen)
    return spawned


def test_a_running_bgutil_server_is_reused(monkeypatch, bgutil_dir):
    spawned = _spawns(monkeypatch, FakeProc())
    monkeypatch.setattr(_bgutil, "is_bgutil_server_ready", lambda: True)
    assert _bgutil.start_bgutil_server() is True
    assert spawned == []


def test_bgutil_without_its_source_cannot_start(monkeypatch, bgutil_dir, capsys):
    (_bgutil.bgutil_server_dir() / "src" / "main.ts").unlink()
    spawned = _spawns(monkeypatch, FakeProc())
    assert _bgutil.start_bgutil_server() is False
    assert spawned == []
    assert "main.ts not found" in capsys.readouterr().out


def test_bgutil_runs_from_the_server_dir_and_logs_to_a_file(monkeypatch, bgutil_dir):
    answers = iter([False, False, True])  # already running? / first poll / second poll
    monkeypatch.setattr(_bgutil, "is_bgutil_server_ready", lambda: next(answers))
    spawned = _spawns(monkeypatch, FakeProc(), "up\n")
    assert _bgutil.start_bgutil_server() is True
    ((cmd, kw),) = spawned
    assert cmd[:2] == ["deno", "run"] and cmd[-2:] == ["--port", "4416"]
    assert Path(kw["cwd"]) == _bgutil.bgutil_server_dir()
    assert cmd[cmd.index("--allow-sys") + 1] == os.path.join("src", "main.ts")
    assert kw["stderr"] == subprocess.STDOUT
    assert kw["stdout"].closed  # the child has its own handle
    assert (bgutil_dir / "logs" / "bgutil_server.log").read_text() == "up\n"


def test_bgutil_runs_from_node_modules_when_installed(monkeypatch, bgutil_dir):
    (_bgutil.bgutil_server_dir() / "node_modules").mkdir()
    answers = iter([False, True])
    monkeypatch.setattr(_bgutil, "is_bgutil_server_ready", lambda: next(answers))
    spawned = _spawns(monkeypatch, FakeProc())
    assert _bgutil.start_bgutil_server() is True
    assert Path(spawned[0][1]["cwd"]) == _bgutil.bgutil_server_dir() / "node_modules"


def test_a_bgutil_that_exits_is_reported_with_its_output(monkeypatch, bgutil_dir, capsys):
    _spawns(monkeypatch, FakeProc(exits_with=1), "deno: bad import\n")
    assert _bgutil.start_bgutil_server() is False
    out = capsys.readouterr().out
    assert "process exited with code 1" in out and "output: deno: bad import" in out
    assert _bgutil._bgutil_proc is None


def test_a_bgutil_that_never_answers_times_out(monkeypatch, bgutil_dir, capsys):
    polls = []
    monkeypatch.setattr(_bgutil, "is_bgutil_server_ready", lambda: polls.append(1) or False)
    proc = FakeProc()
    _spawns(monkeypatch, proc)
    assert _bgutil.start_bgutil_server() is False
    assert "timed out waiting for port 4416" in capsys.readouterr().out
    assert len(polls) == 31  # the check before starting, then 30 s
    assert _bgutil._bgutil_proc is proc  # still ours to stop


def test_a_bgutil_that_cannot_spawn_is_reported(monkeypatch, bgutil_dir, capsys):
    def popen(cmd, **kw):
        raise FileNotFoundError("deno")

    monkeypatch.setattr(_bgutil.subprocess, "Popen", popen)
    assert _bgutil.start_bgutil_server() is False
    assert "start failed: deno" in capsys.readouterr().out
