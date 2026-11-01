#!/usr/bin/env python3
"""
Yume -- Faster-Whisper Server

Runs the whole subtitle pipeline for the browser extension: downloads a video's
audio (yt-dlp/ffmpeg), transcribes it region by region with faster-whisper,
translates and romanizes the lines through a local LLM, and caches everything
in SQLite. The extension creates a job and polls it (see _jobs.py).

Flask starts before the model loads; /health reports loading/ready/error.

Modules: _api (Flask app, routes, security), _settings (config, blacklist),
_model (Whisper), _startup (startup chores), _jobs (the pipeline), _download,
_stream, _ytdlp, _audio, _regions, _filter, _translate, _romanize, _separate, _store.
"""

import signal
import sys
import threading

try:
    import faster_whisper  # noqa: F401
except ImportError:
    print("ERROR: faster-whisper not installed!")
    print("Run: pip install faster-whisper")
    sys.exit(1)

import _api
import _model
import _romanize
import _settings
import _startup


def _report_vocal_isolation():
    print(f"[Yume] Vocal isolation: {_api.vocal_isolation_status()}")


def main():
    _startup.force_utf8_output()
    _startup.cleanup_stale_temps()
    signal.signal(signal.SIGTERM, _startup.shutdown_handler)
    signal.signal(signal.SIGINT, _startup.shutdown_handler)

    _startup.setup_windows_console_handler()

    args = _startup.parse_args()
    _startup.configure_logging(args)
    _settings.apply_config(args)
    _startup.init_pipeline(args)

    _startup.print_startup_banner(args)

    _startup.setup_deno_auth(args)
    _startup.write_token_file(args)

    server_thread = _startup.start_web_server(_api.app, args.port)

    print("")
    print(f"  Listening on http://localhost:{args.port}  (status: loading)")  # noqa: S5332 — display only, loopback
    print("  Loading Whisper model in background thread...")
    print("")

    # Pre-load the Japanese romanizer in the background to hide its startup cost
    # (Windows Defender scans each dictionary file: 30-120 s for pykakasi).
    threading.Thread(target=_romanize.preload, daemon=True).start()
    # Importing torch takes seconds: check vocal isolation now, not in the first job
    threading.Thread(target=_report_vocal_isolation, daemon=True).start()

    # Load model in a background thread so the server is immediately responsive.
    # /health returns {"status": "loading"} until the model is ready.
    # Transcription endpoints return 503 while the model is loading.
    threading.Thread(target=_model.load_model, args=(args,), daemon=True, name="model-loader").start()

    # Block the main thread to keep the process alive (Flask runs in server_thread).
    try:
        server_thread.join()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
