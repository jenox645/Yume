"""llama.cpp's prebuilt server (yume/llama_server.py): choosing the build, safe extraction, command line."""

from __future__ import annotations

import io
import json
import sys
import tarfile
import zipfile
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from yume import llama_server  # noqa: E402

# Asset names as published by ggml-org/llama.cpp (release b11407, October 2026)
WIN = [
    "cudart-llama-bin-win-cuda-12.4-x64.zip",
    "cudart-llama-bin-win-cuda-13.4-x64.zip",
    "llama-b11407-bin-win-cpu-x64.zip",
    "llama-b11407-bin-win-cuda-12.4-x64.zip",
    "llama-b11407-bin-win-cuda-13.4-x64.zip",
    "llama-b11407-bin-win-vulkan-x64.zip",
    "llama-b11407-bin-win-rocm-10.0-x64.zip",
]
LINUX = [
    "cudart-llama-b11407-bin-ubuntu-cuda-12.8-x64.tar.gz",
    "llama-b11407-bin-ubuntu-cuda-12.8-x64.tar.gz",
    "llama-b11407-bin-ubuntu-vulkan-x64.tar.gz",
    "llama-b11407-bin-ubuntu-x64.tar.gz",
]
MAC = ["llama-b11407-bin-macos-arm64.tar.gz", "llama-b11407-bin-macos-x64.tar.gz"]
ASSETS = [{"name": n, "size": 1} for n in WIN + LINUX + MAC]
NVIDIA = {"has_nvidia": True, "has_amd": False}


def pick(gpu, cuda, os_arch):
    with patch.object(llama_server, "_os_arch", return_value=os_arch):
        variant, assets = llama_server.pick_assets(ASSETS, gpu, cuda)
    return variant, [a["name"] for a in assets]


def test_newest_cuda_build_the_driver_supports():
    # a 13.3 driver cannot run the 13.4 build: the 12.4 one is the newest that works
    assert pick(NVIDIA, (13, 3), ("win", "x64")) == (
        "cuda-12.4",
        ["llama-b11407-bin-win-cuda-12.4-x64.zip", "cudart-llama-bin-win-cuda-12.4-x64.zip"],
    )
    assert pick(NVIDIA, (13, 6), ("win", "x64"))[0] == "cuda-13.4"


def test_old_driver_or_other_gpus_get_vulkan():
    assert pick(NVIDIA, (11, 8), ("win", "x64")) == ("vulkan", ["llama-b11407-bin-win-vulkan-x64.zip"])
    assert pick({"has_amd": True}, None, ("win", "x64"))[0] == "vulkan"


def test_cpu_and_other_platforms():
    assert pick({}, None, ("win", "x64")) == ("cpu", ["llama-b11407-bin-win-cpu-x64.zip"])
    assert pick({}, None, ("ubuntu", "x64")) == ("cpu", ["llama-b11407-bin-ubuntu-x64.tar.gz"])
    assert pick(NVIDIA, (12, 9), ("ubuntu", "x64"))[1] == [
        "llama-b11407-bin-ubuntu-cuda-12.8-x64.tar.gz",
        "cudart-llama-b11407-bin-ubuntu-cuda-12.8-x64.tar.gz",
    ]
    assert pick({}, None, ("macos", "arm64")) == ("metal", ["llama-b11407-bin-macos-arm64.tar.gz"])


def test_driver_version_from_old_and_new_nvidia_smi_headers():
    for header, expected in [("| NVIDIA-SMI 550.54   Driver Version: 550.54   CUDA Version: 12.4 |", (12, 4)),
                             ("| NVIDIA-SMI 610.88   KMD Version: 610.88   CUDA UMD Version: 13.3 |", (13, 3))]:  # fmt: skip
        with patch.object(llama_server, "_run", return_value=type("R", (), {"stdout": header})()):
            assert llama_server.driver_cuda_version() == expected


def test_archives_cannot_write_outside_the_target(tmp_path):
    bad_zip = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad_zip, "w") as zf:
        zf.writestr("../escape.txt", "x")
    with pytest.raises(ValueError):
        llama_server._extract(bad_zip, tmp_path / "out")
    assert not (tmp_path / "escape.txt").exists()

    good_tar = tmp_path / "ok.tar.gz"
    with tarfile.open(good_tar, "w:gz") as tf:
        data = b"#!/bin/sh\n"
        info = tarfile.TarInfo("build/bin/llama-server")
        info.size = len(data)
        tf.addfile(info, io.BytesIO(data))
        evil = tarfile.TarInfo("../../evil")
        evil.size = 1
        tf.addfile(evil, io.BytesIO(b"x"))
    llama_server._extract(good_tar, tmp_path / "out2")
    assert (tmp_path / "out2" / "build" / "bin" / "llama-server").exists()
    assert not (tmp_path / "evil").exists()  # filtered out


def test_command_uses_one_slot_and_the_gpu(tmp_path, monkeypatch):
    exe = tmp_path / llama_server.EXE_NAME
    exe.write_bytes(b"")
    monkeypatch.setattr(llama_server, "LLAMA_DIR", tmp_path)
    cmd = llama_server.command("m.gguf", "127.0.0.1", 5000, gpu_layers=True)
    assert cmd[0] == str(exe)
    assert cmd[cmd.index("--parallel") + 1] == "1"  # the default splits the context across slots
    assert "--n-gpu-layers" in cmd
    assert "--n-gpu-layers" not in llama_server.command("m.gguf", "127.0.0.1", 5000, gpu_layers=False)


def test_launch_prefers_llama_server_when_installed(tmp_path, monkeypatch):
    from yume import launch

    exe = tmp_path / llama_server.EXE_NAME
    exe.write_bytes(b"")
    (tmp_path / "build.json").write_text(json.dumps({"tag": "b1", "variant": "cuda-12.4"}))
    monkeypatch.setattr(llama_server, "LLAMA_DIR", tmp_path)
    monkeypatch.setattr(llama_server, "BUILD_FILE", tmp_path / "build.json")
    cmd = launch.llamacpp_command({"translation_host": "127.0.0.1"}, "m.gguf", 5000)
    assert cmd[0] == str(exe) and "--n-gpu-layers" in cmd

    monkeypatch.setattr(llama_server, "LLAMA_DIR", tmp_path / "missing")
    cmd = launch.llamacpp_command({"translation_host": "127.0.0.1"}, "m.gguf", 5000)
    assert cmd[1:3] == ["-m", "llama_cpp.server"]  # falls back to llama-cpp-python
