"""yume/installers.py: which pip commands install_llamacpp_python runs per GPU and OS."""

from __future__ import annotations

import sys
import types
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import yume.hardware  # noqa: E402
import yume.installers as inst  # noqa: E402

NVIDIA = {"has_nvidia": True, "has_amd": False}
AMD = {"has_nvidia": False, "has_amd": True}
CPU = {"has_nvidia": False, "has_amd": False}


def llama(gpu, *, win, fail=(), cuda_in_wheel=True, build_tools=True):
    """Runs install_llamacpp_python; `fail` lists labels (substrings of a pip
    command's tell-tale argument) that exit non-zero. Returns (ok, steps)."""
    steps = []

    def run(cmd, timeout=0, env=None, **kw):
        if cmd[1] == "-c":  # the CUDA check
            steps.append("verify-cuda")
            return types.SimpleNamespace(returncode=0, stdout="True\n" if cuda_in_wheel else "False\n", stderr="")
        if "uvicorn" in " ".join(cmd) or any("fastapi" in c for c in cmd):
            steps.append("server-deps")
            return types.SimpleNamespace(returncode=0, stdout="", stderr="")
        if "whl/cu124" in " ".join(cmd):
            step = "cuda-wheel"
        elif "whl/cpu" in " ".join(cmd):
            step = "cpu-wheel"
        elif env and env.get("CMAKE_ARGS") == "-DGGML_CUDA=on":
            step = "cuda-source"
        elif env and env.get("CMAKE_ARGS") == "-DGGML_HIP=on":
            step = "rocm-source"
        else:
            step = "cpu-source"
        steps.append(step)
        return types.SimpleNamespace(returncode=1 if step in fail else 0, stdout="", stderr="boom")

    with ExitStack() as stack:
        for name, fake in {
            "_run": run,
            "_check_pip": lambda: True,
            "_has_build_tools": lambda: build_tools,
            "_install_build_tools": lambda: False,
            "IS_WIN": win,
            "IS_LIN": not win,
            "info": MagicMock(),
            "warn": MagicMock(),
            "error": MagicMock(),
            "success": MagicMock(),
        }.items():
            stack.enter_context(patch.object(inst, name, fake))
        stack.enter_context(patch.object(yume.hardware, "detect_gpu", lambda: gpu))
        stack.enter_context(patch.object(inst.sys.stdout, "write", lambda s: None))
        return inst.install_llamacpp_python(), steps


@pytest.mark.parametrize(
    ("gpu", "win", "kw", "steps"),
    [
        (NVIDIA, True, {}, ["cuda-wheel", "verify-cuda", "server-deps"]),
        # pip quietly installed a CPU wheel: build with CUDA instead
        (NVIDIA, True, {"cuda_in_wheel": False}, ["cuda-wheel", "verify-cuda", "cuda-source", "server-deps"]),
        (NVIDIA, True, {"cuda_in_wheel": False, "fail": {"cuda-source"}},
         ["cuda-wheel", "verify-cuda", "cuda-source", "cpu-wheel", "server-deps"]),
        (NVIDIA, False, {}, ["cuda-source", "server-deps"]),
        (NVIDIA, False, {"fail": {"cuda-source"}}, ["cuda-source", "cpu-wheel", "server-deps"]),
        (AMD, False, {}, ["rocm-source", "server-deps"]),
        (AMD, True, {}, ["cpu-wheel", "server-deps"]),  # no ROCm build on Windows
        (CPU, True, {}, ["cpu-wheel", "server-deps"]),
        (CPU, False, {"fail": {"cpu-wheel"}}, ["cpu-wheel", "cpu-source", "server-deps"]),
    ],
)  # fmt: skip
def test_llama_cpp_python_install_path(gpu, win, kw, steps):
    ok, ran = llama(gpu, win=win, **kw)
    assert ok and ran == steps


def test_every_build_failing_reports_failure():
    ok, ran = llama(CPU, win=False, fail={"cpu-wheel", "cpu-source"})
    assert not ok and ran == ["cpu-wheel", "cpu-source"]  # no server deps without llama-cpp-python


def test_no_build_tools_stops_before_pip():
    ok, ran = llama(NVIDIA, win=False, build_tools=False)
    assert not ok and ran == []
