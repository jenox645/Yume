"""The real START_YUME script in a temporary folder, with a pocket_yume.py that only
says which Python ran it."""

import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
WIN = sys.platform == "win32"
SCRIPT = "START_YUME.bat" if WIN else "START_YUME.sh"
VENV_PYTHON = Path("Scripts/python.exe") if WIN else Path("bin/python")


@pytest.fixture
def folder(tmp_path):
    """A Yume folder, and a Python on PATH without Yume's packages."""
    base = tmp_path / "base"
    venv.create(base, with_pip=False)
    yume = tmp_path / "yume"
    yume.mkdir()
    shutil.copy(ROOT / SCRIPT, yume)
    (yume / "pocket_yume.py").write_text("import sys\nprint('ran with', sys.prefix)\n")
    return yume, base


def run(yume, base, **env):
    path = str((base / VENV_PYTHON).parent) + os.pathsep + os.environ["PATH"]
    environ = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    cmd = ["cmd", "/c", str(yume / SCRIPT)] if WIN else ["bash", SCRIPT]
    done = subprocess.run(
        cmd, cwd=yume, env={**environ, "PATH": path, **env}, capture_output=True, text=True, errors="replace", timeout=120,
        stdin=subprocess.DEVNULL,
    )  # fmt: skip
    return done.stdout


def test_a_first_run_makes_its_own_environment_and_stays_in_it(folder):
    yume, base = folder
    assert f"ran with {yume / 'yume-env'}" in run(yume, base).replace("\\\\", "\\")
    assert (yume / "yume-env" / VENV_PYTHON).exists()
    second = run(yume, base)
    assert "Creating" not in second and "yume-env" in second


def test_an_activated_environment_is_left_alone(folder):
    yume, base = folder
    assert "yume-env" not in run(yume, base, VIRTUAL_ENV=str(base))
    assert not (yume / "yume-env").exists()


def test_yume_already_in_this_python_needs_no_environment(folder):
    yume, base = folder
    site = base / "Lib" / "site-packages" if WIN else next((base / "lib").glob("python*/site-packages"))
    (site / "faster_whisper").mkdir()
    (site / "faster_whisper" / "__init__.py").write_text("")
    assert "yume-env" not in run(yume, base)
    assert not (yume / "yume-env").exists()
