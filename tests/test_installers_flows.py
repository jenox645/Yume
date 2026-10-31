"""yume/installers.py and llama_python.py: checksums, archive extraction and what each installer does
when a step fails. Downloads and subprocesses are faked; tools go to a tmp folder."""

from __future__ import annotations

import io
import sys
import tarfile
import types
import zipfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import yume.network  # noqa: E402
from yume.utils import safe_extractall  # noqa: E402
import yume.installers as inst  # noqa: E402
import yume.llama_python as llama_python  # noqa: E402


def done(code=0, stdout="", stderr=""):
    return types.SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr)


@pytest.fixture
def said(monkeypatch):
    lines = []
    for name in ("info", "warn", "error", "success"):
        for mod in (inst, llama_python):
            monkeypatch.setattr(mod, name, lambda msg="", *a, name=name: lines.append(f"{name}: {msg}"))
    return lines


@pytest.fixture
def tools(tmp_path, monkeypatch):
    monkeypatch.setattr(inst, "TOOLS_DIR", tmp_path)
    return tmp_path


class TestChecksums:
    @pytest.mark.parametrize(
        ("url", "sums"),
        [
            ("https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe", inst._YTDLP_SUMS),
            ("https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/x.zip", inst._BTBN_SUMS),
            ("https://github.com/denoland/deno/releases/latest/download/d.zip",
             "https://github.com/denoland/deno/releases/latest/download/d.zip.sha256sum"),
        ],
    )  # fmt: skip
    def test_each_tool_has_its_published_sum(self, monkeypatch, url, sums):
        got = []
        monkeypatch.setattr(yume.network, "fetch_published_sha256", lambda s, n: got.append((s, n)) or "abc")
        assert inst._expected_sha256(url) == "abc"
        assert got == [(sums, url.rsplit("/", 1)[-1])]

    def test_a_url_without_published_sums_is_not_looked_up(self, monkeypatch):
        monkeypatch.setattr(yume.network, "fetch_published_sha256", lambda *a: pytest.fail("no lookup"))
        assert inst._expected_sha256("https://evermeet.cx/ffmpeg/getrelease/zip") is None

    def test_an_unreachable_sum_warns_that_nothing_is_verified(self, monkeypatch, said):
        monkeypatch.setattr(yume.network, "fetch_published_sha256", lambda *a: None)
        assert inst._expected_sha256("https://github.com/yt-dlp/yt-dlp/x/yt-dlp") is None
        assert any("without verification" in s for s in said)

    def test_the_checksum_reaches_the_download(self, monkeypatch, said):
        got = {}
        monkeypatch.setitem(inst.DOWNLOAD_URLS, "yt-dlp", "https://example.test/yt-dlp")
        monkeypatch.setattr(inst, "_expected_sha256", lambda url: "f00d")
        monkeypatch.setattr(
            yume.network, "download_file", lambda url, dest, label, sha256: got.update(s=sha256) or True
        )
        assert inst._download_verified("yt-dlp", Path("x"), "yt-dlp")
        assert got == {"s": "f00d"}


class TestArchives:
    def _zip(self, path, names):
        with zipfile.ZipFile(path, "w") as zf:
            for name in names:
                zf.writestr(name, "data")
        return path

    def test_zip_slip_is_refused_before_anything_is_written(self, tmp_path):
        z = self._zip(tmp_path / "a.zip", ["good.txt", "../evil.txt"])
        target = tmp_path / "out"
        target.mkdir()
        with zipfile.ZipFile(z) as zf, pytest.raises(ValueError, match="Unsafe path"):
            safe_extractall(zf, target)
        assert list(target.iterdir()) == [] and not (tmp_path / "evil.txt").exists()

    def test_a_safe_zip_is_extracted(self, tmp_path):
        z = self._zip(tmp_path / "a.zip", ["dir/ok.txt"])
        with zipfile.ZipFile(z) as zf:
            safe_extractall(zf, tmp_path / "out")
        assert (tmp_path / "out" / "dir" / "ok.txt").read_text() == "data"

    def test_ffmpeg_zip_is_flattened_and_nothing_else_is_kept(self, tools, monkeypatch):
        monkeypatch.setattr(inst, "IS_WIN", True)
        z = self._zip(tools / "ffmpeg.zip", ["b/bin/ffmpeg.exe", "b/bin/ffprobe.exe", "b/bin/ffplay.exe", "b/doc.txt"])
        inst._extract_ffmpeg(z)
        assert sorted(p.name for p in tools.iterdir()) == ["ffmpeg.exe", "ffprobe.exe"]  # and the archive is gone

    def test_ffmpeg_tar_never_writes_outside_tools(self, tools, monkeypatch):
        monkeypatch.setattr(inst, "IS_WIN", False)
        modes = []
        monkeypatch.setattr(inst.os, "chmod", lambda p, m: modes.append((Path(p).name, m)))
        archive = tools / "ffmpeg.tar.xz"
        with tarfile.open(archive, "w:xz") as tf:
            for name in ("x/bin/ffmpeg", "x/bin/ffprobe", "../../ffmpeg", "x/doc"):
                info = tarfile.TarInfo(name)
                info.size = 4
                tf.addfile(info, io.BytesIO(b"data"))
        inst._extract_ffmpeg(archive)
        assert sorted(p.name for p in tools.iterdir()) == ["ffmpeg", "ffprobe"]
        assert sorted(n for n, mode in modes if mode == inst.UNIX_EXEC_MODE) == ["ffmpeg", "ffprobe"]  # executable


class TestFfmpeg:
    def _setup(self, monkeypatch, *, win=True, lin=False, download=True):
        monkeypatch.setattr(inst, "IS_WIN", win)
        monkeypatch.setattr(inst, "IS_LIN", lin)
        monkeypatch.setattr(inst, "_download_verified", lambda key, dest, label: download)

    def test_failed_download_stops(self, tools, said, monkeypatch):
        self._setup(monkeypatch, download=False)
        assert inst.install_ffmpeg() is False

    def test_a_broken_archive_is_a_failure_not_a_crash(self, tools, said, monkeypatch, capsys):
        self._setup(monkeypatch)
        (tools / "ffmpeg.zip").write_bytes(b"not a zip")
        assert inst.install_ffmpeg() is False
        assert "Failed" in capsys.readouterr().out

    def test_archive_without_ffmpeg_says_how_to_get_it(self, tools, said, monkeypatch, capsys):
        self._setup(monkeypatch)
        with zipfile.ZipFile(tools / "ffmpeg.zip", "w") as zf:
            zf.writestr("readme.txt", "nothing here")
        assert inst.install_ffmpeg() is False
        assert any("ffmpeg.org/download" in s for s in said)

    @pytest.mark.parametrize(("code", "ok"), [(0, True), (1, False)])
    def test_the_extracted_binary_must_run(self, tools, said, monkeypatch, code, ok):
        monkeypatch.setattr(inst, "IS_WIN", True)
        (tools / "ffmpeg.exe").write_bytes(b"")
        monkeypatch.setattr(inst, "_run", lambda cmd, **k: done(code))
        assert inst._ffmpeg_runs() is ok

    def test_a_mac_with_homebrew_uses_it(self, tools, said, monkeypatch):
        self._setup(monkeypatch, win=False)
        monkeypatch.setattr(inst.shutil, "which", lambda t: "/opt/homebrew/bin/brew")
        ran = []
        monkeypatch.setattr(inst, "_run", lambda cmd, **k: ran.append(cmd) or done())
        assert inst.install_ffmpeg() is True
        assert ran == [["brew", "install", "ffmpeg"]]

    @pytest.mark.parametrize(("ok", "chmods"), [(True, 1), (False, 0)])
    def test_ytdlp_is_made_executable_on_posix(self, tools, monkeypatch, ok, chmods):
        monkeypatch.setattr(inst, "IS_WIN", False)
        monkeypatch.setattr(inst, "_download_verified", lambda *a: ok)
        chmod = MagicMock()
        monkeypatch.setattr(inst.os, "chmod", chmod)
        assert inst.install_ytdlp() is ok
        assert chmod.call_count == chmods


class TestPipHelpers:
    def test_broken_pip_tells_how_to_install_it(self, said, monkeypatch):
        monkeypatch.setattr(inst, "_run", lambda cmd, **k: done(1))
        monkeypatch.setattr(inst, "IS_WIN", False)
        monkeypatch.setattr(inst, "detect_package_manager", lambda: "apt")
        assert inst._check_pip() is False
        assert any("sudo apt install python3-pip" in s for s in said)

    def test_the_system_python_warning_shows_once(self, said, monkeypatch, capsys):
        monkeypatch.setattr(inst, "_run", lambda cmd, **k: done())
        monkeypatch.setattr(inst, "_is_venv", lambda: False)
        monkeypatch.setattr(inst, "_pip_venv_warned", False)
        assert inst._check_pip() and inst._check_pip()
        assert sum("system Python" in s for s in said) == 1

    def test_build_tools_without_a_package_manager(self, said, monkeypatch):
        monkeypatch.setattr(llama_python, "detect_package_manager", lambda: None)
        assert llama_python._install_build_tools() is False
        assert any("install manually" in s for s in said)

    @pytest.mark.parametrize(
        ("pm", "first"),
        [("apt", ["sudo", "apt-get"]), ("dnf", ["sudo", "dnf"]), ("brew", ["brew", "install"]),
         ("pacman", ["sudo", "pacman"]), ("zypper", ["sudo", "zypper"])],
    )  # fmt: skip
    def test_build_tools_use_the_machines_package_manager(self, said, monkeypatch, pm, first):
        ran = []
        monkeypatch.setattr(llama_python, "detect_package_manager", lambda: pm)
        monkeypatch.setattr(llama_python, "_run", lambda cmd, **k: ran.append(cmd) or done())
        assert llama_python._install_build_tools() is True
        assert ran[0][:2] == first and len(ran) == 1

    def test_a_refused_system_install_falls_back_to_pip(self, said, monkeypatch):
        ran = []
        monkeypatch.setattr(llama_python, "detect_package_manager", lambda: "apt")
        monkeypatch.setattr(llama_python, "_run", lambda cmd, **k: ran.append(cmd) or done(1))
        assert llama_python._install_build_tools() is True
        assert ran[1][1:5] == ["-m", "pip", "install", "ninja"]

    def test_progress_shows_pips_last_lines_on_failure(self, said, monkeypatch, capsys):
        err = "\n".join(f"line {i}" for i in range(20))
        monkeypatch.setattr(llama_python, "_run", lambda cmd, **k: done(1, stderr=err))
        assert llama_python._pip_with_progress(["pip"], "Installing x") is False
        out = capsys.readouterr().out
        assert "line 19" in out and "line 9" not in out.replace("line 19", "")
        assert any("failed after" in s for s in said)

    def test_progress_says_when_pip_timed_out(self, said, monkeypatch, capsys):
        monkeypatch.setattr(llama_python, "_run", lambda cmd, **k: done(124, stderr="Timed out"))
        assert llama_python._pip_with_progress(["pip"], "Installing x", timeout=600) is False
        assert any("timed out after 10 minutes" in s for s in said)

    def test_progress_passes_the_environment_only_when_given(self, said, monkeypatch, capsys):
        seen = []
        monkeypatch.setattr(llama_python, "_run", lambda cmd, **k: seen.append(k) or done())
        assert llama_python._pip_with_progress(["pip"], "x", env={"A": "1"})
        assert llama_python._pip_with_progress(["pip"], "x")
        assert seen[0]["env"] == {"A": "1"} and "env" not in seen[1]


class TestPythonDeps:
    def _deps(self, tmp_path, monkeypatch, *, req=True, code=0, stderr="", win=False):
        (tmp_path / "server").mkdir()
        if req:
            (tmp_path / "server" / "requirements.txt").write_text("flask==3\n")
        monkeypatch.setattr(inst, "BASE_DIR", tmp_path)
        monkeypatch.setattr(inst, "_check_pip", lambda: True)
        monkeypatch.setattr(inst, "IS_WIN", win)
        monkeypatch.setattr(inst, "has_build_tools", lambda: True)
        monkeypatch.setattr(inst, "_run", lambda cmd, **k: done(code, stderr=stderr))
        return inst.install_python_deps()

    def test_success(self, tmp_path, said, monkeypatch):
        assert self._deps(tmp_path, monkeypatch) is True

    def test_a_missing_requirements_file_says_to_re_extract(self, tmp_path, said, monkeypatch):
        assert self._deps(tmp_path, monkeypatch, req=False) is False
        assert any("re-extract" in s for s in said)

    def test_a_failed_install_shows_why(self, tmp_path, said, monkeypatch, capsys):
        assert self._deps(tmp_path, monkeypatch, code=1, stderr="No matching distribution for av") is False
        assert "No matching distribution" in capsys.readouterr().out

    def test_a_missing_compiler_on_windows_links_the_build_tools(self, tmp_path, said, monkeypatch, capsys):
        assert self._deps(tmp_path, monkeypatch, code=1, stderr="Microsoft Visual C++ 14.0 required", win=True) is False
        assert any("visual-cpp-build-tools" in s for s in said)


class TestInstallOllama:
    def _common(self, tools, monkeypatch, *, win, mac, download=True):
        monkeypatch.setattr(inst, "IS_WIN", win)
        monkeypatch.setattr(inst, "IS_MAC", mac)
        monkeypatch.setattr(inst.time, "sleep", lambda s: None)
        monkeypatch.setitem(inst.DOWNLOAD_URLS, "ollama", "https://example.test/ollama")
        monkeypatch.setattr(yume.network, "download_file", lambda *a, **k: download)
        monkeypatch.setattr("yume.ui.pause", lambda *a: None)

    def test_windows_waits_for_the_installer_and_removes_it(self, tools, said, monkeypatch):
        self._common(tools, monkeypatch, win=True, mac=False)
        (tools / "OllamaSetup.exe").write_bytes(b"")
        popen = MagicMock()
        monkeypatch.setattr(inst.subprocess, "Popen", popen)
        monkeypatch.setattr(yume.network, "check_server", lambda *a: {"up": True})
        assert inst.install_ollama() is True
        popen.assert_called_once_with([str(tools / "OllamaSetup.exe")])
        assert not (tools / "OllamaSetup.exe").exists()

    def test_windows_setup_still_running_is_not_a_failed_install(self, tools, said, monkeypatch):
        # ollama.exe lands before OllamaSetup.exe exits, and Windows locks a running program
        self._common(tools, monkeypatch, win=True, mac=False)
        (tools / "OllamaSetup.exe").write_bytes(b"")
        monkeypatch.setattr(inst.subprocess, "Popen", MagicMock())
        monkeypatch.setattr(yume.network, "check_server", lambda *a: {"up": True})
        monkeypatch.setattr(Path, "unlink", MagicMock(side_effect=PermissionError(13, "in use")))
        assert inst.install_ollama() is True
        assert not any(s.startswith("error") for s in said)

    def test_windows_installer_that_cannot_start(self, tools, said, monkeypatch):
        self._common(tools, monkeypatch, win=True, mac=False)
        monkeypatch.setattr(inst.subprocess, "Popen", MagicMock(side_effect=OSError("blocked")))
        assert inst.install_ollama() is False
        assert any("blocked" in s for s in said)

    @pytest.mark.parametrize(("win", "mac"), [(True, False), (False, True), (False, False)])
    def test_a_failed_download_stops_on_every_os(self, tools, said, monkeypatch, win, mac):
        self._common(tools, monkeypatch, win=win, mac=mac, download=False)
        monkeypatch.setattr(inst.subprocess, "Popen", lambda *a, **k: pytest.fail("nothing to run"))
        monkeypatch.setattr(inst, "_run", lambda *a, **k: pytest.fail("nothing to run"))
        assert inst.install_ollama() is False

    def test_mac_says_the_last_step_is_manual(self, tools, said, monkeypatch):
        self._common(tools, monkeypatch, win=False, mac=True)
        assert inst.install_ollama() is True
        assert any("Applications" in s for s in said)

    @pytest.mark.parametrize(("code", "ok"), [(0, True), (1, False)])
    def test_linux_runs_the_official_script_interactively_and_deletes_it(self, tools, said, monkeypatch, code, ok):
        self._common(tools, monkeypatch, win=False, mac=False)
        (tools / "ollama_install.sh").write_text("#!/bin/sh\n")
        ran = []
        monkeypatch.setattr(inst, "_run", lambda cmd, **k: ran.append((cmd, k)) or done(code))
        assert inst.install_ollama() is ok
        cmd, kw = ran[0]
        assert cmd == ["bash", str(tools / "ollama_install.sh")]
        assert kw["capture_output"] is False  # it asks for the sudo password
        assert not (tools / "ollama_install.sh").exists()


class TestLlamaServerDeps:
    def test_failing_extra_deps_only_warn(self, said, monkeypatch):
        monkeypatch.setattr(inst, "_check_pip", lambda: True)
        monkeypatch.setattr(inst, "ensure_build_tools", lambda: True)
        monkeypatch.setattr(inst, "install_gpu_build", lambda gpu: True)
        monkeypatch.setattr("yume.hardware.detect_gpu", lambda: {"has_nvidia": True})
        monkeypatch.setattr(inst, "_run", lambda cmd, **k: done(1))
        assert inst.install_llamacpp_python() is True
        assert any("check manually" in s for s in said)

    def test_no_build_tools_says_what_to_install(self, said, monkeypatch, capsys):
        monkeypatch.setattr(llama_python, "IS_WIN", False)
        monkeypatch.setattr(llama_python, "IS_LIN", True)
        monkeypatch.setattr(llama_python, "has_build_tools", lambda: False)
        monkeypatch.setattr(llama_python, "_install_build_tools", lambda: False)
        monkeypatch.setattr(llama_python, "detect_package_manager", lambda: "dnf")
        assert llama_python.ensure_build_tools() is False
        assert "sudo dnf install ninja-build" in capsys.readouterr().out
