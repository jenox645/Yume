"""Port management — availability check, process discovery, conflict resolution."""

from __future__ import annotations

import logging
import re
import socket as _socket
import sys
import time

from yume.ui import C, ask_choice, ask_input, error, info, success, warn

_log = logging.getLogger("pocket_yume")

IS_WIN = sys.platform == "win32"

MIN_PORT = 1
MAX_PORT = 65535
FIRST_UNPRIVILEGED_PORT = 1024
IANA_EPHEMERAL_START = 49152

DEFAULT_TRANSLATION_PORT = 5000
DEFAULT_WHISPER_PORT = 5001


def is_port_free(port: int, host: str = "127.0.0.1") -> bool:
    if not isinstance(port, int) or port < 1 or port > 65535:
        return False
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
                if f":{port}" in line and "LISTENING" in line:
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
            r = _run(["lsof", "-ti", f":{port}"], timeout=10)
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
                    if f":{port}" in sline:
                        m = re.search(r"pid=(\d+)", sline)
                        if m:
                            pid = int(m.group(1))
                            n = _run(["ps", "-p", str(pid), "-o", "comm="], timeout=5)
                            return pid, (n.stdout or "").strip() if n.returncode == 0 else "unknown"
    except Exception as e:
        _log.debug("[get_port_process] port-lookup failed: %s", e)
    return None, None


def kill_port_process(port: int) -> bool:
    """Kill whatever process is using a port. Returns True if freed."""
    from yume.utils import _run

    pid, name = get_port_process(port)
    if pid is None:
        if not IS_WIN:
            try:
                r = _run(["fuser", f"{port}/tcp"], timeout=5)
                if r.returncode == 0 and r.stdout.strip():
                    for p in r.stdout.strip().split():
                        p = p.strip()
                        if p.isdigit():
                            _run(["kill", "-9", p], timeout=5)
                    time.sleep(0.5)
                    if is_port_free(port):
                        success(f"Freed port {port}")
                        return True
            except Exception as e:
                _log.debug("[kill_port_process] fuser-kill failed: %s", e)
        return False
    try:
        if IS_WIN:
            _run(["taskkill", "/F", "/PID", str(pid)], timeout=10)
        else:
            _run(["kill", "-9", str(pid)], timeout=10)
        time.sleep(1)
        if is_port_free(port):
            info(f"Killed {name or 'process'} (PID {pid}) on port {port}")
            return True
        if not IS_WIN:
            _run(["fuser", "-k", f"{port}/tcp"], timeout=5)
            time.sleep(0.5)
        return is_port_free(port)
    except Exception as e:
        warn(f"Could not kill PID {pid}: {e}")
        return False


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

    ch = ask_choice(
        "How to resolve?",
        [
            ("Kill the process", f"Terminate {name or 'PID ' + str(pid)}"),
            ("Use a different port", "Auto-find a free port"),
            ("Enter port manually", None),
            ("Cancel", None),
        ],
        default=0,
    )
    if ch == 0:
        if kill_port_process(port) and is_port_free(port):
            success(f"Port {port} is now free")
            return port
        error("Failed to free port")
        return None
    elif ch == 1:
        new_port = find_free_port(port + 1, exclude=exclude)
        if new_port:
            cfg[f"{key_prefix}_port"] = new_port
            save_config(cfg)
            success(f"Reassigned to port {new_port}")
            return new_port
        error("No free port found")
        return None
    elif ch == 2:
        np = ask_input("Port number", str(port + 1))
        try:
            np = int(np)
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
