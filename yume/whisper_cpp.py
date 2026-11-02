"""whisper.cpp on Vulkan: how Whisper runs on an AMD GPU.

faster-whisper has no AMD backend. Upstream whisper.cpp publishes no Vulkan
binaries, so Yume builds its own from one pinned commit
(.github/workflows/whisper-vulkan.yml) and this module installs that build into
tools/whisper.cpp/. server/whisper_cpp.json pins the build and the models by
SHA-256; server/_whispercpp.py runs them.
"""

from __future__ import annotations

import json
import platform
import shutil
import sys
import tempfile
from pathlib import Path

from yume.constants import MODELS_DIR, SERVER_DIR, TOOLS_DIR
from yume.llama_server import _extract
from yume.network import download_file

WHISPER_DIR = TOOLS_DIR / "whisper.cpp"
EXE_NAME = "whisper-server.exe" if sys.platform == "win32" else "whisper-server"
PINS_FILE = SERVER_DIR / "whisper_cpp.json"
REPO = "https://github.com/jenox645/Yume"


def _pins() -> dict:
    return json.loads(PINS_FILE.read_text(encoding="utf-8"))


RELEASE_PAGE = f"{REPO}/releases/tag/{_pins()['tag']}"


def server_path() -> Path | None:
    """The installed whisper-server, or None."""
    exe = WHISPER_DIR / EXE_NAME
    return exe if exe.is_file() else None


def model_path(name: str) -> Path | None:
    """Where a Whisper model's ggml file belongs; None for a model whisper.cpp has no file of."""
    model = _pins()["models"].get(name)
    return MODELS_DIR / "whisper" / model["file"] if model else None


def model_downloaded(name: str) -> bool:
    path = model_path(name)
    return path is not None and path.is_file()


def available() -> bool:
    """There is a build for this system."""
    return _build_for_this_system() is not None


def _build_for_this_system() -> dict | None:
    if platform.machine().lower() not in ("amd64", "x86_64"):
        return None
    return _pins()["builds"].get(sys.platform)


def install(model: str = "large-v3-turbo") -> bool:
    """The Vulkan build into tools/whisper.cpp/, and the model next to the others."""
    from yume.ui import error, success

    build = _build_for_this_system()
    if build is None:
        error("There is no whisper.cpp build for this system: Whisper stays on the CPU.")
        return False
    url = f"{REPO}/releases/download/{_pins()['tag']}/{build['name']}"
    with tempfile.TemporaryDirectory() as tmp:
        archive = Path(tmp) / build["name"]
        if not download_file(url, archive, "Downloading whisper.cpp", sha256=build["sha256"]):
            return False
        new = WHISPER_DIR.with_name("whisper.cpp.new")
        shutil.rmtree(new, ignore_errors=True)
        new.mkdir(parents=True)
        try:
            _extract(archive, new)
            (new / EXE_NAME).chmod((new / EXE_NAME).stat().st_mode | 0o111)
            shutil.rmtree(WHISPER_DIR, ignore_errors=True)
            new.replace(WHISPER_DIR)
        except OSError as e:  # Windows locks a running whisper-server
            error(f"Could not install whisper.cpp ({e}): stop Yume first, then try again.")
            return False
        finally:
            shutil.rmtree(new, ignore_errors=True)
    if model_path(model) and not download_model(model):  # a custom model has no ggml file: the CPU hears it
        return False
    success("whisper.cpp installed: Whisper will run on your GPU")
    return True


def download_model(name: str) -> bool:
    """The model's ggml file, checked against its pinned SHA-256."""
    path = model_path(name)
    if path is None:
        return False
    if path.is_file():
        return True
    pin = _pins()["models"][name]
    path.parent.mkdir(parents=True, exist_ok=True)
    return download_file(
        f"{_pins()['model_repo']}/{pin['file']}", path, f"Downloading {pin['file']}", sha256=pin["sha256"]
    )
