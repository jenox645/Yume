"""Features that went wrong for a real user: choices lost, silent stops, misleading states."""

from __future__ import annotations

import datetime
import sys
import types
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from yume import health, launch, setup  # noqa: E402
from yume.menus import whisper_model  # noqa: E402

NVIDIA = {"has_nvidia": True, "has_amd": False, "vram_mb": 12288, "name": "RTX 3060"}
NO_GPU = {"has_nvidia": False, "has_amd": False, "vram_mb": 0, "name": None}


def test_running_setup_again_keeps_the_chosen_whisper_model():
    cfg = {"whisper_model": "large-v2"}
    setup._pick_whisper_model(cfg, NVIDIA, rerun=True)
    assert cfg["whisper_model"] == "large-v2"


def test_the_first_setup_takes_the_recommended_model():
    cfg = {"whisper_model": "large-v2"}
    setup._pick_whisper_model(cfg, NVIDIA)
    assert cfg["whisper_model"] == "large-v3-turbo"


def test_running_setup_again_keeps_the_chosen_device_and_precision():
    cfg = {"whisper_device": "cpu", "whisper_compute_type": "int8_float16"}
    setup._pick_whisper_device(cfg, NVIDIA, rocm=False, rerun=True)
    assert cfg == {"whisper_device": "cpu", "whisper_compute_type": "int8_float16"}


def test_a_gpu_that_is_gone_does_not_stay_the_device():
    cfg = {"whisper_device": "cuda", "whisper_compute_type": "float16"}
    setup._pick_whisper_device(cfg, NO_GPU, rocm=False, rerun=True)
    assert cfg == {"whisper_device": "cpu", "whisper_compute_type": "int8"}


@pytest.mark.parametrize(("installed", "method"), [(True, "deno"), (False, "cookies")])
def test_deno_auth_is_saved_only_when_deno_was_installed(installed, method):
    cfg = {"youtube_auth_method": "cookies"}
    with (
        patch.object(setup, "ask_arrow", return_value=1),
        patch.object(setup, "_install_with_retry", return_value=installed),
    ):
        setup._choose_youtube_auth(cfg)
    assert cfg["youtube_auth_method"] == method


@pytest.mark.parametrize(("works_after", "goes_on"), [(True, True), (False, False)])
def test_launch_goes_on_after_installing_the_translation_engine(works_after, goes_on):
    imports = iter([False, works_after])
    with (
        patch.object(launch, "_try_import", lambda m: next(imports)),
        patch.object(launch, "ask_yn", return_value=True),
        patch.object(launch, "pause"),
        patch("yume.installers.install_llamacpp_python"),
        patch.dict(sys.modules, {"fastapi": types.ModuleType("fastapi"), "uvicorn": types.ModuleType("uvicorn")}),
    ):
        assert launch._ensure_python_engine({}) is goes_on


@pytest.mark.parametrize(
    ("typed", "saved"), [("turbo", "large-v3-turbo"), ("large-v1", "large-v1"), ("large-v3", "large-v3")]
)
def test_model_switch_takes_what_the_server_takes_while_it_is_down(typed, saved):
    cfg = {"whisper_host": "127.0.0.1", "whisper_port": 5001}
    with (
        patch.object(whisper_model, "_server_get", return_value=None),
        patch.object(whisper_model, "_use_model") as use,
        patch.object(whisper_model, "success"),
    ):
        whisper_model._model_switch(cfg, typed)
    use.assert_called_once_with(cfg, saved)


def test_model_switch_still_refuses_a_typo_while_the_server_is_down():
    with (
        patch.object(whisper_model, "_server_get", return_value=None),
        patch.object(whisper_model, "_use_model") as use,
        patch.object(whisper_model, "error") as said,
    ):
        whisper_model._model_switch({"whisper_host": "h", "whisper_port": 1}, "turob")
    use.assert_not_called()
    said.assert_called_once()


def _version(days_old):
    return (datetime.date.today() - datetime.timedelta(days=days_old)).strftime("%Y.%m.%d")


@pytest.mark.parametrize(
    ("days_old", "method", "passed", "hint"),
    [
        (10, "cookies", True, ""),
        (200, "cookies", False, "Tools"),
        (200, "deno", False, "pip install -U yt-dlp"),
    ],
)
def test_health_flags_a_yt_dlp_too_old_for_youtube(days_old, method, passed, hint):
    reply = types.SimpleNamespace(returncode=0, stdout=_version(days_old), stderr="")
    with patch.object(health, "find_tool", return_value="/bin/yt-dlp"), patch("yume.utils._run", return_value=reply):
        _name, ok, detail = health._chk_ytdlp_age({"youtube_auth_method": method})
    assert ok is passed and hint in detail
