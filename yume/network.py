"""Network utilities — file download, server health checks, server API calls."""

from __future__ import annotations

import json
import logging
import socket as _socket
import time
import urllib.error
import urllib.request
from pathlib import Path

from yume.ui import C, info

_log = logging.getLogger("pocket_yume")

KiB = 1024
MiB = 1024**2
GiB = 1024**3
DOWNLOAD_CHUNK_SIZE = 64 * KiB

# Injected at runtime by pocket_yume._init_modules() to avoid circular imports
_VERSION: str = "0.0.0"


def set_version(version: str) -> None:
    """Called by pocket_yume at startup to set the version string."""
    global _VERSION
    _VERSION = version

# Global per-process API token cache
_api_token: str | None = None


# ── Downloads ─────────────────────────────────────────────────────────────────


def download_file(url: str, dest, label: str = "Downloading") -> bool:
    """Download a file with progress bar, speed, and ETA display."""
    import sys

    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        req = urllib.request.Request(url, headers={"User-Agent": f"Yume/{_VERSION}"})
        with urllib.request.urlopen(req, timeout=180) as resp:  # nosec B310
            total = int(resp.headers.get("content-length", 0))
            dl = 0
            t0 = time.time()
            if total > 0:
                from yume.hardware import disk_free_gb

                free = disk_free_gb(dest.parent) * GiB
                if free < total * 1.5:
                    print(f"\r  {C.RED}✗{C.RESET}  {label} — Not enough disk space" + " " * 20)
                    info(f"Need ~{total * 1.5 / MiB:.0f} MB, have {free / MiB:.0f} MB free")
                    return False
            with open(dest, "wb") as f:
                while True:
                    chunk = resp.read(DOWNLOAD_CHUNK_SIZE)
                    if not chunk:
                        break
                    f.write(chunk)
                    dl += len(chunk)
                    elapsed = max(0.1, time.time() - t0)
                    speed = dl / elapsed
                    speed_mb = speed / MiB
                    if total > 0:
                        pct = min(100, dl * 100 // total)
                        remaining = (total - dl) / speed if speed > 0 else 0
                        eta_str = (
                            f"{int(remaining // 60)}m{int(remaining % 60):02d}s"
                            if remaining > 60
                            else f"{int(remaining)}s"
                        )
                        mb = dl / MiB
                        tmb = total / MiB
                        bar_w = 20
                        filled = bar_w * pct // 100
                        bar = f"{C.GREEN}{'█' * filled}{C.DIM}{'░' * (bar_w - filled)}{C.RESET}"
                        sys.stdout.write(
                            f"\r  {C.CYAN}↓{C.RESET} {bar} {pct:3d}%  "
                            f"{mb:.1f}/{tmb:.1f} MB  "
                            f"{speed_mb:.1f} MB/s  "
                            f"ETA {eta_str}   "
                        )
                        sys.stdout.flush()
                    else:
                        mb = dl / MiB
                        sys.stdout.write(f"\r  {C.CYAN}↓{C.RESET}  {mb:.1f} MB  {speed_mb:.1f} MB/s  ")
                        sys.stdout.flush()
        elapsed = time.time() - t0
        print(f"\r  {C.GREEN}✓{C.RESET}  {label} — {total / MiB:.1f} MB in {elapsed:.0f}s" + " " * 30)
        return True
    except urllib.error.HTTPError as e:
        print(f"\r  {C.RED}✗{C.RESET}  {label} — HTTP {e.code}: {e.reason}" + " " * 20)
        if e.code == 404:
            info("The download URL may have changed. Try updating Yume.")
        _cleanup_partial(dest)
        return False
    except urllib.error.URLError as e:
        print(f"\r  {C.RED}✗{C.RESET}  {label} — Connection failed: {e.reason}" + " " * 20)
        info("Check your internet connection and try again")
        _cleanup_partial(dest)
        return False
    except Exception as e:
        print(f"\r  {C.RED}✗{C.RESET}  {label} — FAILED: {e}" + " " * 20)
        _cleanup_partial(dest)
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

HEALTH_PATH_OPENAI = "/v1/models"
HEALTH_PATH_OLLAMA = "/api/tags"


def check_server(host: str, port: int, path: str = "/health") -> dict:
    """Check if a server is responding."""
    try:
        url = f"http://{host}:{port}{path}"
        req = urllib.request.Request(url, headers={"User-Agent": "Yume"})
        with urllib.request.urlopen(req, timeout=3) as resp:  # nosec B310
            body = resp.read()
            try:
                return {"up": True, "data": json.loads(body)}
            except (json.JSONDecodeError, ValueError):
                return {"up": True, "data": {"raw": body.decode("utf-8", errors="replace")[:200]}}
    except Exception:
        return {"up": False, "data": {}}


def check_translation_server(host: str, port: int, backend_info: dict | None = None) -> dict:
    """Check translation server health, falling back to raw socket."""
    bi = backend_info or {"hp": HEALTH_PATH_OPENAI}
    primary = bi.get("hp", HEALTH_PATH_OPENAI)

    st = check_server(host, port, primary)
    if st["up"]:
        return st

    s = None
    try:
        s = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
        s.settimeout(2)
        s.connect((host, int(port)))
        return {"up": True, "data": {"status": "busy"}}
    except Exception:
        pass
    finally:
        if s:
            try:
                s.close()
            except Exception:
                pass

    return {"up": False, "data": {}}


def check_ollama_models(host: str = "127.0.0.1", port: int = 11434) -> list[str]:
    try:
        req = urllib.request.Request(f"http://{host}:{port}/api/tags", headers={"User-Agent": "Yume"})
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
            headers={"User-Agent": f"Yume/{_VERSION}"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:  # nosec B310
            files = json.loads(resp.read())
        out = []
        for f in files:
            if f.get("path", "").endswith(".gguf"):
                sb = f.get("size", 0)
                sg = sb / GiB
                out.append(
                    {"name": f["path"], "bytes": sb, "size": f"{sg:.2f} GB" if sg >= 1 else f"{sb / MiB:.0f} MB"}
                )
        return out
    except urllib.error.HTTPError as e:
        from yume.ui import error

        error(f"HuggingFace error: {e.code}")
        return []
    except Exception as e:
        from yume.ui import error

        error(f"Failed: {e}")
        return []


def hf_download(repo: str, filename: str) -> bool:
    from yume.utils import GGUF_DIR

    GGUF_DIR.mkdir(parents=True, exist_ok=True)
    return download_file(
        f"https://huggingface.co/{repo}/resolve/main/{filename}",
        GGUF_DIR / filename,
        f"Downloading {filename}",
    )


# ── API token discovery ────────────────────────────────────────────────────────


def discover_api_token(host: str, port: int) -> str | None:
    """Discover API token from .yume_token file or server /health endpoint."""
    global _api_token
    if _api_token is not None:
        return _api_token

    from yume.utils import BASE_DIR

    token_file = BASE_DIR / ".yume_token"
    if token_file.exists():
        try:
            _api_token = token_file.read_text(encoding="utf-8").strip()
            if _api_token:
                return _api_token
        except Exception as e:
            _log.debug("[discover_api_token] token-file-read failed: %s", e)

    try:
        req = urllib.request.Request(f"http://{host}:{port}/health", headers={"User-Agent": "Yume"})
        with urllib.request.urlopen(req, timeout=3) as resp:  # nosec B310
            data = json.loads(resp.read())
            token = data.get("api_token") or data.get("token")
            if token:
                _api_token = token
                return _api_token
    except Exception as e:
        _log.debug("[discover_api_token] health-check failed: %s", e)

    return None


def server_get(host: str, port: int, path: str, timeout: int = 5) -> dict | None:
    """GET request with API token auth."""
    try:
        headers = {"User-Agent": "Yume"}
        token = discover_api_token(host, port)
        if token:
            headers["X-API-Token"] = token
        req = urllib.request.Request(f"http://{host}:{port}{path}", headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310
            return json.loads(resp.read())
    except Exception:
        return None


def server_post(host: str, port: int, path: str, data: dict | None = None, timeout: int = 30) -> dict:
    """POST request with API token auth."""
    try:
        body = json.dumps(data or {}).encode("utf-8")
        headers = {"Content-Type": "application/json", "User-Agent": "Yume"}
        token = discover_api_token(host, port)
        if token:
            headers["X-API-Token"] = token
        req = urllib.request.Request(f"http://{host}:{port}{path}", data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310
            return json.loads(resp.read())
    except Exception as e:
        return {"error": str(e)}


def discover_servers(cfg: dict, backend_info: dict) -> dict:
    """Check if servers are already running."""
    results: dict = {}

    ws = check_server(cfg["whisper_host"], cfg["whisper_port"], "/health")
    results["whisper"] = ws["up"]

    bk = cfg.get("translation_backend", "llamacpp")
    bi = backend_info.get(bk, backend_info.get("custom", {"hp": HEALTH_PATH_OPENAI}))
    ts = check_server(cfg["translation_host"], cfg["translation_port"], bi["hp"])
    if not ts["up"]:
        ts = check_server(cfg["translation_host"], cfg["translation_port"], HEALTH_PATH_OPENAI)
    results["translation"] = ts["up"]

    default_ollama_port = 11434
    if not results["translation"] and bk == "ollama":
        for port in [default_ollama_port, 5000, 8080]:
            if port != cfg["translation_port"]:
                ts2 = check_server("127.0.0.1", port, HEALTH_PATH_OLLAMA)
                if ts2["up"]:
                    results["ollama_found"] = port
                    break

    return results
