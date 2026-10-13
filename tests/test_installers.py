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


# ── Deno + the YouTube PO token plugin and server ────────────────────────────


def deno(tmp_path, *, deno_ok=True, bgutil_ok=True, with_main=True, codes=None, already=False, broken=False):
    """install_deno with downloads faked as real zips. `codes` maps a command's
    tell-tale argument to its exit code. Returns (ok, tools, commands, said)."""
    tools = tmp_path / "tools"
    tools.mkdir(parents=True)
    codes = codes or {}
    ran, said = [], []
    if already:
        (tools / "bgutil-ytdlp-pot-provider" / "server" / "src").mkdir(parents=True)
        (tools / "bgutil-ytdlp-pot-provider" / "server" / "src" / "main.ts").write_text("")

    def verified(key, dest, label):
        if broken:
            Path(dest).write_bytes(b"not a zip")
        elif deno_ok:
            with zipfile.ZipFile(dest, "w") as zf:
                zf.writestr("deno.exe", "binary")
        return deno_ok

    def download(url, dest, label, **kw):
        if bgutil_ok:
            with zipfile.ZipFile(dest, "w") as zf:
                zf.writestr("bgutil-ytdlp-pot-provider-1.3.1/README.md", "")
                if with_main:
                    zf.writestr("bgutil-ytdlp-pot-provider-1.3.1/server/src/main.ts", "")
        return bgutil_ok

    def run(cmd, **kw):
        ran.append(cmd)
        code = next((c for k, c in codes.items() if k in cmd), 0)
        if code == "raise":
            raise OSError("no such program")
        return types.SimpleNamespace(returncode=code, stdout="", stderr="")

    import yume.network

    say = lambda msg="", *a, **k: said.append(str(msg))  # noqa: E731
    with ExitStack() as stack:
        for name, fake in {
            "TOOLS_DIR": tools, "IS_WIN": True, "EXE": ".exe", "_download_verified": verified, "_run": run,
            "find_tool": lambda t: "C:/deno.exe", **{n: say for n in ("info", "warn", "error", "success")},
        }.items():  # fmt: skip
            stack.enter_context(patch.object(inst, name, fake))
        stack.enter_context(patch.object(yume.network, "download_file", download))
        ok = inst.install_deno()
    return ok, tools, ran, said


def test_deno_install_sets_up_the_plugin_and_the_token_server(tmp_path):
    ok, tools, ran, said = deno(tmp_path)
    assert ok and (tools / "deno.exe").exists() and not (tools / "deno.zip").exists()
    assert (tools / "bgutil-ytdlp-pot-provider" / "server" / "src" / "main.ts").exists()
    assert not (tools / "bgutil.zip").exists() and not (tools / "bgutil-ytdlp-pot-provider-1.3.1").exists()
    assert "bgutil-ytdlp-pot-provider==1.3.1" in ran[0]
    assert ran[1] == ["C:/deno.exe", "install", "--allow-scripts=npm:canvas", "--frozen"]
    assert len(ran) == 2 and "bgutil server dependencies installed" in said


def test_deno_install_retries_without_the_lockfile_and_reports_failures(tmp_path):
    _ok, _t, ran, said = deno(tmp_path / "a", codes={"--frozen": 1})
    assert ran[2] == ["C:/deno.exe", "install", "--allow-scripts=npm:canvas"]
    assert "bgutil server dependencies installed" in said
    ok, _t, ran, said = deno(tmp_path / "b", codes={"--allow-scripts=npm:canvas": 1, "yt-dlp": 1})
    assert ok and len(ran) == 3
    assert any("PO token plugin install failed" in s for s in said)
    assert any("deno install failed" in s for s in said)
    ok, _t, _ran, said = deno(tmp_path / "c", codes={"--allow-scripts=npm:canvas": "raise", "yt-dlp": "raise"})
    assert ok and any("no such program" in s for s in said)


def test_deno_install_stops_or_goes_on_without_the_token_server(tmp_path):
    ok, _t, ran, _said = deno(tmp_path / "a", deno_ok=False)
    assert not ok and ran == []
    ok, tools, ran, said = deno(tmp_path / "b", bgutil_ok=False)
    assert ok and len(ran) == 1 and "bgutil server download failed" in said
    ok, tools, ran, said = deno(tmp_path / "c", with_main=False)
    assert ok and "main.ts not found after extraction" in said and len(ran) == 1
    ok, tools, ran, said = deno(tmp_path / "d", already=True)
    assert ok and "bgutil server already set up" in said and len(ran) == 1


def test_a_broken_deno_archive_fails(tmp_path):
    ok, _t, ran, said = deno(tmp_path, broken=True)
    assert not ok and ran == [] and any(s.startswith("Failed:") for s in said)
