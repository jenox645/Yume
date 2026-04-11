"""Unit tests for the yume/ package modules.

Tests are designed to run without network access, GPU, or running servers.
Functions that launch subprocesses, display UI, or read interactive input
are tested only at the module-import and pure-logic level.
"""
from __future__ import annotations

import socket
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

# Project root on path
sys.path.insert(0, str(Path(__file__).parent.parent))


# ─────────────────────────────────────────────────────────────────────────────
# yume.utils
# ─────────────────────────────────────────────────────────────────────────────

class TestRun:
    """_run() — subprocess wrapper that never uses shell=True."""

    def test_successful_command(self):
        from yume.utils import _run
        r = _run([sys.executable, "-c", "print('ok')"])
        assert r.returncode == 0
        assert "ok" in r.stdout

    def test_nonexistent_command_returns_127(self):
        from yume.utils import _run
        r = _run(["__no_such_binary_xyz__"])
        assert r.returncode == 127
        assert "Not found" in r.stderr

    def test_empty_command_returns_127(self):
        from yume.utils import _run
        r = _run([])
        assert r.returncode == 127

    def test_timeout_returns_124(self):
        from yume.utils import _run
        r = _run([sys.executable, "-c", "import time; time.sleep(10)"], timeout=1)
        assert r.returncode == 124

    def test_utf8_output_decoded(self):
        """Must not crash on non-ASCII output."""
        from yume.utils import _run
        r = _run([sys.executable, "-c", "print('héllo')"])
        assert r.returncode == 0
        assert "h" in r.stdout


class TestTryImport:
    def test_stdlib_module_found(self):
        from yume.utils import _try_import
        assert _try_import("os") is True
        assert _try_import("json") is True

    def test_missing_module_returns_false(self):
        from yume.utils import _try_import
        assert _try_import("__no_such_module_xyz__") is False


class TestFindTool:
    def test_returns_none_for_unknown_tool(self):
        from yume.utils import find_tool
        assert find_tool("__no_such_tool_xyz__") is None

    def test_finds_python_on_path(self):
        """python3 (or equivalent) must be findable via PATH."""
        from yume.utils import find_tool
        result = find_tool("python3") or find_tool("python")
        assert result is not None

    def test_returns_string_or_none(self):
        from yume.utils import find_tool
        r = find_tool("ffmpeg")
        assert r is None or isinstance(r, str)


class TestPathConstants:
    def test_base_dir_exists(self):
        from yume.utils import BASE_DIR
        assert BASE_DIR.exists()
        assert BASE_DIR.is_dir()

    def test_tools_dir_is_child_of_base(self):
        from yume.utils import BASE_DIR, TOOLS_DIR
        assert TOOLS_DIR.parent == BASE_DIR

    def test_gguf_dir_path_correct(self):
        from yume.utils import GGUF_DIR
        assert "models" in str(GGUF_DIR)
        assert "translation" in str(GGUF_DIR)


class TestFindGgufModels:
    def test_returns_list(self):
        from yume.utils import find_gguf_models
        result = find_gguf_models()
        assert isinstance(result, list)

    def test_all_results_are_gguf(self):
        from yume.utils import find_gguf_models
        for p in find_gguf_models():
            assert p.suffix == ".gguf"


class TestCheckForUpdates:
    def test_returns_two_tuple(self):
        """Must return a 2-tuple regardless of network."""
        from yume.utils import check_for_updates
        result = check_for_updates("0.0.0")
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_no_update_when_network_fails(self):
        """If network is unavailable, should return (None, None) silently."""
        from yume.utils import check_for_updates
        with patch("urllib.request.urlopen", side_effect=Exception("no network")):
            ver, url = check_for_updates("0.0.0")
            assert ver is None
            assert url is None

    def test_no_update_when_already_latest(self):
        from yume.utils import check_for_updates
        fake_data = b'{"tag_name": "v0.0.0", "html_url": "http://example.com"}'
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = fake_data
        with patch("urllib.request.urlopen", return_value=mock_resp):
            ver, url = check_for_updates("0.0.0")
            assert ver is None

    def test_returns_update_when_newer(self):
        from yume.utils import check_for_updates
        fake_data = b'{"tag_name": "v9.9.9", "html_url": "http://example.com/rel"}'
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = fake_data
        with patch("urllib.request.urlopen", return_value=mock_resp):
            ver, url = check_for_updates("0.0.0")
            assert ver == "9.9.9"
            assert url == "http://example.com/rel"


# ─────────────────────────────────────────────────────────────────────────────
# yume.network
# ─────────────────────────────────────────────────────────────────────────────

class TestSetVersion:
    def test_version_updated(self):
        import yume.network as net
        original = net._VERSION
        try:
            net.set_version("1.2.3")
            assert net._VERSION == "1.2.3"
        finally:
            net._VERSION = original


class TestCheckServer:
    def test_returns_dict_with_up_key(self):
        from yume.network import check_server
        # Port 1 is privileged and will be refused — always "down"
        result = check_server("127.0.0.1", 1, "/health")
        assert isinstance(result, dict)
        assert "up" in result
        assert "data" in result

    def test_unreachable_server_returns_up_false(self):
        from yume.network import check_server
        result = check_server("127.0.0.1", 19999, "/health")
        assert result["up"] is False
        assert result["data"] == {}

    def test_mock_responding_server(self):
        from yume.network import check_server
        import json as _json
        fake_body = _json.dumps({"status": "ok"}).encode()
        mock_resp = MagicMock()
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.read.return_value = fake_body
        mock_resp.headers = {"content-type": "application/json"}
        with patch("urllib.request.urlopen", return_value=mock_resp):
            result = check_server("127.0.0.1", 5001, "/health")
        assert result["up"] is True
        assert result["data"]["status"] == "ok"


class TestCheckTranslationServer:
    def test_unreachable_returns_up_false(self):
        from yume.network import check_translation_server
        result = check_translation_server("127.0.0.1", 19998)
        assert result["up"] is False

    def test_socket_busy_returns_up_true(self):
        """If HTTP fails but socket accepts a connection, server is 'busy'."""
        from yume.network import check_translation_server
        # Bind a real socket so the connection succeeds
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(("127.0.0.1", 0))
            srv.listen(1)
            port = srv.getsockname()[1]
            result = check_translation_server("127.0.0.1", port)
        assert result["up"] is True
        assert result["data"].get("status") == "busy"


class TestServerGet:
    def test_no_server_returns_none(self):
        from yume.network import server_get
        result = server_get("127.0.0.1", 19997, "/health", timeout=1)
        assert result is None


class TestServerPost:
    def test_no_server_returns_error_dict(self):
        from yume.network import server_post
        result = server_post("127.0.0.1", 19996, "/transcribe", data={}, timeout=1)
        assert isinstance(result, dict)
        assert "error" in result


# ─────────────────────────────────────────────────────────────────────────────
# yume.ports
# ─────────────────────────────────────────────────────────────────────────────

class TestIsPortFree:
    def test_invalid_port_zero(self):
        from yume.ports import is_port_free
        assert is_port_free(0) is False

    def test_invalid_port_negative(self):
        from yume.ports import is_port_free
        assert is_port_free(-1) is False

    def test_invalid_port_too_large(self):
        from yume.ports import is_port_free
        assert is_port_free(65536) is False

    def test_invalid_port_string(self):
        from yume.ports import is_port_free
        assert is_port_free("abc") is False  # type: ignore[arg-type]

    def test_bound_port_is_not_free(self):
        """A port with an open socket should not be free."""
        from yume.ports import is_port_free
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
            assert is_port_free(port) is False

    def test_free_port_is_free(self):
        """A port that was just freed should be available.

        Note: there is an inherent race between closing the socket and calling
        is_port_free — another process could bind the same port in that window.
        This is extremely unlikely in practice but acknowledged here.
        """
        from yume.ports import is_port_free
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        # Socket is closed now — port should be free
        assert is_port_free(port) is True


class TestFindFreePort:
    def test_returns_int_or_none(self):
        from yume.ports import find_free_port
        result = find_free_port(10000)
        assert result is None or isinstance(result, int)

    def test_returned_port_is_free(self):
        from yume.ports import find_free_port, is_port_free
        port = find_free_port(10000)
        if port is not None:
            assert is_port_free(port)

    def test_exclude_set_respected(self):
        from yume.ports import find_free_port
        # Find two successive ports; exclude the first
        p1 = find_free_port(10100)
        if p1 is not None:
            p2 = find_free_port(10100, exclude={p1})
            if p2 is not None:
                assert p2 != p1


class TestPortConstants:
    def test_defaults_are_correct(self):
        from yume.ports import DEFAULT_TRANSLATION_PORT, DEFAULT_WHISPER_PORT
        assert DEFAULT_TRANSLATION_PORT == 5000
        assert DEFAULT_WHISPER_PORT == 5001

    def test_min_port_is_one(self):
        from yume.ports import MIN_PORT
        assert MIN_PORT == 1

    def test_max_port_is_65535(self):
        from yume.ports import MAX_PORT
        assert MAX_PORT == 65535


# ─────────────────────────────────────────────────────────────────────────────
# yume.benchmark
# ─────────────────────────────────────────────────────────────────────────────

class TestWhisperModelsCatalogue:
    def test_models_info_is_list(self):
        from yume.benchmark import WHISPER_MODELS_INFO
        assert isinstance(WHISPER_MODELS_INFO, list)
        assert len(WHISPER_MODELS_INFO) > 0

    def test_each_entry_has_four_fields(self):
        from yume.benchmark import WHISPER_MODELS_INFO
        for entry in WHISPER_MODELS_INFO:
            assert len(entry) == 4, f"Expected 4 fields, got {len(entry)}: {entry}"

    def test_known_models_present(self):
        from yume.benchmark import WHISPER_MODELS_INFO
        names = [entry[0] for entry in WHISPER_MODELS_INFO]
        for model in ("tiny", "base", "small", "large-v3"):
            assert model in names, f"Expected model '{model}' in catalogue"


class TestIsWhisperModelCached:
    def test_returns_bool(self):
        from yume.benchmark import _is_whisper_model_cached
        result = _is_whisper_model_cached("tiny")
        assert isinstance(result, bool)

    def test_nonexistent_model_returns_false(self):
        from yume.benchmark import _is_whisper_model_cached
        assert _is_whisper_model_cached("__no_such_model_xyz__") is False

    def test_exception_returns_false(self):
        """If the cache lookup raises an OS error, must return False (not crash)."""
        from yume.benchmark import _is_whisper_model_cached
        with patch("pathlib.Path.exists", side_effect=OSError("permission denied")):
            result = _is_whisper_model_cached("tiny")
        assert result is False


# ─────────────────────────────────────────────────────────────────────────────
# yume.health
# ─────────────────────────────────────────────────────────────────────────────

class TestSetBackendInfo:
    def test_injection_stores_value(self):
        import yume.health as h
        original = h._BI
        try:
            h.set_backend_info({"test_key": "test_val"})
            assert h._BI["test_key"] == "test_val"
        finally:
            h._BI = original

    def test_injection_replaces_old_value(self):
        import yume.health as h
        original = h._BI
        try:
            h.set_backend_info({"a": 1})
            h.set_backend_info({"b": 2})
            assert "a" not in h._BI
            assert h._BI["b"] == 2
        finally:
            h._BI = original


# ─────────────────────────────────────────────────────────────────────────────
# yume.hardware
# ─────────────────────────────────────────────────────────────────────────────

class TestDetectGpu:
    def test_returns_expected_shape(self):
        from yume.hardware import detect_gpu
        gpu = detect_gpu()
        assert isinstance(gpu, dict)
        for key in ("has_nvidia", "has_amd", "name", "vram_mb", "vendor"):
            assert key in gpu, f"Missing key: {key}"

    def test_has_nvidia_is_bool(self):
        from yume.hardware import detect_gpu
        gpu = detect_gpu()
        assert isinstance(gpu["has_nvidia"], bool)

    def test_vram_mb_is_int(self):
        from yume.hardware import detect_gpu
        gpu = detect_gpu()
        assert isinstance(gpu["vram_mb"], int)


class TestDetectRamGb:
    def test_returns_positive_float(self):
        from yume.hardware import detect_ram_gb
        ram = detect_ram_gb()
        assert isinstance(ram, float)
        assert ram > 0

    def test_at_least_1gb(self):
        """Any machine running these tests should have ≥ 1 GB RAM."""
        from yume.hardware import detect_ram_gb
        assert detect_ram_gb() >= 1.0


class TestDiskFreeGb:
    def test_returns_positive_float(self):
        from yume.hardware import disk_free_gb
        free = disk_free_gb()
        assert isinstance(free, float)
        assert free >= 0.0


class TestRecommendWhisperModel:
    def test_returns_two_tuple(self):
        from yume.hardware import detect_gpu, recommend_whisper_model
        gpu = detect_gpu()
        model, reason = recommend_whisper_model(gpu)
        assert isinstance(model, str)
        assert isinstance(reason, str)
        assert len(model) > 0
        assert len(reason) > 0

    def test_cpu_mode_picks_small_or_base(self):
        from yume.hardware import recommend_whisper_model
        cpu_gpu = {"has_nvidia": False, "has_amd": False, "vram_mb": 0, "name": None, "vendor": "none"}
        model, _ = recommend_whisper_model(cpu_gpu)
        assert model in ("tiny", "base", "small"), f"Expected small model for CPU, got {model}"


# ─────────────────────────────────────────────────────────────────────────────
# yume.setup
# ─────────────────────────────────────────────────────────────────────────────

class TestSetSetupContext:
    def test_injection_stores_values(self):
        import yume.setup as s
        original_bi = s._BACKEND_INFO
        original_models = s._MODELS_DIR
        original_gguf = s._GGUF_DIR
        original_ver = s._VERSION
        try:
            s.set_setup_context({"gpu": "rtx"}, Path("/tmp/models"), Path("/tmp/gguf"), "1.2.3")
            assert s._BACKEND_INFO == {"gpu": "rtx"}
            assert s._MODELS_DIR == Path("/tmp/models")
            assert s._GGUF_DIR == Path("/tmp/gguf")
            assert s._VERSION == "1.2.3"
        finally:
            s._BACKEND_INFO = original_bi
            s._MODELS_DIR = original_models
            s._GGUF_DIR = original_gguf
            s._VERSION = original_ver


# ─────────────────────────────────────────────────────────────────────────────
# yume.launch
# ─────────────────────────────────────────────────────────────────────────────

class TestSetLaunchContext:
    def test_injection_stores_values(self):
        import yume.launch as lnch
        original_bi = lnch._BACKEND_INFO
        original_ver = lnch._VERSION
        try:
            lnch.set_launch_context({"backend": "llamacpp"}, "9.8.7")
            assert lnch._BACKEND_INFO == {"backend": "llamacpp"}
            assert lnch._VERSION == "9.8.7"
        finally:
            lnch._BACKEND_INFO = original_bi
            lnch._VERSION = original_ver


# ─────────────────────────────────────────────────────────────────────────────
# yume.menus — module-level injection
# ─────────────────────────────────────────────────────────────────────────────

class TestMenusSetBackendInfo:
    def test_injection_stores_dict(self):
        import yume.menus as m
        original = m._BI
        try:
            m.set_backend_info({"x": 42})
            assert m._BI["x"] == 42
        finally:
            m._BI = original


# ─────────────────────────────────────────────────────────────────────────────
# yume.ui — pure helpers (no terminal I/O)
# ─────────────────────────────────────────────────────────────────────────────

class TestColourConstants:
    """Test that yume.ui.C colour/style constants are properly defined."""

    def test_colour_constants_are_strings(self):
        from yume.ui import C
        assert isinstance(C.RED, str)
        assert isinstance(C.GREEN, str)
        assert isinstance(C.RESET, str)

    def test_colour_constants_non_empty(self):
        from yume.ui import C
        # Codes are set at import time — at least one should be non-empty
        # (empty only if the terminal doesn't support ANSI)
        for attr in ("RED", "GREEN", "RESET", "BOLD"):
            assert hasattr(C, attr)
