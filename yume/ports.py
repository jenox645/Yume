"""Port management — availability check, process discovery, conflict resolution."""

from __future__ import annotations

import logging
import re
import socket as _socket
import sys
import time

from config import DEFAULT_TRANSLATION_PORT, DEFAULT_WHISPER_PORT
from yume.ui import C, ask_arrow, ask_input, ask_yn, error, info, success, warn

_log = logging.getLogger("pocket_yume")

IS_WIN = sys.platform == "win32"

MIN_PORT = 1
MAX_PORT = 65535
FIRST_UNPRIVILEGED_PORT = 1024
IANA_EPHEMERAL_START = 49152

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
    if not isinstance(port, int) or port < 1 or port > 65535:
        return False
    # Something accepting connections means busy, even when bind() below would
    # succeed (Windows lets 127.0.0.1 bind next to a listener on 0.0.0.0).
    try:
        with _socket.create_connection((host, port), timeout=0.3):
            return False
    except OSError:
        pass
    s = None
    try:
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        s.settimeout(1)
        s.bind((host, port))
        return True
    except (OSError, _socket.error):
        return False
    finally:
        if s:
            try:
                s.close()
            except Exception:
                pass


def find_free_port(start: int = DEFAULT_TRANSLATION_PORT, exclude: set | None = None) -> int | None:
    exclude = exclude or set()
    for p in range(max(FIRST_UNPRIVILEGED_PORT, start), min(start + 200, MAX_PORT + 1)):
        if p not in exclude and is_port_free(p):
            return p
    for p in range(IANA_EPHEMERAL_START, IANA_EPHEMERAL_START + 100):
        if p not in exclude and is_port_free(p):
            return p
    return None


def get_port_process(port: int) -> tuple[int | None, str | None]:
    """Find who owns a port. Returns (pid, name) or (None, None)."""
    from yume.utils import _run

    try:
        if IS_WIN:
            r = _run(["netstat", "-ano"], timeout=10)
            if r.returncode != 0 or not r.stdout:
                return None, None
            for line in r.stdout.splitlines():
                if re.search(rf":{port}(?:\s|$)", line) and "LISTENING" in line:
                    parts = line.split()
                    pid_str = parts[-1] if parts else ""
                    if pid_str.isdigit() and int(pid_str) != 0:
                        pid = int(pid_str)
                        r2 = _run(["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"], timeout=5)
                        name = "unknown"
                        if r2.returncode == 0 and r2.stdout:
                            for row in r2.stdout.splitlines():
                                if str(pid) in row and "," in row:
                                    name = row.split(",")[0].strip('"')
                                    break
                        return pid, name
        else:
            # LISTEN only: plain "lsof -ti :PORT" also lists CLIENTS connected to
            # the port (e.g. the browser), which would then be killed.
            r = _run(["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"], timeout=10)
            if r.returncode == 0 and r.stdout and r.stdout.strip():
                pid_str = r.stdout.strip().split("\n")[0].strip()
                if pid_str.isdigit():
                    pid = int(pid_str)
                    r2 = _run(["ps", "-p", str(pid), "-o", "comm="], timeout=5)
                    name = (r2.stdout or "").strip() if r2.returncode == 0 else "unknown"
                    return pid, name
            r2 = _run(["ss", "-tlnp"], timeout=10)
            if r2.returncode == 0 and r2.stdout:
                for sline in r2.stdout.splitlines():
                    if re.search(rf":{port}(?:\s|$)", sline):
                        m = re.search(r"pid=(\d+)", sline)
                        if m:
                            pid = int(m.group(1))
                            n = _run(["ps", "-p", str(pid), "-o", "comm="], timeout=5)
                            return pid, (n.stdout or "").strip() if n.returncode == 0 else "unknown"
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
        return is_port_free(port)
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


def ensure_port_free(port: int, cfg: dict, key_prefix: str, exclude: set | None = None) -> int | None:
    """Free up a port interactively. Returns port or None."""
    if is_port_free(port):
        return port
    pid, name = get_port_process(port)
    warn(f"Port {port} is in use by {name or 'unknown'} (PID {pid or '?'})")
    import platform

    if platform.system() == "Darwin" and port == 5000:
        info(f"{C.DIM}macOS uses port 5000 for AirPlay Receiver.{C.RESET}")
        info(f"{C.DIM}Disable it in System Settings > General > AirDrop & Handoff > AirPlay Receiver,{C.RESET}")
        info(f"{C.DIM}or let Yume use a different port (recommended).{C.RESET}")
        print()
    from config import save_config

    # Default = reassign, NOT kill: Enter-mashing must never terminate another
    # app's process (on macOS port 5000 is often AirPlay).
    ch = ask_arrow(
        "How to resolve?",
        [
            ("Use a different port", "Auto-find a free port (recommended)"),
            ("Kill the process", f"Terminate {name or ('PID ' + str(pid) if pid else 'unknown process')}"),
            ("Enter port manually", None),
            ("Cancel", None),
        ],
        default=0,
    )
    if ch == 1:
        if kill_port_process(port) and is_port_free(port):
            success(f"Port {port} is now free")
            return port
        error("Failed to free port")
        return None
    elif ch == 0:
        new_port = find_free_port(port + 1, exclude=exclude)
        if new_port:
            cfg[f"{key_prefix}_port"] = new_port
            save_config(cfg)
            success(f"Reassigned to port {new_port}")
            if key_prefix == "whisper":
                warn(f"Set the Whisper port to {new_port} in the browser extension popup (Server Settings) too.")
            return new_port
        error("No free port found")
        return None
    elif ch == 2:
        np = ask_input("Port number", str(port + 1))
        try:
            np = int(np)
            if np < 1 or np > 65535:
                error("Port must be between 1 and 65535")
                return None
            if is_port_free(np):
                cfg[f"{key_prefix}_port"] = np
                save_config(cfg)
                return np
            else:
                error(f"Port {np} is also in use")
                return None
        except ValueError:
            error("Invalid port")
            return None
    return None


def show_ports_status(cfg: dict) -> None:
    """Display port status overview."""
    from yume.ui import section

    section("Port Status")
    for label, key, default_port in [
        ("Whisper", "whisper", DEFAULT_WHISPER_PORT),
        ("Translation", "translation", DEFAULT_TRANSLATION_PORT),
    ]:
        host = cfg.get(f"{key}_host", "127.0.0.1")
        port = cfg.get(f"{key}_port", default_port)
        free = is_port_free(port, host)
        if free:
            info(f"{label:12s} {host}:{port}  -- {C.GREEN}free{C.RESET}")
        else:
            pid, name = get_port_process(port)
            who = f"{name} (PID {pid})" if pid else "unknown process"
            warn(f"{label:12s} {host}:{port}  -- {C.RED}in use{C.RESET} by {who}")
