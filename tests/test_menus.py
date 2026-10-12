"""Interactive menus driven with scripted answers and a fake server."""

from __future__ import annotations

import sys
import types
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


# ── Settings menu ─────────────────────────────────────────────────────────────

import yume.menus.settings as st  # noqa: E402

SUBMENUS = ["_set_whisper", "_menu_backend", "_set_addrs", "_menu_translation_prompt",
            "_menu_romanization_prompt", "_menu_yt_auth", "menu_one_click"]  # fmt: skip
BACK = 10


def settings(choices, cfg=None, *, inputs=(), yns=(), backups=(), gpu=None, imported=None):
    cfg = cfg or {
        "whisper_model": "small", "whisper_device": "auto", "whisper_compute_type": "auto",
        "whisper_host": "127.0.0.1", "whisper_port": 5001, "translation_host": "127.0.0.1",
        "translation_port": 5000, "youtube_auth_method": "cookies", "translation_backend": "ollama",
    }  # fmt: skip
    rows: list = []
    calls = {name: MagicMock() for name in SUBMENUS}
    fakes = {
        "ask_arrow": MagicMock(side_effect=list(choices)),
        "ask_input": MagicMock(side_effect=list(inputs)),
        "ask_yn": MagicMock(side_effect=list(yns)),
        "header": MagicMock(),
        "pause": MagicMock(),
        "info": MagicMock(),
        "bullet": MagicMock(),
        "success": MagicMock(),
        "table": lambda headers, r, **kw: rows.append(r),
        "detect_gpu": lambda: gpu or {"has_nvidia": True, "vram_mb": 12288},
        "_one_click_label": lambda cfg: "on",
        **calls,
    }
    exported, imports, saved = MagicMock(), MagicMock(return_value=imported), MagicMock()
    with ExitStack() as stack:
        for name, fake in fakes.items():
            stack.enter_context(patch.object(st, name, fake))
        stack.enter_context(patch.object(st, "BASE_DIR", MagicMock(glob=lambda pat: list(backups))))
        stack.enter_context(patch("config.config_export", exported))
        stack.enter_context(patch("config.config_import", imports))
        stack.enter_context(patch("config.save_config", saved))
        st.settings_menu(cfg)
    return types.SimpleNamespace(cfg=cfg, rows=rows, calls=calls, exported=exported, imports=imports, saved=saved)


def test_each_entry_opens_its_submenu():
    r = settings([*range(len(SUBMENUS)), BACK])
    for name in SUBMENUS:
        r.calls[name].assert_called_once()


def test_esc_leaves():
    r = settings([-1])
    assert not any(m.called for m in r.calls.values())


def test_auto_device_and_precision_are_resolved_for_display():
    rows = settings([BACK]).rows[0]
    assert "cuda" in rows[1][1] and "float16 (auto)" in rows[1][1]
    rows = settings([BACK], gpu={"has_nvidia": False}).rows[0]
    assert rows[1][1] == "cpu (auto) / int8 (auto)"
    assert rows[8][1] == "cookies (chrome)"


def test_export_and_import_from_a_listed_backup():
    backups = [Path("yume_config_backup_2.json"), Path("yume_config_backup_1.json")]
    r = settings([7, 8, BACK], inputs=["2"], backups=backups, imported={"whisper_model": "medium"})
    r.exported.assert_called_once()
    r.imports.assert_called_once_with(backups[1])
    assert r.cfg["whisper_model"] == "medium"


def test_import_from_a_typed_path():
    r = settings([8, BACK], inputs=["D:/my.json"], backups=[Path("yume_config_backup_1.json")], imported={"x": 1})
    r.imports.assert_called_once_with("D:/my.json")
    assert r.cfg["x"] == 1
    r = settings([8, BACK], inputs=["D:/my.json"])  # no backups: asks for a path
    r.imports.assert_called_once_with("D:/my.json")


def test_reset_only_when_confirmed():
    r = settings([9, BACK], yns=[False])
    r.saved.assert_not_called()
    r = settings([9, BACK], yns=[True])
    r.saved.assert_called_once()
    from config import DEFAULT_CONFIG

    assert r.cfg["first_run_complete"] is True
    assert r.cfg["whisper_model"] == DEFAULT_CONFIG["whisper_model"]
    assert r.cfg["translation_backend"] == DEFAULT_CONFIG["translation_backend"]
