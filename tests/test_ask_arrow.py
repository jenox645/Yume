"""ui.ask_arrow with scripted key presses, on both the Windows and the POSIX key path."""

from __future__ import annotations

import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import yume.ui as ui  # noqa: E402

OPTS = [("One", "first"), ("Two", None), ("Three", "third")]

# Windows (msvcrt.getch) byte sequences
UP, DOWN, ENTER, ESC, CTRL_C = b"\xe0H", b"\xe0P", b"\r", b"\x1b", b"\x03"


def win(keys: list[bytes], **kw):
    stream = [bytes([b]) for k in keys for b in k]
    msvcrt = types.SimpleNamespace(getch=lambda: stream.pop(0))
    with (
        patch.dict(sys.modules, {"msvcrt": msvcrt}),
        patch.object(ui, "IS_WIN", True),
        patch.object(ui.sys.stdin, "isatty", return_value=True),
        patch.object(ui.sys, "stdout", MagicMock()),
    ):
        return ui.ask_arrow("Pick", OPTS, **kw)


def posix(keys: list[bytes], **kw):
    """POSIX: arrows are ESC [ A/B; a lone ESC has nothing after it."""
    stream = [bytes([b]) for k in keys for b in k]
    termios = types.SimpleNamespace(tcgetattr=lambda fd: "cooked", tcsetattr=MagicMock(), TCSANOW=0)
    tty = types.SimpleNamespace(setraw=MagicMock())
    with (
        patch.dict(sys.modules, {"termios": termios, "tty": tty}),
        patch.object(ui, "IS_WIN", False),
        patch.object(ui.sys.stdin, "isatty", return_value=True),
        patch.object(ui.sys.stdin, "fileno", return_value=0),
        patch.object(ui.sys, "stdout", MagicMock()),
        patch.object(ui.os, "read", lambda fd, n: stream.pop(0)),
        patch.object(ui, "_read_byte_nonblock", lambda fd: stream.pop(0) if stream and stream[0] == b"[" else b""),
    ):
        result = ui.ask_arrow("Pick", OPTS, **kw)
    termios.tcsetattr.assert_called_with(0, 0, "cooked")  # the terminal is left in cooked mode
    return result


@pytest.mark.parametrize(
    ("keys", "expected"),
    [
        ([ENTER], 0),
        ([DOWN, DOWN, ENTER], 2),
        ([UP, ENTER], 2),  # wraps around
        ([DOWN, DOWN, DOWN, ENTER], 0),
        ([b"3", ENTER], 2),
        ([b"9", ENTER], 0),  # no 9th option: ignored
        ([ESC], -1),
    ],
)
def test_windows_keys(keys, expected):
    assert win(keys) == expected


def test_windows_esc_does_nothing_without_back():
    assert win([DOWN, ESC, ENTER], allow_back=False) == 1


def test_windows_ctrl_c_interrupts():
    with pytest.raises(KeyboardInterrupt):
        win([CTRL_C])


def test_default_is_preselected_and_clamped():
    assert win([ENTER], default=1) == 1
    assert win([ENTER], default=99) == 2


@pytest.mark.parametrize(
    ("keys", "expected"),
    [
        ([b"\r"], 0),
        ([b"\x1b[B", b"\x1b[B", b"\n"], 2),
        ([b"\x1b[A", b"\r"], 2),
        ([b"2", b"\r"], 1),
        ([b"\x1b"], -1),
    ],
)
def test_posix_keys(keys, expected):
    assert posix(keys) == expected


def test_posix_esc_does_nothing_without_back():
    assert posix([b"\x1b", b"\x1b[B", b"\r"], allow_back=False) == 1


@pytest.mark.parametrize("key", [b"\x03", b"\x04"])
def test_posix_ctrl_c_and_ctrl_d_interrupt(key):
    with pytest.raises(KeyboardInterrupt):
        posix([key])


def test_not_a_terminal_falls_back_to_numbered_input():
    with patch.object(ui.sys.stdin, "isatty", return_value=False), patch.object(ui, "ask_choice", return_value=1) as ac:
        assert ui.ask_arrow("Pick", OPTS) == 1
    ac.assert_called_once_with("Pick", OPTS, 0, True)


def test_no_options():
    assert ui.ask_arrow("Pick", []) == -1


def test_the_selected_description_is_shown():
    out = []
    stdout = MagicMock(write=out.append)
    msvcrt = types.SimpleNamespace(getch=lambda: b"\r")
    with (
        patch.dict(sys.modules, {"msvcrt": msvcrt}),
        patch.object(ui, "IS_WIN", True),
        patch.object(ui.sys.stdin, "isatty", return_value=True),
        patch.object(ui.sys, "stdout", stdout),
    ):
        ui.ask_arrow("Pick", OPTS, default=2)
    text = "".join(out)
    assert "third" in text and "first" not in text
    assert "Esc back" in text
