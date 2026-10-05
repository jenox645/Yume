"""Native messaging host: lets the browser extension start and stop Yume.

The browser runs this script (through the launcher written by `register()`)
for each message the extension sends with runtime.sendNativeMessage, and
talks to it over stdin/stdout: a 4-byte native-endian length, then UTF-8 JSON.

Messages ({"cmd": ...}):
    ping    → {"ok": true, "protocol": 1}
    status  → {"ok": true, "state": "stopped|starting|running|stopping", "message", "last_error", "managed"}
    start   → starts `pocket_yume.py serve` unless Yume is already up
    stop    → stops the servers `serve` started

Only the extension IDs listed in the host manifest may call it (the browser
enforces this), and it can do nothing beyond starting/stopping Yume.
"""

from __future__ import annotations

import json
import logging
import os
import struct
import sys
from pathlib import Path

if __package__ in (None, ""):  # run as a script by the browser
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from yume.hardware import IS_WIN  # noqa: E402
from yume.utils import BASE_DIR  # noqa: E402

_log = logging.getLogger("pocket_yume.native_host")

HOST_NAME = "com.pocketyume.yume"
PROTOCOL = 1
MAX_MESSAGE = 64 * 1024  # requests are tiny; refuse anything big

# The ID Chromium derives from the "key" in extension/manifest.json (stable for
# the unpacked extension), and the gecko id from the same manifest.
CHROMIUM_EXTENSION_IDS = ("eagcngkomlidpdaggkkgefjobcjgjaio",)
FIREFOX_EXTENSION_ID = "yume-subtitles@pocketyume"

HOST_DIR = BASE_DIR / "config" / "native_host"

# HKCU registry keys (Windows) — Opera and Vivaldi read Chrome's.
_WIN_CHROMIUM_KEYS = {
    "Chrome": r"Software\Google\Chrome\NativeMessagingHosts",
    "Edge": r"Software\Microsoft\Edge\NativeMessagingHosts",
    "Brave": r"Software\BraveSoftware\Brave-Browser\NativeMessagingHosts",
    "Chromium": r"Software\Chromium\NativeMessagingHosts",
}
_WIN_FIREFOX_KEY = r"Software\Mozilla\NativeMessagingHosts"


def _posix_dirs() -> dict[str, tuple[Path, bool]]:
    """{browser: (NativeMessagingHosts dir, is_firefox)} for Linux/macOS."""
    home = Path.home()
    if sys.platform == "darwin":
        sup = home / "Library" / "Application Support"
        return {
            "Chrome": (sup / "Google" / "Chrome" / "NativeMessagingHosts", False),
            "Chromium": (sup / "Chromium" / "NativeMessagingHosts", False),
            "Edge": (sup / "Microsoft Edge" / "NativeMessagingHosts", False),
            "Brave": (sup / "BraveSoftware" / "Brave-Browser" / "NativeMessagingHosts", False),
            "Firefox": (sup / "Mozilla" / "NativeMessagingHosts", True),
        }
    cfg = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    return {
        "Chrome": (cfg / "google-chrome" / "NativeMessagingHosts", False),
        "Chromium": (cfg / "chromium" / "NativeMessagingHosts", False),
        "Edge": (cfg / "microsoft-edge" / "NativeMessagingHosts", False),
        "Brave": (cfg / "BraveSoftware" / "Brave-Browser" / "NativeMessagingHosts", False),
        "Firefox": (home / ".mozilla" / "native-messaging-hosts", True),
    }


# ── Protocol ─────────────────────────────────────────────────────────────────


def read_message(stream) -> dict | None:
    """One message from `stream` (binary), or None at end of input."""
    head = stream.read(4)
    if len(head) < 4:
        return None
    (size,) = struct.unpack("=I", head)
    if size > MAX_MESSAGE:
        raise ValueError(f"message too large ({size} bytes)")
    body = stream.read(size)
    if len(body) < size:
        return None
    msg = json.loads(body.decode("utf-8"))
    if not isinstance(msg, dict):
        raise ValueError("message must be a JSON object")
    return msg


def write_message(stream, msg: dict) -> None:
    data = json.dumps(msg, ensure_ascii=False).encode("utf-8")
    stream.write(struct.pack("=I", len(data)) + data)
    stream.flush()


def handle(msg: dict) -> dict:
    """Answer one request."""
    from config import load_config
    from yume import service
    from yume.network import check_server

    cmd = msg.get("cmd")
    if cmd == "ping":
        return {"ok": True, "protocol": PROTOCOL}
    if cmd not in ("status", "start", "stop"):
        return {"ok": False, "error": f"unknown command: {cmd!r}"}

    cfg = load_config()
    st = service.read_state()
    managed = st["state"] != "stopped"

    def whisper_up() -> bool:
        return check_server(cfg["whisper_host"], cfg["whisper_port"], "/health")["up"]

    if cmd == "status":
        state = st["state"]
        if not managed and whisper_up():
            state = "running"  # started from the launcher / CLI
        return {
            "ok": True,
            "state": state,
            "message": st.get("message", ""),
            "last_error": st.get("last_error", ""),
            "managed": managed,
        }

    if cmd == "start":
        if managed:
            return {"ok": True, "state": st["state"], "message": st.get("message", ""), "managed": True}
        if whisper_up():
            return {"ok": True, "state": "running", "managed": False}
        if not cfg.get("first_run_complete"):
            return {"ok": False, "error": "Yume is not set up yet — run START_YUME once to finish setup."}
        pid = service.spawn_detached(["serve"])
        service.mark_starting(pid)
        _log.info("started serve (pid %s)", pid)
        return {"ok": True, "state": "starting", "message": "Starting Yume…", "managed": True}

    # stop
    if not managed:
        if whisper_up():
            return {"ok": False, "error": "Yume was started from the launcher — stop it there."}
        return {"ok": True, "state": "stopped"}
    stopped = service.request_stop()
    return {"ok": stopped, "state": "stopped" if stopped else "stopping"}


def main() -> int:
    # The browser reads protocol frames from stdout: keep the real handle for
    # them and send anything else that gets printed to the log instead.
    if IS_WIN:
        import msvcrt

        msvcrt.setmode(sys.stdin.fileno(), os.O_BINARY)
        msvcrt.setmode(sys.stdout.fileno(), os.O_BINARY)
    out = sys.stdout.buffer
    inp = sys.stdin.buffer
    log_dir = BASE_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log = open(log_dir / "native_host.log", "a", encoding="utf-8", errors="replace")  # noqa: SIM115
    sys.stdout = sys.stderr = log
    logging.basicConfig(stream=log, level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    try:
        while True:
            try:
                msg = read_message(inp)
            except ValueError as e:
                write_message(out, {"ok": False, "error": str(e)})
                return 1
            if msg is None:
                return 0
            try:
                reply = handle(msg)
            except Exception as e:
                _log.exception("request failed: %r", msg)
                reply = {"ok": False, "error": str(e)}
            write_message(out, reply)
    finally:
        log.close()


# ── Registration ─────────────────────────────────────────────────────────────


def _launcher_path() -> Path:
    return HOST_DIR / ("yume_host.bat" if IS_WIN else "yume_host.sh")


def _write_launcher() -> Path:
    """A launcher the browser can execute (manifests need a single path)."""
    from yume.service import console_python

    HOST_DIR.mkdir(parents=True, exist_ok=True)
    path = _launcher_path()
    script = Path(__file__).resolve()
    if IS_WIN:
        # The browser starts it hidden; python.exe (not pythonw) keeps stdio working.
        path.write_bytes(f'@echo off\r\n"{console_python()}" "{script}" %*\r\n'.encode())
    else:
        path.write_bytes(f'#!/bin/sh\nexec "{sys.executable}" "{script}" "$@"\n'.encode())
        path.chmod(0o755)
    return path


def _manifest(launcher: Path, firefox: bool) -> dict:
    m = {
        "name": HOST_NAME,
        "description": "Yume — lets the browser extension start and stop the local Yume servers",
        "path": str(launcher),
        "type": "stdio",
    }
    if firefox:
        m["allowed_extensions"] = [FIREFOX_EXTENSION_ID]
    else:
        m["allowed_origins"] = [f"chrome-extension://{i}/" for i in CHROMIUM_EXTENSION_IDS]
    return m


def _winreg():
    import winreg

    return winreg


def register() -> list[str]:
    """Register the host for every supported browser. Returns the browsers done."""
    launcher = _write_launcher()
    done = []
    if IS_WIN:
        winreg = _winreg()
        chromium = HOST_DIR / f"{HOST_NAME}.json"
        firefox = HOST_DIR / f"{HOST_NAME}.firefox.json"
        chromium.write_text(json.dumps(_manifest(launcher, False), indent=2), encoding="utf-8")
        firefox.write_text(json.dumps(_manifest(launcher, True), indent=2), encoding="utf-8")
        keys = {**_WIN_CHROMIUM_KEYS, "Firefox": _WIN_FIREFOX_KEY}
        for browser, base in keys.items():
            target = firefox if browser == "Firefox" else chromium
            with winreg.CreateKey(winreg.HKEY_CURRENT_USER, f"{base}\\{HOST_NAME}") as k:
                winreg.SetValueEx(k, "", 0, winreg.REG_SZ, str(target))
            done.append(browser)
        return done
    for browser, (d, is_ff) in _posix_dirs().items():
        if not d.parent.exists():  # browser not installed (no profile dir)
            continue
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{HOST_NAME}.json").write_text(json.dumps(_manifest(launcher, is_ff), indent=2), encoding="utf-8")
        done.append(browser)
    return done


def unregister() -> None:
    """Remove every registration and the generated files."""
    if IS_WIN:
        winreg = _winreg()
        for base in (*_WIN_CHROMIUM_KEYS.values(), _WIN_FIREFOX_KEY):
            try:
                winreg.DeleteKey(winreg.HKEY_CURRENT_USER, f"{base}\\{HOST_NAME}")
            except OSError:
                pass
    else:
        for d, _ in _posix_dirs().values():
            (d / f"{HOST_NAME}.json").unlink(missing_ok=True)
    if HOST_DIR.exists():
        for f in HOST_DIR.iterdir():
            f.unlink(missing_ok=True)
        HOST_DIR.rmdir()


def registered_browsers() -> list[str]:
    """Browsers whose registration points at this Yume folder."""
    found = []
    if IS_WIN:
        winreg = _winreg()
        for browser, base in {**_WIN_CHROMIUM_KEYS, "Firefox": _WIN_FIREFOX_KEY}.items():
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, f"{base}\\{HOST_NAME}") as k:
                    target = Path(winreg.QueryValueEx(k, "")[0])
            except OSError:
                continue
            if target.parent == HOST_DIR and target.exists():
                found.append(browser)
        return found
    for browser, (d, _) in _posix_dirs().items():
        try:
            m = json.loads((d / f"{HOST_NAME}.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if Path(m.get("path", "")).parent == HOST_DIR:
            found.append(browser)
    return found


if __name__ == "__main__":
    sys.exit(main())
