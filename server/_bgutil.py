"""bgutil-ytdlp-pot-provider server lifecycle management.

bgutil generates YouTube PO tokens so yt-dlp can download age-gated / bot-checked
videos without requiring cookies.  It runs as a local HTTP server on port 4416.

Architecture:
  yt-dlp → bgutil plugin (pip) → HTTP request to 127.0.0.1:4416
  bgutil server (deno)          → runs BotGuard JS → returns PO token
"""

import os
import shutil
import subprocess
import time
from pathlib import Path


BGUTIL_PORT = 4416

_bgutil_proc = None  # Subprocess handle for the managed bgutil server


def bgutil_server_dir():
    """Return the path to the bgutil server source directory."""
    return Path(__file__).parent.parent / "tools" / "bgutil-ytdlp-pot-provider" / "server"


def is_bgutil_server_ready():
    """Return True if the bgutil HTTP server is responding on port 4416."""
    try:
        import urllib.request

        resp = urllib.request.urlopen(f"http://127.0.0.1:{BGUTIL_PORT}/ping", timeout=3)  # noqa: S5332 — loopback only, no TLS needed
        return resp.status == 200
    except Exception:
        return False


def setup_bgutil_server():
    """Download and set up the bgutil server if not already present.

    Downloads the repo as a zip from GitHub, extracts it, and runs deno install.
    Returns True if the server directory is ready.
    """
    server_dir = bgutil_server_dir()
    main_ts = server_dir / "src" / "main.ts"

    if main_ts.exists():
        print(f"  bgutil server:    found at {server_dir}")
        return True

    print("  bgutil server:    not found — downloading...")
    repo_parent = server_dir.parent.parent  # tools/
    repo_parent.mkdir(parents=True, exist_ok=True)

    zip_path = repo_parent / "bgutil-ytdlp-pot-provider.zip"
    try:
        import urllib.request

        url = "https://github.com/Brainicism/bgutil-ytdlp-pot-provider/archive/refs/tags/1.3.1.zip"
        print("  bgutil server:    downloading from GitHub...")
        urllib.request.urlretrieve(url, str(zip_path))
    except Exception as e:
        print(f"  bgutil server:    download failed: {e}")
        return False

    try:
        import zipfile

        with zipfile.ZipFile(str(zip_path), "r") as zf:
            # Guard against zip-slip path traversal
            repo_parent_resolved = repo_parent.resolve()
            for member in zf.namelist():
                member_path = (repo_parent / member).resolve()
                if not str(member_path).startswith(str(repo_parent_resolved)):
                    print(f"  bgutil server:    unsafe path in archive: {member}")
                    return False
            zf.extractall(str(repo_parent))
        zip_path.unlink(missing_ok=True)

        extracted = repo_parent / "bgutil-ytdlp-pot-provider-1.3.1"
        target = repo_parent / "bgutil-ytdlp-pot-provider"
        if extracted.exists():
            if target.exists():
                shutil.rmtree(str(target))
            extracted.rename(target)
        print("  bgutil server:    extracted")
    except Exception as e:
        print(f"  bgutil server:    extract failed: {e}")
        zip_path.unlink(missing_ok=True)
        for _leftover in (repo_parent / "bgutil-ytdlp-pot-provider-1.3.1", repo_parent / "bgutil-ytdlp-pot-provider"):
            if _leftover.exists():
                shutil.rmtree(str(_leftover), ignore_errors=True)
        return False

    if not main_ts.exists():
        print(f"  bgutil server:    main.ts not found at {main_ts}")
        return False

    print("  bgutil server:    installing dependencies (deno install)...")
    try:
        r = subprocess.run(
            ["deno", "install", "--allow-scripts=npm:canvas", "--frozen"],
            cwd=str(server_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=180,
        )
        if r.returncode == 0:
            print("  bgutil server:    dependencies installed")
        else:
            r2 = subprocess.run(
                ["deno", "install", "--allow-scripts=npm:canvas"],
                cwd=str(server_dir),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=180,
            )
            if r2.returncode == 0:
                print("  bgutil server:    dependencies installed (without --frozen)")
            else:
                print(f"  bgutil server:    deno install failed: {(r2.stderr or '')[-200:]}")
                return False
    except Exception as e:
        print(f"  bgutil server:    deno install failed: {e}")
        return False

    return True


def start_bgutil_server():
    """Start the bgutil HTTP server on port 4416 as a background process.

    Returns True if the server starts and is ready within 30 s.
    """
    global _bgutil_proc

    if is_bgutil_server_ready():
        print(f"  bgutil server:    already running on port {BGUTIL_PORT}")
        return True

    server_dir = bgutil_server_dir()
    node_modules = server_dir / "node_modules"
    main_ts = server_dir / "src" / "main.ts"

    if not main_ts.exists():
        print("  bgutil server:    main.ts not found — cannot start")
        return False

    cwd = str(node_modules) if node_modules.exists() else str(server_dir)
    try:
        main_rel = os.path.relpath(str(main_ts), cwd)
    except ValueError:
        main_rel = str(main_ts)

    print(f"  bgutil server:    starting on port {BGUTIL_PORT}...")
    try:
        _bgutil_proc = subprocess.Popen(
            [
                "deno",
                "run",
                "--no-prompt",
                "--allow-env",
                "--allow-net",
                "--allow-ffi=.",
                "--allow-read=.",
                "--allow-sys",
                main_rel,
                "--port",
                str(BGUTIL_PORT),
            ],
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

        for _i in range(30):
            time.sleep(1)
            if is_bgutil_server_ready():
                print(f"  bgutil server:    ready on port {BGUTIL_PORT} (PO token generation active)")
                return True
            if _bgutil_proc.poll() is not None:
                stderr = ""
                try:
                    stderr = (
                        _bgutil_proc.stderr.read().decode("utf-8", errors="replace")[-300:]
                        if _bgutil_proc.stderr
                        else ""
                    )
                except Exception:
                    pass
                print(f"  bgutil server:    process exited with code {_bgutil_proc.returncode}")
                if stderr:
                    print(f"  bgutil server:    stderr: {stderr}")
                _bgutil_proc = None
                return False

        print(f"  bgutil server:    timed out waiting for port {BGUTIL_PORT}")
        return False

    except Exception as e:
        print(f"  bgutil server:    start failed: {e}")
        return False


def stop_bgutil_server():
    """Stop the bgutil server on exit."""
    global _bgutil_proc
    if _bgutil_proc and _bgutil_proc.poll() is None:
        try:
            _bgutil_proc.terminate()
            _bgutil_proc.wait(timeout=5)
        except Exception:
            try:
                _bgutil_proc.kill()
            except Exception:
                pass
    _bgutil_proc = None
