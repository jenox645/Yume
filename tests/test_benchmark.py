"""The Whisper benchmark, driven with a fake WhisperModel and scripted answers."""

from __future__ import annotations

import itertools
import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import yume.benchmark as bench  # noqa: E402

NVIDIA_12GB = {"has_nvidia": True, "has_amd": False, "vram_mb": 12288, "name": "RTX 3060", "vendor": "nvidia"}
NVIDIA_6GB = {**NVIDIA_12GB, "vram_mb": 6144}
NO_GPU = {"has_nvidia": False, "has_amd": False, "vram_mb": 0, "name": None, "vendor": "none"}


class Run:
    """One benchmark run: `fail` maps (model, device) to the error its load raises."""

    def __init__(self, tmp_path, gpu, selection, *, yns=(), fail=None, cached=True):
        self.tmp_path = tmp_path
        self.gpu = gpu
        self.selection = selection
        self.yns = list(yns)
        self.fail = fail or {}
        self.cached = cached
        self.loads: list[tuple[str, str, str]] = []
        self.rows: list[list[str]] = []
        self.saved = MagicMock()
        self.wav_seen = False

    def _model_class(self):
        run = self

        class FakeModel:
            def __init__(self, name, device, compute_type):
                run.loads.append((name, device, compute_type))
                err = run.fail.get((name, device))
                if err:
                    raise RuntimeError(err)

            def transcribe(self, path, **kw):
                run.wav_seen = Path(path).exists()
                return iter([MagicMock()]), MagicMock()

        return FakeModel

    def go(self, cfg):
        fw = MagicMock(WhisperModel=self._model_class())
        fakes = {
            "LOGS_DIR": self.tmp_path,
            "detect_gpu": MagicMock(return_value=self.gpu),
            "find_tool": MagicMock(return_value=None),  # write the tone with the wave module
            "ask_input": MagicMock(return_value=self.selection),
            "ask_yn": MagicMock(side_effect=self.yns),
            "header": MagicMock(),
            "pause": MagicMock(),
            # A clock that advances 1 s per reading: each pass "takes" 1 s (30x realtime)
            "time": MagicMock(perf_counter=MagicMock(side_effect=itertools.count())),
            "table": lambda headers, rows, **kw: self.rows.extend(rows),
            "_is_whisper_model_cached": MagicMock(return_value=self.cached),
        }
        with ExitStack() as stack:
            stack.enter_context(patch.dict(sys.modules, {"faster_whisper": fw, "torch": MagicMock()}))
            for name, fake in fakes.items():
                stack.enter_context(patch.object(bench, name, fake))
            stack.enter_context(patch("config.save_config", self.saved))
            bench.benchmark_whisper(cfg)
        return self


def test_recommended_model_runs_on_cuda_and_can_be_adopted(tmp_path):
    rec, _ = bench.recommend_whisper_model(NVIDIA_12GB)
    # start, then switch to the most accurate fast model
    r = Run(tmp_path, NVIDIA_12GB, "rec", yns=[True, True]).go(cfg := {"whisper_model": "tiny"})
    assert r.loads == [(rec, "cuda", "float16")]
    assert r.wav_seen
    assert [row[0] for row in r.rows] == [rec]
    if rec in bench._ACCURACY_RANK:
        assert cfg["whisper_model"] == rec
        r.saved.assert_called_once_with(cfg)
    assert not (tmp_path / "_benchmark_test.wav").exists()  # test audio cleaned up


def test_numbers_pick_models_and_bad_entries_are_ignored(tmp_path):
    names = [m[0] for m in bench.WHISPER_MODELS_INFO]
    r = Run(tmp_path, NVIDIA_12GB, "1, 3,99,x", yns=[True, False]).go({"whisper_model": names[0]})
    assert [load[0] for load in r.loads] == [names[0], names[2]]


def test_all_runs_only_models_that_fit(tmp_path):
    r = Run(tmp_path, NVIDIA_6GB, "all", yns=[True, False]).go({})
    fitting = [n for n, _p, mb, _d in bench.WHISPER_MODELS if mb <= 6144]
    assert [load[0] for load in r.loads] == fitting


def test_precision_follows_the_gpu(tmp_path):
    r = Run(tmp_path, NVIDIA_6GB, "1", yns=[True, False]).go({})
    assert r.loads[0][1:] == ("cuda", "int8_float16")
    r = Run(tmp_path, NO_GPU, "1", yns=[True, False]).go({})
    assert r.loads[0][1:] == ("cpu", "int8")


def test_missing_cuda_libraries_offer_a_cpu_retry(tmp_path):
    name = bench.WHISPER_MODELS_INFO[0][0]
    fail = {(name, "cuda"): "Library cublas64_12.dll is not found or cannot be loaded"}
    r = Run(tmp_path, NVIDIA_12GB, "1", yns=[True, True, False], fail=fail).go({})
    assert [load[1] for load in r.loads] == ["cuda", "cpu"]
    assert r.rows[0][0] == f"{name} (CPU)"


def test_declined_cpu_retry_is_a_failed_row(tmp_path):
    name = bench.WHISPER_MODELS_INFO[0][0]
    fail = {(name, "cuda"): "CUDA failed with error out of memory"}
    r = Run(tmp_path, NVIDIA_12GB, "1", yns=[True, False], fail=fail).go({})
    assert r.rows[0][0] == name
    assert "FAIL: CUDA failed" in r.rows[0][-1]


def test_other_errors_fail_without_a_retry(tmp_path):
    name = bench.WHISPER_MODELS_INFO[0][0]
    r = Run(tmp_path, NVIDIA_12GB, "1", yns=[True], fail={(name, "cuda"): "disk full"}).go({})
    assert len(r.loads) == 1
    assert "FAIL: disk full" in r.rows[0][-1]


def test_nothing_selected_stops_before_loading(tmp_path):
    r = Run(tmp_path, NVIDIA_12GB, "x").go({})
    assert r.loads == []


def test_declining_the_download_stops(tmp_path):
    r = Run(tmp_path, NVIDIA_12GB, "1", yns=[False], cached=False).go({})
    assert r.loads == []


def test_without_faster_whisper_nothing_runs(tmp_path):
    with patch.dict(sys.modules, {"faster_whisper": None}), patch.object(bench, "pause"):
        with patch.object(bench, "detect_gpu") as gpu, patch.object(bench, "header"):
            bench.benchmark_whisper({})
    gpu.assert_not_called()
