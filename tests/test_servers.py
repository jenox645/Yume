"""yume/servers.py: the server process core shared by the launcher and the
headless supervisor. Real child processes (tiny Python scripts) stand in for
the Whisper and translation servers."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from yume import servers  # noqa: E402

CFG = {
    "whisper_host": "127.0.0.1",
    "whisper_port": 5997,
    "translation_host": "127.0.0.1",
    "translation_port": 5996,
}


@pytest.fixture
def logs(tmp_path, monkeypatch):
    monkeypatch.setattr(servers, "LOGS_DIR", tmp_path)
    monkeypatch.setattr("yume.ports.stop_bgutil_server", lambda: None)
    monkeypatch.setattr("yume.ports.kill_port_process", lambda port, interactive=True: True)
    return tmp_path


def _child(code: str) -> list[str]:
    return [sys.executable, "-c", code]


# ── crash diagnosis ───────────────────────────────────────────────────────────


def test_diagnosis_from_the_log():
    oom = ["Traceback ...", "RuntimeError: CUDA failed with error out of memory"]
    assert "VRAM" in servers.diagnose("Whisper", oom)[0]
    assert "VRAM" in servers.diagnose("Translation", oom)[0]
    cudnn = ["Could not load library cudnn_ops64_9.dll"]
    assert "CUDA libraries" in servers.diagnose("Whisper", cudnn)[0]
    assert servers.diagnose("Translation", cudnn) == []  # a Whisper-only cause
    assert servers.diagnose("Whisper", ["all good"]) == []


def test_start_result_message_carries_the_diagnosis():
    r = servers.StartResult(error="Whisper: exited with code 1", diagnosis=["The GPU is out of VRAM.", "Do X."])
    assert not r.ok and r.message() == "Whisper: exited with code 1 — The GPU is out of VRAM."
    assert servers.StartResult().ok and servers.StartResult().message() == ""


# ── waiting ───────────────────────────────────────────────────────────────────


def test_wait_ready_outcomes(monkeypatch):
    monkeypatch.setattr(servers.time, "sleep", lambda s: None)
    assert servers.wait_ready(None, lambda: None, 5) == ("", False)
    assert servers.wait_ready(None, lambda: "the model failed to load: x", 5) == ("the model failed to load: x", False)
    assert servers.wait_ready(None, lambda: "", 5, stop=lambda: True) == ("stop requested", False)
    ticks = []
    clock = iter(range(1000))
    monkeypatch.setattr(servers.time, "time", lambda: next(clock))
    err, timed_out = servers.wait_ready(None, lambda: "", 3, tick=ticks.append)
    assert timed_out and "in time" in err and ticks  # the spinner was driven


def test_wait_ready_notices_an_exited_process(logs):
    procs = servers.Processes()
    p = procs.spawn("Whisper", _child("raise SystemExit(3)"), "w.log", env=None)
    assert servers.wait_ready(p, lambda: "", 30) == ("exited with code 3", False)
    procs.stop(CFG)


# ── starting ──────────────────────────────────────────────────────────────────


def test_start_whisper_waits_for_the_model(logs, monkeypatch):
    calls = iter(["", "", None])  # loading, loading, ready
    monkeypatch.setattr(servers, "whisper_up", lambda cfg: False)
    monkeypatch.setattr(servers, "whisper_command", lambda cfg: _child("import time; time.sleep(30)"))
    monkeypatch.setattr(servers, "whisper_ready", lambda cfg: next(calls))
    monkeypatch.setattr(servers.time, "sleep", lambda s: None)
    procs = servers.Processes()
    r = servers.start_whisper(CFG, procs, env=None)
    assert r.ok and not r.already_running and procs.names() == ["Whisper"]
    procs.stop(CFG, timeout=5)
    assert procs.dead()  # stopped


def test_a_crash_is_explained_from_its_log(logs, monkeypatch):
    monkeypatch.setattr(servers, "whisper_up", lambda cfg: False)
    crash = "print('RuntimeError: CUDA failed with error out of memory', flush=True); raise SystemExit(1)"
    monkeypatch.setattr(servers, "whisper_command", lambda cfg: _child(crash))
    monkeypatch.setattr(servers, "whisper_ready", lambda cfg: "")
    procs = servers.Processes()
    r = servers.start_whisper(CFG, procs, env=None)
    procs.stop(CFG)
    assert r.error == "Whisper: exited with code 1" and not r.timed_out
    assert "VRAM" in r.diagnosis[0] and "VRAM" in r.message()
    assert any("out of memory" in line for line in servers.log_tail(servers.WHISPER_LOG))


def test_a_running_server_is_used_not_restarted(logs, monkeypatch):
    monkeypatch.setattr(servers, "whisper_up", lambda cfg: True)
    procs = servers.Processes()
    r = servers.start_whisper(CFG, procs, env=None)
    assert r.ok and r.already_running and not procs  # and never stopped by Yume


def test_a_busy_port_is_left_alone(logs, monkeypatch):
    monkeypatch.setattr(servers, "whisper_up", lambda cfg: False)
    monkeypatch.setattr(servers, "whisper_command", lambda cfg: ["x"])
    monkeypatch.setattr("yume.ports.kill_port_process", lambda port, interactive=True: False)
    r = servers.start_whisper(CFG, servers.Processes(), env=None)
    assert "used by another program" in r.error


def test_llamacpp_needs_a_model(logs, monkeypatch):
    monkeypatch.setattr(servers, "translation_up", lambda cfg, bi: False)
    monkeypatch.setattr(servers, "resolve_gguf", lambda cfg: None)
    assert ".gguf" in servers.start_llamacpp(CFG, {}, servers.Processes(), env=None).error


# ── stopping and logs ─────────────────────────────────────────────────────────


def test_stop_ends_children_and_the_po_token_helper(logs, monkeypatch):
    stopped = []
    monkeypatch.setattr("yume.ports.stop_bgutil_server", lambda: stopped.append("bgutil"))
    procs = servers.Processes()
    procs.spawn("Whisper", _child("import time; time.sleep(30)"), "w.log", env=None)
    procs.spawn("Translation", _child("import time; time.sleep(30)"), "t.log", env=None)
    procs.stop(CFG, timeout=5)
    assert len(procs.dead()) == 2 and stopped == ["bgutil"]
    assert all(lh.closed for lh in procs.logs)


def test_children_never_read_the_launchers_keys(logs, monkeypatch):
    seen = {}
    real = servers.subprocess.Popen

    def popen(cmd, **kw):
        seen.update(kw)
        return real(cmd, **kw)

    monkeypatch.setattr(servers.subprocess, "Popen", popen)
    procs = servers.Processes()
    procs.spawn("Whisper", _child("pass"), "w.log", env=None)
    procs.stop(CFG)
    assert seen["stdin"] is servers.subprocess.DEVNULL


def test_logs_rotate_past_5_mb(logs):
    big = logs / "whisper_server.log"
    big.write_bytes(b"x" * (5 * 1024 * 1024 + 1))
    servers.open_rotating_log("whisper_server.log").close()
    assert (logs / "whisper_server.log.1").stat().st_size > 5 * 1024 * 1024
    assert big.stat().st_size == 0


def test_ollama_is_started_from_where_it_was_found(logs, monkeypatch):
    spawned = []

    def spawn(self, name, cmd, log_name, env):
        spawned.append(cmd)
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr("yume.network.check_server", lambda *a: {"up": False, "data": {}})
    monkeypatch.setattr(servers.Processes, "spawn", spawn)
    monkeypatch.setattr(servers, "find_ollama", lambda: "/Applications/Ollama.app/Contents/Resources/ollama")
    r = servers.start_ollama(CFG, servers.Processes(), {})
    assert spawned == [["/Applications/Ollama.app/Contents/Resources/ollama", "serve"]]
    assert "not installed" in r.error
    monkeypatch.setattr(servers, "find_ollama", lambda: None)
    r = servers.start_ollama(CFG, servers.Processes(), {})
    assert len(spawned) == 1 and "ollama.com/download" in r.error
