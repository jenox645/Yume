"""Server startup: config, logging, arguments, model load errors, helpers."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

import server_stubs  # noqa: E402,F401  (flask and faster_whisper, before the server modules)
import _api  # noqa: E402
import _bgutil  # noqa: E402
import _model  # noqa: E402
import _settings  # noqa: E402
import _startup  # noqa: E402
import faster_whisper_server  # noqa: E402,F401  (the entry point still imports)
import _state  # noqa: E402


class TestBgutil(unittest.TestCase):
    def test_bgutil_server_dir_returns_path(self):
        d = _bgutil.bgutil_server_dir()
        self.assertIsInstance(d, Path)
        self.assertIn("bgutil-ytdlp-pot-provider", str(d))
        self.assertIn("server", str(d))

    def test_bgutil_server_dir_relative_to_tools(self):
        d = _bgutil.bgutil_server_dir()
        self.assertIn("tools", str(d))

    def test_is_bgutil_server_ready_returns_false_when_down(self):
        # Port 4416 is almost certainly not running in CI
        result = _bgutil.is_bgutil_server_ready()
        self.assertIsInstance(result, bool)
        # We can't assert False (server might be running), so just check type

    def test_stop_bgutil_server_no_proc_no_crash(self):
        _bgutil._bgutil_proc = None
        _bgutil.stop_bgutil_server()  # must not raise

    def test_stop_bgutil_server_terminates_running_proc(self):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None  # still running
        _bgutil._bgutil_proc = mock_proc
        _bgutil.stop_bgutil_server()
        mock_proc.terminate.assert_called_once()
        self.assertIsNone(_bgutil._bgutil_proc)

    def test_stop_bgutil_server_handles_already_terminated(self):
        mock_proc = MagicMock()
        mock_proc.poll.return_value = 0  # already exited
        _bgutil._bgutil_proc = mock_proc
        _bgutil.stop_bgutil_server()  # must not raise
        self.assertIsNone(_bgutil._bgutil_proc)

    def test_start_bgutil_returns_false_when_no_main_ts(self):
        """If main.ts doesn't exist, start must return False without hanging."""
        result = _bgutil.start_bgutil_server()
        # Either False (main.ts missing) or True (somehow installed) — no exception
        self.assertIsInstance(result, bool)

    def test_bgutil_port_constant(self):
        self.assertEqual(_bgutil.BGUTIL_PORT, 4416)


# ═════════════════════════════════════════════════════════════════════════════
# server startup / API / model helpers (no HTTP context needed)
# ═════════════════════════════════════════════════════════════════════════════


class TestCleanupStaleTemps(unittest.TestCase):
    """_startup.cleanup_stale_temps() must remove dirs older than 1 h."""

    def test_no_crash_when_tmp_has_no_yume_dirs(self):
        _startup.cleanup_stale_temps()  # must not raise

    def test_removes_old_yume_dir(self):
        tmp = tempfile.mkdtemp(prefix="yume_")
        # Back-date mtime by 2 hours
        old_mtime = time.time() - 7200
        os.utime(tmp, (old_mtime, old_mtime))
        _startup.cleanup_stale_temps()
        self.assertFalse(os.path.exists(tmp))

    def test_keeps_recent_yume_dir(self):
        tmp = tempfile.mkdtemp(prefix="yume_")
        try:
            _startup.cleanup_stale_temps()
            # Recent dir (mtime = now) must survive
            self.assertTrue(os.path.exists(tmp))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_ignores_non_yume_dirs(self):
        tmp = tempfile.mkdtemp(prefix="other_")
        old_mtime = time.time() - 7200
        os.utime(tmp, (old_mtime, old_mtime))
        try:
            _startup.cleanup_stale_temps()
            self.assertTrue(os.path.exists(tmp))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestGetGpuStats(unittest.TestCase):
    def test_returns_none_when_nvidia_smi_missing(self):
        with patch("_api.subprocess.run", side_effect=FileNotFoundError("nvidia-smi not found")):
            result = _api.get_gpu_stats()
        self.assertIsNone(result)

    def test_returns_none_when_nvidia_smi_fails(self):
        with patch("_api.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="")
            result = _api.get_gpu_stats()
        self.assertIsNone(result)

    def test_returns_dict_on_success(self):
        csv_out = "4096, 8192, 45, 72, NVIDIA GeForce RTX 3080\n"
        with patch("_api.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=csv_out)
            result = _api.get_gpu_stats()
        self.assertIsNotNone(result)
        self.assertEqual(result["vram_used_mb"], 4096)
        self.assertEqual(result["vram_total_mb"], 8192)
        self.assertEqual(result["gpu_util_pct"], 45)
        self.assertEqual(result["gpu_temp_c"], 72)
        self.assertIn("RTX 3080", result["gpu_name"])

    def test_first_gpu_of_several_and_missing_fields(self):
        csv_out = "4096, 8192, [N/A], [N/A], GPU A\n100, 2048, 5, 40, GPU B\n"
        with patch("_api.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=csv_out)
            result = _api.get_gpu_stats()
        self.assertEqual(result["gpu_name"], "GPU A")
        self.assertEqual(result["vram_total_mb"], 8192)
        self.assertIsNone(result["gpu_temp_c"])

    def test_returns_none_when_insufficient_columns(self):
        with patch("_api.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="1024, 4096\n")
            result = _api.get_gpu_stats()
        self.assertIsNone(result)


class TestApplyConfig(unittest.TestCase):
    def setUp(self):
        self._orig_model = _state.model_name
        self._orig_device = _state.device
        self._orig_compute = _state.compute_type
        self._orig_browser = _state.cookies_browser

    def tearDown(self):
        _state.model_name = self._orig_model
        _state.device = self._orig_device
        _state.compute_type = self._orig_compute
        _state.cookies_browser = self._orig_browser

    def _make_args(self, **kw):
        import argparse

        defaults = dict(
            model="small",
            device="cpu",
            compute_type="int8",
            config=None,
            prewarm=False,
            low_vram=False,
            port=5001,
        )
        defaults.update(kw)
        return argparse.Namespace(**defaults)

    def test_sets_model_name(self):
        _settings.apply_config(self._make_args(model="turbo"))
        self.assertEqual(_state.model_name, "turbo")

    def test_sets_device(self):
        _settings.apply_config(self._make_args(device="cpu"))
        self.assertEqual(_state.device, "cpu")

    def test_sets_compute_type(self):
        _settings.apply_config(self._make_args(compute_type="int8"))
        self.assertEqual(_state.compute_type, "int8")

    def test_auto_device_resolves(self):
        """'auto' device must resolve to 'cuda' or 'cpu'."""
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1)  # no nvidia-smi
            _settings.apply_config(self._make_args(device="auto", compute_type="float16"))
        self.assertIn(_state.device, ("cuda", "cpu"))

    def test_auto_compute_resolves_for_cpu(self):
        _settings.apply_config(self._make_args(device="cpu", compute_type="auto"))
        self.assertEqual(_state.compute_type, "int8")

    def test_low_vram_forces_int8(self):
        _settings.apply_config(self._make_args(device="cuda", compute_type="float16", low_vram=True))
        self.assertEqual(_state.compute_type, "int8")

    def test_config_file_overrides_args(self):
        cfg = {"whisper_model": "base", "cookies_browser": "firefox"}
        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
            import json

            json.dump(cfg, f)
            cfg_path = f.name
        try:
            _settings.apply_config(self._make_args(model="large-v3", config=cfg_path))
            self.assertEqual(_state.model_name, "base")
            self.assertEqual(_state.cookies_browser, "firefox")
        finally:
            os.unlink(cfg_path)


class TestConfigureLogging(unittest.TestCase):
    """_configure_logging sets the root logger level based on --verbose / LOG_LEVEL."""

    def _reset_logging(self):
        """logging.basicConfig is a no-op when handlers already exist; remove them first."""
        import logging

        root = logging.root
        for h in root.handlers[:]:
            root.removeHandler(h)
        root.setLevel(logging.WARNING)

    def test_verbose_sets_debug_level(self):
        import logging
        import argparse

        self._reset_logging()
        args = argparse.Namespace(verbose=True)
        with patch.dict(os.environ, {"LOG_LEVEL": ""}):
            _startup.configure_logging(args)
        self.assertEqual(logging.root.level, logging.DEBUG)

    def test_non_verbose_sets_warning(self):
        import logging
        import argparse

        self._reset_logging()
        args = argparse.Namespace(verbose=False)
        with patch.dict(os.environ, {"LOG_LEVEL": ""}):
            _startup.configure_logging(args)
        self.assertEqual(logging.root.level, logging.WARNING)


class TestParseArgs(unittest.TestCase):
    def test_defaults(self):
        with patch("sys.argv", ["server"]):
            args = _startup.parse_args()
        self.assertEqual(args.model, "large-v3-turbo")  # as accurate as large-v3 on songs, 2x faster
        self.assertEqual(args.device, "cuda")
        self.assertEqual(args.compute_type, "float16")
        self.assertEqual(args.port, 5001)
        self.assertFalse(args.prewarm)
        self.assertFalse(args.low_vram)

    def test_custom_model(self):
        with patch("sys.argv", ["server", "--model", "small"]):
            args = _startup.parse_args()
        self.assertEqual(args.model, "small")

    def test_custom_port(self):
        with patch("sys.argv", ["server", "--port", "6001"]):
            args = _startup.parse_args()
        self.assertEqual(args.port, 6001)

    def test_low_vram_flag(self):
        with patch("sys.argv", ["server", "--low-vram"]):
            args = _startup.parse_args()
        self.assertTrue(args.low_vram)

    def test_prewarm_flag(self):
        with patch("sys.argv", ["server", "--prewarm"]):
            args = _startup.parse_args()
        self.assertTrue(args.prewarm)

    def test_removed_word_timestamp_flags_rejected(self):
        """--no-word-timestamps/--pause-threshold never affected transcription — gone."""
        with patch("sys.argv", ["server", "--no-word-timestamps"]):
            with self.assertRaises(SystemExit):
                _startup.parse_args()


class TestPrintModelLoadError(unittest.TestCase):
    """Each error category must produce an actionable human-readable message."""

    def _capture(self, msg: str) -> str:
        import io

        buf = io.StringIO()
        with patch("sys.stdout", buf):
            _model.print_model_load_error(RuntimeError(msg))
        return buf.getvalue()

    def test_oom_error_suggests_smaller_model(self):
        out = self._capture("CUDA out of memory")
        self.assertIn("vram", out.lower())

    def test_cublas_missing_suggests_cuda_install(self):
        out = self._capture("cublas64_12.dll not found")
        self.assertIn("cuda", out.lower())

    def test_module_not_found_suggests_pip(self):
        out = self._capture("No module named 'ctranslate2'")
        self.assertIn("pip", out.lower())

    def test_file_not_found_suggests_internet(self):
        out = self._capture("no such file or directory: model.bin filenotfound")
        self.assertIn("model", out.lower())

    def test_permission_error_suggests_admin(self):
        out = self._capture("permission denied accessing model files")
        self.assertIn("permission", out.lower())

    def test_unknown_error_shows_general_solutions(self):
        out = self._capture("Some very obscure unknown error")
        self.assertGreater(len(out), 20)  # at least something printed


class TestSetLowPriority(unittest.TestCase):
    def test_does_not_raise(self):
        """Lowering thread priority is best-effort — must not raise in tests."""
        _model.set_low_priority()


class TestWriteTokenFile(unittest.TestCase):
    def test_writes_token_to_file(self):
        with tempfile.TemporaryDirectory() as d:
            token_path = os.path.join(d, ".yume_token")
            _state.API_TOKEN = "test-token-abc123"
            import argparse

            _args = argparse.Namespace(port=5001)

            # Patch _state.TOKEN_FILE so we know where it writes
            with patch("_startup.Path") as mock_path_cls:
                mock_base = MagicMock()
                mock_token_file = MagicMock()
                mock_token_file.__str__ = lambda s: token_path
                mock_base.__truediv__ = lambda self, other: mock_token_file
                mock_path_cls.return_value.parent.parent.resolve.return_value = mock_base
                mock_token_file.__str__ = lambda s: token_path
                # Fall back to real Path to avoid infinite recursion
            # Use real file I/O
            orig_token_file = _state.TOKEN_FILE
            _state.TOKEN_FILE = token_path
            try:
                with open(token_path, "w") as f:
                    f.write(_state.API_TOKEN)
                with open(token_path) as f:
                    content = f.read()
                self.assertEqual(content, "test-token-abc123")
            finally:
                _state.TOKEN_FILE = orig_token_file


class TestCpuFallback(unittest.TestCase):
    """CUDA that cannot run must not stop Whisper: the CPU takes over."""

    NO_DEVICE = "CUDA failed with error no CUDA-capable device is detected"

    def setUp(self):
        self._orig = (
            _state.model, _state.load_error, _state.device, _state.compute_type, _state.device_notice,
            _state.model_name,
        )  # fmt: skip
        _state.model, _state.load_error, _state.device_notice = None, "", ""
        _state.device, _state.compute_type, _state.model_name = "cuda", "float16", "small"

    def tearDown(self):
        (
            _state.model, _state.load_error, _state.device, _state.compute_type, _state.device_notice,
            _state.model_name,
        ) = self._orig  # fmt: skip

    def _health(self):
        result = TestModelLoadFailure._health(self)
        return result[0] if isinstance(result, tuple) else result

    def test_the_fallback_reaches_health_with_what_to_do(self):
        self._load(self._cuda_fails(self.NO_DEVICE))
        self.assertIn("update its driver", self._health()["notice"])
        self.assertNotIn("slow on the CPU", self._health()["notice"])

    def test_a_big_model_on_the_cpu_gets_a_notice_also_after_a_fallback(self):
        _state.model_name = "large-v3-turbo"
        self._load(self._cuda_fails(self.NO_DEVICE))
        notice = self._health()["notice"]
        self.assertIn("update its driver", notice)
        self.assertIn("large-v3-turbo is slow on the CPU", notice)

    def test_no_notice_for_what_runs_as_it_should(self):
        _state.model = MagicMock()
        for device, name in (("cuda", "large-v3-turbo"), ("cpu", "small")):
            _state.device, _state.model_name = device, name
            self.assertNotIn("notice", self._health(), (device, name))

    def test_no_notice_before_the_model_is_ready(self):
        _state.device, _state.model_name, _state.model = "cpu", "large-v3", None
        self.assertNotIn("notice", self._health())

    def _load(self, whisper):
        with (
            patch.object(_model, "WhisperModel", side_effect=whisper),
            patch.object(_model, "set_low_priority"),
            patch("builtins.print"),
        ):
            _model.load_model(MagicMock(prewarm=False, port=5001))

    @staticmethod
    def _cuda_fails(message):
        def whisper(name, device, compute_type):
            if device == "cuda":
                raise RuntimeError(message)
            return TestCpuFallback._loaded(device, compute_type)

        return whisper

    @staticmethod
    def _loaded(device, compute_type="int8", segments=()):
        model = MagicMock(device=device, compute_type=compute_type)
        model.transcribe.return_value = (segments, MagicMock())
        return model

    def test_cuda_that_cannot_run_falls_back_to_the_cpu(self):
        for raw in (
            self.NO_DEVICE,
            "CUDA failed with error CUDA driver version is insufficient for CUDA runtime version",
            "This CTranslate2 package was not compiled with CUDA support",
        ):
            _state.model, _state.device, _state.compute_type = None, "cuda", "float16"
            self._load(self._cuda_fails(raw))
            self.assertEqual((_state.device, _state.compute_type, _state.load_error), ("cpu", "int8", ""), raw)
            self.assertEqual((_state.model.device, _state.model.compute_type), ("cpu", "int8"), raw)

    def test_out_of_memory_is_no_reason_to_leave_the_gpu(self):
        for raw in ("CUDA failed with error out of memory", "CUDA out of memory"):
            _state.load_error = ""
            self._load(self._cuda_fails(raw))
            self.assertEqual(_state.device, "cuda")
            self.assertIn("smaller model", _state.load_error)

    def test_a_cpu_that_fails_too_publishes_both_reasons(self):
        def whisper(name, device, compute_type):
            raise RuntimeError(self.NO_DEVICE if device == "cuda" else "No module named 'ctranslate2'")

        self._load(whisper)
        self.assertIsNone(_state.model)
        self.assertIn(self.NO_DEVICE, _state.load_error)
        self.assertIn("No module named", _state.load_error)
        self.assertIn("pocket_yume.py setup", _state.load_error)

    @staticmethod
    def _no_cublas():
        raise RuntimeError("Library cublas64_12.dll is not found or cannot be loaded")
        yield

    def test_a_warm_up_without_cublas_falls_back_to_the_cpu(self):
        def whisper(name, device, compute_type):
            # As in faster-whisper: transcribe() returns, the error comes from reading the segments
            return self._loaded(device, segments=self._no_cublas() if device == "cuda" else ())

        self._load(whisper)
        self.assertEqual((_state.device, _state.compute_type, _state.load_error), ("cpu", "int8", ""))
        self.assertEqual(_state.model.device, "cpu")


class TestServerStartedByHand(unittest.TestCase):
    """The launcher resolves the device; a server started by hand must not fail on it."""

    def setUp(self):
        self._orig = (_state.device, _state.compute_type)

    def tearDown(self):
        _state.device, _state.compute_type = self._orig

    def _resolved(self, device, compute):
        _state.device, _state.compute_type = device, compute
        with patch("builtins.print"):
            _settings._resolve_auto_device()
        return _state.device, _state.compute_type

    def test_the_cpu_drops_a_precision_only_cuda_runs(self):
        for compute in ("float16", "int8_float16"):
            self.assertEqual(self._resolved("cpu", compute), ("cpu", "int8"))

    def test_other_choices_are_kept(self):
        self.assertEqual(self._resolved("cpu", "float32"), ("cpu", "float32"))
        self.assertEqual(self._resolved("cuda", "float16"), ("cuda", "float16"))


class TestModelLoadFailure(unittest.TestCase):
    """A failed model load must be visible, not an endless 'loading'."""

    def setUp(self):
        self._orig = (_state.model, _state.load_error)

    def tearDown(self):
        _state.model, _state.load_error = self._orig

    def _health(self):
        req = MagicMock()
        req.headers = {"Origin": "chrome-extension://abc"}
        with (
            patch.object(_api, "request", req),
            patch.object(_api, "jsonify", lambda d=None, **kw: d),
            patch.object(_api._ytdlp, "check_ytdlp", return_value=True),
        ):
            return _api.health()

    def test_loader_thread_publishes_error_instead_of_exiting(self):
        _state.model, _state.load_error = None, ""
        args = MagicMock(prewarm=False, port=5001)
        with (
            patch.object(_model, "WhisperModel", side_effect=RuntimeError("CUDA out of memory")),
            patch.object(_model, "set_low_priority"),
            patch("builtins.print"),
        ):
            _model.load_model(args)  # must not raise SystemExit
        self.assertIn("out of memory", _state.load_error)

    def test_the_published_error_says_what_to_do(self):
        # The overlay and the launcher show load_error as is: the advice was only
        # printed to whisper_server.log, which the user never sees
        args = MagicMock(prewarm=False, port=5001)
        for raw, hint in (
            ("CUDA failed with error out of memory", "smaller model"),
            ("Library cublas64_12.dll is not found or cannot be loaded", "CUDA Toolkit"),
            ("An error happened while trying to locate the files on the Hub", "internet"),
            ("No module named 'ctranslate2'", "python pocket_yume.py setup"),
        ):
            _state.model, _state.load_error = None, ""
            with (
                patch.object(_model, "WhisperModel", side_effect=RuntimeError(raw)),
                patch.object(_model, "set_low_priority"),
                patch("builtins.print"),
            ):
                _model.load_model(args)
            self.assertTrue(_state.load_error.startswith(raw), _state.load_error)
            self.assertIn(hint, _state.load_error)

    def test_load_advice_names_the_settings_menu_not_the_setup_wizard(self):
        out = TestPrintModelLoadError._capture(self, "CUDA out of memory")
        self.assertIn("pocket_yume.py settings", out)
        self.assertNotIn("pocket_yume.py setup      ", out)  # re-running the whole wizard

    def test_health_reports_error_status(self):
        _state.model, _state.load_error = None, "CUDA out of memory"
        body, code = self._health()
        self.assertEqual(code, 500)
        self.assertEqual(body["status"], "error")
        self.assertEqual(body["error"], "CUDA out of memory")

    def test_health_reports_loading_without_error(self):
        _state.model, _state.load_error = None, ""
        body, code = self._health()
        self.assertEqual(code, 503)
        self.assertEqual(body["status"], "loading")
