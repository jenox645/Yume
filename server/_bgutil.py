"""bgutil-ytdlp-pot-provider server lifecycle management.

bgutil generates YouTube PO tokens so yt-dlp can download age-gated / bot-checked
videos without requiring cookies.  It runs as a local HTTP server on port 4416.

Architecture:
  yt-dlp → bgutil plugin (pip) → HTTP request to 127.0.0.1:4416
  bgutil server (deno)          → runs BotGuard JS → returns PO token
"""

import os
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
    # Output goes to a file: an undrained PIPE fills up after a while and the
    # server then blocks forever on its next log line (PO tokens stop coming).
    log_dir = Path(__file__).parent.parent / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / "bgutil_server.log"
    try:
        log = open(log_path, "w", encoding="utf-8", errors="replace")  # noqa: SIM115 — owned by the child
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
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        log.close()  # the child has its own handle

        for _i in range(30):
            time.sleep(1)
            if is_bgutil_server_ready():
                print(f"  bgutil server:    ready on port {BGUTIL_PORT} (PO token generation active)")
                return True
            if _bgutil_proc.poll() is not None:
                tail = ""
                try:
                    tail = log_path.read_text(encoding="utf-8", errors="replace")[-300:]
                except OSError:
                    pass
                print(f"  bgutil server:    process exited with code {_bgutil_proc.returncode}")
                if tail:
                    print(f"  bgutil server:    output: {tail}")
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
