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


# ── FFmpeg ─────────────────────────────────────────────────────────────────────

import io  # noqa: E402
import tarfile  # noqa: E402
import zipfile  # noqa: E402


def ffmpeg(tmp_path, platform, *, archive_names=("ffmpeg", "ffprobe"), runs=True, brew=False):
    """install_ffmpeg with the download faked as a real archive of `archive_names`."""
    tools = tmp_path / "tools"
    ran = []

    def download(key_or_url, dest, label, **kw):
        dest = Path(dest)
        if dest.suffix == ".zip":
            with zipfile.ZipFile(dest, "w") as zf:
                for n in archive_names:
                    zf.writestr(f"ffmpeg-master/bin/{n}", f"binary {n}")
        else:
            with tarfile.open(dest, "w:xz") as tf:
                for n in archive_names:
                    data = f"binary {n}".encode()
                    info = tarfile.TarInfo(f"ffmpeg-master-linux64/bin/{n}")
                    info.size = len(data)
                    tf.addfile(info, io.BytesIO(data))
        return True

    def run(cmd, **kw):
        ran.append(cmd)
        return types.SimpleNamespace(returncode=0 if runs else 1, stdout="ffmpeg version 7", stderr="")

    import yume.network

    with ExitStack() as stack:
        for name, fake in {
            "TOOLS_DIR": tools,
            "IS_WIN": platform == "win",
            "IS_LIN": platform == "linux",
            "_download_verified": lambda key, dest, label: download(key, dest, label),
            "_run": run,
            "info": MagicMock(),
            "warn": MagicMock(),
            "error": MagicMock(),
        }.items():
            stack.enter_context(patch.object(inst, name, fake))
        stack.enter_context(patch.object(yume.network, "download_file", download))
        stack.enter_context(patch.object(inst.shutil, "which", lambda t: "/opt/homebrew/bin/brew" if brew else None))
        stack.enter_context(patch("builtins.print"))
        ok = inst.install_ffmpeg()
    return ok, tools, ran


def test_ffmpeg_windows_takes_both_exes_from_the_zip(tmp_path):
    ok, tools, ran = ffmpeg(tmp_path, "win", archive_names=("ffmpeg.exe", "ffprobe.exe", "ffplay.exe"))
    assert ok
    assert sorted(p.name for p in tools.iterdir()) == ["ffmpeg.exe", "ffprobe.exe"]  # no ffplay, no zip
    assert ran[-1] == [str(tools / "ffmpeg.exe"), "-version"]


def test_ffmpeg_windows_zip_without_ffmpeg_fails(tmp_path):
    ok, _, _ = ffmpeg(tmp_path, "win", archive_names=("readme.txt",))
    assert not ok


def test_ffmpeg_windows_that_does_not_run_fails(tmp_path):
    ok, _, _ = ffmpeg(tmp_path, "win", archive_names=("ffmpeg.exe", "ffprobe.exe"), runs=False)
    assert not ok


def test_ffmpeg_linux_takes_both_binaries_from_the_tarball(tmp_path):
    ok, tools, _ = ffmpeg(tmp_path, "linux")
    assert ok
    assert sorted(p.name for p in tools.iterdir()) == ["ffmpeg", "ffprobe"]


def test_ffmpeg_mac_uses_homebrew_when_present(tmp_path):
    ok, _, ran = ffmpeg(tmp_path, "mac", brew=True)
    assert ok and ran == [["brew", "install", "ffmpeg"]]


def test_ffmpeg_mac_without_homebrew_unzips(tmp_path):
    ok, tools, _ = ffmpeg(tmp_path, "mac", archive_names=("ffmpeg",))
    assert ok and (tools / "ffmpeg").exists()  # was left nested inside the archive folder
