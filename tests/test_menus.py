"""Interactive menus driven with scripted answers and a fake server."""

from __future__ import annotations

import sys
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).parent.parent))

import yume.benchmark  # noqa: E402
import yume.menus.server as srv  # noqa: E402

MODELS = [name for name, *_ in yume.benchmark.WHISPER_MODELS_INFO]


class WhisperMenu:
    """_menu_whisper_model with a fake server (`stats` None = server down)."""

    def __init__(self, choice, *, stats=None, inputs=(), switch=None):
        self.choice = choice
        self.stats = stats
        self.inputs = list(inputs)
        self.switch = switch if switch is not None else {"model": "switched"}
        self.posts: list[dict] = []
        self.saved = MagicMock()
        self.said: list[str] = []
        self.options: list = []
        self.default = None

    def _arrow(self, prompt, opts, default=0, **kw):
        self.options, self.default = opts, default
        return self.choice

    def _post(self, h, p, path, body, timeout=0):
        self.posts.append(body)
        return self.switch

    def go(self, cfg):
        say = lambda msg="", *a, **k: self.said.append(str(msg))  # noqa: E731
        fakes = {
            "_server_get": lambda h, p, path: self.stats,
            "_server_post": self._post,
            "ask_arrow": self._arrow,
            "header": MagicMock(),
            "pause": MagicMock(),
            "info": say,
            "warn": say,
            "error": say,
            "success": say,
        }
        with ExitStack() as stack:
            for name, fake in fakes.items():
                stack.enter_context(patch.object(srv, name, fake))
            stack.enter_context(patch("builtins.input", side_effect=self.inputs))
            stack.enter_context(patch.object(yume.benchmark, "_is_whisper_model_cached", lambda m: m == "small"))
            stack.enter_context(patch("config.save_config", self.saved))
            srv._menu_whisper_model(cfg)
        return self


def _cfg(model="small"):
    return {"whisper_host": "127.0.0.1", "whisper_port": 5001, "whisper_model": model}


def test_offline_switch_only_changes_the_config():
    cfg = _cfg()
    m = WhisperMenu(MODELS.index("medium")).go(cfg)
    assert cfg["whisper_model"] == "medium"
    assert m.posts == []
    m.saved.assert_called_once()
    assert any("applies on next launch" in s for s in m.said)


def test_online_switch_asks_the_server():
    cfg = _cfg()
    m = WhisperMenu(MODELS.index("medium"), stats={"model": "small", "device": "cuda"}, switch={"model": "medium"})
    m.go(cfg)
    assert m.posts == [{"model": "medium"}]
    assert cfg["whisper_model"] == "medium"


def test_a_failed_switch_keeps_the_config():
    cfg = _cfg()
    m = WhisperMenu(MODELS.index("medium"), stats={"model": "small"}, switch={"error": "CUDA out of memory"}).go(cfg)
    assert cfg["whisper_model"] == "small"
    m.saved.assert_not_called()
    assert any("CUDA out of memory" in s for s in m.said)


def test_the_active_model_is_preselected_and_tagged():
    m = WhisperMenu(len(MODELS) + 1, stats={"model": "small"}).go(_cfg())  # Back
    assert m.default == MODELS.index("small")
    label, hint = m.options[MODELS.index("small")]
    assert hint == "active" and "[downloaded]" in label
    assert "[not downloaded]" in m.options[MODELS.index("medium")][0]


def test_choosing_the_active_model_changes_nothing():
    cfg = _cfg()
    m = WhisperMenu(MODELS.index("small")).go(cfg)
    m.saved.assert_not_called()
    assert any("Already active" in s for s in m.said)


def test_custom_model_folder_with_a_friendly_name(tmp_path):
    for f in ("model.bin", "config.json", "tokenizer.json", "vocabulary.txt"):
        (tmp_path / f).write_text("x")
    cfg = _cfg()
    WhisperMenu(len(MODELS), inputs=[f'"{tmp_path}"', "My Whisper"]).go(cfg)
    assert cfg["whisper_model"] == str(tmp_path.resolve())
    assert cfg["whisper_model_name"] == "My Whisper"


def test_custom_model_folder_missing_files_is_refused(tmp_path):
    (tmp_path / "model.bin").write_text("x")
    cfg = _cfg()
    m = WhisperMenu(len(MODELS), inputs=[str(tmp_path)]).go(cfg)
    assert cfg["whisper_model"] == "small"
    assert any("missing: config.json, tokenizer.json, vocabulary.txt" in s for s in m.said)


def test_custom_model_cancel_and_bad_folder(tmp_path):
    cfg = _cfg()
    WhisperMenu(len(MODELS), inputs=[""]).go(cfg)
    m = WhisperMenu(len(MODELS), inputs=[str(tmp_path / "nope")]).go(cfg)
    assert cfg["whisper_model"] == "small"
    assert any("Directory not found" in s for s in m.said)


def test_an_active_custom_model_shows_its_name():
    cfg = {**_cfg(str(Path("D:/models/ft-whisper"))), "whisper_model_name": "Fine-tuned"}
    m = WhisperMenu(len(MODELS) + 1).go(cfg)
    assert m.default == len(MODELS)  # the custom entry
    assert m.options[len(MODELS)][1] == "active"
    assert any("Fine-tuned" in s for s in m.said)
