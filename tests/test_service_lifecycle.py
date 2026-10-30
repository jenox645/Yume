"""yume/service.py and yume/native_host.py beyond the happy path: what serve()
leaves behind, stop requests, detached spawning on each OS, damaged state files,
and registration per browser/OS. No real server, port or registry is touched."""

from __future__ import annotations

import io
import json
import logging
import os
import subprocess
import sys
import threading
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from yume import native_host, service  # noqa: E402

CFG = {"whisper_host": "127.0.0.1", "whisper_port": 59991, "translation_port": 59992, "auto_stop_minutes": 0}


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    for name, file in (("STATE_FILE", "service.json"), ("STOP_FILE", "service.stop"), ("LOCK_FILE", "service.lock")):
        monkeypatch.setattr(service, name, tmp_path / file)
    monkeypatch.setattr(service, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(service, "TOKEN_FILE", tmp_path / ".yume_token")
    monkeypatch.setattr("yume.ports.stop_bgutil_server", lambda: None)
    monkeypatch.setattr("yume.ports.is_port_free", lambda port: True)
    monkeypatch.setattr("yume.servers.open_rotating_log", lambda name: io.StringIO())
    return tmp_path


def _serve(cfg=None) -> int:
    """serve() redirects stdout, stderr and logging to its log: put them back."""
    out, err, handlers = sys.stdout, sys.stderr, logging.root.handlers[:]
    try:
        return service.serve(dict(cfg or CFG))
    finally:
        sys.stdout, sys.stderr, logging.root.handlers = out, err, handlers


def _sleeper(seconds=30):
    return subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({seconds})"])


def _dead_pid() -> int:
    p = _sleeper(0)
    p.wait()
    return p.pid


class TestServeLeavesNothingBehind:
    def test_a_clean_exit_clears_state_stop_file_and_lock(self, state_dir, monkeypatch):
        def run(self):
            service.STOP_FILE.write_text("1")
            return 0

        monkeypatch.setattr(service._Supervisor, "run", run)
        assert _serve() == 0
        assert not service.STATE_FILE.exists()
        assert not service.STOP_FILE.exists()
        assert not service.LOCK_FILE.exists()

    def test_an_old_stop_request_does_not_stop_the_new_supervisor(self, state_dir, monkeypatch):
        service.STOP_FILE.write_text("left by a crash")
        seen = []
        monkeypatch.setattr(service._Supervisor, "run", lambda self: seen.append(self._stop_requested()) or 0)
        _serve()
        assert seen == [False]

    def test_a_failed_start_stays_readable_after_exit(self, state_dir, monkeypatch):
        monkeypatch.setattr("yume.servers.build_server_env", lambda: {})
        monkeypatch.setattr(service._Supervisor, "_start_translation", lambda self, env: "no .gguf translation model")
        assert _serve() == 1
        data = json.loads(service.STATE_FILE.read_text(encoding="utf-8"))
        assert data["state"] == "error" and data["message"] == "no .gguf translation model"
        assert not service.LOCK_FILE.exists()
        data["pid"] = _dead_pid()  # the supervisor has exited
        service.STATE_FILE.write_text(json.dumps(data), encoding="utf-8")
        assert service.read_state() == {"state": "stopped", "last_error": "no .gguf translation model"}

    def test_a_crash_in_the_supervisor_is_recorded(self, state_dir, monkeypatch):
        def run(self):
            raise RuntimeError("boom")

        monkeypatch.setattr(service._Supervisor, "run", run)
        assert _serve() == 1
        assert json.loads(service.STATE_FILE.read_text(encoding="utf-8"))["message"] == "boom"

    def test_stopping_during_startup_is_not_reported_as_an_error(self, state_dir, monkeypatch):
        def stopped_while_starting(self, env):
            service.STOP_FILE.write_text("1")
            return "Translation: stop requested"

        monkeypatch.setattr("yume.servers.build_server_env", lambda: {})
        monkeypatch.setattr(service._Supervisor, "_start_translation", stopped_while_starting)
        assert _serve() == 1
        assert not service.STATE_FILE.exists()  # no last_error for the popup to show

    @pytest.mark.parametrize(("name", "removed"), [("Whisper", True), ("Translation", False)])
    def test_the_token_goes_only_with_our_own_whisper_server(self, state_dir, name, removed):
        service.TOKEN_FILE.write_text("secret")
        sup = service._Supervisor(CFG)
        sup.procs.procs.append((name, MagicMock()))
        sup.shutdown()
        assert service.TOKEN_FILE.exists() is not removed


class TestStopRequests:
    def test_waits_until_the_supervisor_is_gone(self, state_dir):
        p = _sleeper()
        service.STATE_FILE.write_text(json.dumps({"pid": p.pid, "state": "running"}))
        # kill + wait: on POSIX an unreaped child still answers kill(pid, 0)
        t = threading.Timer(0.2, lambda: (p.kill(), p.wait()))
        t.start()
        assert service.request_stop(timeout=20) is True
        t.join()
        assert service.STOP_FILE.exists()  # that is the request

    def test_says_so_when_it_does_not_stop_in_time(self, state_dir):
        p = _sleeper()
        try:
            service.STATE_FILE.write_text(json.dumps({"pid": p.pid, "state": "running"}))
            assert service.request_stop(timeout=0.3) is False
        finally:
            p.kill()
            p.wait()


class TestIdle:
    def test_activity_counts_only_when_the_server_says_so(self, monkeypatch):
        import yume.network

        sup = service._Supervisor(CFG)
        for stats, active in (({"active": 2}, True), ({"active": 0}, False), ({}, False), (None, False)):
            monkeypatch.setattr(yume.network, "server_get", lambda *a, stats=stats, **k: stats)
            assert sup._active() is active

    def test_a_video_being_subtitled_postpones_the_auto_stop(self, state_dir, monkeypatch):
        sup = service._Supervisor({**CFG, "auto_stop_minutes": 0.001})  # 60 ms
        monkeypatch.setattr(service, "TICK_S", 0.03)
        monkeypatch.setattr("yume.servers.build_server_env", lambda: {})
        monkeypatch.setattr(sup, "_start_translation", lambda env: None)
        monkeypatch.setattr(sup, "_start_whisper", lambda env: sup.procs.procs.append(("Whisper", MagicMock())))
        sup.procs.dead = lambda: []
        polls = iter([True] * 6)
        seen = []
        monkeypatch.setattr(sup, "_active", lambda: seen.append(1) or next(polls, False))
        assert sup.run() == 0
        assert len(seen) > 6  # stopped only after the activity ended


class TestStartBackends:
    @pytest.mark.parametrize(
        ("backend", "calls"), [("llamacpp", ["llamacpp"]), ("ollama", ["ollama"]), ("lmstudio", []), ("custom", [])]
    )
    def test_only_the_backends_yume_runs_are_started(self, monkeypatch, backend, calls):
        made = []
        ok = types.SimpleNamespace(message=lambda: "")
        monkeypatch.setattr(service.servers, "start_llamacpp", lambda *a, **k: made.append("llamacpp") or ok)
        monkeypatch.setattr(service.servers, "start_ollama", lambda *a, **k: made.append("ollama") or ok)
        sup = service._Supervisor({**CFG, "translation_backend": backend})
        assert sup._start_translation({}) is None and made == calls

    def test_a_start_failure_becomes_the_message(self, monkeypatch):
        bad = types.SimpleNamespace(message=lambda: "Whisper: exited with code 1 — A Python package is missing.")
        monkeypatch.setattr(service.servers, "start_whisper", lambda *a, **k: bad)
        assert "package is missing" in service._Supervisor(CFG)._start_whisper({})


class TestDamagedStateFiles:
    @pytest.mark.parametrize(
        "text",
        ['{"pid": %d}' % os.getpid(), '{"pid": "abc", "state": "running"}', '{"pid": [1], "state": "running"}'],
    )
    def test_damaged_state_reads_as_stopped(self, state_dir, text):
        service.STATE_FILE.write_text(text)
        assert service.read_state() == {"state": "stopped"}
        assert service.request_stop(timeout=0.1) is True  # and stopping does not crash on it

    def test_a_lock_that_is_not_a_pid_is_taken_over(self, state_dir):
        service.LOCK_FILE.write_text("garbage")
        assert service._acquire_lock()
        assert service.LOCK_FILE.read_text() == str(os.getpid())

    def test_only_the_owner_releases_the_lock(self, state_dir):
        service.LOCK_FILE.write_text("1")
        service._release_lock()
        assert service.LOCK_FILE.exists()

    def test_probing_a_process_on_posix(self, monkeypatch):
        monkeypatch.setattr(service, "IS_WIN", False)

        def kill(pid, sig):
            assert sig == 0
            raise {1: ProcessLookupError, 2: PermissionError}[pid]()

        monkeypatch.setattr(service.os, "kill", kill)
        assert service.pid_alive(1) is False
        assert service.pid_alive(2) is True  # exists, not ours
        assert service.pid_alive(-5) is False


class _Popen:
    """Records Popen calls; `refuse` flags raise OSError like a forbidden breakaway."""

    def __init__(self, refuse=0):
        self.calls, self.refuse, self.killed = [], refuse, 0

    def __call__(self, cmd, creationflags=0, **kw):
        self.calls.append(types.SimpleNamespace(cmd=cmd, flags=creationflags, kw=kw))
        if creationflags & self.refuse:
            raise OSError("access denied")
        return types.SimpleNamespace(pid=4000 + len(self.calls), kill=self._kill)

    def _kill(self):
        self.killed += 1


class TestDetachedSpawn:
    CMD = ["python", "pocket_yume.py", "serve"]

    def test_posix_starts_a_new_session_without_our_stdio(self, monkeypatch):
        popen = _Popen()
        monkeypatch.setattr(service, "IS_WIN", False)
        monkeypatch.setattr(service.subprocess, "Popen", popen)
        assert service._spawn_detached_cmd(self.CMD) == 4001
        kw = popen.calls[0].kw
        assert kw["start_new_session"] is True and kw["close_fds"] is True
        assert kw["stdin"] == kw["stdout"] == kw["stderr"] == subprocess.DEVNULL

    def _windows(self, monkeypatch, popen, in_job=False, wmi=None):
        monkeypatch.setattr(service, "IS_WIN", True)
        monkeypatch.setattr(service.subprocess, "Popen", popen)
        monkeypatch.setattr(service, "_in_job", lambda pid: in_job)
        monkeypatch.setattr(service, "_spawn_wmi", wmi or (lambda cmd: pytest.fail("no WMI expected")))

    def test_windows_breaks_away_from_the_browsers_job(self, monkeypatch):
        popen = _Popen()
        self._windows(monkeypatch, popen)
        assert service._spawn_detached_cmd(self.CMD) == 4001
        assert popen.calls[0].flags & service._CREATE_BREAKAWAY_FROM_JOB
        assert popen.calls[0].flags & service._CREATE_NO_WINDOW

    def test_windows_child_still_in_a_job_is_replaced_through_wmi(self, monkeypatch):
        popen = _Popen()
        self._windows(monkeypatch, popen, in_job=True, wmi=lambda cmd: 777)
        assert service._spawn_detached_cmd(self.CMD) == 777
        assert popen.killed == 1  # it would have died with the host

    def test_windows_refused_breakaway_uses_wmi(self, monkeypatch):
        popen = _Popen(refuse=service._CREATE_BREAKAWAY_FROM_JOB)
        self._windows(monkeypatch, popen, wmi=lambda cmd: 778)
        assert service._spawn_detached_cmd(self.CMD) == 778

    def test_windows_without_wmi_still_starts_something(self, monkeypatch):
        def no_wmi(cmd):
            raise OSError("WMI is off")

        popen = _Popen(refuse=service._CREATE_BREAKAWAY_FROM_JOB)
        self._windows(monkeypatch, popen, wmi=no_wmi)
        assert service._spawn_detached_cmd(self.CMD) == 4002
        assert not popen.calls[1].flags & service._CREATE_BREAKAWAY_FROM_JOB

    def _wmi(self, monkeypatch, stdout, stderr=""):
        import yume.utils

        ran = []
        result = types.SimpleNamespace(stdout=stdout, stderr=stderr)
        monkeypatch.setattr(yume.utils, "_run", lambda cmd, **kw: ran.append(cmd) or result)
        return ran

    def test_wmi_returns_the_created_process(self, monkeypatch):
        ran = self._wmi(monkeypatch, "0 4321\n")
        monkeypatch.setattr(service, "BASE_DIR", Path("C:/it's here"))
        assert service._spawn_wmi(["py", "a b.py"]) == 4321
        ps = ran[0][-1]
        assert "it''s here" in ps  # a quote in the folder name cannot end the PowerShell string
        assert '"a b.py"' in ps

    @pytest.mark.parametrize("stdout", ["9 0", "", "nonsense"])
    def test_wmi_failure_raises(self, monkeypatch, stdout):
        self._wmi(monkeypatch, stdout, "denied")
        with pytest.raises(OSError):
            service._spawn_wmi(["py"])

    def test_spawn_detached_runs_this_checkout(self, monkeypatch):
        got = []
        monkeypatch.setattr(service, "windowless_python", lambda: "pyw")
        monkeypatch.setattr(service, "_spawn_detached_cmd", lambda cmd: got.append(cmd) or 5)
        assert service.spawn_detached(["serve"]) == 5
        assert got == [["pyw", str(service.BASE_DIR / "pocket_yume.py"), "serve"]]

    def test_mark_starting_ignores_an_unknown_pid(self, state_dir):
        service.mark_starting(0)
        assert not service.STATE_FILE.exists()


class TestHostAnswers:
    def _handle(self, monkeypatch, msg, state, stopped=True, up=False):
        import config
        import yume.network

        monkeypatch.setattr(config, "load_config", lambda: {"whisper_host": "h", "whisper_port": 1})
        monkeypatch.setattr(service, "read_state", lambda: state)
        monkeypatch.setattr(service, "request_stop", lambda: stopped)
        monkeypatch.setattr(yume.network, "check_server", lambda *a: {"up": up, "data": {}})
        return native_host.handle(msg)

    def test_stop_that_timed_out_is_not_reported_as_stopped(self, monkeypatch):
        r = self._handle(monkeypatch, {"cmd": "stop"}, {"state": "running", "pid": 1}, stopped=False)
        assert r == {"ok": False, "state": "stopping"}

    def test_stop_when_nothing_runs(self, monkeypatch):
        assert self._handle(monkeypatch, {"cmd": "stop"}, {"state": "stopped"}) == {"ok": True, "state": "stopped"}

    def test_status_passes_the_supervisors_progress(self, monkeypatch):
        st = {"state": "starting", "pid": 1, "message": "Loading the speech model…"}
        r = self._handle(monkeypatch, {"cmd": "status"}, st, up=True)
        assert r["state"] == "starting" and r["managed"] is True and "speech model" in r["message"]

    def test_a_message_without_a_command_is_refused(self, monkeypatch):
        assert self._handle(monkeypatch, {}, {"state": "stopped"})["ok"] is False


class TestRegistrationPaths:
    def _home(self, tmp_path, monkeypatch, platform):
        monkeypatch.setattr(native_host.sys, "platform", platform)
        monkeypatch.setattr(native_host.Path, "home", classmethod(lambda cls: tmp_path))
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    def test_macos_folders(self, tmp_path, monkeypatch):
        self._home(tmp_path, monkeypatch, "darwin")
        dirs = native_host._posix_dirs()
        sup = tmp_path / "Library" / "Application Support"
        assert dirs["Chrome"] == (sup / "Google" / "Chrome" / "NativeMessagingHosts", False)
        assert dirs["Edge"] == (sup / "Microsoft Edge" / "NativeMessagingHosts", False)
        assert dirs["Firefox"] == (sup / "Mozilla" / "NativeMessagingHosts", True)

    def test_linux_folders_follow_xdg_config_home(self, tmp_path, monkeypatch):
        self._home(tmp_path, monkeypatch, "linux")
        assert native_host._posix_dirs()["Chrome"][0] == tmp_path / ".config" / "google-chrome" / "NativeMessagingHosts"
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
        dirs = native_host._posix_dirs()
        assert dirs["Chromium"][0] == tmp_path / "xdg" / "chromium" / "NativeMessagingHosts"
        # Firefox never moved to XDG
        assert dirs["Firefox"] == (tmp_path / ".mozilla" / "native-messaging-hosts", True)

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX executable bit")
    def test_the_posix_launcher_is_executable(self, tmp_path, monkeypatch):
        monkeypatch.setattr(native_host, "HOST_DIR", tmp_path)
        assert os.access(native_host._write_launcher(), os.X_OK)

    def test_a_damaged_or_foreign_manifest_is_not_ours(self, tmp_path, monkeypatch):
        chrome, firefox = tmp_path / "chrome", tmp_path / "firefox"
        chrome.mkdir()
        firefox.mkdir()
        (chrome / f"{native_host.HOST_NAME}.json").write_text("{nope")
        (firefox / f"{native_host.HOST_NAME}.json").write_text(json.dumps({"path": str(tmp_path / "other" / "x")}))
        monkeypatch.setattr(native_host, "IS_WIN", False)
        monkeypatch.setattr(native_host, "HOST_DIR", tmp_path / "nh")
        monkeypatch.setattr(native_host, "_posix_dirs", lambda: {"Chrome": (chrome, False), "Firefox": (firefox, True)})
        assert native_host.registered_browsers() == []

    def test_unregister_when_never_registered(self, tmp_path, monkeypatch):
        monkeypatch.setattr(native_host, "IS_WIN", False)
        monkeypatch.setattr(native_host, "HOST_DIR", tmp_path / "nh")
        monkeypatch.setattr(native_host, "_posix_dirs", lambda: {"Chrome": (tmp_path / "no", False)})
        native_host.unregister()  # nothing there, nothing raised
