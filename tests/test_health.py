"""The health check, with every probe faked: which rows it shows and what fixes it suggests."""

from __future__ import annotations

import sys
import types
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import yume.health as health  # noqa: E402
from yume import fonts  # noqa: E402

GPU = {"has_nvidia": True, "has_amd": False, "vram_mb": 12288, "name": "RTX 3060", "vendor": "nvidia"}


class Check:
    """Fakes for one health check run; `rows` and `advice` hold what it printed."""

    def __init__(self, **over):
        self.cfg = {"translation_backend": "llamacpp", "whisper_port": 5001, "translation_port": 5000}
        self.cfg.update(over.pop("cfg", {}))
        self.tools = over.pop("tools", {"yt-dlp", "ffmpeg", "ffprobe", "deno"})
        self.whisper = over.pop("whisper", {"status": "ready"})  # /health JSON, {} when down
        self.translation_up = over.pop("translation_up", True)
        self.free_ports = over.pop("free_ports", set())
        self.port_owner = over.pop("port_owner", (1234, "other.exe"))
        self.e2e = over.pop("e2e", {"success": True, "translation": "Nice weather today"})
        self.native_llama = over.pop("native_llama", True)
        self.ggufs = over.pop("ggufs", [Path("qwen.gguf")])
        assert not over, over
        self.rows: list[tuple] = []
        self.advice: list[str] = []

    def _table(self, headers, rows, **kw):
        for name, result, detail in rows:
            self.rows.append((name, "PASS" in result, detail))

    def row(self, name):
        return next(r for r in self.rows if r[0] == name)

    def go(self):
        import yume.llama_server
        import yume.network
        import yume.utils
        import yume.vocals

        def run(cmd, **kw):
            return types.SimpleNamespace(returncode=0, stdout="pip 25.3 from x", stderr="")

        ls = MagicMock()
        ls.server_path.return_value = "llama-server.exe" if self.native_llama else None
        ls.build_label.return_value = "llama-server b9000 (cuda)"
        ls.python_engine_on_gpu = yume.llama_server.python_engine_on_gpu
        fakes = {
            "detect_gpu": MagicMock(return_value=GPU),
            "detect_ram_gb": MagicMock(return_value=32.0),
            "disk_free_gb": MagicMock(return_value=100.0),
            "find_tool": lambda t: f"/tools/{t}" if t in self.tools else None,
            "check_server": lambda host, port, path: {"up": bool(self.whisper), "data": dict(self.whisper)},
            "check_translation_server": lambda h, p, bi: {"up": self.translation_up, "data": {}},
            "is_port_free": lambda port: port in self.free_ports,
            "get_port_process": MagicMock(return_value=self.port_owner),
            "find_gguf_models": MagicMock(return_value=self.ggufs),
            "header": MagicMock(),
            "panel": MagicMock(),
            "table": self._table,
            "info": lambda msg="", *a: self.advice.append(str(msg)),
            "warn": lambda msg="", *a: self.advice.append(str(msg)),
            "success": MagicMock(),
        }
        with ExitStack() as stack:
            for name, fake in fakes.items():
                stack.enter_context(patch.object(health, name, fake))
            stack.enter_context(patch.object(yume.utils, "_run", run))
            stack.enter_context(patch.object(yume.utils, "missing_requirements", lambda: []))
            stack.enter_context(patch.object(yume.network, "server_post", lambda *a, **k: self.e2e))
            stack.enter_context(patch.object(yume.vocals, "health_row", lambda cfg: ("Vocal isolation", True, "off")))
            stack.enter_context(patch("yume.llama_server", ls, create=True))
            stack.enter_context(patch("config.load_config", lambda: dict(self.cfg)))
            health.health_check(self.cfg)
        return self


def test_a_running_yume_passes_with_the_end_to_end_translation():
    c = Check().go()
    assert c.row("Whisper port 5001")[1:] == (True, "in use by the running server")
    assert c.row("Translation port 5000")[1:] == (True, "in use by the running server")
    assert c.row("Whisper server responding")[1:] == (True, "status: ready")
    name, ok, detail = c.row("Translation end-to-end")
    assert ok and detail.endswith("Nice weather today")
    assert c.row("Translation engine")[1:] == (True, "llama-server b9000 (cuda)")
    assert c.row("GGUF model present")[1:] == (True, "qwen.gguf")


def test_rows_come_in_display_order():
    names = [r[0] for r in Check().go().rows]
    order = ["GPU detection", "yt-dlp installed", "pip available", "Server packages", "Translation engine",
             "Japanese by word", "Vocal isolation", "Config loadable", "Translation port 5000",
             "Whisper port 5001", "Whisper server script", "GGUF model present"]  # fmt: skip
    assert [n for n in names if n in order] == order


def test_a_stopped_yume_is_fine_when_its_ports_are_free():
    c = Check(whisper={}, translation_up=False, free_ports={5000, 5001}).go()
    assert c.row("Whisper port 5001")[1:] == (True, "free — starts on Launch")
    assert c.row("Translation port 5000")[1:] == (True, "free — starts on Launch")
    assert not any(r[0] == "Whisper server responding" for r in c.rows)


def test_a_port_held_by_another_program_fails_with_a_fix():
    c = Check(whisper={}, translation_up=False, free_ports={5000}).go()
    assert c.row("Whisper port 5001")[1:] == (False, "held by other.exe (PID 1234), not by the server")
    assert any("Free port 5001" in line for line in c.advice)


def test_an_external_llm_that_is_down_fails():
    c = Check(cfg={"translation_backend": "ollama"}, translation_up=False, free_ports={5000}).go()
    assert c.row("Translation port 5000")[1:] == (False, "nothing listening")
    assert not any(r[0] == "GGUF model present" for r in c.rows)


def test_a_translation_server_on_another_machine_passes():
    # Its port is free HERE, which read as "nothing listening" although it answered
    cfg = {"translation_backend": "custom", "translation_host": "192.168.1.5"}
    c = Check(cfg=cfg, free_ports={5000}).go()
    assert c.row("Translation port 5000")[1:] == (True, "in use by the running server")


def test_a_missing_tool_fails_with_a_download_link():
    c = Check(tools={"ffmpeg", "ffprobe", "deno"}).go()
    assert c.row("yt-dlp installed")[1:] == (False, "not found")
    assert any("yt-dlp/releases" in line for line in c.advice)


def test_deno_is_only_checked_without_cookie_auth():
    assert any(r[0] == "deno installed" for r in Check().go().rows)
    assert not any(r[0] == "deno installed" for r in Check(cfg={"youtube_auth_method": "cookies"}).go().rows)


def test_a_failed_translation_points_at_the_llm_settings():
    c = Check(e2e={"success": False, "error": "model not found"}).go()
    assert c.row("Translation end-to-end")[1:] == (False, "model not found")
    assert any("Translation settings" in line for line in c.advice)


def test_a_whisper_error_status_fails_with_restart_advice():
    c = Check(whisper={"status": "error", "error": "CUDA out of memory"}).go()
    assert c.row("Whisper server responding")[1:] == (False, "status: error — CUDA out of memory")
    assert not any(r[0] == "Translation end-to-end" for r in c.rows)
    assert any("pocket_yume.py launch" in line for line in c.advice)


def test_no_translation_model_says_where_to_get_one():
    # A new user's first health check: the row failed with no advice at all
    c = Check(ggufs=[]).go()
    assert c.row("GGUF model present")[1:] == (False, "none in models/translation/")
    assert any("Download Translation Model" in line for line in c.advice)


def test_a_cpu_only_llama_cpp_python_points_at_the_gpu_engine(monkeypatch):
    # llama-cpp-python without CUDA: re-running setup reinstalls the same CPU
    # wheel; Tools → Translation Engine installs llama.cpp's prebuilt GPU server
    lib = types.ModuleType("llama_cpp.llama_cpp")
    monkeypatch.setitem(sys.modules, "llama_cpp", types.ModuleType("llama_cpp"))
    monkeypatch.setitem(sys.modules, "llama_cpp.llama_cpp", lib)
    c = Check(native_llama=False).go()
    assert c.row("llama-cpp CUDA support")[1] is False
    assert any("Translation Engine" in line for line in c.advice)


def test_status_without_a_translation_model_says_where_to_get_one(capsys):
    import yume.benchmark
    import yume.status

    with (
        patch.object(yume.status, "find_gguf_models", return_value=[]),
        patch.object(yume.benchmark, "_is_whisper_model_cached", return_value=True),
    ):
        yume.status._status_models({"whisper_model": "small", "gguf_model_path": ""})
    out = capsys.readouterr().out
    assert "no GGUF models found" in out and "Download Translation Model" in out


def test_issue_summary_lists_the_failures():
    c = Check(tools=set()).go()
    summary = next(line for line in c.advice if "issue(s) found" in line)
    assert "yt-dlp installed" in summary and "ffmpeg installed" in summary


def test_fonts_are_found_by_their_windows_file_names():
    files = [
        Path(f"C:/Windows/Fonts/{n}") for n in ("msyh.ttc", "YuGothR.ttc", "malgun.ttf", "simsun.ttc", "arial.ttf")
    ]
    found = {script: fonts._matching(files, patterns) for script, (patterns, _) in fonts._SCRIPT_FONTS.items()}
    assert found == {
        "Japanese": ["YuGothR.ttc"],
        "Chinese": ["msyh.ttc", "simsun.ttc"],
        "Korean": ["malgun.ttf"],
        "Arabic": [],
    }
    assert fonts._SCRIPT_FONTS["Arabic"][1] == "Noto Naskh Arabic"  # was "Noto Sans AR", which does not exist


def test_reports_run_as_commands_do_not_wait_for_enter(monkeypatch):
    """`pocket_yume.py status | health | fonts` printed their report, then sat at
    "Press Enter to continue..."; the menu still waits so its next screen does
    not wipe the report."""
    import yume.cli as cli
    import yume.menus.tools as tools
    import yume.status

    for fn in (yume.status.show_status, health.health_check, fonts.detect_fonts):
        assert "pause" not in fn.__code__.co_names, fn.__name__
    waited = []
    monkeypatch.setattr(cli, "pause", lambda *a: waited.append(1))
    assert cli._then_pause(lambda cfg, args: "report")({}, []) == "report" and waited == [1]
    menu = {label: fn for label, _d, fn in cli.MAIN_MENU}
    for label, command in (("System Status", cli._status), ("Health Check", cli._health)):
        assert menu[label].__closure__[0].cell_contents is command  # the menu waits...
    assert cli.COMMANDS["status"].run is cli._status  # ...the command does not
    assert "pause" in tools._fonts.__code__.co_names


def _broken_package(tmp_path, monkeypatch):
    """What llama_cpp does when its library will not load (a CUDA DLL missing)."""
    (tmp_path / "broken_pkg_xyz.py").write_text('raise RuntimeError("Failed to load shared library")\n')
    monkeypatch.syspath_prepend(tmp_path)
    return "broken_pkg_xyz"


def test_a_package_that_fails_to_load_is_a_failed_check_not_a_crash(tmp_path, monkeypatch):
    pkg = _broken_package(tmp_path, monkeypatch)
    assert health._chk_pkg(pkg, "Broken") == ("Broken", False, "fails to load: Failed to load shared library")
    assert health._chk_roma(pkg, "x")[1:] == (True, "optional, not installed — x will use the LLM (slower)")


def test_status_survives_a_package_that_fails_to_load(tmp_path, monkeypatch):
    import yume.utils

    assert yume.utils._try_import(_broken_package(tmp_path, monkeypatch)) is False
