"""llama.cpp's prebuilt server (yume/llama_server.py): choosing the build, safe extraction, command line."""

from __future__ import annotations

import io
import json
import sys
import tarfile
import types
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


def test_translation_server_prefers_llama_server_when_installed(tmp_path, monkeypatch):
    from yume import servers

    exe = tmp_path / llama_server.EXE_NAME
    exe.write_bytes(b"")
    (tmp_path / "build.json").write_text(json.dumps({"tag": "b1", "variant": "cuda-12.4"}))
    monkeypatch.setattr(llama_server, "LLAMA_DIR", tmp_path)
    monkeypatch.setattr(llama_server, "BUILD_FILE", tmp_path / "build.json")
    cmd = servers.llamacpp_command({"translation_host": "127.0.0.1"}, "m.gguf", 5000)
    assert cmd[0] == str(exe) and "--n-gpu-layers" in cmd

    monkeypatch.setattr(llama_server, "LLAMA_DIR", tmp_path / "missing")
    cmd = servers.llamacpp_command({"translation_host": "127.0.0.1"}, "m.gguf", 5000)
    assert cmd[1:3] == ["-m", "llama_cpp.server"]  # falls back to llama-cpp-python


# ── install() ────────────────────────────────────────────────────────────────


class Install:
    """install() against a fake GitHub release; archives are real zips."""

    def __init__(self, tmp_path, monkeypatch, *, exe_in_archive=True, download_ok=True, runs=True):
        self.tools = tmp_path / "tools"
        self.tools.mkdir()
        self.llama = self.tools / "llama.cpp"
        self.downloads: list[str] = []
        self.said: list[str] = []
        self.exe_in_archive, self.download_ok = exe_in_archive, download_ok
        say = lambda msg="", *a, **k: self.said.append(str(msg))  # noqa: E731
        import yume.network
        import yume.ui

        for name, value in {
            "TOOLS_DIR": self.tools,
            "LLAMA_DIR": self.llama,
            "BUILD_FILE": self.llama / "build.json",
        }.items():
            monkeypatch.setattr(llama_server, name, value)
        monkeypatch.setattr(llama_server, "_os_arch", lambda: ("win", "x64"))
        monkeypatch.setattr(llama_server, "driver_cuda_version", lambda: (13, 4))
        monkeypatch.setattr(llama_server, "_releases", self._releases)
        monkeypatch.setattr(yume.network, "download_file", self._download)
        out = "version: 11407 (abc123)\n" if runs else "error while loading shared libraries\n"
        monkeypatch.setattr(
            llama_server,
            "_run",
            lambda cmd, **kw: types.SimpleNamespace(returncode=0 if runs else 1, stdout=out, stderr=""),
        )
        for name in ("info", "warn", "error", "success"):
            monkeypatch.setattr(yume.ui, name, say)

    def _releases(self):
        assets = [{"name": n, "size": 1_000_000, "browser_download_url": f"https://x/{n}"} for n in WIN]
        return [{"tag_name": "master", "assets": []}, {"tag_name": "b11407", "assets": assets}]

    def _download(self, url, dest, label, sha256=None):
        self.downloads.append(Path(dest).name)
        if not self.download_ok:
            return False
        with zipfile.ZipFile(dest, "w") as zf:
            if Path(dest).name.startswith("cudart"):
                zf.writestr("cudart64_13.dll", "dll")
            elif self.exe_in_archive:
                zf.writestr(f"build/bin/{llama_server.EXE_NAME}", "exe")
        return True


def test_install_puts_the_cuda_build_and_its_runtime_together(tmp_path, monkeypatch):
    t = Install(tmp_path, monkeypatch)
    assert llama_server.install(NVIDIA) is True
    assert t.downloads == ["llama-b11407-bin-win-cuda-13.4-x64.zip", "cudart-llama-bin-win-cuda-13.4-x64.zip"]
    exe = llama_server.server_path()
    assert exe == t.llama / "build" / "bin" / llama_server.EXE_NAME
    assert (exe.parent / "cudart64_13.dll").exists()  # the runtime DLLs sit next to the exe
    assert llama_server.installed_build() == {"tag": "b11407", "variant": "cuda-13.4"}
    assert sorted(p.name for p in t.tools.iterdir()) == ["llama.cpp"]  # no archives, no .new/.old
    assert any("llama-server installed (cuda-13.4): version: 11407" in s for s in t.said)


def test_install_replaces_an_older_build(tmp_path, monkeypatch):
    t = Install(tmp_path, monkeypatch)
    t.llama.mkdir()
    (t.llama / "stale.txt").write_text("old")
    assert llama_server.install(NVIDIA)
    assert not (t.llama / "stale.txt").exists()


def test_a_failed_download_keeps_the_installed_build(tmp_path, monkeypatch):
    t = Install(tmp_path, monkeypatch, download_ok=False)
    t.llama.mkdir()
    (t.llama / "build.json").write_text('{"tag": "b1"}')
    assert llama_server.install(NVIDIA) is False
    assert llama_server.installed_build() == {"tag": "b1"}
    assert sorted(p.name for p in t.tools.iterdir()) == ["llama.cpp"]


def test_an_archive_without_the_server_is_refused(tmp_path, monkeypatch):
    t = Install(tmp_path, monkeypatch, exe_in_archive=False)
    assert llama_server.install(NVIDIA) is False
    assert not t.llama.exists()
    assert any("not found in the downloaded build" in s for s in t.said)


def test_a_locked_old_build_is_restored(tmp_path, monkeypatch):
    # Windows locks the files of a running llama-server: the swap fails midway
    t = Install(tmp_path, monkeypatch)
    t.llama.mkdir()
    (t.llama / "build.json").write_text('{"tag": "b1"}')
    real_replace = Path.replace

    def replace(self, target):
        if self.name == "llama.cpp.new":
            raise PermissionError("in use")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", replace)
    assert llama_server.install(NVIDIA) is False
    assert llama_server.installed_build() == {"tag": "b1"}
    assert any("stop Yume first" in s for s in t.said)


def test_a_build_that_does_not_start_is_reported(tmp_path, monkeypatch):
    t = Install(tmp_path, monkeypatch, runs=False)
    assert llama_server.install(NVIDIA) is False
    assert any("does not start: error while loading shared libraries" in s for s in t.said)


def test_no_build_for_this_system_or_no_github(tmp_path, monkeypatch):
    t = Install(tmp_path, monkeypatch)
    monkeypatch.setattr(llama_server, "_releases", lambda: [{"tag_name": "b1", "assets": []}])
    assert llama_server.install(NVIDIA) is False
    monkeypatch.setattr(llama_server, "_releases", lambda: (_ for _ in ()).throw(OSError("offline")))
    assert llama_server.install(NVIDIA) is False
    assert any("No llama.cpp build" in s for s in t.said) and any("offline" in s for s in t.said)
