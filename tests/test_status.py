"""`pocket_yume.py status` and `ports`: what they print, with every probe faked."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import yume.health as status  # noqa: E402
import yume.ports as ports  # noqa: E402

NVIDIA = {"has_nvidia": True, "has_amd": False, "vram_mb": 12288, "name": "RTX 3060"}
AMD = {"has_nvidia": False, "has_amd": True, "vram_mb": 0, "name": "RX 6800"}
CPU = {"has_nvidia": False, "has_amd": False, "vram_mb": 0, "name": None}
CFG = {
    "whisper_host": "127.0.0.1", "whisper_port": 5001, "translation_host": "127.0.0.1", "translation_port": 5000,
    "translation_backend": "llamacpp", "whisper_model": "small", "gguf_model_path": "",
}  # fmt: skip


def table_rows(fn, *args):
    """The rows a report function hands to table()."""
    rows = []
    with patch.object(status, "table", lambda headers, r, **k: rows.extend(r)):
        fn(*args)
    return rows


def hardware(gpu, ram=32.0, disk=100.0):
    status._hw_cache = None
    with (
        patch.object(status, "detect_gpu", lambda: gpu),
        patch.object(status, "detect_ram_gb", lambda: ram),
        patch.object(status, "disk_free_gb", lambda: disk),
    ):
        rows = table_rows(status._status_hardware)
    status._hw_cache = None
    return {r[0]: r[1] for r in rows}


def test_hardware_names_the_gpu():
    assert "RTX 3060 (12288 MB)" in hardware(NVIDIA)["GPU"]
    assert "RX 6800" in hardware(AMD)["GPU"] and "MB" not in hardware(AMD)["GPU"]
    assert "None (CPU mode)" in hardware(CPU)["GPU"]


def test_hardware_flags_low_memory_and_disk():
    rows = hardware(CPU, ram=4.0, disk=5.0)
    assert "low" in rows["RAM"] and "low!" in rows["Disk"]
    rows = hardware(CPU, ram=16.0, disk=50.0)
    assert "OK" in rows["RAM"] and "OK" in rows["Disk"]


def test_hardware_is_cached_for_a_while():
    status._hw_cache = None
    probe = MagicMock(return_value=CPU)
    with (
        patch.object(status, "detect_gpu", probe),
        patch.object(status, "detect_ram_gb", lambda: 1.0),
        patch.object(status, "disk_free_gb", lambda: 1.0),
    ):
        status._hardware()
        status._hardware()
    status._hw_cache = None
    assert probe.call_count == 1


def test_tools_row_per_tool():
    with patch.object(status, "find_tool", lambda t: "/tools/ffmpeg" if t == "ffmpeg" else None):
        rows = table_rows(status._status_tools, {"youtube_auth_method": "cookies"})
        names = {r[0]: r for r in rows}
        assert "MISSING" in names["yt-dlp"][1] and "installed" in names["ffmpeg"][1]
        assert "skipped" in names["deno"][1]
        rows = table_rows(status._status_tools, {"youtube_auth_method": "deno"})
    assert "MISSING" in {r[0]: r for r in rows}["deno"][1]


def test_packages_list_llama_cpp_python_only_when_it_is_the_engine():
    import yume.llama_server

    def names(cfg, server):
        with patch.object(yume.llama_server, "server_path", lambda: server):
            return [r[0] for r in table_rows(status._status_packages, cfg)]

    assert "llama-cpp-python" in names(CFG, None)
    assert "llama-cpp-python" not in names(CFG, Path("llama-server"))
    assert "llama-cpp-python" not in names({**CFG, "translation_backend": "ollama"}, None)


def test_servers_fall_back_to_the_openai_path():
    paths = []

    def check(host, port, path):
        paths.append((port, path))
        return {"up": path == "/v1/models", "data": {}}

    with patch.object(status, "check_server", check):
        rows = table_rows(status._status_servers, {**CFG, "translation_backend": "ollama"})
    assert (5000, "/api/tags") in paths and (5000, "/v1/models") in paths
    assert "stopped" in rows[0][2] and "running" in rows[1][2] and rows[1][0] == "Ollama"


def test_models_report_custom_and_cached_whisper_models(capsys, tmp_path):
    import yume.benchmark

    with (
        patch.object(status, "find_gguf_models", return_value=[]),
        patch.object(yume.benchmark, "_is_whisper_model_cached", return_value=False),
    ):
        status._status_models({**CFG, "whisper_model": str(tmp_path)})
        assert "custom:" in capsys.readouterr().out
        status._status_models(CFG)
        assert "not downloaded" in capsys.readouterr().out
    with (
        patch.object(status, "find_gguf_models", return_value=[]),
        patch.object(yume.benchmark, "_is_whisper_model_cached", return_value=True),
    ):
        status._status_models(CFG)
    assert "downloaded" in capsys.readouterr().out


def test_models_report_the_gguf_in_use_or_those_available(capsys, tmp_path):
    import yume.benchmark

    gguf = tmp_path / "m.gguf"
    gguf.write_bytes(b"x")
    with (
        patch.object(status, "find_gguf_models", return_value=[gguf]),
        patch.object(yume.benchmark, "_is_whisper_model_cached", return_value=True),
    ):
        status._status_models({**CFG, "gguf_model_path": str(gguf)})
        out = capsys.readouterr().out
        assert "Translation:" in out and "m.gguf" in out and "1 GGUF file(s)" not in out
        status._status_models(CFG)
    assert "1 GGUF file(s)" in capsys.readouterr().out


def test_status_ports_say_who_holds_a_busy_port(capsys):
    with (
        patch.object(status, "is_port_free", lambda port, host: port == 5001),
        patch.object(status, "get_port_process", lambda port: (77, "other.exe")),
    ):
        status._status_ports(CFG)
    out = capsys.readouterr().out
    assert "Whisper      127.0.0.1:5001  " in out and "available" in out
    assert "Translation  127.0.0.1:5000  " in out and "in use" in out and "by other.exe (PID 77)" in out


def test_ports_command_says_free_or_in_use(capsys):
    with (
        patch.object(ports, "is_port_free", lambda port, host: port == 5000),
        patch.object(ports, "get_port_process", lambda port: (None, None)),
    ):
        ports.show_ports_status(CFG)
    out = capsys.readouterr().out
    assert "Port Status" in out
    assert "Translation  127.0.0.1:5000  -- " in out and "free" in out
    assert "Whisper      127.0.0.1:5001  -- " in out and "by unknown process" in out


def test_show_status_prints_every_part(capsys):
    called = []
    parts = (
        "_status_hardware",
        "_status_tools",
        "_status_packages",
        "_status_servers",
        "_status_models",
        "_status_ports",
    )
    with patch.object(status, "header"):
        patches = [patch.object(status, p, lambda *a, p=p: called.append(p)) for p in parts]
        for p in patches:
            p.start()
        try:
            status.show_status(CFG)
        finally:
            for p in patches:
                p.stop()
    assert called == list(parts)
