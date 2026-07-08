"""Terminal UI helpers — colours, panels, tables, spinners, input prompts."""

from __future__ import annotations

import os
import platform
import re
import shutil
import sys
import time

PLAT = platform.system()
IS_WIN = PLAT == "Windows"


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


def table(headers: list, rows: list, col_styles: list | None = None, title: str = "") -> None:
    """Render a formatted table."""
    col_styles = col_styles or [C.RESET] * len(headers)
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            if i < len(widths):
                clean = ANSI_ESCAPE_RE.sub("", str(cell))
                widths[i] = max(widths[i], len(clean))

    max_w = tw() - 6
    total = sum(widths) + (len(widths) - 1) * 3
    if total > max_w:
        scale = max_w / total
        widths = [max(4, int(w * scale)) for w in widths]

    def _row(cells, styles=None):
        parts = []
        for i, cell in enumerate(cells):
            s = styles[i] if styles and i < len(styles) else C.RESET
            clean = ANSI_ESCAPE_RE.sub("", str(cell))
            pad = widths[i] - len(clean) if i < len(widths) else 0
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


def spin_wait(check_fn, message: str, timeout: int = 180, interval: float = 0.5) -> bool:
    """Spinner until check_fn() returns True."""
    frames = _pick_spinner()
    start = time.time()
    i = 0
    try:
        while time.time() - start < timeout:
            frame = frames[i % len(frames)]
            elapsed = int(time.time() - start)
            sys.stdout.write(f"\r  {C.CYAN}{frame}{C.RESET}  {message} {C.DIM}({elapsed}s){C.RESET}   ")
            sys.stdout.flush()
            if check_fn():
                sys.stdout.write(f"\r  {C.GREEN}✓{C.RESET}  {message} {C.DIM}({elapsed}s){C.RESET}   \n")
                sys.stdout.flush()
                return True
            time.sleep(interval)
            i += 1
        sys.stdout.write(f"\r  {C.YELLOW}⏱{C.RESET}  {message} {C.DIM}(timed out after {timeout}s){C.RESET}   \n")
        sys.stdout.flush()
        return False
    except KeyboardInterrupt:
        sys.stdout.write(f"\r  {C.RED}✗{C.RESET}  {message} {C.DIM}(cancelled){C.RESET}   \n")
        sys.stdout.flush()
        raise


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
    os.system("cls" if IS_WIN else "clear")


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
    print(center(f"{C.PURPLE}You Understand More Easily{C.RESET}"))
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


def ask_choice(prompt: str, options: list, default: int = 0, allow_back: bool = True) -> int:
    print(f"\n  {C.GOLD}?{C.RESET}  {prompt}\n")
    for i, (label, desc) in enumerate(options):
        mk = f"{C.GREEN}>{C.RESET}" if i == default else " "
        print(f"  {mk} {C.BOLD}{i + 1}.{C.RESET} {label}")
        if desc:
            print(f"      {C.DIM}{desc}{C.RESET}")
    bh = ", b=back" if allow_back else ""
    print()
    while True:
        try:
            r = input(f"  {C.DIM}[1-{len(options)}{bh}, default={default + 1}]: {C.RESET}").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return -1 if allow_back else default
        if r == "":
            return default
        if r == "b" and allow_back:
            return -1
        try:
            c = int(r) - 1
            if 0 <= c < len(options):
                return c
        except ValueError:
            pass


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


def ask_arrow(prompt: str, options: list, default: int = 0, allow_back: bool = True) -> int:
    """Arrow-key + number-key navigated selection menu.

    ↑↓ navigate, 1-9 jump to item, Enter select, Esc go back (-1).
    Falls back to ask_choice() when stdin is not a TTY or raw mode is unavailable.
    """
    if not options:
        return -1

    # Non-TTY fallback (piped input, CI, etc.)
    if not sys.stdin.isatty():
        return ask_choice(prompt, options, default, allow_back)

    sel = max(0, min(default, len(options) - 1))
    # Whether any option has a description — used to maintain constant menu height.
    has_any_desc = any(desc for _, desc in options)

    def _render() -> int:
        """Render menu to stdout; return number of \\n chars written (for cursor rewind)."""
        out = []
        out.append(f"\n  {C.GOLD}?{C.RESET}  {prompt}\n\n")
        for i, (label, desc) in enumerate(options):
            num = f"{C.DIM}{i + 1}.{C.RESET} " if i < 9 else "   "
            if i == sel:
                out.append(f"  {C.GREEN}\u25b6{C.RESET} {num}{C.BOLD}{label}{C.RESET}\n")
            else:
                out.append(f"    {num}{label}\n")
            # Always allocate a desc line to keep height constant; blank for non-selected.
            if has_any_desc:
                if i == sel and desc:
                    out.append(f"      {C.DIM}{desc}{C.RESET}\n")
                else:
                    out.append("\n")
        hint = "\u2191\u2193 navigate  1-9 jump  Enter select"
        if allow_back:
            hint += "  Esc back"
        out.append(f"\n  {C.DIM}{hint}{C.RESET}")
        text = "".join(out)
        sys.stdout.write(text)
        sys.stdout.flush()
        return text.count("\n")

    def _clear(n: int) -> None:
        """Move cursor up n lines and clear to end of screen."""
        sys.stdout.write(f"\r\033[{n}A\033[J")
        sys.stdout.flush()

    def _handle_digit(digit_byte: bytes) -> bool:
        """Handle a numeric digit key press. Moves cursor to 1-indexed option. Returns True if redraw needed."""
        nonlocal sel
        try:
            idx = int(digit_byte.decode()) - 1
            if 0 <= idx < len(options):
                sel = idx
                return True
        except (ValueError, UnicodeDecodeError):
            pass
        return False

    if IS_WIN:
        try:
            import msvcrt  # type: ignore[import]
        except ImportError:
            return ask_choice(prompt, options, default, allow_back)
        try:
            n = _render()
            while True:
                ch = msvcrt.getch()
                if ch == b"\xe0":
                    ch2 = msvcrt.getch()
                    if ch2 == b"H":  # Up arrow
                        sel = (sel - 1) % len(options)
                        _clear(n)
                        n = _render()
                    elif ch2 == b"P":  # Down arrow
                        sel = (sel + 1) % len(options)
                        _clear(n)
                        n = _render()
                elif ch == b"\r":
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                    return sel
                elif ch == b"\x1b":
                    # Esc must never activate the highlighted item — when back
                    # is disallowed (e.g. main menu) it does nothing.
                    if allow_back:
                        sys.stdout.write("\n")
                        sys.stdout.flush()
                        return -1
                elif ch == b"\x03":
                    raise KeyboardInterrupt
                elif ch.isdigit():
                    if _handle_digit(ch):
                        _clear(n)
                        n = _render()
        except KeyboardInterrupt:
            sys.stdout.write("\n")
            sys.stdout.flush()
            raise
        except Exception:
            return ask_choice(prompt, options, default, allow_back)
    else:
        try:
            import termios
            import tty
        except ImportError:
            return ask_choice(prompt, options, default, allow_back)
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            n = _render()
            tty.setraw(fd)
            while True:
                b = os.read(fd, 1)
                if b == b"\x1b":
                    b2 = _read_byte_nonblock(fd)
                    if b2 == b"[":
                        b3 = os.read(fd, 1)
                        if b3 == b"A":  # Up arrow
                            sel = (sel - 1) % len(options)
                            termios.tcsetattr(fd, termios.TCSANOW, old)
                            _clear(n)
                            n = _render()
                            tty.setraw(fd)
                        elif b3 == b"B":  # Down arrow
                            sel = (sel + 1) % len(options)
                            termios.tcsetattr(fd, termios.TCSANOW, old)
                            _clear(n)
                            n = _render()
                            tty.setraw(fd)
                    elif allow_back:
                        # Plain Esc key. Esc must never activate the highlighted
                        # item — when back is disallowed it does nothing.
                        termios.tcsetattr(fd, termios.TCSANOW, old)
                        sys.stdout.write("\n")
                        sys.stdout.flush()
                        return -1
                elif b in (b"\r", b"\n"):
                    termios.tcsetattr(fd, termios.TCSANOW, old)
                    sys.stdout.write("\n")
                    sys.stdout.flush()
                    return sel
                elif b in (b"\x03", b"\x04"):  # Ctrl+C / Ctrl+D
                    termios.tcsetattr(fd, termios.TCSANOW, old)
                    raise KeyboardInterrupt
                elif b.isdigit():
                    if _handle_digit(b):
                        termios.tcsetattr(fd, termios.TCSANOW, old)
                        _clear(n)
                        n = _render()
                        tty.setraw(fd)
        except KeyboardInterrupt:
            try:
                termios.tcsetattr(fd, termios.TCSANOW, old)
            except Exception:
                pass
            sys.stdout.write("\n")
            sys.stdout.flush()
            raise
        except Exception:
            try:
                termios.tcsetattr(fd, termios.TCSANOW, old)
            except Exception:
                pass
            return ask_choice(prompt, options, default, allow_back)
