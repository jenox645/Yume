"""ports.get_port_process against faked netstat / tasklist / lsof / ss / ps output."""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import yume.ports as ports  # noqa: E402
import yume.utils  # noqa: E402

NETSTAT = """
Active Connections

  Proto  Local Address          Foreign Address        State           PID
  TCP    0.0.0.0:135            0.0.0.0:0              LISTENING       1100
  TCP    127.0.0.1:5001         127.0.0.1:61000        ESTABLISHED     7777
  TCP    127.0.0.1:50010        0.0.0.0:0              LISTENING       8888
  TCP    127.0.0.1:5001         0.0.0.0:0              LISTENING       4242
"""
TASKLIST = '"python.exe","4242","Console","1","123,456 K"\n'
SS = """State  Recv-Q Send-Q Local Address:Port Peer Address:Port Process
LISTEN 0      128    127.0.0.1:5000      0.0.0.0:*     users:(("llama-server",pid=3131,fd=3))
"""


def owner(tools: dict, win: bool, port=5001):
    """`tools` maps a command name to its stdout (missing: exit 1)."""
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        out = tools.get(cmd[0])
        return types.SimpleNamespace(returncode=0 if out is not None else 1, stdout=out or "", stderr="")

    with patch.object(yume.utils, "_run", run), patch.object(ports, "IS_WIN", win):
        return ports.get_port_process(port), calls


def test_windows_listening_socket_and_its_name():
    (pid, name), calls = owner({"netstat": NETSTAT, "tasklist": TASKLIST}, win=True)
    assert (pid, name) == (4242, "python.exe")  # not the ESTABLISHED client, not port 50010
    assert calls[1][:3] == ["tasklist", "/FI", "PID eq 4242"]


def test_windows_name_unknown_when_tasklist_fails():
    (pid, name), _ = owner({"netstat": NETSTAT}, win=True)
    assert (pid, name) == (4242, "unknown")


def test_windows_port_not_listening():
    assert owner({"netstat": NETSTAT}, win=True, port=6000)[0] == (None, None)
    assert owner({}, win=True)[0] == (None, None)


def test_posix_lsof_listener():
    (pid, name), calls = owner({"lsof": "5151\n5152\n", "ps": "ollama\n"}, win=False)
    assert (pid, name) == (5151, "ollama")
    assert "-sTCP:LISTEN" in calls[0]  # never a client connected to the port


def test_posix_falls_back_to_ss():
    (pid, name), _ = owner({"ss": SS, "ps": "llama-server\n"}, win=False, port=5000)
    assert (pid, name) == (3131, "llama-server")


def test_posix_name_unknown_when_ps_fails():
    (pid, name), _ = owner({"lsof": "5151\n"}, win=False)
    assert (pid, name) == (5151, "unknown")


def test_posix_nobody_listening():
    assert owner({"ss": SS}, win=False, port=5001)[0] == (None, None)
    assert owner({}, win=False)[0] == (None, None)


# ── ensure_port_free: the "port in use" prompt ───────────────────────────────


def resolve(choice, typed="", free=(), killed=True, found=5002, key="whisper"):
    """ensure_port_free(5001) with the user picking `choice` (and typing `typed`);
    `free` are the ports that are free. Returns (result, saved config, output)."""
    import config

    cfg, saved, out, free = {}, [], [], set(free)

    def kill(p):
        if killed:
            free.add(p)
        return killed

    with (
        patch.object(ports, "is_port_free", lambda p, *a: p in free),
        patch.object(ports, "get_port_process", lambda p: (4242, "python.exe")),
        patch.object(ports, "ask_arrow", lambda *a, **k: choice),
        patch.object(ports, "ask_input", lambda *a, **k: typed),
        patch.object(ports, "kill_port_process", kill),
        patch.object(ports, "find_free_port", lambda p, exclude=None: found),
        patch.object(config, "save_config", lambda c: saved.append(dict(c))),
        patch.object(ports, "success", out.append),
        patch.object(ports, "error", out.append),
        patch.object(ports, "warn", out.append),
        patch.object(ports, "info", out.append),
    ):
        return ports.ensure_port_free(5001, cfg, key), saved, out


def test_a_free_port_is_kept_without_asking():
    assert resolve(choice=None, free={5001}) == (5001, [], [])


def test_use_a_different_port_saves_it():
    port, saved, out = resolve(0)
    assert port == 5002 and saved == [{"whisper_port": 5002}]
    assert any("extension popup" in line for line in out)  # the extension must follow
    port, saved, out = resolve(0, key="translation")
    assert port == 5002 and saved == [{"translation_port": 5002}]
    assert not any("extension popup" in line for line in out)
    assert resolve(0, found=None)[:2] == (None, [])


def test_kill_frees_the_port_or_says_it_could_not():
    assert resolve(1)[:2] == (5001, [])
    port, saved, _out = resolve(1, killed=False)
    assert (port, saved) == (None, [])


def test_a_typed_port_is_checked():
    assert resolve(2, typed="6000", free={6000})[:2] == (6000, [{"whisper_port": 6000}])
    for typed in ("6000", "0", "70000", "abc"):  # in use, out of range, not a number
        assert resolve(2, typed=typed)[:2] == (None, [])


def test_cancel_changes_nothing():
    assert resolve(3)[:2] == (None, [])
    assert resolve(-1)[:2] == (None, [])  # Esc


def test_macos_explains_airplay_on_port_5000():
    def notes(port, mac):
        out = []
        with (
            patch.object(ports, "is_port_free", lambda p, *a: False),
            patch.object(ports, "get_port_process", lambda p: (None, None)),
            patch.object(ports, "ask_arrow", lambda *a, **k: 3),
            patch.object(ports, "warn", out.append),
            patch.object(ports, "info", out.append),
            patch.object(ports, "IS_MAC", mac),
        ):
            ports.ensure_port_free(port, {}, "translation")
        return out

    assert any("AirPlay" in line for line in notes(5000, True))
    assert not any("AirPlay" in line for line in notes(5001, True))
    assert not any("AirPlay" in line for line in notes(5000, False))


def test_the_prompt_names_what_it_would_kill():
    def kill_label(owner):
        options = []
        with (
            patch.object(ports, "is_port_free", lambda p, *a: False),
            patch.object(ports, "get_port_process", lambda p: owner),
            patch.object(ports, "ask_arrow", lambda prompt, opts, **k: options.extend(opts) or 3),
            patch.object(ports, "warn", lambda m: None),
        ):
            ports.ensure_port_free(5001, {}, "whisper")
        return options[1][1]

    assert kill_label((42, "app.exe")) == "Terminate app.exe"
    assert kill_label((42, None)) == "Terminate PID 42"
    assert kill_label((None, None)) == "Terminate unknown process"


# ── kill_port_process ────────────────────────────────────────────────────────


def kill(*, ours=True, yes=True, interactive=True, frees=True, win=True, owner=(4242, "python.exe")):
    """kill_port_process(5001). Returns (result, commands run, output)."""
    ran, out = [], []
    free = iter([False] + [frees] * 3)

    def run(cmd, **kw):
        ran.append(cmd)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    with (
        patch.object(yume.utils, "_run", run),
        patch.object(ports, "IS_WIN", win),
        patch.object(ports, "is_port_free", lambda p: next(free)),
        patch.object(ports, "get_port_process", lambda p: owner),
        patch.object(ports, "is_yume_process", lambda pid, name: ours),
        patch.object(ports, "ask_yn", lambda *a, **k: yes),
        patch.object(ports.time, "sleep", lambda s: None),
        patch.object(ports, "info", out.append),
        patch.object(ports, "warn", out.append),
    ):
        return ports.kill_port_process(5001, interactive=interactive), ran, out


def test_yumes_own_server_is_killed_without_asking():
    ok, ran, out = kill()
    assert ok is True and ran == [["taskkill", "/F", "/PID", "4242"]]
    assert any("Killed python.exe (PID 4242)" in line for line in out)
    assert kill(win=False)[1] == [["kill", "-9", "4242"]]


def test_another_program_is_only_killed_after_a_yes():
    ok, ran, out = kill(ours=False, yes=False)
    assert ok is False and ran == [] and any("Left it running" in line for line in out)
    assert kill(ours=False, yes=True)[0] is True


def test_another_program_is_left_alone_without_a_person_to_ask():
    ok, ran, out = kill(ours=False, interactive=False)
    assert ok is False and ran == [] and any("leaving it alone" in line for line in out)


def test_an_unknown_owner_is_never_killed():
    ok, ran, out = kill(owner=(None, None))
    assert ok is False and ran == [] and any("could not be identified" in line for line in out)


def test_a_port_still_busy_after_the_kill_reports_failure():
    assert kill(frees=False)[0] is False


def test_a_free_port_needs_no_kill():
    with patch.object(ports, "is_port_free", lambda p: True):
        assert ports.kill_port_process(5001) is True


# ── the Deno PO-token helper ─────────────────────────────────────────────────


def bgutil(owner, *, free=False, win=True):
    ran = []
    with (
        patch.object(yume.utils, "_run", lambda cmd, **k: ran.append(cmd)),
        patch.object(ports, "IS_WIN", win),
        patch.object(ports, "is_port_free", lambda p: free),
        patch.object(ports, "get_port_process", lambda p: owner),
    ):
        ports.stop_bgutil_server()
    return ran


def test_only_deno_on_the_token_port_is_stopped():
    assert bgutil((7, "deno.exe")) == [["taskkill", "/F", "/PID", "7"]]
    assert bgutil((7, "deno"), win=False) == [["kill", "7"]]
    assert bgutil((7, "other.exe")) == [] and bgutil((None, None)) == [] and bgutil((7, "deno"), free=True) == []
