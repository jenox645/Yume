"""Network utilities — file download, server health checks, server API calls."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import socket as _socket
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from config import DEFAULT_OLLAMA_PORT, DEFAULT_TRANSLATION_PORT, DEFAULT_WHISPER_PORT
from yume.constants import VERSION, BASE_DIR, GGUF_DIR, KiB, MiB, GiB, HEALTH_PATH_OPENAI, HEALTH_PATH_OLLAMA
from yume.ui import C, error, info

_log = logging.getLogger("pocket_yume")

DOWNLOAD_CHUNK_SIZE = 64 * KiB


# Global per-process API token cache
_api_token: str | None = None


# ── Downloads ─────────────────────────────────────────────────────────────────


def fetch_published_sha256(checksums_url: str, filename: str) -> str | None:
    """SHA-256 for `filename` from a published checksum file, or None.

    Handles "HASH  name" lists (yt-dlp SHA2-256SUMS, BtbN checksums.sha256) and
    single-file sums (Deno's *.sha256sum, which on Windows is PowerShell
    Get-FileHash output: "Hash : HEX" on its own line).
    """
    try:
        req = urllib.request.Request(checksums_url, headers={"User-Agent": f"Yume/{VERSION}"})
        with urllib.request.urlopen(req, timeout=30) as resp:  # nosec B310 — official release URL
            text = resp.read(1_000_000).decode("utf-8", errors="replace")
    except Exception as e:
        _log.debug("[fetch_published_sha256] %s: %s", checksums_url, e)
        return None
    hexes = []
    for line in text.splitlines():
        m = re.search(r"\b([0-9a-fA-F]{64})\b", line)
        if not m:
            continue
        if filename in line:
            return m.group(1).lower()
        hexes.append(m.group(1).lower())
    return hexes[0] if len(hexes) == 1 else None  # single-file sum without a name


def _room_for(dest: Path, total: int, label: str) -> bool:
    """Enough disk space for a download of `total` bytes (with room to spare)."""
    from yume.hardware import disk_free_gb

    free = disk_free_gb(dest.parent) * GiB
    if free >= total * 1.5:
        return True
    print(f"\r  {C.RED}✗{C.RESET}  {label} — Not enough disk space" + " " * 20)
    info(f"Need ~{total * 1.5 / MiB:.0f} MB, have {free / MiB:.0f} MB free")
    return False


def _progress_line(dl: int, total: int, elapsed: float) -> str:
    """The download's progress line: bar, size, speed and ETA (size and speed
    only when the server sent no length)."""
    speed = dl / max(0.1, elapsed)
    speed_mb = speed / MiB
    if total <= 0:
        return f"\r  {C.CYAN}↓{C.RESET}  {dl / MiB:.1f} MB  {speed_mb:.1f} MB/s  "
    pct = min(100, dl * 100 // total)
    remaining = (total - dl) / speed if speed > 0 else 0
    eta = f"{int(remaining // 60)}m{int(remaining % 60):02d}s" if remaining > 60 else f"{int(remaining)}s"
    filled = 20 * pct // 100
    bar = f"{C.GREEN}{'█' * filled}{C.DIM}{'░' * (20 - filled)}{C.RESET}"
    return f"\r  {C.CYAN}↓{C.RESET} {bar} {pct:3d}%  {dl / MiB:.1f}/{total / MiB:.1f} MB  {speed_mb:.1f} MB/s  ETA {eta}   "


def _write_to(part: Path, resp, total: int, t0: float) -> tuple[int, str]:
    """Write the response to `part`, showing progress; (bytes written, their SHA-256)."""
    dl = 0
    digest = hashlib.sha256()
    with open(part, "wb") as f:
        while chunk := resp.read(DOWNLOAD_CHUNK_SIZE):
            f.write(chunk)
            digest.update(chunk)
            dl += len(chunk)
            sys.stdout.write(_progress_line(dl, total, time.time() - t0))
            sys.stdout.flush()
    return dl, digest.hexdigest()


def _fetch(url: str, dest: Path, part: Path, label: str, sha256: str | None) -> bool:
    """Download into `part`, check it, put it at `dest`. False: no room; raises otherwise."""
    req = urllib.request.Request(url, headers={"User-Agent": f"Yume/{VERSION}"})
    with urllib.request.urlopen(req, timeout=180) as resp:  # nosec B310
        total = int(resp.headers.get("content-length", 0))
        t0 = time.time()
        if total > 0 and not _room_for(dest, total, label):
            return False
        dl, got_sha256 = _write_to(part, resp, total, t0)
    if total and dl < total:
        raise OSError(f"connection closed after {dl / MiB:.1f} of {total / MiB:.1f} MB")
    if sha256 and got_sha256 != sha256.lower():
        raise OSError(f"SHA-256 mismatch — expected {sha256[:12]}…, got {got_sha256[:12]}… (file deleted)")
    part.replace(dest)
    elapsed = time.time() - t0
    print(f"\r  {C.GREEN}✓{C.RESET}  {label} — {dl / MiB:.1f} MB in {elapsed:.0f}s" + " " * 30)
    return True


def _say_failed(label: str, e: BaseException) -> None:
    if isinstance(e, urllib.error.HTTPError):
        print(f"\r  {C.RED}✗{C.RESET}  {label} — HTTP {e.code}: {e.reason}" + " " * 20)
        if e.code == 404:
            info("The download URL may have changed. Try updating Yume.")
    elif isinstance(e, urllib.error.URLError):
        print(f"\r  {C.RED}✗{C.RESET}  {label} — Connection failed: {e.reason}" + " " * 20)
        info("Check your internet connection and try again")
    else:
        print(f"\r  {C.RED}✗{C.RESET}  {label} — FAILED: {e or type(e).__name__}" + " " * 20)


def download_file(url: str, dest, label: str = "Downloading", sha256: str | None = None) -> bool:
    """Download a file with progress bar, speed, and ETA display.

    With `sha256`, the file is verified before it is put in place; a mismatch
    deletes it and fails (a truncated/corrupted model or a tampered binary must
    never be used).
    """
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    # Download to "<name>.part" and rename only when complete. Writing straight
    # to dest left a truncated file after Ctrl+C or a dropped connection — for
    # a .gguf the launcher then auto-selected it and llama.cpp crashed on load.
    part = dest.with_name(dest.name + ".part")
    try:
        return _fetch(url, dest, part, label, sha256)
    except BaseException as e:  # incl. KeyboardInterrupt: never leave a partial file behind
        _say_failed(label, e)
        _cleanup_partial(part)
        if isinstance(e, KeyboardInterrupt):
            raise
        return False


def _cleanup_partial(path) -> None:
    """Remove a partially downloaded file."""
    try:
        p = Path(path)
        if p.exists():
            p.unlink()
            _log.debug("[download] Cleaned up partial download: %s", p.name)
    except Exception:
        pass


# ── Server health checks ──────────────────────────────────────────────────────


def check_server(host: str, port: int, path: str = "/health") -> dict:
    """Check if a server is responding."""
    try:
        url = f"http://{host}:{port}{path}"  # noqa: S5332 — local server health check, no TLS on loopback
        req = urllib.request.Request(url, headers={"User-Agent": "Yume"})
        with urllib.request.urlopen(req, timeout=3) as resp:  # nosec B310
            body = resp.read()
            try:
                return {"up": True, "data": json.loads(body)}
            except (json.JSONDecodeError, ValueError):
                return {"up": True, "data": {"raw": body.decode("utf-8", errors="replace")[:200]}}
    except urllib.error.HTTPError as e:
        # Not healthy, but the body still says why (Whisper /health returns
        # 503 {"status": "loading"} or 500 {"status": "error", "error": ...}).
        try:
            data = json.loads(e.read())
        except Exception:
            data = {}
        return {"up": False, "data": data if isinstance(data, dict) else {}}
    except Exception:
        return {"up": False, "data": {}}


def check_translation_server(host: str, port: int, backend_info: dict | None = None) -> dict:
    """Check translation server health, falling back to raw socket."""
    bi = backend_info or {"hp": HEALTH_PATH_OPENAI}
    primary = bi.get("hp", HEALTH_PATH_OPENAI)

    st = check_server(host, port, primary)
    if st["up"]:
        return st

    try:
        with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
            s.settimeout(2)
            s.connect((host, int(port)))
        return {"up": True, "data": {"status": "busy"}}
    except Exception:
        return {"up": False, "data": {}}


def check_ollama_models(host: str = "127.0.0.1", port: int = DEFAULT_OLLAMA_PORT) -> list[str]:
    try:
        url = f"http://{host}:{port}{HEALTH_PATH_OLLAMA}"  # noqa: S5332 — local Ollama server
        req = urllib.request.Request(url, headers={"User-Agent": "Yume"})
        with urllib.request.urlopen(req, timeout=3) as resp:  # nosec B310
            return [m["name"] for m in json.loads(resp.read()).get("models", [])]
    except Exception:
        return []


# ── HuggingFace model browser ─────────────────────────────────────────────────


def hf_list_gguf(repo: str) -> list[dict]:
    """List .gguf files in a HuggingFace repo."""
    try:
        req = urllib.request.Request(
            f"https://huggingface.co/api/models/{repo}/tree/main",
            headers={"User-Agent": f"Yume/{VERSION}"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:  # nosec B310
            files = json.loads(resp.read())
        out = []
        for f in files:
            if f.get("path", "").endswith(".gguf"):
                sb = f.get("size", 0)
                sg = sb / GiB
                out.append(
                    {
                        "name": f["path"],
                        "bytes": sb,
                        "size": f"{sg:.2f} GB" if sg >= 1 else f"{sb / MiB:.0f} MB",
                        # HuggingFace stores big files in LFS; its oid IS the SHA-256
                        "sha256": (f.get("lfs") or {}).get("oid"),
                    }
                )
        return out
    except urllib.error.HTTPError as e:
        error(f"HuggingFace error: {e.code}")
        return []
    except Exception as e:
        error(f"Failed: {e}")
        return []


def hf_download(repo: str, filename: str, sha256: str | None = None) -> bool:
    GGUF_DIR.mkdir(parents=True, exist_ok=True)
    return download_file(
        f"https://huggingface.co/{repo}/resolve/main/{filename}",
        GGUF_DIR / filename,
        f"Downloading {filename}",
        sha256=sha256,
    )


# ── API token discovery ────────────────────────────────────────────────────────


def _is_localhost(host: str) -> bool:
    """Return True only for loopback addresses — guards token transmission over plain HTTP."""
    return host in ("localhost", "127.0.0.1", "::1")


def discover_api_token(host: str, port: int) -> str | None:
    """Discover API token from .yume_token file or server /health endpoint."""
    global _api_token
    if _api_token is not None:
        return _api_token

    token_file = BASE_DIR / ".yume_token"
    if token_file.exists():
        try:
            _api_token = token_file.read_text(encoding="utf-8").strip()
            if _api_token:
                return _api_token
        except Exception as e:
            _log.debug("[discover_api_token] token-file-read failed: %s", e)

    try:
        req = urllib.request.Request(f"http://{host}:{port}/health", headers={"User-Agent": "Yume"})  # noqa: S5332 — local server
        with urllib.request.urlopen(req, timeout=3) as resp:  # nosec B310
            data = json.loads(resp.read())
            token = data.get("api_token") or data.get("token")
            if token:
                _api_token = token
                return _api_token
    except Exception as e:
        _log.debug("[discover_api_token] health-check failed: %s", e)

    return None


def reset_api_token() -> None:
    """Forget the cached API token so the next request re-discovers it. Each server
    run mints a fresh token (secrets.token_urlsafe); a token cached from a previous
    run — e.g. after Stop + relaunch within one interactive CLI session — would
    otherwise fail auth on every request, making the runtime menu's blacklist, stats
    and model-switch appear as 'server not running'. Called on server (re)start; the
    request helpers also self-heal on a 401/403 by resetting and retrying once."""
    global _api_token
    _api_token = None


def _api_request(host: str, port: int, path: str, timeout: int, body: bytes | None = None) -> dict:
    headers = {"User-Agent": "Yume"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    token = discover_api_token(host, port) if _is_localhost(host) else None
    if token:
        headers["X-API-Token"] = token
    req = urllib.request.Request(f"http://{host}:{port}{path}", data=body, headers=headers)  # noqa: S5332 — local server
    with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310
        return json.loads(resp.read())


def _api_call(host: str, port: int, path: str, timeout: int, body: bytes | None = None) -> dict:
    """The server's JSON reply; raises what the request raises. Self-heals a stale cached token on 401/403."""
    try:
        return _api_request(host, port, path, timeout, body)
    except urllib.error.HTTPError as e:
        if e.code not in (401, 403) or not _is_localhost(host):
            raise
    # A token cached from a previous server run is now stale — drop it and
    # re-discover the current server's token once before giving up.
    reset_api_token()
    return _api_request(host, port, path, timeout, body)


def server_get(host: str, port: int, path: str, timeout: int = 5) -> dict | None:
    """GET request with API token auth."""
    try:
        return _api_call(host, port, path, timeout)
    except Exception:
        return None


def server_post(host: str, port: int, path: str, data: dict | None = None, timeout: int = 30) -> dict:
    """POST request with API token auth."""
    try:
        return _api_call(host, port, path, timeout, json.dumps(data or {}).encode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}"}
    except Exception as e:
        return {"error": str(e)}


def discover_servers(cfg: dict, backend_info: dict) -> dict:
    """Check if servers are already running."""
    results: dict = {}

    ws = check_server(cfg.get("whisper_host", "127.0.0.1"), cfg.get("whisper_port", DEFAULT_WHISPER_PORT), "/health")
    results["whisper"] = ws["up"]

    bk = cfg.get("translation_backend", "llamacpp")
    bi = backend_info.get(bk, backend_info.get("custom", {"hp": HEALTH_PATH_OPENAI}))
    t_host = cfg.get("translation_host", "127.0.0.1")
    t_port = cfg.get("translation_port", DEFAULT_TRANSLATION_PORT)
    ts = check_server(t_host, t_port, bi["hp"])
    if not ts["up"]:
        ts = check_server(t_host, t_port, HEALTH_PATH_OPENAI)
    results["translation"] = ts["up"]

    if not results["translation"] and bk == "ollama":
        for port in [DEFAULT_OLLAMA_PORT, DEFAULT_TRANSLATION_PORT, 8080]:
            if port != t_port:
                ts2 = check_server("127.0.0.1", port, HEALTH_PATH_OLLAMA)
                if ts2["up"]:
                    results["ollama_found"] = port
                    break

    return results
