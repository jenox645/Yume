"""Server startup chores: arguments, logging, console handlers, the banner,
the yt-dlp self-update, Deno auth, the API token file, the web server thread,
and the job pipeline. main() in faster_whisper_server.py calls them in order.
"""

import atexit
import logging
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import _bgutil
import _jobs
import _settings
import _state
import _store
import _translate
import _ytdlp


def force_utf8_output():
    """UTF-8 + line-buffered output.

    Windows consoles/log pipes default to cp932/cp1252 — Japanese text in logs
    would raise UnicodeEncodeError. And when the CLI launches the server its
    stdout is a log FILE, which Python block-buffers: the CLI's "View Logs" and
    crash diagnosis read stale logs, and a hard crash (CUDA abort) loses the
    last lines. Done in main(), not at import: rebinding sys.stdout at import
    time breaks anything that imports this module (pytest)."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        except (AttributeError, ValueError):
            pass


def cleanup_stale_temps():
    """Clean orphaned yume_* temp dirs from previous crashed instances."""
    tmp = tempfile.gettempdir()
    cleaned = 0
    try:
        for entry in os.listdir(tmp):
            if entry.startswith("yume_") and os.path.isdir(os.path.join(tmp, entry)):
                path = os.path.join(tmp, entry)
                try:
                    age = time.time() - os.path.getmtime(path)
                    if age > 3600:
                        shutil.rmtree(path, ignore_errors=True)
                        cleaned += 1
                except Exception:
                    pass
    except Exception:
        pass
    if cleaned:
        print(f"[Yume] Startup cleanup: removed {cleaned} stale temp dirs")


def shutdown_handler(signum, frame):
    """Handle SIGTERM/SIGINT — clean up and exit gracefully."""
    print(f"\n[Yume] Received signal {signum}, cleaning up...")
    if _state.TOKEN_FILE and os.path.exists(_state.TOKEN_FILE):
        try:
            os.unlink(_state.TOKEN_FILE)
        except Exception:
            pass
    sys.exit(0)


def setup_windows_console_handler():
    """Register a Windows CTRL_CLOSE_EVENT handler to prevent MKL abort."""
    if sys.platform != "win32":
        return
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        CTRL_CLOSE_EVENT = 2
        CTRL_LOGOFF_EVENT = 5
        CTRL_SHUTDOWN_EVENT = 6

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_ulong)
        def _win_console_handler(event):
            if event in (CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT):
                shutdown_handler(event, None)
                return True
            return False

        kernel32.SetConsoleCtrlHandler(_win_console_handler, True)
        _state._win_console_handler_ref = _win_console_handler  # prevent GC
    except Exception:
        pass


def parse_args():
    import argparse

    parser = argparse.ArgumentParser(description="Yume Whisper Server")
    parser.add_argument("--model", default="large-v3-turbo")
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--compute-type", default="float16")
    parser.add_argument("--port", type=int, default=5001)
    parser.add_argument("--config", type=str, default=None)
    parser.add_argument("--prewarm", action="store_true")
    parser.add_argument("--low-vram", action="store_true", help="Force int8 compute type to reduce VRAM usage")
    parser.add_argument("--verbose", "-v", action="store_true")
    return parser.parse_args()


def configure_logging(args):
    if args.verbose or os.environ.get("LOG_LEVEL", "").upper() == "DEBUG":
        logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    else:
        logging.basicConfig(level=logging.WARNING)
        logging.getLogger("werkzeug").setLevel(logging.ERROR)


def init_pipeline(args):
    """Open the durable cache and start the job workers."""
    cfg_dir = Path(args.config).parent if args.config else Path(__file__).resolve().parent.parent / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    _state.store = _store.Store(cfg_dir / "yume_cache.db")
    atexit.register(_state.store.close)
    _state.jobs = _jobs.JobManager(_translate.Translator(_settings.translation_settings))


def print_startup_banner(args):
    print("=" * 70)
    print(f"  YUME -- Whisper Server v{_state.SERVER_VERSION}")
    print("=" * 70)
    print(f"  Model:            {_state.model_name}")
    print(f"  Device:           {_state.device}")
    print(f"  Compute Type:     {_state.compute_type}")
    print(f"  Port:             {args.port}")
    print("  Pipeline:         jobs (download once, quiet-point regions, cached)")
    print("  Whisper Params:   music-tuned (beam 5, no word timestamps)")
    print("  VAD Filter:       OFF (required for music)")
    yt_info = _state.youtube_auth_method
    if _state.youtube_auth_method == "cookies":
        yt_info += f" ({_state.cookies_browser})"
    print(f"  YouTube Auth:     {yt_info}")

    # Romanization availability (import-only, no dict load — kakasi dict loads in background thread)
    print(f"  Python exe:       {sys.executable}")
    roma_parts = []
    try:
        import importlib.util

        if importlib.util.find_spec("fugashi") and importlib.util.find_spec("unidic_lite"):
            roma_parts.append("ja(cutlet, by word)")
        else:
            import pykakasi  # noqa: F401

            roma_parts.append("ja(pykakasi)")
    except Exception as e:
        import site

        paths = site.getsitepackages() if hasattr(site, "getsitepackages") else ["(no site-packages)"]
        print(f"  [roma] pykakasi import failed: {type(e).__name__}: {e}")
        print(f"  [roma] site-packages: {paths[0] if paths else '?'}")
    try:
        from pypinyin import pinyin  # noqa: F401

        roma_parts.append("zh(pypinyin)")
    except ImportError:
        pass
    if roma_parts:
        print(f"  Romanization:     {', '.join(roma_parts)} (instant)")
    else:
        print("  Romanization:     LLM-only (pip install -r server/requirements.txt for instant)")
    print("=" * 70)

    # Tool version checks — deferred to background so they don't block model load
    def _check_tool_versions():
        results = []
        try:
            if not _ytdlp.check_ytdlp():
                results.append(("yt-dlp", None, ["  WARNING: yt-dlp not found in PATH!"]))
            else:
                v = subprocess.run([*_ytdlp.ytdlp_cmd(), "--version"], capture_output=True, text=True, timeout=10)
                version = v.stdout.strip() or "?"
                results.append(("yt-dlp", maybe_update_ytdlp(version), None))
        except Exception as exc:
            results.append(("yt-dlp", None, [f"  WARNING: yt-dlp check failed: {exc}"]))
        try:
            if not shutil.which("ffmpeg"):
                results.append(
                    (
                        "ffmpeg",
                        None,
                        [
                            "  WARNING: ffmpeg not found in PATH!",
                            "  Audio slicing will fail. Run the setup wizard to install ffmpeg.",
                        ],
                    )
                )
            else:
                v = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, timeout=10)
                ffver = v.stdout.split("\n")[0].split(" ")[2] if v.returncode == 0 else "?"
                results.append(("ffmpeg", ffver, None))
        except Exception as exc:
            results.append(("ffmpeg", None, [f"  WARNING: ffmpeg check failed: {exc}"]))
        for tool, version, warnings in results:
            if version is not None:
                print(f"  {tool + ':':18s}{version}")
            elif warnings:
                for line in warnings:
                    print(line)

    threading.Thread(target=_check_tool_versions, daemon=True).start()


YTDLP_MAX_AGE_DAYS = 30


def maybe_update_ytdlp(version):
    """Self-update the standalone yt-dlp when it is older than a month (YouTube
    changes break old versions: a 7-month-old one got HTTP 403 on every video).
    pip installs (Deno auth) are left to the CLI. Returns the version to show."""
    age = _release_age(version)
    exe = shutil.which("yt-dlp")
    if age is None or age <= YTDLP_MAX_AGE_DAYS or not exe or _state.youtube_auth_method == "deno":
        return version
    # Releases can be weeks apart: ask at most once a day, not on every start
    stamp = Path(exe).with_name(".yt-dlp-update-check")
    if _stamped_today(stamp):
        return version
    print(f"  yt-dlp:           {version} is {age} days old — checking for an update...")
    return _update_ytdlp(exe, version, stamp)


def _release_age(version):
    """Days since the release of a yt-dlp version ("2026.08.19"), None if it is not a date."""
    import datetime

    try:
        released = datetime.date(*map(int, version.split(".")[:3]))
    except ValueError:
        return None
    return (datetime.date.today() - released).days


def _stamped_today(stamp):
    try:
        return time.time() - stamp.stat().st_mtime < 86400
    except OSError:
        return False


def _update_ytdlp(exe, version, stamp):
    try:
        r = subprocess.run([exe, "-U"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=180)
        v = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=10).stdout.strip()
        if r.returncode == 0:
            stamp.touch()
            return f"{v} (updated from {version})" if v and v != version else f"{version} (latest)"
        tail = (r.stdout + r.stderr).strip().splitlines()[-1:] or ["no output"]
        print(f"  yt-dlp:           update failed: {tail[0][:150]}")
    except (OSError, subprocess.TimeoutExpired) as e:
        print(f"  yt-dlp:           update failed: {e}")
    return f"{version} (could not update — Tools → yt-dlp)"


def setup_deno_auth(args):
    """Check deno availability and start bgutil server if youtube_auth_method == 'deno'."""
    if _state.youtube_auth_method != "deno":
        return

    if not _deno_runs():
        _switch_to_cookies()
        return

    # The server never installs packages or downloads code at startup (that used
    # to happen silently, unpinned, on every launch). Setup is the CLI's job:
    # Tools -> Deno installs Deno, pip yt-dlp, the PO-token plugin and its server.
    missing = _missing_deno_packages()
    if missing:
        print(f"  WARNING: Deno auth needs {', '.join(missing)} — run: python pocket_yume.py (Tools -> Deno)")

    if _bgutil.start_bgutil_server():
        atexit.register(_bgutil.stop_bgutil_server)
    else:
        print("  bgutil server:    not running — YouTube downloads will fall back to cookies")


def _deno_runs():
    try:
        r = subprocess.run(["deno", "--version"], capture_output=True, text=True, timeout=5)
    except Exception:
        return False
    if r.returncode != 0:
        return False
    print(f"  Deno:             {r.stdout.split(chr(10))[0].strip()}")
    return True


def _switch_to_cookies():
    print("")
    print("  WARNING: youtube_auth_method is 'deno' but Deno is not installed.")
    print("  Auto-switching to 'cookies' auth.")
    print("  To fix: install Deno (https://deno.land), or set youtube_auth_method='cookies'.")
    _state.youtube_auth_method = "cookies"
    if _state.cookies_browser == "chrome" and platform.system() != "Windows":
        _state.cookies_browser = "firefox"
    print(f"  Now using: cookies ({_state.cookies_browser})")
    print("")


def _missing_deno_packages():
    missing = []
    try:
        import yt_dlp  # noqa: F401 — pip yt-dlp is what discovers the plugin
    except ImportError:
        missing.append("yt-dlp (pip)")
    try:
        import yt_dlp_plugins.extractor.getpot_bgutil  # noqa: F401  # type: ignore[import-not-found]
    except ImportError:
        missing.append("bgutil-ytdlp-pot-provider")
    return missing


def write_token_file(args):
    """Write the API token to .yume_token before model load (extension needs it early)."""
    base_dir = Path(__file__).parent.parent.resolve()
    _state.TOKEN_FILE = str(base_dir / ".yume_token")
    try:
        if os.path.exists(_state.TOKEN_FILE):
            os.unlink(_state.TOKEN_FILE)  # a leftover may have looser permissions
        fd = os.open(_state.TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(_state.API_TOKEN)
        print(f"  API token:        written to {_state.TOKEN_FILE}")
    except Exception as e:
        print(f"  API token:        file write FAILED ({e})")
        print("                    Extension may not auto-discover the server.")
    print("  Security:         Host validation + API token + URL validation")


def start_web_server(app, port):
    """Start the Flask/Waitress server in a daemon thread. Returns the thread."""

    def _start_server():
        try:
            from waitress import serve

            print("  Server:           Waitress (production)")
            print("")
            serve(app, host="127.0.0.1", port=port, threads=4, channel_timeout=300, recv_bytes=65536)
        except ImportError:
            print("  Server:           Flask dev (install waitress for production)")
            print("")
            app.run(host="127.0.0.1", port=port, debug=False, threaded=True)

    t = threading.Thread(target=_start_server, daemon=True)
    t.start()
    return t
