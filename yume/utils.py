"""Core utilities — subprocess wrapper, tool discovery, misc helpers."""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

_log = logging.getLogger("pocket_yume")

BASE_DIR = Path(__file__).parent.parent.resolve()
TOOLS_DIR = BASE_DIR / "tools"
SERVER_DIR = BASE_DIR / "server"
EXT_DIR = BASE_DIR / "extension"
GGUF_DIR = BASE_DIR / "models" / "translation"
LOGS_DIR = BASE_DIR / "logs"

EXE = ".exe" if sys.platform == "win32" else ""

KiB = 1024
MiB = 1024**2
GiB = 1024**3

# Injected at runtime by pocket_yume.py — avoids importing VERSION from there
VERSION = "0.0.9"


def _run(cmd: list, timeout: int = 30, **kw) -> subprocess.CompletedProcess:
    """subprocess.run wrapper: forces UTF-8 encoding to prevent Windows cp1252 crash."""
    kw.setdefault("capture_output", True)
    kw.setdefault("timeout", timeout)
    if kw.get("capture_output") or kw.get("stdout") == subprocess.PIPE:
        kw.setdefault("encoding", "utf-8")
        kw.setdefault("errors", "replace")
    if not cmd:
        return subprocess.CompletedProcess(cmd, returncode=127, stdout="", stderr="Empty command")
    try:
        return subprocess.run(cmd, **kw)  # nosec B603
    except FileNotFoundError:
        return subprocess.CompletedProcess(
            cmd, returncode=127, stdout="", stderr=f"Not found: {cmd[0] if cmd else '?'}"
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, returncode=124, stdout="", stderr="Timed out")


def find_tool(name: str) -> str | None:
    """Return path to a tool, checking tools/ first then PATH."""
    b = TOOLS_DIR / f"{name}{EXE}"
    if b.exists():
        return str(b)
    if not EXE:  # Unix: also check without extension
        b2 = TOOLS_DIR / name
        if b2.exists():
            return str(b2)
    return shutil.which(name)


def _try_import(module_name: str) -> bool:
    """Check if a Python module can be imported."""
    try:
        __import__(module_name)
        return True
    except ImportError:
        return False


def find_gguf_models() -> list[Path]:
    """Return list of .gguf files in models/translation/."""
    GGUF_DIR.mkdir(parents=True, exist_ok=True)
    return list(GGUF_DIR.glob("*.gguf"))


def rotate_logs(max_size_mb: int = 10, keep: int = 3) -> None:
    """Rotate log files if they exceed max_size_mb. Keep N backups."""
    for name in ["whisper_server.log", "translation_server.log"]:
        lp = LOGS_DIR / name
        if not lp.exists():
            continue
        size_mb = lp.stat().st_size / MiB
        if size_mb < max_size_mb:
            continue
        for i in range(keep, 0, -1):
            old = LOGS_DIR / f"{name}.{i}"
            new = LOGS_DIR / f"{name}.{i + 1}"
            if old.exists():
                if i == keep:
                    old.unlink()
                else:
                    old.rename(new)
        lp.rename(LOGS_DIR / f"{name}.1")
        _log.debug("[rotate_logs] Rotated %s (%.1f MB)", name, size_mb)


def check_for_updates(version: str = VERSION) -> tuple[str | None, str | None]:
    """Check GitHub for newer Yume releases. Returns (latest_version, url) or (None, None)."""
    try:
        req = urllib.request.Request(
            "https://api.github.com/repos/jenox645/Yume/releases/latest",
            headers={"User-Agent": f"Yume/{version}", "Accept": "application/vnd.github.v3+json"},
        )
        with urllib.request.urlopen(req, timeout=5) as resp:  # nosec B310
            data = json.loads(resp.read().decode("utf-8"))
            latest = data.get("tag_name", "").lstrip("v")
            try:
                remote = tuple(int(x) for x in latest.split("."))
                local = tuple(int(x) for x in version.split("."))
                is_newer = remote > local
            except (ValueError, TypeError):
                is_newer = False
            if latest and is_newer:
                return latest, data.get("html_url", "")
            return None, None
    except Exception:
        return None, None
