"""yume/servers.py: the command lines, environment and models the servers start
with, and how children are spawned and stopped. Everything is faked: no server
starts and no real port is probed."""

from __future__ import annotations

import subprocess
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from yume import servers  # noqa: E402

NVIDIA = {"has_nvidia": True, "has_amd": False, "vram_mb": 12288}
SMALL_NVIDIA = {"has_nvidia": True, "has_amd": False, "vram_mb": 4096}
AMD = {"has_nvidia": False, "has_amd": True, "vram_mb": 8192}
CPU = {"has_nvidia": False, "has_amd": False, "vram_mb": 0}

CFG = {
    "whisper_host": "127.0.0.1",
    "whisper_port": 59981,
    "whisper_model": "small",
    "whisper_device": "auto",
    "whisper_compute_type": "auto",
    "translation_host": "127.0.0.1",
    "translation_port": 59982,
}


@pytest.fixture
def logs(tmp_path, monkeypatch):
    monkeypatch.setattr(servers, "LOGS_DIR", tmp_path)
    return tmp_path


class TestInterpreters:
    def _exe(self, tmp_path, monkeypatch, name, *siblings):
        for n in (name, *siblings):
            (tmp_path / n).write_bytes(b"")
        monkeypatch.setattr(servers.sys, "executable", str(tmp_path / name))
        return tmp_path

    def test_console_python_leaves_pythonw(self, tmp_path, monkeypatch):
        d = self._exe(tmp_path, monkeypatch, "pythonw.exe", "python.exe")
        assert servers.console_python() == str(d / "python.exe")

    def test_console_python_without_a_sibling_keeps_the_interpreter(self, tmp_path, monkeypatch):
        d = self._exe(tmp_path, monkeypatch, "pythonw.exe")
        assert servers.console_python() == str(d / "pythonw.exe")

    def test_windowless_python_is_for_windows_only(self, tmp_path, monkeypatch):
        d = self._exe(tmp_path, monkeypatch, "python.exe", "pythonw.exe")
        monkeypatch.setattr(servers, "IS_WIN", True)
        assert servers.windowless_python() == str(d / "pythonw.exe")
        monkeypatch.setattr(servers, "IS_WIN", False)
        assert servers.windowless_python() == str(d / "python.exe")


class TestEnvironment:
    def test_tools_folder_goes_first_on_path_once(self, tmp_path, monkeypatch):
        monkeypatch.setattr(servers, "TOOLS_DIR", tmp_path / "tools")
        monkeypatch.setattr(servers, "IS_WIN", True)  # no AMD probing
        monkeypatch.setenv("PATH", "elsewhere")
        env = servers.build_server_env()
        assert env["PATH"].split(servers.os.pathsep) == [str(tmp_path / "tools"), "elsewhere"]
        monkeypatch.setenv("PATH", env["PATH"])
        assert servers.build_server_env()["PATH"] == env["PATH"]

    @pytest.mark.parametrize(("gfx", "override"), [("gfx1010", True), ("gfx1030", False)])
    def test_rdna1_needs_the_rocm_override(self, monkeypatch, gfx, override):
        monkeypatch.delenv("HSA_OVERRIDE_GFX_VERSION", raising=False)
        monkeypatch.setattr(servers, "IS_WIN", False)
        monkeypatch.setattr(servers, "detect_gpu", lambda: AMD)
        monkeypatch.setattr(
            servers, "_run", lambda cmd, **k: types.SimpleNamespace(returncode=0, stdout=f"Name: {gfx}")
        )
        assert ("HSA_OVERRIDE_GFX_VERSION" in servers.build_server_env()) is override

    def test_a_failing_rocminfo_is_not_fatal(self, monkeypatch):
        def boom(cmd, **k):
            raise OSError("no rocminfo")

        monkeypatch.delenv("HSA_OVERRIDE_GFX_VERSION", raising=False)
        monkeypatch.setattr(servers, "IS_WIN", False)
        monkeypatch.setattr(servers, "detect_gpu", lambda: AMD)
        monkeypatch.setattr(servers, "_run", boom)
        assert "HSA_OVERRIDE_GFX_VERSION" not in servers.build_server_env()


class TestWhisperCommand:
    @pytest.mark.parametrize(
        ("gpu", "win", "expected"),
        [
            (NVIDIA, True, ("cuda", "float16")),
            (SMALL_NVIDIA, True, ("cuda", "int8_float16")),
            (AMD, False, ("cuda", "float16")),
            (AMD, True, ("cpu", "int8")),  # ROCm is Linux only
            (CPU, False, ("cpu", "int8")),
        ],
    )
    def test_auto_resolves_for_the_machine(self, monkeypatch, gpu, win, expected):
        monkeypatch.setattr(servers, "detect_gpu", lambda: gpu)
        monkeypatch.setattr(servers, "IS_WIN", win)
        assert servers.resolve_whisper_device(CFG) == expected

    def test_an_explicit_choice_is_kept(self, monkeypatch):
        monkeypatch.setattr(servers, "detect_gpu", lambda: NVIDIA)
        cfg = {**CFG, "whisper_device": "cpu", "whisper_compute_type": "float32"}
        assert servers.resolve_whisper_device(cfg) == ("cpu", "float32")

    def test_command_passes_the_config_only_when_it_exists(self, tmp_path, monkeypatch):
        import config

        (tmp_path / "faster_whisper_server.py").write_text("")
        monkeypatch.setattr(servers, "SERVER_DIR", tmp_path)
        monkeypatch.setattr(servers, "detect_gpu", lambda: CPU)
        monkeypatch.setattr(config, "CONFIG_FILE", tmp_path / "yume_config.json")
        cmd = servers.whisper_command(CFG)
        assert cmd[:2] == [sys.executable, str(tmp_path / "faster_whisper_server.py")]
        assert cmd[cmd.index("--port") + 1] == "59981" and cmd[cmd.index("--model") + 1] == "small"
        assert "--config" not in cmd
        (tmp_path / "yume_config.json").write_text("{}")
        assert servers.whisper_command(CFG)[-2:] == ["--config", str(tmp_path / "yume_config.json")]

    def test_a_missing_server_script_gives_no_command(self, tmp_path, monkeypatch):
        monkeypatch.setattr(servers, "SERVER_DIR", tmp_path)
        assert servers.whisper_command(CFG) is None


class TestLlamaCommand:
    def _no_native(self, monkeypatch):
        import yume.llama_server

        monkeypatch.setattr(yume.llama_server, "server_path", lambda: None)

    @pytest.mark.parametrize(("gpu", "win", "offload"), [(NVIDIA, True, True), (AMD, False, True), (AMD, True, False)])
    def test_python_fallback_offloads_only_where_a_gpu_build_exists(self, monkeypatch, gpu, win, offload):
        self._no_native(monkeypatch)
        monkeypatch.setattr(servers, "detect_gpu", lambda: gpu)
        monkeypatch.setattr(servers, "IS_WIN", win)
        cmd = servers.llamacpp_command(CFG, "m.gguf", 59982)
        assert cmd[:3] == [sys.executable, "-m", "llama_cpp.server"]
        assert cmd[cmd.index("--model") + 1] == "m.gguf" and cmd[cmd.index("--port") + 1] == "59982"
        assert ("--n_gpu_layers" in cmd) is offload

    @pytest.mark.parametrize(("variant", "gpu_build"), [("cuda", True), ("cpu", False)])
    def test_llama_server_is_preferred(self, monkeypatch, variant, gpu_build):
        import yume.llama_server as ls

        got = []
        monkeypatch.setattr(ls, "server_path", lambda: "llama-server")
        monkeypatch.setattr(ls, "installed_build", lambda: {"variant": variant})
        monkeypatch.setattr(ls, "command", lambda *a: got.append(a) or ["llama-server"])
        assert servers.llamacpp_command(CFG, "m.gguf", 59982) == ["llama-server"]
        assert got == [("m.gguf", "127.0.0.1", 59982, gpu_build)]


class TestResolveGguf:
    def test_the_configured_model_wins(self, tmp_path, monkeypatch):
        gguf = tmp_path / "chosen.gguf"
        gguf.write_bytes(b"")
        monkeypatch.setattr(servers, "find_gguf_models", lambda: pytest.fail("must not scan"))
        assert servers.resolve_gguf({"gguf_model_path": str(gguf)}) == str(gguf)

    def test_a_moved_model_falls_back_to_the_folder_and_is_saved(self, tmp_path, monkeypatch):
        import config

        found, saved = tmp_path / "found.gguf", []
        monkeypatch.setattr(servers, "find_gguf_models", lambda: [found])
        monkeypatch.setattr(config, "save_config", lambda c: saved.append(c["gguf_model_path"]))
        cfg = {"gguf_model_path": str(tmp_path / "gone.gguf")}
        assert servers.resolve_gguf(cfg) == str(found)
        assert saved == [str(found)] and cfg["gguf_model_path"] == str(found)

    def test_no_model_at_all(self, monkeypatch):
        monkeypatch.setattr(servers, "find_gguf_models", lambda: [])
        assert servers.resolve_gguf({"gguf_model_path": ""}) is None


class TestLogs:
    def test_tail(self, logs):
        (logs / "x.log").write_text("\n".join(f"line {i}" for i in range(30)), encoding="utf-8")
        tail = servers.log_tail("x.log", lines=2)
        assert tail == ["line 28", "line 29"]

    def test_tail_of_a_missing_log_is_empty(self, logs):
        assert servers.log_tail("none.log") == []

    def test_rotation_keeps_the_older_generations(self, logs):
        (logs / "w.log").write_bytes(b"x" * (5 * 1024 * 1024 + 1))
        (logs / "w.log.1").write_text("older")
        (logs / "w.log.2").write_text("old")
        (logs / "w.log.3").write_text("oldest")
        servers.open_rotating_log("w.log").close()
        assert (logs / "w.log.2").read_text() == "older" and (logs / "w.log.3").read_text() == "old"
        assert not (logs / "w.log.4").exists()  # three backups at most
        assert (logs / "w.log").stat().st_size == 0


class TestDiagnosis:
    @pytest.mark.parametrize(
        ("name", "log", "first_words"),
        [
            ("Whisper", "CUDA failed to alloc memory", "The GPU does not have enough"),
            ("Translation", "cuda: out of memory", "The GPU does not have enough"),
            ("Whisper", "cannot load cublas64_12.dll", "CUDA libraries are missing"),
            ("Whisper", "ModuleNotFoundError: No module named 'flask'", "A Python package is missing"),
            ("Ollama", "listen tcp: bind: address already in use", "Its port is already in use"),
            ("Whisper", "OSError: [Errno 13] Permission denied", "Permission denied"),
            ("Whisper", "ConnectionError: huggingface.co timeout", "A network error"),
        ],
    )
    def test_each_known_crash(self, name, log, first_words):
        assert servers.diagnose(name, [log])[0].startswith(first_words)

    def test_advice_is_only_given_for_servers_it_applies_to(self):
        assert servers.diagnose("Ollama", ["cublas64_12.dll not found"]) == []
        assert servers.diagnose("Whisper", ["something else entirely"]) == []

    def test_the_list_is_a_copy(self):
        servers.diagnose("Whisper", ["no module named x"]).append("changed")
        assert "changed" not in servers.diagnose("Whisper", ["no module named x"])


class TestSpawnAndStop:
    def _popen(self, monkeypatch):
        calls = []

        def popen(cmd, **kw):
            calls.append((cmd, kw))
            return MagicMock(pid=1)

        monkeypatch.setattr(servers.subprocess, "Popen", popen)
        return calls

    def test_hidden_children_run_on_the_console_python_without_a_window(self, logs, monkeypatch):
        calls = self._popen(monkeypatch)
        monkeypatch.setattr(servers, "console_python", lambda: "python.exe")
        monkeypatch.setattr(servers, "IS_WIN", True)
        servers.Processes(hidden=True).spawn("Whisper", [sys.executable, "s.py"], "w.log", {})
        cmd, kw = calls[0]
        assert cmd == ["python.exe", "s.py"] and kw["creationflags"] == servers.CREATE_NO_WINDOW

    def test_other_programs_keep_their_command_and_visible_children_their_console(self, logs, monkeypatch):
        calls = self._popen(monkeypatch)
        monkeypatch.setattr(servers, "IS_WIN", True)
        servers.Processes(hidden=True).spawn("Ollama", ["ollama", "serve"], "o.log", {})
        servers.Processes().spawn("Whisper", [sys.executable, "s.py"], "w.log", {})
        assert calls[0][0] == ["ollama", "serve"]
        assert calls[1][0][0] == sys.executable and calls[1][1]["creationflags"] == 0

    def _procs(self, monkeypatch, *names):
        import yume.ports

        freed, helper = [], []
        monkeypatch.setattr(yume.ports, "is_port_free", lambda port: False)
        monkeypatch.setattr(yume.ports, "kill_port_process", lambda port, interactive=True: freed.append(port) or True)
        monkeypatch.setattr(yume.ports, "stop_bgutil_server", lambda: helper.append(1))
        procs = servers.Processes()
        for name in names:
            procs.procs.append((name, MagicMock()))
        return procs, freed, helper

    def test_stop_frees_only_the_ports_of_servers_yume_started(self, monkeypatch):
        procs, freed, helper = self._procs(monkeypatch, "Whisper")
        procs.stop(CFG)
        assert freed == [59981] and helper == [1]

    def test_stop_never_touches_an_ollama_port(self, monkeypatch):
        procs, freed, helper = self._procs(monkeypatch, "Ollama")
        procs.stop({**CFG, "translation_port": 11434})
        assert freed == [] and helper == []  # and no PO-token helper without Whisper

    def test_stop_asks_nobody(self, monkeypatch):
        import yume.ports

        procs, _f, _h = self._procs(monkeypatch, "Translation")
        seen = []
        monkeypatch.setattr(yume.ports, "kill_port_process", lambda port, interactive=True: seen.append(interactive))
        procs.stop(CFG)
        assert seen == [False]  # never a prompt, and never another program's process

    def test_stop_kills_a_child_that_ignores_terminate(self, monkeypatch):
        procs, _f, _h = self._procs(monkeypatch, "Translation")
        child = procs.procs[0][1]
        child.wait.side_effect = [subprocess.TimeoutExpired("x", 1), 0]
        procs.stop(CFG, timeout=0)
        child.kill.assert_called_once()

    def test_newest_child_stops_first(self, monkeypatch):
        procs, _f, _h = self._procs(monkeypatch, "Translation", "Whisper")
        order = []
        for name, child in procs.procs:
            child.terminate.side_effect = lambda name=name: order.append(name)
        procs.stop(CFG)
        assert order == ["Whisper", "Translation"]

    def test_dead_reports_the_exit_code(self):
        procs = servers.Processes()
        procs.procs = [("Whisper", MagicMock(poll=lambda: None)), ("Ollama", MagicMock(poll=lambda: 3, returncode=3))]
        assert procs.dead() == ["Ollama (exit code 3)"]


class TestReadiness:
    @pytest.mark.parametrize(
        ("data", "expected"),
        [
            ({"status": "ready"}, None),
            ({"status": "loading"}, ""),
            ({}, ""),
            ({"status": "error", "error": "no VRAM"}, "the model failed to load: no VRAM"),
            ({"status": "error"}, "the model failed to load: unknown error"),
        ],
    )
    def test_whisper_ready(self, monkeypatch, data, expected):
        monkeypatch.setattr("yume.network.check_server", lambda *a: {"up": bool(data), "data": data})
        assert servers.whisper_ready(CFG) == expected

    def test_a_model_load_error_ends_the_wait_with_the_diagnosis(self, logs, monkeypatch):
        (logs / "w.log").write_text("cuda: out of memory", encoding="utf-8")
        p = MagicMock(poll=lambda: None)
        r = servers._started("Whisper", p, "w.log", lambda: "the model failed to load: x", 5, None, None)
        assert r.error == "Whisper: the model failed to load: x" and not r.timed_out
        assert r.diagnosis[0].startswith("The GPU does not have enough")

    def test_a_timeout_blames_nothing(self, logs, monkeypatch):
        (logs / "w.log").write_text("cuda: out of memory", encoding="utf-8")
        monkeypatch.setattr(servers.time, "sleep", lambda s: None)
        p = MagicMock(poll=lambda: None)
        r = servers._started("Whisper", p, "w.log", lambda: "", 0, None, None)
        assert r.timed_out and r.diagnosis == []


class TestStart:
    def test_whisper_without_its_script(self, monkeypatch):
        monkeypatch.setattr(servers, "whisper_up", lambda cfg: False)
        monkeypatch.setattr(servers, "whisper_command", lambda cfg: None)
        assert "re-extract" in servers.start_whisper(CFG, servers.Processes(), {}).error

    def test_translation_port_held_by_another_program(self, logs, monkeypatch):
        import yume.ports

        monkeypatch.setattr(servers, "translation_up", lambda cfg, bk: False)
        monkeypatch.setattr(servers, "resolve_gguf", lambda cfg: "m.gguf")
        monkeypatch.setattr(yume.ports, "kill_port_process", lambda port, interactive=True: False)
        procs = servers.Processes()
        r = servers.start_llamacpp(CFG, {}, procs, {})
        assert r.error == "translation port 59982 is used by another program" and not procs

    def test_translation_already_up_is_used(self, monkeypatch):
        monkeypatch.setattr(servers, "translation_up", lambda cfg, bk: True)
        assert servers.start_llamacpp(CFG, {}, servers.Processes(), {}).already_running

    def test_llama_children_write_utf8(self, logs, monkeypatch):
        import yume.ports

        seen = {}
        states = iter([False, True])
        monkeypatch.setattr(servers, "translation_up", lambda cfg, bk: next(states))
        monkeypatch.setattr(servers, "resolve_gguf", lambda cfg: "m.gguf")
        monkeypatch.setattr(servers, "llamacpp_command", lambda *a: ["llama-server"])
        monkeypatch.setattr(yume.ports, "kill_port_process", lambda port, interactive=True: True)
        monkeypatch.setattr(servers.time, "sleep", lambda s: None)

        def spawn(self, name, cmd, log_name, env):
            seen.update(env)
            return MagicMock(poll=lambda: None)

        monkeypatch.setattr(servers.Processes, "spawn", spawn)
        assert servers.start_llamacpp(CFG, {}, servers.Processes(), {"A": "1"}).ok
        assert seen == {"A": "1", "PYTHONUTF8": "1"}
