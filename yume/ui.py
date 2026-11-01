"""Terminal UI helpers — colours, panels, tables, spinners, input prompts."""

from __future__ import annotations

import os
import re
import shutil
import sys
import unicodedata

from yume.constants import IS_WIN


# ── Colour codes ──────────────────────────────────────────────────────────────


def _tone(rgb: tuple, c256: int, c16: int) -> str:
    """One colour at the depth this terminal has. Mid tones: readable on dark and on light."""
    if os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit") or os.environ.get("WT_SESSION"):
        return "\033[38;2;{};{};{}m".format(*rgb)
    if "256color" in os.environ.get("TERM", ""):
        return f"\033[38;5;{c256}m"
    return f"\033[{c16}m"


class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    BLUE = "\033[94m"
    MAGENTA = "\033[95m"
    CYAN = "\033[96m"
    GOLD = _tone((209, 73, 91), 167, 91)  # YUME-chan's red: prompts, the seal
    PURPLE = _tone((91, 141, 239), 69, 94)  # her cobalt

    @classmethod
    def disable(cls):
        """Strip all ANSI codes — plain text fallback."""
        cls.RESET = cls.BOLD = cls.DIM = ""
        cls.RED = cls.GREEN = cls.YELLOW = ""
        cls.BLUE = cls.MAGENTA = cls.CYAN = ""
        cls.GOLD = cls.PURPLE = ""


# Pre-compiled regex for stripping ANSI escape codes (used in panel/table rendering)
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")

# ── Box drawing ───────────────────────────────────────────────────────────────

BOX_CHARS = {
    "rounded": {"tl": "╭", "tr": "╮", "bl": "╰", "br": "╯", "h": "─", "v": "│"},
    "simple": {"tl": "+", "tr": "+", "bl": "+", "br": "+", "h": "-", "v": "|"},
}


def _safe_box():
    """Pick box chars that work on the current terminal."""
    if IS_WIN:
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
            mode = ctypes.c_ulong()
            kernel32.GetConsoleMode(handle, ctypes.byref(mode))
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
            return BOX_CHARS["rounded"]
        except Exception:
            return BOX_CHARS["simple"]
    return BOX_CHARS["rounded"]


def panel(text: str, title: str = "", style: str = "") -> None:
    """A titled block of lines: text aligned on the page, no box around it."""
    if title:
        print(f"  {C.BOLD}{title}{C.RESET}")
    for line in text.split("\n"):
        print(f"  {line}{C.RESET}")


def _width(text) -> int:
    """Columns a cell takes on screen: no ANSI codes, CJK characters count two."""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in ANSI_ESCAPE_RE.sub("", str(text)))


def table(headers: list, rows: list, col_styles: list | None = None, title: str = "") -> None:
    """Rows in aligned columns, headed by a title. No borders: the alignment is the table."""
    widths = [max([_width(str(h))] + [_width(str(r[i])) for r in rows]) for i, h in enumerate(headers)]
    print()
    if title:
        print(f"  {C.BOLD}{title}{C.RESET}")
    for row in rows:
        cells = []
        for i, cell in enumerate(row):
            style = col_styles[i] if col_styles and i < len(col_styles) else ""
            cells.append(f"{style}{cell}{C.RESET}{' ' * (widths[i] - _width(str(cell)))}")
        print("  " + "  ".join(cells).rstrip())


# ── Spinner ───────────────────────────────────────────────────────────────────

SPINNERS = {
    "dots": ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"],
    "simple": ["-", "\\", "|", "/"],
}


def _pick_spinner() -> list:
    if IS_WIN:
        try:
            "⠋".encode(sys.stdout.encoding or "utf-8")
            return SPINNERS["dots"]
        except (UnicodeEncodeError, LookupError):
            return SPINNERS["simple"]
    return SPINNERS["dots"]


BAR_CELLS = 20


class Spinner:
    """One line with a progress bar, driven from outside: tick(elapsed) while
    waiting, then finish(). A server gives no percentage, so the bar follows the
    time it usually takes and stops short of full until it is really ready."""

    def __init__(self, name: str, doing: str = "", expected: float = 30):
        self.name = name
        self.doing = doing
        self.expected = expected
        self.elapsed = 0

    def _line(self, filled: int, note: str, tone: str = "") -> None:
        bar = "#" * filled + "-" * (BAR_CELLS - filled)
        sys.stdout.write(f"\r  {self.name:<11} {C.PURPLE}[{bar}]{C.RESET}  {tone}{note}{C.RESET}\033[K")
        sys.stdout.flush()

    def tick(self, elapsed: float = 0) -> None:
        self.elapsed = int(elapsed)
        filled = min(BAR_CELLS - 1, int(BAR_CELLS * elapsed / self.expected))
        self._line(filled, f"{self.doing} {self.elapsed} s".strip(), C.DIM)

    def finish(self, outcome: str) -> None:
        """outcome: "ok", "timeout", "failed" or "cancelled"."""
        if outcome == "ok":
            self._line(BAR_CELLS, f"ready in {self.elapsed} s", C.GREEN)
        else:
            notes = {"timeout": f"still going after {self.elapsed} s", "failed": "failed", "cancelled": "cancelled"}
            tones = {"timeout": C.YELLOW, "failed": C.RED, "cancelled": C.DIM}
            self._line(
                min(BAR_CELLS - 1, int(BAR_CELLS * self.elapsed / self.expected)), notes[outcome], tones[outcome]
            )
        sys.stdout.write("\n")
        sys.stdout.flush()


# ── Console helpers ────────────────────────────────────────────────────────────


def enable_ansi() -> None:
    """Enable ANSI escape sequences on Windows. Disable colours on failure."""
    if IS_WIN:
        ansi_ok = False
        try:
            import ctypes

            k32 = ctypes.windll.kernel32
            h = k32.GetStdHandle(-11)
            mode = ctypes.c_ulong()
            if k32.GetConsoleMode(h, ctypes.byref(mode)):
                if k32.SetConsoleMode(h, mode.value | 0x0004):
                    ansi_ok = True
        except Exception:
            pass

        if not ansi_ok:
            C.disable()

        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:
            from yume.utils import _run

            _run(["cmd", "/c", "chcp", "65001"])


def clear() -> None:
    if IS_WIN:
        from yume.utils import _run

        _run(["cmd", "/c", "cls"], capture_output=False)
    else:
        sys.stdout.write("\033[2J\033[H")  # ANSI clear + home — no shell, no `clear` binary needed
        sys.stdout.flush()


def tw() -> int:
    return shutil.get_terminal_size((80, 24)).columns


def center(t: str) -> str:
    return t.center(tw())


def header(sub: str | None = None, version: str = "") -> None:
    clear()
    print()
    name = f"{C.BOLD}Yume{C.RESET}" + (f" {C.BOLD}{version}{C.RESET}" if version else "")
    where = f"  {C.DIM}{sub}{C.RESET}" if sub else ""
    try:
        print(f"  {C.GOLD}\033[7m 夢 {C.RESET} {name}{where}")
    except UnicodeEncodeError:  # a legacy console code page
        print(f"  {name}{where}")
    print()


def section(t: str) -> None:
    print(f"\n  {C.BOLD}{t}{C.RESET}")


def info(m: str) -> None:
    print(f"  {m}")


def success(m: str) -> None:
    print(f"  {C.GREEN}+{C.RESET}  {m}")


def warn(m: str) -> None:
    print(f"  {C.YELLOW}!{C.RESET}  {C.YELLOW}{m}{C.RESET}")


def error(m: str) -> None:
    print(f"  {C.RED}x{C.RESET}  {C.RED}{m}{C.RESET}")


def bullet(m: str, indent: int = 2) -> None:
    print(f"{' ' * indent}{C.DIM}-{C.RESET} {m}")


def pause(m: str = "Press Enter to continue...") -> None:
    try:
        input(f"\n  {C.DIM}{m}{C.RESET}")
    except (EOFError, KeyboardInterrupt):
        pass


def ask_yn(prompt: str, default: bool = True) -> bool:
    h = "[Y/n]" if default else "[y/N]"
    while True:
        try:
            r = input(f"  {C.GOLD}?{C.RESET}  {prompt} {C.DIM}{h}{C.RESET} ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return default
        if r == "":
            return default
        if r in ("y", "yes"):
            return True
        if r in ("n", "no"):
            return False


def ask_input(prompt: str, default: str = "") -> str:
    h = f" [{default}]" if default else ""
    try:
        r = input(f"  {C.GOLD}?{C.RESET}  {prompt}{C.DIM}{h}{C.RESET}: ").strip()
        return r if r else default
    except (EOFError, KeyboardInterrupt):
        print()
        return default


def _show_choices(prompt: str, options: list, default: int) -> None:
    print(f"\n  {C.GOLD}?{C.RESET}  {prompt}\n")
    for i, (label, desc) in enumerate(options):
        mk = f"{C.GREEN}>{C.RESET}" if i == default else " "
        print(f"  {mk} {C.BOLD}{i + 1}.{C.RESET} {label}")
        if desc:
            print(f"      {C.DIM}{desc}{C.RESET}")
    print()


def _typed_choice(reply: str, count: int, default: int, allow_back: bool) -> int | None:
    """The option a typed reply picks (-1 = back); None = ask again."""
    if reply == "":
        return default
    if reply == "b" and allow_back:
        return -1
    try:
        c = int(reply) - 1
    except ValueError:
        return None
    return c if 0 <= c < count else None


def ask_choice(prompt: str, options: list, default: int = 0, allow_back: bool = True) -> int:
    _show_choices(prompt, options, default)
    bh = ", b=back" if allow_back else ""
    while True:
        try:
            r = input(f"  {C.DIM}[1-{len(options)}{bh}, default={default + 1}]: {C.RESET}").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return -1 if allow_back else default
        c = _typed_choice(r, len(options), default, allow_back)
        if c is not None:
            return c


def _read_byte_nonblock(fd: int) -> bytes:
    """Read one byte from fd without blocking (Unix only). Returns b'' on timeout."""
    import fcntl

    old_fl = fcntl.fcntl(fd, fcntl.F_GETFL)
    fcntl.fcntl(fd, fcntl.F_SETFL, old_fl | os.O_NONBLOCK)
    try:
        return os.read(fd, 1)
    except (BlockingIOError, InterruptedError, OSError):
        return b""
    finally:
        fcntl.fcntl(fd, fcntl.F_SETFL, old_fl)


def _windows_keys(msvcrt):
    """Key names from the Windows console: up, down, enter, esc, interrupt, or the character."""
    while True:
        ch = msvcrt.getch()
        if ch == b"\xe0":  # arrow keys come as two bytes
            yield {b"H": "up", b"P": "down"}.get(msvcrt.getch(), "")
        else:
            yield {b"\r": "enter", b"\x1b": "esc", b"\x03": "interrupt"}.get(ch) or ch.decode(errors="ignore")


def _posix_keys(termios, tty, fd: int):
    """Key names from a POSIX terminal. Raw mode only while reading a key, so
    the menu is drawn in the normal (cooked) mode."""
    cooked = termios.tcgetattr(fd)
    while True:
        tty.setraw(fd)
        try:
            b = os.read(fd, 1)
            if b == b"\x1b":  # arrows are ESC [ A/B; a lone ESC has nothing after it
                key = {b"A": "up", b"B": "down"}.get(os.read(fd, 1), "") if _read_byte_nonblock(fd) == b"[" else "esc"
            else:
                key = {b"\r": "enter", b"\n": "enter", b"\x03": "interrupt", b"\x04": "interrupt"}.get(b)
                key = key or b.decode(errors="ignore")
        finally:
            termios.tcsetattr(fd, termios.TCSANOW, cooked)
        yield key


def _fit(text: str, room: int) -> str:
    """Text cut to `room` columns: a wrapped row would break the menu's redraw."""
    if _width(text) <= room:
        return text
    out = ""
    for ch in text:
        if _width(out + ch) > room - 3:
            break
        out += ch
    return out.rstrip() + "..." if room > 3 else ""


def _render_arrow_menu(prompt: str, options: list, sel: int, allow_back: bool) -> int:
    """Draw the menu; return the number of lines written (to rewind the cursor)."""
    labels = max(_width(label) for label, _ in options)
    out = [f"\n  {C.DIM}{prompt}{C.RESET}\n\n" if prompt else ""]
    for i, (label, desc) in enumerate(options):
        mark = ">" if i == sel else " "
        key = str(i + 1) if i < 9 else " "
        name = f"{C.BOLD}{label}{C.RESET}" if i == sel else label
        gap = " " * (labels - _width(label) + 2)
        hint = _fit(desc or "", tw() - labels - 10)
        out.append(f"  {C.GOLD}{mark} {key}{C.RESET}  {name}{gap}{C.DIM}{hint}{C.RESET}\n")
    hint = "Arrows move  1-9 jump  Enter selects" + ("  Esc back" if allow_back else "")
    out.append(f"\n  {C.DIM}{hint}{C.RESET}")
    text = "".join(out)
    sys.stdout.write(text)
    sys.stdout.flush()
    return text.count("\n")


def _move(sel: int, key: str, count: int) -> int:
    """The selection after a key: arrows wrap around, 1-9 jump to an option."""
    if key == "up":
        return (sel - 1) % count
    if key == "down":
        return (sel + 1) % count
    if key.isdigit() and 1 <= int(key) <= count:
        return int(key) - 1
    return sel


def _key_stream():
    """Key names from this console. Imports here: without them, the caller falls
    back before anything is drawn."""
    if IS_WIN:
        import msvcrt  # type: ignore[import]

        return _windows_keys(msvcrt)
    import termios
    import tty

    return _posix_keys(termios, tty, sys.stdin.fileno())


def _end_line() -> None:
    sys.stdout.write("\n")
    sys.stdout.flush()


def _arrow_menu(prompt: str, options: list, sel: int, allow_back: bool, keys) -> int:
    """Draw the menu and follow the keys until one ends it."""
    n = _render_arrow_menu(prompt, options, sel, allow_back)
    for key in keys:
        if key == "interrupt":
            raise KeyboardInterrupt
        # Esc never activates the highlighted item: without back it does nothing
        if key == "enter" or (key == "esc" and allow_back):
            _end_line()
            return sel if key == "enter" else -1
        new = _move(sel, key, len(options))
        if new != sel:
            sel = new
            sys.stdout.write(f"\r\033[{n}A\033[J")  # rewind and clear the menu
            n = _render_arrow_menu(prompt, options, sel, allow_back)
    return -1


def ask_arrow(prompt: str, options: list, default: int = 0, allow_back: bool = True) -> int:
    """Arrow-key + number-key navigated selection menu.

    ↑↓ navigate, 1-9 jump to item, Enter select, Esc go back (-1).
    Falls back to ask_choice() when stdin is not a TTY or raw mode is unavailable.
    """
    if not options:
        return -1
    if not sys.stdin.isatty():  # piped input, CI
        return ask_choice(prompt, options, default, allow_back)

    sel = max(0, min(default, len(options) - 1))
    try:
        return _arrow_menu(prompt, options, sel, allow_back, _key_stream())
    except KeyboardInterrupt:
        _end_line()
        raise
    except Exception:  # no msvcrt / termios, or the terminal refused raw mode
        return ask_choice(prompt, options, default, allow_back)
