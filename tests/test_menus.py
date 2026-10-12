"""Interactive menus driven with scripted answers and a fake server."""

from __future__ import annotations

import json
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


# ── Translation model manager ─────────────────────────────────────────────────

import yume.installers  # noqa: E402
import yume.menus.tools as tools  # noqa: E402


def manage(choices, cfg, *, inputs=(), yns=(), ggufs=()):
    pulled, browsed, saved = [], MagicMock(), MagicMock()
    fakes = {
        "ask_arrow": MagicMock(side_effect=list(choices)),
        "ask_input": MagicMock(side_effect=list(inputs)),
        "ask_yn": MagicMock(side_effect=list(yns)),
        "header": MagicMock(),
        "pause": MagicMock(),
        "info": MagicMock(),
        "bullet": MagicMock(),
        "success": MagicMock(),
        "warn": MagicMock(),
        "find_gguf_models": lambda: list(ggufs),
        "check_ollama_models": lambda h, p: ["qwen2.5:7b"],
        "browse_hf": browsed,
    }
    with ExitStack() as stack:
        for name, fake in fakes.items():
            stack.enter_context(patch.object(tools, name, fake))
        stack.enter_context(patch.object(yume.installers, "pull_ollama_model", lambda m: pulled.append(m) or True))
        stack.enter_context(patch("config.save_config", saved))
        tools._manage_model(cfg)
    return types.SimpleNamespace(cfg=cfg, pulled=pulled, browsed=browsed, saved=saved)


def test_model_manager_rename_and_pull():
    r = manage(
        [0, 1, 4],
        {"translation_backend": "ollama", "translation_model": "qwen2.5:7b"},
        inputs=["gemma3:12b", "gemma3:12b"],
    )
    assert r.cfg["translation_model"] == "gemma3:12b"
    assert r.pulled == ["gemma3:12b"]


def test_model_manager_select_a_gguf_and_switch_to_llamacpp(tmp_path):
    a, b = tmp_path / "a.gguf", tmp_path / "qwen-7b.gguf"
    a.write_bytes(b"x")
    b.write_bytes(b"xx")
    cfg = {"translation_backend": "ollama", "translation_host": "10.0.0.2", "translation_port": 11434}
    manage([3, 1, 4], cfg, yns=[True], ggufs=[a, b])
    assert (cfg["gguf_model_path"], cfg["translation_model"]) == (str(b), "qwen-7b")
    assert (cfg["translation_backend"], cfg["translation_host"], cfg["translation_port"]) == (
        "llamacpp",
        "127.0.0.1",
        5000,
    )


def test_model_manager_without_ggufs_and_browse():
    r = manage([3, 2, -1], {"translation_backend": "llamacpp"})
    r.browsed.assert_called_once()
    r.saved.assert_not_called()


# ── HuggingFace browser ───────────────────────────────────────────────────────

import yume.menus.hf_browser as hf  # noqa: E402

GB = 1024**3
FILES = [
    {"name": "qwen-q8_0.gguf", "bytes": int(8.1 * GB), "sha256": "a"},
    {"name": "qwen-q4_k_m.gguf", "bytes": int(4.4 * GB), "sha256": "b"},
    {"name": "qwen-q2_k.gguf", "bytes": int(3.0 * GB), "sha256": "c"},
    {"name": "qwen-f16-00001-of-00002.gguf", "bytes": 8 * GB},
    {"name": "qwen-f16-00002-of-00002.gguf", "bytes": 7 * GB},
]


def browse(choices, cfg, *, yns=(), inputs=(), files=FILES, vram_mb=8192, download_ok=True):
    prompts, downloads = [], []

    def arrow(prompt, opts, default=0, **kw):
        prompts.append((prompt, opts, default))
        ch = choices.pop(0)
        return default if ch == "default" else ch

    fakes = {
        "ask_arrow": arrow,
        "ask_input": MagicMock(side_effect=list(inputs)),
        "ask_yn": MagicMock(side_effect=list(yns)),
        "header": MagicMock(),
        "pause": MagicMock(),
        "info": MagicMock(),
        "warn": MagicMock(),
        "error": MagicMock(),
        "success": MagicMock(),
        "detect_gpu": lambda: {"has_nvidia": vram_mb > 0, "vram_mb": vram_mb, "name": "GPU"},
        "hf_list_gguf": lambda repo: list(files),
        "hf_download": lambda repo, part, sha: downloads.append((repo, part, sha)) or download_ok,
    }
    with ExitStack() as stack:
        for name, fake in fakes.items():
            stack.enter_context(patch.object(hf, name, fake))
        stack.enter_context(patch("config.save_config"))
        hf.browse_hf(cfg)
    return types.SimpleNamespace(cfg=cfg, prompts=prompts, downloads=downloads)


def test_hf_recommends_the_largest_q4_that_fits_and_downloads_it():
    cfg = {"translation_backend": "llamacpp"}
    r = browse([0, "default"], cfg, yns=[True])  # repo 0, then the preselected file
    _prompt, opts, default = r.prompts[1]
    labels = [label for label, _ in opts]
    assert "★ recommended" in labels[default] and labels[default].startswith("qwen-q4_k_m.gguf")
    assert "too large" in labels[-2]  # the 15 GB f16, both parts as one entry
    assert "2 files" in labels[-2]
    assert r.downloads == [("Qwen/Qwen2.5-7B-Instruct-GGUF", "qwen-q4_k_m.gguf", "b")]
    assert cfg["gguf_model_path"].endswith("qwen-q4_k_m.gguf") and cfg["translation_model"] == "qwen-q4_k_m.gguf"


def test_hf_split_model_downloads_every_part_and_offers_llamacpp():
    cfg = {"translation_backend": "ollama"}
    browse([0, 3], cfg, yns=[True, True])  # entry 3: the f16 in two parts
    assert cfg["gguf_model_path"].endswith("qwen-f16-00001-of-00002.gguf")
    assert cfg["translation_backend"] == "llamacpp"


def test_hf_failed_part_stops_and_keeps_the_config():
    cfg = {"translation_backend": "llamacpp"}
    r = browse([0, 3], cfg, yns=[True], download_ok=False)
    assert len(r.downloads) == 1 and "gguf_model_path" not in cfg


def test_hf_other_repo_cpu_mode_and_back():
    cfg = {"translation_backend": "llamacpp"}
    n = len(hf._HF_RECOMMENDED_REPOS)
    r = browse([n, 4, n + 1], cfg, inputs=["me/my-GGUF"], vram_mb=0)  # other repo, Back from files, Back
    labels = [label for label, _ in r.prompts[1][1]]
    assert not any("fits" in lbl or "too large" in lbl for lbl in labels)  # no GPU: no fit tags
    assert "★ recommended" in labels[1]  # largest Q4_K_M; on CPU everything "fits"
    assert r.downloads == []


def test_hf_repo_without_ggufs():
    r = browse([0, len(hf._HF_RECOMMENDED_REPOS) + 1], {"translation_backend": "llamacpp"}, files=[])
    assert len(r.prompts) == 2  # back at the repo list


# ── Blacklist: menu and `blacklist` command, server up or down ───────────────


class Blacklist:
    """The server's list (`server` None = server down: config/blacklist.json in tmp)."""

    def __init__(self, tmp_path, server=None, saved_file=()):
        self.server = None if server is None else list(server)
        self.file = tmp_path / "blacklist.json"
        if saved_file:
            self.file.write_text(json.dumps(list(saved_file)), encoding="utf-8")
        self.said: list[str] = []

    def _get(self, h, p, path):
        return {"blacklist": list(self.server)} if self.server is not None else None

    def _post(self, h, p, path, body, **kw):
        assert path == "/blacklist/update"
        self.server = list(body["blacklist"])
        return {"success": True}

    def saved(self):
        return self.server if self.server is not None else json.loads(self.file.read_text(encoding="utf-8"))

    def run(self, fn, *args, choices=(), inputs=(), yns=()):
        say = lambda msg="", *a, **k: self.said.append(str(msg))  # noqa: E731
        fakes = {
            "_server_get": self._get,
            "_server_post": self._post,
            "_bl_file": lambda: self.file,
            "ask_arrow": MagicMock(side_effect=list(choices)),
            "ask_input": MagicMock(side_effect=list(inputs)),
            "ask_yn": MagicMock(side_effect=list(yns)),
            "header": MagicMock(),
            "pause": MagicMock(),
            **{n: say for n in ("info", "warn", "error", "success", "bullet")},
        }
        with ExitStack() as stack:
            for name, fake in fakes.items():
                stack.enter_context(patch.object(srv, name, fake))
            fn({"whisper_host": "127.0.0.1", "whisper_port": 5001}, *args)
        return self


def test_blacklist_command_add_remove_clear_on_the_server(tmp_path):
    b = Blacklist(tmp_path, server=["Subscribe"])
    b.run(srv.cli_blacklist, ["add", "Thank", "you"])
    assert b.saved() == ["Subscribe", "Thank you"]
    b.run(srv.cli_blacklist, ["add", "thank you"])
    assert b.saved() == ["Subscribe", "Thank you"] and any("Already blocked" in s for s in b.said)
    b.run(srv.cli_blacklist, ["rm", "SUBSCRIBE"])
    assert b.saved() == ["Thank you"]
    b.run(srv.cli_blacklist, ["clear"])
    assert b.saved() == []


def test_blacklist_command_edits_the_saved_file_when_the_server_is_down(tmp_path):
    b = Blacklist(tmp_path, saved_file=["Subscribe"])
    b.run(srv.cli_blacklist, ["add", "Thanks"])
    assert b.saved() == ["Subscribe", "Thanks"]
    b.run(srv.cli_blacklist, ["list"])
    assert any("server not running" in s for s in b.said)
    b.run(srv.cli_blacklist, ["remove", "nope"])
    assert any("Not in blacklist" in s for s in b.said)


def test_blacklist_menu_add_remove_clear(tmp_path):
    b = Blacklist(tmp_path, server=["Subscribe"])
    b.run(srv._menu_blacklist, choices=[0, 3], inputs=["Thank you"])
    assert b.saved() == ["Subscribe", "Thank you"]
    b.run(srv._menu_blacklist, choices=[1, 0, 3])  # remove the first
    assert b.saved() == ["Thank you"]
    b.run(srv._menu_blacklist, choices=[2, 3], yns=[False])
    assert b.saved() == ["Thank you"]
    b.run(srv._menu_blacklist, choices=[2, 3], yns=[True])
    assert b.saved() == []


def test_blacklist_menu_offline_and_back_from_remove(tmp_path):
    items = [f"phrase {i}" for i in range(25)]
    b = Blacklist(tmp_path, saved_file=items)
    b.run(srv._menu_blacklist, choices=[1, 20, 3])  # index 20 is "Back" (20 shown), not phrase 20
    assert b.saved() == items
    assert any("Server not running" in s for s in b.said)


def test_blacklist_menu_ignores_case_like_the_command(tmp_path):
    b = Blacklist(tmp_path, server=["Subscribe"])
    b.run(srv._menu_blacklist, choices=[0, 3], inputs=["subscribe"])  # the menu used to add it twice
    assert b.saved() == ["Subscribe"] and "Already blocked" in b.said
