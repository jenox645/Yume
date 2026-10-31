"""Terminal UI helpers — colours, panels, tables, spinners, input prompts."""

from __future__ import annotations

import os
import re
import shutil
import sys
import unicodedata

from yume.constants import IS_WIN


# ── Colour codes ──────────────────────────────────────────────────────────────


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
    WHITE = "\033[97m"
    GOLD = "\033[38;5;220m"
    PURPLE = "\033[38;5;141m"

    @classmethod
    def disable(cls):
        """Strip all ANSI codes — plain text fallback."""
        cls.RESET = cls.BOLD = cls.DIM = ""
        cls.RED = cls.GREEN = cls.YELLOW = ""
        cls.BLUE = cls.MAGENTA = cls.CYAN = ""
        cls.WHITE = cls.GOLD = cls.PURPLE = ""


# Pre-compiled regex for stripping ANSI escape codes (used in panel/table rendering)
ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*m")

# ── Box drawing ───────────────────────────────────────────────────────────────

BOX_CHARS = {
    "rounded": {"tl": "╭", "tr": "╮", "bl": "╰", "br": "╯", "h": "─", "v": "│"},
    "heavy": {"tl": "┏", "tr": "┓", "bl": "┗", "br": "┛", "h": "━", "v": "┃"},
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


def panel(text: str, title: str = "", style: str = "", width: int | None = None, pad: int = 1) -> None:
    """Draw a rounded panel/box around text."""
    b = _safe_box()
    w = width or min(tw() - 2, 80)
    inner = w - 2 - (pad * 2)
    lines = []
    for raw_line in text.split("\n"):
        while len(raw_line) > inner:
            lines.append(raw_line[:inner])
            raw_line = raw_line[inner:]
        lines.append(raw_line)

    if title:
        t = f" {title} "
        t_visible = len(ANSI_ESCAPE_RE.sub("", t))
        top = f"{b['tl']}{t}{b['h'] * max(0, w - 2 - t_visible)}{b['tr']}"
    else:
        top = f"{b['tl']}{b['h'] * (w - 2)}{b['tr']}"
    bot = f"{b['bl']}{b['h'] * (w - 2)}{b['br']}"

    color = style or C.DIM
    print(f"  {color}{top}{C.RESET}")
    for line in lines:
        clean = ANSI_ESCAPE_RE.sub("", line)
        padding = inner - len(clean)
        print(f"  {color}{b['v']}{C.RESET}{' ' * pad}{line}{' ' * max(0, padding)}{' ' * pad}{color}{b['v']}{C.RESET}")
    print(f"  {color}{bot}{C.RESET}")


def _width(text) -> int:
    """Columns a cell takes on screen: no ANSI codes, CJK characters count two."""
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in ANSI_ESCAPE_RE.sub("", str(text)))


def table(headers: list, rows: list, col_styles: list | None = None, title: str = "") -> None:
    """Render a formatted table."""
    col_styles = col_styles or [C.RESET] * len(headers)
    widths = [_width(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            if i < len(widths):
                widths[i] = max(widths[i], _width(cell))

    # Too wide (a long path in the last column): only the last column gives way.
    # Cells are never cut, so shrinking every column pushed each row's later
    # cells out of line ("Translation server responding   PASS").
    max_w = tw() - 6
    total = sum(widths) + (len(widths) - 1) * 3
    if total > max_w:
        widths[-1] = max(4, widths[-1] - (total - max_w))

    def _row(cells, styles=None):
        parts = []
        for i, cell in enumerate(cells):
            s = styles[i] if styles and i < len(styles) else C.RESET
            last = i == len(cells) - 1
            pad = 0 if last or i >= len(widths) else widths[i] - _width(cell)
            parts.append(f"{s}{cell}{C.RESET}{' ' * max(0, pad)}")
        return "   ".join(parts)

    sep = f"  {C.DIM}{'─' * (sum(widths) + (len(widths) - 1) * 3)}{C.RESET}"
    if title:
        print(f"\n  {C.BOLD}{title}{C.RESET}")
    print(sep)
    print(f"  {_row(headers, [C.BOLD + C.DIM] * len(headers))}")
    print(sep)
    for row in rows:
        print(f"  {_row(row, col_styles)}")
    print(sep)


# ── Spinner ───────────────────────────────────────────────────────────────────

SPINNERS = {
    "dots": ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"],
    "line": ["-", "\\", "|", "/"],
    "arrows": ["←", "↖", "↑", "↗", "→", "↘", "↓", "↙"],
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


class Spinner:
    """A one-line spinner driven from outside: tick(elapsed) while waiting, then
    finish(). Used as the progress callback of yume.servers.wait_ready."""

    def __init__(self, message: str):
        self.message = message
        self.frames = _pick_spinner()
        self.i = 0
        self.elapsed = 0

    def _line(self, mark: str, note: str) -> None:
        sys.stdout.write(f"\r  {mark}  {self.message} {C.DIM}({note}){C.RESET}   ")
        sys.stdout.flush()

    def tick(self, elapsed: float = 0) -> None:
        self.elapsed = int(elapsed)
        self._line(f"{C.CYAN}{self.frames[self.i % len(self.frames)]}{C.RESET}", f"{self.elapsed}s")
        self.i += 1

    def finish(self, outcome: str) -> None:
        """outcome: "ok", "timeout", "failed" or "cancelled"."""
        marks = {"ok": f"{C.GREEN}✓", "timeout": f"{C.YELLOW}⏱", "failed": f"{C.RED}✗", "cancelled": f"{C.RED}✗"}
        notes = {"timeout": f"timed out after {self.elapsed}s", "cancelled": "cancelled"}
        self._line(f"{marks[outcome]}{C.RESET}", notes.get(outcome, f"{self.elapsed}s"))
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
            import ctypes as _ctypes

            _ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            _ctypes.windll.kernel32.SetConsoleCP(65001)
        except Exception:
            import subprocess

            subprocess.run(["cmd", "/c", "chcp", "65001"], capture_output=True)  # nosec B603


def clear() -> None:
    import subprocess

    if IS_WIN:
        subprocess.run(["cmd", "/c", "cls"], check=False)  # nosec B603 B607
    else:
        sys.stdout.write("\033[2J\033[H")  # ANSI clear + home — no shell, no `clear` binary needed
        sys.stdout.flush()


def tw() -> int:
    return shutil.get_terminal_size((80, 24)).columns


def center(t: str) -> str:
    return t.center(tw())


def gold_hr() -> None:
    print(f"{C.GOLD}{'─' * tw()}{C.RESET}")


def header(sub: str | None = None, version: str = "") -> None:
    clear()
    print()
    # The product is YUME — the logo spells the product, not the launcher.
    try:
        logo = [
            f"{C.GOLD}╦ ╦  ╦ ╦  ╔╦╗  ╔═╗{C.RESET}",
            f"{C.GOLD}╚╦╝  ║ ║  ║║║  ║╣ {C.RESET}",
            f"{C.GOLD} ╩   ╚═╝  ╩ ╩  ╚═╝{C.RESET}",
        ]
        for line in logo:
            print(center(line))
    except UnicodeEncodeError:
        print(center(f"{C.GOLD}{C.BOLD}Y  U  M  E{C.RESET}"))
    print(center(f"{C.PURPLE}YUME-chan · You'll Understand More Easily{C.RESET}"))
    tag = "Pocket Yume CLI" + (f" · v{version}" if version else "")
    print(center(f"{C.DIM}{tag}{C.RESET}"))
    if sub:
        print(center(f"{C.BOLD}{sub}{C.RESET}"))
    print()


def section(t: str) -> None:
    w = max(1, tw() - len(t) - 8)
    print(f"\n  {C.GOLD}─── {C.BOLD}{t} {C.GOLD}{'─' * w}{C.RESET}\n")


def info(m: str) -> None:
    print(f"  {C.CYAN}i{C.RESET}  {m}")


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


def _render_arrow_menu(prompt: str, options: list, sel: int, allow_back: bool) -> int:
    """Draw the menu; return the number of lines written (to rewind the cursor)."""
    # Every option gets a description line (blank unless selected): constant height
    has_any_desc = any(desc for _, desc in options)
    out = [f"\n  {C.GOLD}?{C.RESET}  {prompt}\n\n"]
    for i, (label, desc) in enumerate(options):
        num = f"{C.DIM}{i + 1}.{C.RESET} " if i < 9 else "   "
        if i == sel:
            out.append(f"  {C.GREEN}▶{C.RESET} {num}{C.BOLD}{label}{C.RESET}\n")
        else:
            out.append(f"    {num}{label}\n")
        if has_any_desc:
            out.append(f"      {C.DIM}{desc}{C.RESET}\n" if i == sel and desc else "\n")
    hint = "↑↓ navigate  1-9 jump  Enter select" + ("  Esc back" if allow_back else "")
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
