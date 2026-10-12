"""yume/cli.py: the command table, `help`, and the main menu loop."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from yume import cli  # noqa: E402


def test_help_lists_every_command(capsys):
    cli.print_help()
    out = capsys.readouterr().out
    for name, cmd in cli.COMMANDS.items():
        assert any(usage.split()[0] == name for usage, _ in cmd.help), f"{name}: help names another command"
        for usage, desc in cmd.help:
            assert f"{usage:<20} {desc}" in out


@pytest.mark.parametrize("name", sorted(cli.COMMANDS))
def test_each_command_runs_its_handler_with_the_arguments(name):
    handler = MagicMock()
    cmd = cli.COMMANDS[name]
    with patch.dict(cli.COMMANDS, {name: cli.Command(handler, cmd.group, cmd.help)}):
        with patch.object(cli, "load_config", return_value={"k": 1}):
            cli.run_command(name, ["a", "b"])
    handler.assert_called_once_with({"k": 1}, ["a", "b"])


def test_aliases_and_help_flags(capsys):
    handler = MagicMock()
    with patch.dict(cli.COMMANDS, {"guide": cli.Command(handler, "Run", ())}):
        with patch.object(cli, "load_config", return_value={}):
            cli.run_command("guides", [])
    handler.assert_called_once()
    for flag in cli.HELP_FLAGS:
        cli.run_command(flag, [])
        assert "Usage:" in capsys.readouterr().out


def test_unknown_command(capsys):
    cli.run_command("nonsense", [])
    assert "Unknown: nonsense" in capsys.readouterr().out


def test_import_without_a_path_shows_its_usage():
    with patch.object(cli, "config_import") as imp, patch.object(cli, "error") as err:
        cli.COMMANDS["import"].run({}, [])
    imp.assert_not_called()
    assert "import <path>" in err.call_args[0][0]


def _menu(choices, handlers):
    """Run main_menu with scripted choices; each handler is a MagicMock."""
    menu = [(label, desc, handlers.get(i, MagicMock())) for i, (label, desc, _fn) in enumerate(cli.MAIN_MENU)]
    cfg = {"first_run_complete": True, "whisper_model": "m", "whisper_host": "h", "whisper_port": 1}
    cfg |= {"translation_host": "h", "translation_port": 2}
    with (
        patch.object(cli, "MAIN_MENU", menu),
        patch.object(cli, "load_config", return_value=cfg),
        patch.object(cli, "ask_arrow", side_effect=choices),
        patch.object(cli, "header"),
        patch.object(cli, "panel"),
        patch.object(cli, "_hardware_label", return_value=""),
        patch.object(cli, "_servers_label", return_value=""),
        patch.object(cli, "_update_notice"),
    ):
        cli.main_menu()
    return cfg


def test_menu_runs_the_chosen_entry_until_exit():
    status = MagicMock()
    _menu([1, 1, len(cli.MAIN_MENU)], {1: status})
    assert status.call_count == 2


def test_a_handler_returning_a_config_replaces_it():
    first = MagicMock(side_effect=lambda cfg, args: {**cfg, "new": True})  # e.g. Re-run Setup
    second = MagicMock()
    _menu([6, 0, len(cli.MAIN_MENU)], {6: first, 0: second})
    assert second.call_args[0][0]["new"] is True
