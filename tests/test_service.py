"""One-click start: headless supervisor (yume/service.py) and native host (yume/native_host.py)."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import struct
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from yume import native_host, service  # noqa: E402


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(service, "STATE_FILE", tmp_path / "service.json")
    monkeypatch.setattr(service, "STOP_FILE", tmp_path / "service.stop")
    monkeypatch.setattr(service, "LOCK_FILE", tmp_path / "service.lock")
    # shutdown() deletes the token file of the Whisper server it stopped: never
    # the real one of a Yume running on this machine
    monkeypatch.setattr(service, "TOKEN_FILE", tmp_path / ".yume_token")
    # ...nor stop a Deno PO-token server running on this machine
    monkeypatch.setattr("yume.ports.stop_bgutil_server", lambda: None)
    return tmp_path


def _sleeper(seconds=30):
    return subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({seconds})"])


# ── protocol ─────────────────────────────────────────────────────────────────


class TestFraming:
    def test_roundtrip(self):
        buf = io.BytesIO()
        native_host.write_message(buf, {"cmd": "start", "x": "日本語"})
        buf.seek(0)
        assert native_host.read_message(buf) == {"cmd": "start", "x": "日本語"}
        assert native_host.read_message(buf) is None  # end of input

    def test_length_prefix_is_native_uint32(self):
        buf = io.BytesIO()
        native_host.write_message(buf, {"a": 1})
        raw = buf.getvalue()
        assert struct.unpack("=I", raw[:4])[0] == len(raw) - 4

    def test_oversized_message_refused(self):
        buf = io.BytesIO(struct.pack("=I", native_host.MAX_MESSAGE + 1) + b"{}")
        with pytest.raises(ValueError):
            native_host.read_message(buf)

    def test_truncated_body_is_end_of_input(self):
        assert native_host.read_message(io.BytesIO(struct.pack("=I", 10) + b"{}")) is None

    def test_non_object_refused(self):
        with pytest.raises(ValueError):
            native_host.read_message(io.BytesIO(struct.pack("=I", 2) + b"[]"))


# ── requests ─────────────────────────────────────────────────────────────────


class TestHandle:
    CFG = {"whisper_host": "127.0.0.1", "whisper_port": 5999, "first_run_complete": True}

    def _handle(self, msg, *, state=None, up=False, cfg=None, spawn=None):
        with (
            patch("config.load_config", return_value=dict(cfg or self.CFG)),
            patch.object(service, "read_state", return_value=state or {"state": "stopped"}),
            patch("yume.network.check_server", return_value={"up": up, "data": {}}),
            patch.object(service, "spawn_detached", spawn or (lambda args: 4242)) as _,
            patch.object(service, "request_stop", return_value=True),
            patch.object(service, "mark_starting"),  # never the real config/service.json
        ):
            return native_host.handle(msg)

    def test_ping(self):
        assert self._handle({"cmd": "ping"}) == {"ok": True, "protocol": native_host.PROTOCOL}

    def test_unknown_command(self):
        assert self._handle({"cmd": "rm -rf"})["ok"] is False

    def test_status_stopped(self):
        r = self._handle({"cmd": "status"})
        assert r["state"] == "stopped" and r["managed"] is False

    def test_status_reports_launcher_started_server_as_running(self):
        r = self._handle({"cmd": "status"}, up=True)
        assert r["state"] == "running" and r["managed"] is False

    def test_status_passes_last_error(self):
        r = self._handle({"cmd": "status"}, state={"state": "stopped", "last_error": "no .gguf"})
        assert r["last_error"] == "no .gguf"

    def test_start_spawns_serve(self):
        calls = []
        r = self._handle({"cmd": "start"}, spawn=lambda args: calls.append(args) or 1)
        assert r["ok"] and r["state"] == "starting" and calls == [["serve"]]

    def test_start_twice_does_not_spawn_again(self):
        calls = []
        r = self._handle(
            {"cmd": "start"},
            state={"state": "starting", "pid": 1, "message": "Loading"},
            spawn=lambda args: calls.append(args),
        )
        assert r["state"] == "starting" and r["message"] == "Loading" and calls == []

    def test_start_when_already_up_from_launcher(self):
        calls = []
        r = self._handle({"cmd": "start"}, up=True, spawn=lambda args: calls.append(args))
        assert r == {"ok": True, "state": "running", "managed": False} and calls == []

    def test_start_refused_before_setup(self):
        r = self._handle({"cmd": "start"}, cfg={**self.CFG, "first_run_complete": False})
        assert r["ok"] is False and "set up" in r["error"]

    def test_stop_managed(self):
        assert self._handle({"cmd": "stop"}, state={"state": "running", "pid": 1}) == {"ok": True, "state": "stopped"}

    def test_stop_does_not_touch_launcher_started_servers(self):
        r = self._handle({"cmd": "stop"}, up=True)
        assert r["ok"] is False and "launcher" in r["error"]


# ── registration ─────────────────────────────────────────────────────────────


class _FakeWinreg:
    HKEY_CURRENT_USER = "HKCU"
    REG_SZ = 1

    def __init__(self):
        self.keys: dict[str, str] = {}

    class _Key:
        def __init__(self, reg, path):
            self.reg, self.path = reg, path

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def CreateKey(self, root, path):
        self.keys.setdefault(path, "")
        return self._Key(self, path)

    def OpenKey(self, root, path):
        if path not in self.keys:
            raise OSError("no key")
        return self._Key(self, path)

    def SetValueEx(self, key, name, reserved, kind, value):
        self.keys[key.path] = value

    def QueryValueEx(self, key, name):
        return self.keys[key.path], self.REG_SZ

    def DeleteKey(self, root, path):
        if path not in self.keys:
            raise OSError("no key")
        del self.keys[path]


class TestRegistration:
    def test_windows_registers_every_browser(self, tmp_path, monkeypatch):
        reg = _FakeWinreg()
        monkeypatch.setattr(native_host, "IS_WIN", True)
        monkeypatch.setattr(native_host, "HOST_DIR", tmp_path / "nh")
        monkeypatch.setattr(native_host, "_winreg", lambda: reg)
        done = native_host.register()
        assert set(done) == {"Chrome", "Edge", "Brave", "Chromium", "Firefox"}
        chrome = json.loads(
            Path(reg.keys[rf"Software\Google\Chrome\NativeMessagingHosts\{native_host.HOST_NAME}"]).read_text()
        )
        assert chrome["name"] == native_host.HOST_NAME and chrome["type"] == "stdio"
        assert chrome["allowed_origins"] == ["chrome-extension://eagcngkomlidpdaggkkgefjobcjgjaio/"]
        assert "allowed_extensions" not in chrome
        ff = json.loads(Path(reg.keys[rf"Software\Mozilla\NativeMessagingHosts\{native_host.HOST_NAME}"]).read_text())
        assert ff["allowed_extensions"] == ["yume-subtitles@pocketyume"]
        launcher = Path(chrome["path"])
        assert launcher.exists() and launcher.read_bytes().count(b"\r\n") == 2
        assert str(Path(native_host.__file__).resolve()) in launcher.read_text()
        assert set(native_host.registered_browsers()) == set(done)

        native_host.unregister()
        assert reg.keys == {} and not (tmp_path / "nh").exists()
        assert native_host.registered_browsers() == []

    def test_foreign_registration_not_reported_as_ours(self, tmp_path, monkeypatch):
        reg = _FakeWinreg()
        reg.keys[rf"Software\Google\Chrome\NativeMessagingHosts\{native_host.HOST_NAME}"] = str(tmp_path / "x.json")
        monkeypatch.setattr(native_host, "IS_WIN", True)
        monkeypatch.setattr(native_host, "HOST_DIR", tmp_path / "nh")
        monkeypatch.setattr(native_host, "_winreg", lambda: reg)
        assert native_host.registered_browsers() == []

    def test_posix_only_installed_browsers(self, tmp_path, monkeypatch):
        (tmp_path / "chrome").mkdir()  # Chrome installed, Firefox not
        dirs = {
            "Chrome": (tmp_path / "chrome" / "NativeMessagingHosts", False),
            "Firefox": (tmp_path / "mozilla" / "native-messaging-hosts", True),
        }
        monkeypatch.setattr(native_host, "IS_WIN", False)
        monkeypatch.setattr(native_host, "HOST_DIR", tmp_path / "nh")
        monkeypatch.setattr(native_host, "_posix_dirs", lambda: dirs)
        assert native_host.register() == ["Chrome"]
        m = json.loads((dirs["Chrome"][0] / f"{native_host.HOST_NAME}.json").read_text())
        assert Path(m["path"]).name == "yume_host.sh"
        assert Path(m["path"]).read_text().startswith("#!/bin/sh\n")
        assert native_host.registered_browsers() == ["Chrome"]
        native_host.unregister()
        assert native_host.registered_browsers() == []


class TestExtensionIdentity:
    def test_manifest_key_yields_the_allowed_id(self):
        manifest = json.loads((ROOT / "extension" / "manifest.json").read_text(encoding="utf-8"))
        digest = hashlib.sha256(base64.b64decode(manifest["key"])).hexdigest()[:32]
        ext_id = "".join(chr(ord("a") + int(c, 16)) for c in digest)
        assert ext_id in native_host.CHROMIUM_EXTENSION_IDS
        assert manifest["browser_specific_settings"]["gecko"]["id"] == native_host.FIREFOX_EXTENSION_ID
        assert "nativeMessaging" in manifest["permissions"]

    def test_background_uses_the_host_name(self):
        js = (ROOT / "extension" / "js" / "background.js").read_text(encoding="utf-8")
        assert f"'{native_host.HOST_NAME}'" in js


# ── supervisor state ─────────────────────────────────────────────────────────


class TestState:
    def test_pid_alive(self):
        import os

        assert service.pid_alive(os.getpid())
        assert not service.pid_alive(0) and not service.pid_alive(None)
        p = _sleeper(0)
        p.wait()
        assert not service.pid_alive(p.pid)

    def test_no_state_file_is_stopped(self, state_dir):
        assert service.read_state() == {"state": "stopped"}
        assert service.request_stop(timeout=0.1) is True

    def test_dead_supervisor_is_stopped_and_keeps_its_error(self, state_dir):
        p = _sleeper(0)
        p.wait()
        service.STATE_FILE.write_text(json.dumps({"pid": p.pid, "state": "error", "message": "boom"}))
        assert service.read_state() == {"state": "stopped", "last_error": "boom"}
        service.STATE_FILE.write_text(json.dumps({"pid": p.pid, "state": "running"}))
        assert service.read_state() == {"state": "stopped"}

    def test_live_supervisor_state(self, state_dir):
        import os

        service.STATE_FILE.write_text(json.dumps({"pid": os.getpid(), "state": "running"}))
        assert service.read_state()["state"] == "running"

    def test_corrupt_state_file(self, state_dir):
        service.STATE_FILE.write_text("{nope")
        assert service.read_state() == {"state": "stopped"}


class TestSupervisor:
    CFG = {"whisper_host": "127.0.0.1", "whisper_port": 5999, "translation_port": 5998}

    def _sup(self, monkeypatch, cfg, active=False, die=False):
        monkeypatch.setattr(service, "TICK_S", 0.05)
        sup = service._Supervisor(cfg, {})

        def start_child(env):
            p = _sleeper(0 if die else 30)
            sup.procs.append(("Whisper", p))
            return None

        monkeypatch.setattr(sup, "_start_translation", lambda env: None)
        monkeypatch.setattr(sup, "_start_whisper", start_child)
        monkeypatch.setattr(sup, "_active", lambda: active)
        monkeypatch.setattr("yume.launch.build_server_env", lambda: {})
        return sup

    def test_idle_auto_stop(self, state_dir, monkeypatch):
        sup = self._sup(monkeypatch, {**self.CFG, "auto_stop_minutes": 0.003})  # ~0.2 s
        t = time.time()
        assert sup.run() == 0
        assert time.time() - t < 10
        sup.shutdown()
        assert all(p.poll() is not None for _, p in sup.procs)

    def test_stop_file(self, state_dir, monkeypatch):
        sup = self._sup(monkeypatch, {**self.CFG, "auto_stop_minutes": 0}, active=True)
        import threading

        threading.Timer(0.5, lambda: service.STOP_FILE.write_text("1")).start()
        assert sup.run() == 0
        sup.shutdown()

    def test_dead_child_is_an_error(self, state_dir, monkeypatch):
        sup = self._sup(monkeypatch, {**self.CFG, "auto_stop_minutes": 0}, die=True)
        time.sleep(0.3)
        assert sup.run() == 1
        state = json.loads(service.STATE_FILE.read_text())
        assert state["state"] == "error" and "Whisper" in state["message"]
        sup.shutdown()

    def test_start_error_is_recorded(self, state_dir, monkeypatch):
        sup = self._sup(monkeypatch, self.CFG)
        monkeypatch.setattr(sup, "_start_translation", lambda env: "no .gguf translation model")
        assert sup.run() == 1
        assert json.loads(service.STATE_FILE.read_text())["message"] == "no .gguf translation model"

    def test_nothing_to_supervise(self, state_dir, monkeypatch):
        sup = self._sup(monkeypatch, self.CFG)
        monkeypatch.setattr(sup, "_start_whisper", lambda env: None)  # already running elsewhere
        assert sup.run() == 0 and sup.procs == []


@pytest.mark.skipif(sys.platform != "win32", reason="Windows job objects")
class TestDetachedSpawnOutlivesJob:
    """Browsers run the host in a kill-on-close job; `serve` must survive it."""

    def _run_in_job(self, breakaway_ok: bool) -> bool:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.OpenProcess.restype = wintypes.HANDLE

        class Basic(ctypes.Structure):
            _fields_ = [
                ("a", ctypes.c_int64), ("b", ctypes.c_int64), ("LimitFlags", wintypes.DWORD),
                ("c", ctypes.c_size_t), ("d", ctypes.c_size_t), ("e", wintypes.DWORD),
                ("f", ctypes.c_size_t), ("g", wintypes.DWORD), ("h", wintypes.DWORD),
            ]  # fmt: skip

        class Ext(ctypes.Structure):
            _fields_ = [("Basic", Basic), ("Io", ctypes.c_uint64 * 6), ("m", ctypes.c_size_t * 4)]

        job = k32.CreateJobObjectW(None, None)
        info = Ext()
        info.Basic.LimitFlags = 0x2000 | (0x800 if breakaway_ok else 0)  # KILL_ON_JOB_CLOSE | BREAKAWAY_OK
        assert k32.SetInformationJobObject(wintypes.HANDLE(job), 9, ctypes.byref(info), ctypes.sizeof(info))
        code = (
            f"import sys; sys.path.insert(0, {str(ROOT)!r}); from yume import service as s;"
            "print(s._spawn_detached_cmd([s.windowless_python(), '-c', 'import time; time.sleep(30)']), flush=True);"
            "import time; time.sleep(60)"
        )
        host = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True, creationflags=0x4)
        h = k32.OpenProcess(0x1F0FFF, False, host.pid)
        try:
            assert k32.AssignProcessToJobObject(wintypes.HANDLE(job), wintypes.HANDLE(h))
            ctypes.WinDLL("ntdll").NtResumeProcess(wintypes.HANDLE(h))
            pid = int(host.stdout.readline())
            k32.CloseHandle(wintypes.HANDLE(job))  # the browser drops the host → job killed
            time.sleep(1.5)
            assert host.poll() is not None
            alive = service.pid_alive(pid)
        finally:
            k32.CloseHandle(wintypes.HANDLE(h))
        subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True)
        return alive

    def test_breakaway(self):
        assert self._run_in_job(breakaway_ok=True)

    def test_wmi_fallback_when_breakaway_forbidden(self):
        assert self._run_in_job(breakaway_ok=False)


class TestSingleSupervisor:
    """Two start requests within a second must not run two supervisors, which
    would kill each other's servers through kill_port_process."""

    def test_lock_is_exclusive_and_released(self, state_dir):
        assert service._acquire_lock()
        service.LOCK_FILE.write_text(str(_sleeper(30).pid))  # another live supervisor holds it
        assert not service._acquire_lock()

    def test_stale_lock_is_taken_over(self, state_dir):
        p = _sleeper(0)
        p.wait()
        service.LOCK_FILE.write_text(str(p.pid))  # its owner is gone
        assert service._acquire_lock()
        service._release_lock()
        assert not service.LOCK_FILE.exists()

    def test_spawned_supervisor_is_recorded_at_once(self, state_dir):
        import os

        service.mark_starting(os.getpid())  # stands in for the spawned process
        st = service.read_state()
        assert st["state"] == "starting" and st["pid"] == os.getpid()

    def test_second_serve_exits_while_one_runs(self, state_dir, monkeypatch):
        holder = _sleeper(30)
        service.LOCK_FILE.write_text(str(holder.pid))
        monkeypatch.setattr("yume.launch._open_rotating_log", lambda name: io.StringIO())
        ran = []
        monkeypatch.setattr(service._Supervisor, "run", lambda self: ran.append(1) or 0)
        import logging

        out, err, handlers = sys.stdout, sys.stderr, logging.root.handlers[:]
        try:
            assert service.serve({"whisper_port": 1, "translation_port": 2}, {}) == 0
        finally:  # serve() redirects output and logging to its log file
            sys.stdout, sys.stderr, logging.root.handlers = out, err, handlers
            holder.kill()
        assert ran == []
