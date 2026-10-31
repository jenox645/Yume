"""What the server and the CLI keep on disk survives a bad file, a crash and two writers."""

from __future__ import annotations

import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

import _settings  # noqa: E402
import _state  # noqa: E402
import config  # noqa: E402
from yume import cli  # noqa: E402


@pytest.fixture
def state(monkeypatch):
    for name in ("model_name", "model_display_name", "device", "compute_type", "config_file", "config_mtime"):
        monkeypatch.setattr(_state, name, getattr(_state, name))
    for name in ("translation_host", "translation_port", "blacklist_file", "user_blacklist", "jobs"):
        monkeypatch.setattr(_state, name, getattr(_state, name))
    monkeypatch.setattr(_state, "jobs", None)
    return _state


def _args(path):
    return SimpleNamespace(model="base", device="cpu", compute_type="int8", config=str(path), port=5001)


@pytest.mark.parametrize("text", ["{broken", "[1, 2]", "5"])
def test_a_broken_config_does_not_stop_the_server(tmp_path, state, text, capsys):
    path = tmp_path / "yume_config.json"
    path.write_text(text, encoding="utf-8")
    args = _args(path)
    _settings.apply_config(args)
    assert state.model_name == "base"
    assert args.port == 5001
    assert "ignored" in capsys.readouterr().out


def test_fixing_a_broken_config_reaches_the_running_server(tmp_path, state):
    path = tmp_path / "yume_config.json"
    path.write_text("{broken", encoding="utf-8")
    _settings.apply_config(_args(path))
    path.write_text(json.dumps({"translation_port": 5050}), encoding="utf-8")
    state.config_mtime = 0
    _settings.reload_config()
    assert state.translation_port == 5050


@pytest.mark.parametrize("text", ["5", "[1, 2]", '"text"'])
def test_a_config_turned_into_something_else_is_ignored_while_running(tmp_path, state, text):
    path = tmp_path / "yume_config.json"
    path.write_text(text, encoding="utf-8")
    state.config_file, state.config_mtime = str(path), 0
    state.translation_port = 5000
    _settings.reload_config()
    assert state.translation_port == 5000


@pytest.mark.parametrize("value", ["abc", 0, 70000, None])
def test_a_bad_port_keeps_the_one_in_use(tmp_path, state, value):
    path = tmp_path / "yume_config.json"
    path.write_text(json.dumps({"whisper_port": value, "translation_port": value}), encoding="utf-8")
    state.translation_port = 5000
    args = _args(path)
    _settings.apply_config(args)
    assert args.port == 5001
    assert state.translation_port == 5000


def test_a_port_written_as_text_works(tmp_path, state):
    path = tmp_path / "yume_config.json"
    path.write_text(json.dumps({"whisper_port": "5002", "translation_port": "5003"}), encoding="utf-8")
    args = _args(path)
    _settings.apply_config(args)
    assert (args.port, state.translation_port) == (5002, 5003)


def test_lines_reported_at_the_same_moment_all_stay(tmp_path, state):
    state.blacklist_file = str(tmp_path / "blacklist.json")
    state.user_blacklist = []
    start = threading.Barrier(16)

    def report(n):
        start.wait()
        _settings.change_blacklist(lambda current: [*current, f"line {n}"])

    threads = [threading.Thread(target=report, args=(n,)) for n in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(state.user_blacklist) == 16
    assert sorted(json.loads(Path(state.blacklist_file).read_text(encoding="utf-8"))) == sorted(state.user_blacklist)


def test_a_failed_blacklist_write_keeps_the_saved_list(tmp_path, state):
    path = tmp_path / "blacklist.json"
    path.write_text('["kept"]', encoding="utf-8")
    state.blacklist_file = str(path)
    with patch("_settings.json.dump", side_effect=OSError("disk full")):
        _settings.save_blacklist(["new"])
    assert json.loads(path.read_text(encoding="utf-8")) == ["kept"]


def test_a_config_save_killed_halfway_keeps_the_old_file(tmp_path, monkeypatch):
    path = tmp_path / "yume_config.json"
    path.write_text('{"whisper_port": 5001}', encoding="utf-8")
    monkeypatch.setattr(config, "CONFIG_FILE", path)
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path)

    def half_a_file(_cfg, f, **_kwargs):
        f.write('{"whisper_po')
        raise KeyboardInterrupt

    with patch("config.json.dump", side_effect=half_a_file), pytest.raises(KeyboardInterrupt):
        config.save_config({"whisper_port": 5002})
    assert json.loads(path.read_text(encoding="utf-8")) == {"whisper_port": 5001}


def test_health_says_how_it_went_in_the_exit_code():
    with patch("yume.health.health_check", return_value=False), pytest.raises(SystemExit) as e:
        cli.COMMANDS["health"].run({}, [])
    assert e.value.code == 1
    with patch("yume.health.health_check", return_value=True):
        cli.COMMANDS["health"].run({}, [])


def test_the_menu_entry_for_health_never_quits_the_menu():
    with patch("yume.health.health_check", return_value=False):
        cli._health({}, [])


@pytest.mark.parametrize("body", [[1, 2], 5, "text", None])
def test_a_json_body_that_is_no_object_counts_as_empty(body):
    import _api

    with patch.object(_api, "request", SimpleNamespace(get_json=lambda silent: body)):
        assert _api._json_body() == {}


def test_a_model_name_that_is_no_text_is_a_bad_request():
    import _api

    assert _api._requested_model(5) == (None, {"error": "Missing 'model' field"})


def test_both_in_an_error_is_no_bot_check():
    import _download

    assert not any(w in "both formats failed" for w in _download._AUTH_ERROR_WORDS)
    assert any(w in "confirm you're not a bot" for w in _download._AUTH_ERROR_WORDS)
