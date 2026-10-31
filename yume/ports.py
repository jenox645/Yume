"""Port management — availability check, process discovery, conflict resolution."""

from __future__ import annotations

import logging
import re
import socket as _socket
import time

from config import DEFAULT_TRANSLATION_PORT, DEFAULT_WHISPER_PORT, MAX_PORT
from yume.constants import IS_MAC, IS_WIN
from yume.ui import C, ask_arrow, ask_input, ask_yn, error, info, section, success, warn

_log = logging.getLogger("pocket_yume")


MIN_PORT = 1
FIRST_UNPRIVILEGED_PORT = 1024
IANA_EPHEMERAL_START = 49152
AIRPLAY_PORT = 5000

# What Yume itself runs on its ports. Ollama / llama-server are their own
# binaries; Whisper and llama-cpp-python are Python, and "python" alone is not
# enough — the user's own Flask app or notebook on port 5000 is Python too — so
# a Python process must also have one of Yume's scripts on its command line.
# Anything else (e.g. macOS AirPlay Receiver on 5000) is never killed silently.
_YUME_PROCESS_HINTS = ("llama", "ollama")
_YUME_CMDLINE_HINTS = ("faster_whisper_server", "llama_cpp.server", "pocket_yume")


def _process_cmdline(pid: int) -> str:
    """Full command line of a process ("" if unknown)."""
    from yume.utils import _run

    try:
        if IS_WIN:
            r = _run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command",
                 f"(Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}').CommandLine"],
                timeout=20,
            )  # fmt: skip
        else:
            r = _run(["ps", "-p", str(int(pid)), "-o", "args="], timeout=5)
        return (r.stdout or "").strip() if r.returncode == 0 else ""
    except Exception as e:
        _log.debug("[_process_cmdline] %s", e)
        return ""


def is_yume_process(pid: int, name: str | None) -> bool:
    label = (name or "").lower()
    if any(h in label for h in _YUME_PROCESS_HINTS):
        return True
    if "python" in label:
        cmd = _process_cmdline(pid).lower()
        return any(h in cmd for h in _YUME_CMDLINE_HINTS)
    return False


def is_port_free(port: int, host: str = "127.0.0.1") -> bool:
    if not isinstance(port, int) or not MIN_PORT <= port <= MAX_PORT:
        return False
    # Something accepting connections means busy, even when bind() below would
    # succeed (Windows lets 127.0.0.1 bind next to a listener on 0.0.0.0).
    try:
        with _socket.create_connection((host, port), timeout=0.3):
            return False
    except OSError:
        pass
    try:
        with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
            s.settimeout(1)
            s.bind((host, port))
        return True
    except OSError:
        return False


def find_free_port(start: int = DEFAULT_TRANSLATION_PORT, exclude: set | None = None) -> int | None:
    exclude = exclude or set()
    for p in range(max(FIRST_UNPRIVILEGED_PORT, start), min(start + 200, MAX_PORT + 1)):
        if p not in exclude and is_port_free(p):
            return p
    for p in range(IANA_EPHEMERAL_START, IANA_EPHEMERAL_START + 100):
        if p not in exclude and is_port_free(p):
            return p
    return None


def _tool_lines(cmd: list[str], timeout: int = 10) -> list[str]:
    """A command's output lines; none when it fails."""
    from yume.utils import _run

    r = _run(cmd, timeout=timeout)
    return (r.stdout or "").splitlines() if r.returncode == 0 else []


def _windows_listening_pid(port: int) -> int | None:
    on_port = re.compile(rf":{port}(?:\s|$)")
    for line in _tool_lines(["netstat", "-ano"]):
        parts = line.split()
        if on_port.search(line) and "LISTENING" in line and parts[-1].isdigit() and int(parts[-1]):
            return int(parts[-1])
    return None


def _posix_listening_pid(port: int) -> int | None:
    # -sTCP:LISTEN: plain "lsof -ti :PORT" also lists clients (e.g. the browser)
    pids = [ln.strip() for ln in _tool_lines(["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"]) if ln.strip()]
    if pids and pids[0].isdigit():
        return int(pids[0])
    on_port = re.compile(rf":{port}(?:\s|$)")
    for line in _tool_lines(["ss", "-tlnp"]):
        m = re.search(r"pid=(\d+)", line) if on_port.search(line) else None
        if m:
            return int(m.group(1))
    return None


def _listening_pid(port: int) -> int | None:
    """The PID of the process LISTENING on the port (never a client connected to it)."""
    return _windows_listening_pid(port) if IS_WIN else _posix_listening_pid(port)


def _process_name(pid: int) -> str:
    if IS_WIN:
        rows = _tool_lines(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"], timeout=5)
        return next((row.split(",")[0].strip('"') for row in rows if str(pid) in row and "," in row), "unknown")
    lines = _tool_lines(["ps", "-p", str(pid), "-o", "comm="], timeout=5)
    return lines[0].strip() if lines else "unknown"


def get_port_process(port: int) -> tuple[int | None, str | None]:
    """Find who owns a port. Returns (pid, name) or (None, None)."""
    try:
        pid = _listening_pid(port)
        return (pid, _process_name(pid)) if pid else (None, None)
    except Exception as e:
        _log.debug("[get_port_process] port-lookup failed: %s", e)
        return None, None


def kill_port_process(port: int, interactive: bool = True) -> bool:
    """Kill the process listening on a port. Returns True if freed.

    Only processes that look like Yume's own servers are killed without asking.
    Anything else is killed only after the user confirms (interactive=True);
    with interactive=False it is left alone.
    """
    from yume.utils import _run

    if is_port_free(port):
        return True
    pid, name = get_port_process(port)
    if pid is None:
        warn(f"Port {port} is busy but its owner could not be identified — not killing anything.")
        return False

    label = name or "unknown process"
    if not is_yume_process(pid, name):
        if not interactive:
            warn(f"Port {port} is used by {label} (PID {pid}) — leaving it alone.")
            return False
        warn(f"Port {port} is used by {C.BOLD}{label}{C.RESET} (PID {pid}), which is not a Yume server.")
        if not ask_yn(f"Kill {label} to free port {port}?", False):
            info("Left it running. Change the port in Settings → Server addresses.")
            return False

    try:
        if IS_WIN:
            _run(["taskkill", "/F", "/PID", str(pid)], timeout=10)
        else:
            _run(["kill", "-9", str(pid)], timeout=10)
        time.sleep(1)
        if is_port_free(port):
            info(f"Killed {label} (PID {pid}) on port {port}")
            return True
        return False
    except Exception as e:
        warn(f"Could not kill PID {pid}: {e}")
        return False


BGUTIL_PORT = 4416  # PO-token server (Deno) the Whisper server starts for YouTube auth


def stop_bgutil_server() -> None:
    """Stop the Deno PO-token server after stopping the Whisper server. The
    Whisper server stops it at exit — but not when it is terminated, which is
    how Yume stops it (TerminateProcess on Windows skips exit handlers)."""
    from yume.utils import _run

    if is_port_free(BGUTIL_PORT):
        return
    pid, name = get_port_process(BGUTIL_PORT)
    if not pid or "deno" not in (name or "").lower():
        return  # not ours
    try:
        if IS_WIN:
            _run(["taskkill", "/F", "/PID", str(pid)], timeout=10)
        else:
            _run(["kill", str(pid)], timeout=10)
    except Exception as e:
        _log.debug("[stop_bgutil_server] %s", e)


def _airplay_note(port: int) -> None:
    if IS_MAC and port == AIRPLAY_PORT:
        info(f"{C.DIM}macOS uses port {AIRPLAY_PORT} for AirPlay Receiver.{C.RESET}")
        info(f"{C.DIM}Disable it in System Settings > General > AirDrop & Handoff > AirPlay Receiver,{C.RESET}")
        info(f"{C.DIM}or let Yume use a different port (recommended).{C.RESET}")
        print()


def _kill_owner(port: int) -> int | None:
    if kill_port_process(port) and is_port_free(port):
        success(f"Port {port} is now free")
        return port
    error("Failed to free port")
    return None


def ensure_port_free(port: int, cfg: dict, key_prefix: str, exclude: set | None = None) -> int | None:
    """Free up a port interactively. Returns port or None."""
    if is_port_free(port):
        return port
    pid, name = get_port_process(port)
    warn(f"Port {port} is in use by {name or 'unknown'} (PID {pid or '?'})")
    _airplay_note(port)
    owner = name or (f"PID {pid}" if pid else "unknown process")
    # Default = reassign, NOT kill: Enter-mashing must never terminate another
    # app's process (on macOS port 5000 is often AirPlay).
    ch = ask_arrow(
        "How to resolve?",
        [
            ("Use a different port", "Auto-find a free port (recommended)"),
            ("Kill the process", f"Terminate {owner}"),
            ("Enter port manually", None),
            ("Cancel", None),
        ],
        default=0,
    )
    if ch == 0:
        return _other_port(port, cfg, key_prefix, exclude)
    if ch == 1:
        return _kill_owner(port)
    if ch == 2:
        return _typed_port(port, cfg, key_prefix, exclude)
    return None


def _save_port(cfg: dict, key_prefix: str, port: int) -> None:
    from config import save_config

    cfg[f"{key_prefix}_port"] = port
    save_config(cfg)


def _remind_extension(key_prefix: str, port: int) -> None:
    if key_prefix == "whisper":
        warn(f"Set the Whisper port to {port} in the browser extension popup (Server Settings) too.")


def _other_port(port: int, cfg: dict, key_prefix: str, exclude: set | None) -> int | None:
    """Move to the next free port above `port` and save it."""
    new_port = find_free_port(port + 1, exclude=exclude)
    if not new_port:
        error("No free port found")
        return None
    _save_port(cfg, key_prefix, new_port)
    success(f"Reassigned to port {new_port}")
    _remind_extension(key_prefix, new_port)
    return new_port


def _typed_port(port: int, cfg: dict, key_prefix: str, exclude: set | None) -> int | None:
    """Ask for a port number; save and return it if it is valid and free."""
    try:
        typed = int(ask_input("Port number", str(port + 1)))
    except ValueError:
        error("Invalid port")
        return None
    if not MIN_PORT <= typed <= MAX_PORT:
        error(f"Port must be between {MIN_PORT} and {MAX_PORT}")
        return None
    if typed in (exclude or ()):
        error(f"Port {typed} is for the other server")
        return None
    if not is_port_free(typed):
        error(f"Port {typed} is also in use")
        return None
    _save_port(cfg, key_prefix, typed)
    _remind_extension(key_prefix, typed)
    return typed


def port_owners(cfg: dict) -> list[tuple[str, str, int, str | None]]:
    """(label, host, port, who holds it) per server; None when the port is free."""
    rows = []
    for label, key, default_port in [
        ("Whisper", "whisper", DEFAULT_WHISPER_PORT),
        ("Translation", "translation", DEFAULT_TRANSLATION_PORT),
    ]:
        host = cfg.get(f"{key}_host", "127.0.0.1")
        port = cfg.get(f"{key}_port", default_port)
        who = None
        if not is_port_free(port, host):
            pid, name = get_port_process(port)
            who = f"{name} (PID {pid})" if pid else "unknown process"
        rows.append((label, host, port, who))
    return rows


def show_ports_status(cfg: dict) -> None:
    """Display port status overview."""
    section("Port Status")
    for label, host, port, who in port_owners(cfg):
        if who is None:
            info(f"{label:12s} {host}:{port}  -- {C.GREEN}free{C.RESET}")
        else:
            warn(f"{label:12s} {host}:{port}  -- {C.RED}in use{C.RESET} by {who}")
