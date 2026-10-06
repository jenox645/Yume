#!/usr/bin/env python3
"""
Yume -- Faster-Whisper Server

Runs the whole subtitle pipeline for the browser extension: downloads a video's
audio (yt-dlp/ffmpeg), transcribes it region by region with faster-whisper,
translates and romanizes the lines through a local LLM, and caches everything
in SQLite. The extension creates a job and polls it (see _jobs.py).

Flask starts before the model loads; /health reports loading/ready/error.
"""

import atexit
import json
import logging
import math
import os
import platform
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, request

try:
    from faster_whisper import WhisperModel
except ImportError:
    print("ERROR: faster-whisper not installed!")
    print("Run: pip install faster-whisper")
    sys.exit(1)

import _state
import _audio
import _bgutil
import _jobs
import _romanize
import _separate
import _store
import _translate
from _security import validate_url

# Must match pocket_yume.VERSION and extension/manifest.json (tests check it)
SERVER_VERSION = "0.1.0"

# ── Flask app ─────────────────────────────────────────────────────────────────
app = Flask(__name__)
# Every endpoint takes small JSON (URLs, ids, text lines) — cap request bodies.
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024

# Trusted origins for CORS — browser extensions only. Web pages, including ones
# served from localhost, have no business calling this server.
_CORS_ORIGINS = ("chrome-extension://", "moz-extension://")
_CORS_HEADERS = "Content-Type, X-API-Token"
_CORS_METHODS = "GET, POST, OPTIONS"


def _is_trusted_origin(origin) -> bool:
    """True for browser-extension origins (any Chromium or Firefox extension)."""
    return bool(origin) and origin.startswith(_CORS_ORIGINS)


# ── Security: shared secret token ────────────────────────────────────────────
# Generated at startup, written to .yume_token so the extension can discover it.
_state.API_TOKEN = secrets.token_urlsafe(32)

ALLOWED_HOSTS = {"127.0.0.1", "localhost"}


def _token_ok(token):
    """Constant-time token check. Compares bytes: compare_digest raises
    TypeError on non-ASCII str, which turned a bad header into a 500."""
    return bool(token) and secrets.compare_digest(token.encode("utf-8"), _state.API_TOKEN.encode("utf-8"))


@app.before_request
def _security_checks():
    """Host header + API token validation.  Blocks DNS rebinding and CSRF."""
    if request.method == "OPTIONS":
        # Validate origin before echoing CORS headers for preflight
        origin = request.headers.get("Origin")
        if not _is_trusted_origin(origin):
            return jsonify({"error": "Forbidden: untrusted origin"}), 403
        return "", 204

    host = request.host.split(":")[0].lower()
    if host not in ALLOWED_HOSTS:
        print(f"[Yume] BLOCKED: DNS rebinding attempt from Host: {request.host}")
        return jsonify({"error": "Forbidden: invalid host"}), 403

    if request.path not in ("/health", "/favicon.ico"):
        if not _token_ok(request.headers.get("X-API-Token", "")):
            print(f"[Yume] BLOCKED: invalid/missing API token on {request.method} {request.path}")
            return jsonify({"error": "Forbidden: invalid token"}), 403

    return None


@app.after_request
def _add_cors_headers(response):
    """Attach CORS headers to every response for trusted origins only."""
    origin = request.headers.get("Origin")
    if _is_trusted_origin(origin):
        response.headers["Access-Control-Allow-Origin"] = origin
        response.headers["Access-Control-Allow-Methods"] = _CORS_METHODS
        response.headers["Access-Control-Allow-Headers"] = _CORS_HEADERS
        response.headers["Vary"] = "Origin"
    return response


# ── Startup cleanup ───────────────────────────────────────────────────────────


def _cleanup_stale_temps():
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


def _shutdown_handler(signum, frame):
    """Handle SIGTERM/SIGINT — clean up and exit gracefully."""
    print(f"\n[Yume] Received signal {signum}, cleaning up...")
    if _state.TOKEN_FILE and os.path.exists(_state.TOKEN_FILE):
        try:
            os.unlink(_state.TOKEN_FILE)
        except Exception:
            pass
    sys.exit(0)


# ── GPU stats ─────────────────────────────────────────────────────────────────


def _get_gpu_stats():
    """Get GPU VRAM and utilisation via nvidia-smi. Returns dict or None."""
    try:
        result = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=memory.used,memory.total,utilization.gpu,temperature.gpu,name",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            # One line per GPU — report the first. Fields can read "[N/A]"
            # (laptops, some drivers): those become None instead of failing.
            parts = [p.strip() for p in result.stdout.strip().splitlines()[0].split(",", 4)]
            if len(parts) >= 5:

                def _int(v):
                    return int(v) if v.isdigit() else None

                if _int(parts[0]) is None or _int(parts[1]) is None:
                    return None
                return {
                    "vram_used_mb": _int(parts[0]),
                    "vram_total_mb": _int(parts[1]),
                    "gpu_util_pct": _int(parts[2]),
                    "gpu_temp_c": _int(parts[3]),
                    "gpu_name": parts[4],
                }
    except Exception:
        pass
    return None


# ── Routes: health & status ───────────────────────────────────────────────────


@app.route("/health", methods=["GET"])
def health():
    """Minimal unauthenticated response for discovery; full info for token holders."""
    origin = request.headers.get("Origin", "")
    safe_caller = (not origin) or origin.startswith("chrome-extension://") or origin.startswith("moz-extension://")

    is_ready = _state.model is not None
    if _state.load_error:
        status = "error"
    else:
        status = "ready" if is_ready else "loading"
    base = {
        "status": status,
        "version": SERVER_VERSION,
        "api": 2,  # 2 = job API (/jobs); the old per-chunk API is gone
        "ytdlp_available": _audio.check_ytdlp(),
    }
    if _state.load_error:
        base["error"] = _state.load_error
    if safe_caller:
        base["api_token"] = _state.API_TOKEN

    if _token_ok(request.headers.get("X-API-Token", "")):
        base.update(
            {
                "model": _state.model_name,
                "device": _state.device,
                "compute_type": _state.compute_type,
                "translation_backend": _state.translation_backend,
                "translation_address": f"{_state.translation_host}:{_state.translation_port}",
            }
        )

    if status == "error":
        return jsonify(base), 500
    return jsonify(base), (200 if is_ready else 503)


@app.route("/stats", methods=["GET"])
def stats():
    """Session statistics + live GPU info for the popup dashboard."""
    with _state.stats_lock:
        s = dict(_state.server_stats)

    uptime = time.time() - s["start_time"]
    s["uptime_seconds"] = round(uptime)
    s["uptime_human"] = f"{int(uptime // 3600)}h{int((uptime % 3600) // 60)}m"

    if s["regions_transcribed"] > 0:
        s["avg_whisper_time"] = round(s["total_whisper_time"] / s["regions_transcribed"], 1)
    else:
        s["avg_whisper_time"] = 0

    s["library_size"] = len({v["video_key"] for v in _state.store.library()}) if _state.store else 0
    s.update(_state.jobs.stats() if _state.jobs else {"jobs": 0, "active": 0})
    s["blacklist_size"] = len(_state.user_blacklist)
    s["gpu"] = _get_gpu_stats()
    s["model"] = _state.model_name
    s["model_display_name"] = _state.model_display_name or ""
    s["device"] = _state.device
    s["compute_type"] = _state.compute_type
    s["vocal_isolation"] = _vocal_isolation_status()

    return jsonify(s)


def _vocal_isolation_status():
    if not _state.vocal_isolation:
        return "off"
    ok, why = _separate.available()
    return f"on ({_separate.MODEL}, CUDA)" if ok else f"unavailable: {why}"


def _report_vocal_isolation():
    print(f"[Yume] Vocal isolation: {_vocal_isolation_status()}")


# ── Route: model hot-swap ─────────────────────────────────────────────────────


@app.route("/model/switch", methods=["POST"])
def switch_model():
    """Hot-swap the Whisper model without restarting the server."""
    data = request.get_json(silent=True) or {}
    new_model = data.get("model")
    if not new_model:
        return jsonify({"error": "Missing 'model' field"}), 400

    # Multilingual models only: *.en and distil-* models are English-only and
    # cannot transcribe the languages Yume exists for.
    valid_models = ["tiny", "base", "small", "medium", "large-v1", "large-v2", "large-v3", "turbo", "large-v3-turbo"]

    is_local_path = os.path.sep in new_model or "/" in new_model
    if is_local_path:
        raw_path = Path(new_model)
        if not raw_path.is_absolute() or ".." in raw_path.parts:
            return jsonify({"error": "Model path must be absolute and must not contain '..'"}), 400
        model_path = raw_path.resolve()
        if not model_path.is_dir():
            return jsonify({"error": f"Directory not found: {model_path}"}), 400
        required = ["model.bin", "config.json"]
        missing = [f for f in required if not (model_path / f).exists()]
        if missing:
            return jsonify({"error": f"Not a valid CTranslate2 model — missing: {', '.join(missing)}"}), 400
        new_model = str(model_path)
    elif new_model not in valid_models:
        return jsonify({"error": f"Unknown model: {new_model}", "valid": valid_models}), 400

    if new_model == "turbo":
        new_model = "large-v3-turbo"

    if new_model == _state.model_name:
        return jsonify({"status": "already_loaded", "model": _state.model_name})

    if not _state.model_switch_lock.acquire(blocking=False):
        return jsonify({"error": "A model switch is already in progress"}), 409

    try:
        old_model = _state.model_name
        print(f"[Yume] Switching model: {old_model} -> {new_model}")

        try:
            # Load outside transcribe_lock so in-flight transcriptions finish on the old model
            new_whisper = WhisperModel(new_model, device=_state.device, compute_type=_state.compute_type)
        except Exception as e:
            # The old model was never touched — it keeps serving; no rollback needed
            print(f"[Yume] Model switch failed (still on {old_model}): {e}")
            return jsonify({"error": f"Switch failed: {str(e)}", "model": old_model}), 500

        with _state.transcribe_lock:
            _state.model_name = new_model
            _state.model = new_whisper
            # The friendly name from config described the OLD model
            _state.model_display_name = ""
        # Jobs belong to the old model; clients recreate theirs on the next poll
        # (404) and get transcripts for the new model.
        if _state.jobs:
            _state.jobs.drop_all(f"Whisper model switched to {new_model}")
        _persist_whisper_model(new_model)
        print(f"[Yume] Model switched to {_state.model_name}")
        return jsonify({"status": "ok", "model": _state.model_name, "previous": old_model})
    finally:
        _state.model_switch_lock.release()


def _persist_whisper_model(model):
    """Save a switched model to the config file, so the next start (launcher or
    one-click) loads it instead of reverting to the old one."""
    path = _state.config_file
    if not path:
        return
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
        cfg["whisper_model"] = model
        cfg["whisper_model_name"] = ""
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
        _state.config_mtime = os.path.getmtime(path)  # our own write: nothing to reload
    except (OSError, ValueError) as e:
        print(f"[Yume] Could not save the model choice to the config: {e}")


# ── Route: translation model discovery ───────────────────────────────────────


@app.route("/translation/models", methods=["GET"])
def list_translation_models():
    """Query the translation backend for available models."""
    _reload_translation_config()
    url = f"http://{_state.translation_host}:{_state.translation_port}"  # noqa: S5332 — local LLM backend
    models = []

    try:
        if _state.translation_backend == "ollama":
            import urllib.request

            req = urllib.request.Request(f"http://{_state.translation_host}:{_state.translation_port}/api/tags")  # noqa: S5332 — local LLM backend
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
                for m in data.get("models", []):
                    models.append({"id": m["name"], "name": m["name"], "size": m.get("size", 0)})
        else:
            import urllib.request

            req = urllib.request.Request(f"{url}/v1/models")
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
                for m in data.get("data", []):
                    models.append({"id": m.get("id", "?"), "name": m.get("id", "?")})
    except Exception as e:
        print(f"[Yume] Model list query failed: {e}")

    gguf_dir = Path(__file__).parent.parent / "models" / "translation"
    ggufs = []
    if gguf_dir.exists():
        for f in gguf_dir.glob("*.gguf"):
            ggufs.append({"name": f.name, "size_mb": round(f.stat().st_size / (1024 * 1024), 1)})

    return jsonify(
        {
            "backend": _state.translation_backend,
            "translation_url": url,
            "models": models,
            "local_ggufs": ggufs,
            "note": (
                "llama.cpp requires server restart to switch models" if _state.translation_backend == "llamacpp" else ""
            ),
        }
    )


# ── Routes: subtitle jobs ─────────────────────────────────────────────────────

# Whisper language codes are 2-3 lowercase letters ("ja", "yue", "haw")
_LANG_RE = re.compile(r"^[a-z]{2,3}$")
_MAX_DURATION_S = 24 * 3600


def _job_or_404(job_id):
    job = _state.jobs.get(job_id) if _state.jobs else None
    if job is None:
        return None, (jsonify({"error": "Unknown job (server restarted or model switched) — create it again"}), 404)
    return job, None


@app.route("/jobs", methods=["POST"])
def create_job():
    """Start (or rejoin) the subtitle job for one video + language pair."""
    _reload_translation_config()  # vocal_isolation may have changed
    if _state.load_error:
        return jsonify({"error": f"Whisper could not load its model: {_state.load_error}"}), 503
    data = request.get_json(silent=True) or {}
    url = str(data.get("url") or "")
    stream_url = str(data.get("stream_url") or "")
    if not url and not stream_url:
        return jsonify({"error": "Missing url"}), 400
    for u in (url, stream_url):
        if u:
            valid, err = validate_url(u)
            if not valid:
                return jsonify({"error": f"Invalid URL: {err}"}), 400
    language = str(data.get("language") or "").lower()
    if language in ("", "auto"):
        language = None
    elif not _LANG_RE.match(language):
        return jsonify({"error": f"Invalid language code: {language!r}"}), 400
    target = str(data.get("target") or "").strip()[:40] or None
    try:
        duration = float(data.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    # Live streams report Infinity; a bogus value would plan endless regions
    if not math.isfinite(duration) or duration < 0 or duration > _MAX_DURATION_S:
        duration = 0.0
    try:
        playhead = float(data.get("t") or 0)
    except (TypeError, ValueError):
        playhead = 0.0
    if not math.isfinite(playhead) or playhead < 0 or playhead > _MAX_DURATION_S:
        playhead = 0.0
    try:
        job = _state.jobs.create(
            {
                "video_id": data.get("video_id"),
                "url": url,
                "stream_url": stream_url,
                "language": language,
                "target": target,
                "romanize": bool(data.get("romanize")),
                "title": str(data.get("title") or ""),
                "duration": duration,
                "playhead": playhead,
            }
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify(job.snapshot(0, events=True))


@app.route("/jobs/<job_id>", methods=["GET"])
def get_job(job_id):
    """Poll a job: segments changed since `since`; `t` is the playhead (priority)."""
    job, err = _job_or_404(job_id)
    if err:
        return err
    try:
        since = int(request.args.get("since", 0) or 0)
        playhead = request.args.get("t")
        playhead = float(playhead) if playhead not in (None, "") else None
    except (TypeError, ValueError):
        return jsonify({"error": "since must be an integer and t a number"}), 400
    _state.jobs.poll(job, playhead)
    return jsonify(job.snapshot(since, events=request.args.get("events") == "1"))


@app.route("/jobs/<job_id>/options", methods=["POST"])
def job_options(job_id):
    job, err = _job_or_404(job_id)
    if err:
        return err
    data = request.get_json(silent=True) or {}
    if "romanize" in data:
        _state.jobs.set_romanize(job, bool(data["romanize"]))
    return jsonify({"success": True})


@app.route("/jobs/<job_id>/export", methods=["GET"])
def job_export(job_id):
    job, err = _job_or_404(job_id)
    if err:
        return err
    fmt = "vtt" if request.args.get("format") == "vtt" else "srt"
    with job.lock:
        segments = [dict(s) for s in job.segments.values()]
    progress = job.snapshot(0)["progress"]
    content, count = _jobs.export_subtitles(segments, fmt)
    return jsonify({"content": content, "count": count, "format": fmt, "progress": progress})


# ── Routes: library (durable cache) ───────────────────────────────────────────


@app.route("/library", methods=["GET"])
def library():
    return jsonify({"videos": _state.store.library()})


@app.route("/library/export", methods=["GET"])
def library_export():
    a = request.args
    video_key, language, model = a.get("video_key", ""), a.get("language", "auto"), a.get("model", "")
    if not video_key or not model:
        return jsonify({"error": "video_key and model are required"}), 400
    fmt = "vtt" if a.get("format") == "vtt" else "srt"
    content, count = _jobs.library_export(video_key, language, model, a.get("target") or None, fmt)
    return jsonify({"content": content, "count": count, "format": fmt})


@app.route("/library/delete", methods=["POST"])
def library_delete():
    video_key = str((request.get_json(silent=True) or {}).get("video_key") or "")
    if not video_key:
        return jsonify({"error": "video_key is required"}), 400
    _state.store.delete_video(video_key)
    return jsonify({"success": True})


# ── Routes: hallucination blacklist ───────────────────────────────────────────


def _set_blacklist(items):
    """Replace, persist and re-apply the user blacklist — the single source of
    truth for both the CLI and the extension popup."""
    seen = set()
    clean = []
    for item in items:
        s = str(item).strip()
        if s and s.lower() not in seen:
            seen.add(s.lower())
            clean.append(s)
    _state.user_blacklist = clean
    try:
        if _state.blacklist_file:
            with open(_state.blacklist_file, "w", encoding="utf-8") as f:
                json.dump(clean, f, ensure_ascii=False, indent=2)
    except OSError as e:
        print(f"[Yume] Blacklist save failed: {e}")
    if _state.jobs:
        _state.jobs.refilter()
    print(f"[Yume] User blacklist: {len(clean)} items")
    return jsonify({"success": True, "count": len(clean), "blacklist": clean})


@app.route("/blacklist", methods=["GET"])
def get_blacklist():
    return jsonify({"blacklist": _state.user_blacklist, "count": len(_state.user_blacklist)})


@app.route("/blacklist/update", methods=["POST"])
def update_blacklist():
    incoming = (request.get_json(silent=True) or {}).get("blacklist", [])
    if not isinstance(incoming, list):
        return jsonify({"error": "blacklist must be a list"}), 400
    return _set_blacklist(incoming)


@app.route("/blacklist/add", methods=["POST"])
def blacklist_add():
    text = str((request.get_json(silent=True) or {}).get("text") or "").strip()
    if not text:
        return jsonify({"error": "text is required"}), 400
    return _set_blacklist([*_state.user_blacklist, text])


@app.route("/blacklist/remove", methods=["POST"])
def blacklist_remove():
    text = str((request.get_json(silent=True) or {}).get("text") or "").strip().lower()
    return _set_blacklist([b for b in _state.user_blacklist if b.lower() != text])


# ── Routes: translation backend ───────────────────────────────────────────────


@app.route("/translation/health", methods=["GET"])
def translation_health():
    """Is the configured LLM reachable? (The extension no longer talks to it.)"""
    return jsonify(_state.jobs.translator.health())


@app.route("/translation/test", methods=["POST"])
def translation_test():
    """Translate one sentence end-to-end — used by the CLI health check."""
    data = request.get_json(silent=True) or {}
    text = str(data.get("text") or "今日はいい天気ですね")[:500]
    try:
        out = _state.jobs.translator.translate_batch(
            [text], data.get("language") or "ja", data.get("target") or "English"
        )
        return jsonify({"success": True, "translation": out[0]})
    except _translate.TranslationError as e:
        return jsonify({"success": False, "error": str(e)}), 502


# ── Routes: cache management ──────────────────────────────────────────────────


@app.route("/cache/clear", methods=["POST"])
def clear_cache():
    """Wipe the durable cache (transcripts, translations, library) and live jobs."""
    _state.store.clear()
    with _state.cache_lock:
        _state.stream_url_cache.clear()
    _state.jobs.drop_all("Cache cleared")
    return jsonify({"success": True})


# ── main() ────────────────────────────────────────────────────────────────────


def _force_utf8_output():
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


def main():
    _force_utf8_output()
    _cleanup_stale_temps()
    signal.signal(signal.SIGTERM, _shutdown_handler)
    signal.signal(signal.SIGINT, _shutdown_handler)

    _setup_windows_console_handler()

    args = _parse_args()
    _configure_logging(args)
    _apply_config(args)
    _init_pipeline(args)

    _print_startup_banner(args)

    _setup_deno_auth(args)
    _write_token_file(args)

    server_thread = _start_flask_thread(args.port)

    print("")
    print(f"  Listening on http://localhost:{args.port}  (status: loading)")  # noqa: S5332 — display only, loopback
    print("  Loading Whisper model in background thread...")
    print("")

    # Pre-load kakasi dictionary in background to hide its 30-120 s startup cost
    # (Windows Defender scans each file in the dictionary).
    threading.Thread(target=lambda: _romanize.get_kakasi(), daemon=True).start()
    # Importing torch takes seconds: check vocal isolation now, not in the first job
    threading.Thread(target=_report_vocal_isolation, daemon=True).start()

    # Load model in a background thread so the server is immediately responsive.
    # /health returns {"status": "loading"} until the model is ready.
    # Transcription endpoints return 503 while the model is loading.
    threading.Thread(target=_load_model, args=(args,), daemon=True, name="model-loader").start()

    # Block the main thread to keep the process alive (Flask runs in server_thread).
    try:
        server_thread.join()
    except KeyboardInterrupt:
        pass


_TRANSLATION_KEYS = {
    "translation_host": "translation_host",
    "translation_port": "translation_port",
    "translation_backend": "translation_backend",
    "translation_model": "translation_model",
    "translation_prompt": "translation_prompt",
    "romanization_prompt": "romanization_prompt",
    "vocal_isolation": "vocal_isolation",  # not translation, but re-read live the same way
}


def _load_translation_config(cfg):
    """Copy the translation keys of a config dict into _state."""
    for key, attr in _TRANSLATION_KEYS.items():
        if key in cfg:
            setattr(_state, attr, cfg[key] if cfg[key] is not None else "")


def _reload_translation_config():
    """Re-read the config file when it changed (one stat() per LLM request), so
    translation settings edited in the CLI apply to the running server."""
    path = _state.config_file
    if not path:
        return
    try:
        mtime = os.path.getmtime(path)
        if mtime == _state.config_mtime:
            return
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
        _state.config_mtime = mtime
    except (OSError, ValueError):
        return  # missing or mid-write: keep the current settings
    before = _translation_settings(reload=False)
    _load_translation_config(cfg)
    if _translation_settings(reload=False) != before:
        print("[Yume] Translation settings reloaded from the config file")


def _translation_settings(reload=True):
    """Live translation config for the Translator (read on every request)."""
    if reload:
        _reload_translation_config()
    return {
        "host": _state.translation_host,
        "port": _state.translation_port,
        "backend": _state.translation_backend,
        "model": _state.translation_model,
        "prompt": _state.translation_prompt,
        "roma_prompt": _state.romanization_prompt,
    }


def _init_pipeline(args):
    """Open the durable cache and start the job workers."""
    cfg_dir = Path(args.config).parent if args.config else Path(__file__).resolve().parent.parent / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    _state.store = _store.Store(cfg_dir / "yume_cache.db")
    atexit.register(_state.store.close)
    _state.jobs = _jobs.JobManager(_translate.Translator(_translation_settings))


def _setup_windows_console_handler():
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
                _shutdown_handler(event, None)
                return True
            return False

        kernel32.SetConsoleCtrlHandler(_win_console_handler, True)
        _state._win_console_handler_ref = _win_console_handler  # prevent GC
    except Exception:
        pass


def _parse_args():
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


def _configure_logging(args):
    if args.verbose or os.environ.get("LOG_LEVEL", "").upper() == "DEBUG":
        logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    else:
        logging.basicConfig(level=logging.WARNING)
        logging.getLogger("werkzeug").setLevel(logging.ERROR)


def _apply_config(args):
    """Apply CLI args, then overlay config file values into _state."""
    _state.model_name = args.model
    _state.device = args.device
    _state.compute_type = args.compute_type

    if args.config and os.path.exists(args.config):
        with open(args.config, encoding="utf-8") as f:
            cfg = json.load(f)
        _state.model_name = cfg.get("whisper_model", _state.model_name)
        _state.model_display_name = cfg.get("whisper_model_name", "")
        # NOTE: whisper_device / whisper_compute_type are intentionally NOT loaded
        # from config here.  The CLI resolves "auto" → "cuda"/"cpu" before launching
        # the server and passes the resolved value via --device / --compute-type.
        args.port = cfg.get("whisper_port", args.port)
        _state.youtube_auth_method = cfg.get("youtube_auth_method", _state.youtube_auth_method)
        _state.cookies_browser = cfg.get("cookies_browser", _state.cookies_browser)
        _load_translation_config(cfg)
        _state.config_file = args.config
        _state.config_mtime = os.path.getmtime(args.config)

    # Persisted user blacklist — reported hallucinations must survive restarts
    cfg_dir = Path(args.config).parent if args.config else Path(__file__).resolve().parent.parent / "config"
    _state.blacklist_file = str(cfg_dir / "blacklist.json")
    try:
        with open(_state.blacklist_file, encoding="utf-8") as f:
            items = json.load(f)
        if isinstance(items, list):
            _state.user_blacklist = [str(i).strip() for i in items if str(i).strip()]
            if _state.user_blacklist:
                print(f"[Yume] Loaded {len(_state.user_blacklist)} blacklist items from {_state.blacklist_file}")
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[Yume] Blacklist load failed: {e}")

    # Resolve 'auto' device using CTranslate2's own detection. (This used to test
    # `"cuda" in get_supported_compute_types("cuda")` — a set of compute types like
    # {"float16", "int8"}, which never contains "cuda" — so "auto" always meant CPU.)
    if _state.device == "auto":
        try:
            import ctranslate2

            _state.device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
        except Exception:
            _state.device = "cpu"

    if _state.compute_type == "auto":
        _state.compute_type = "float16" if _state.device == "cuda" else "int8"

    # --low-vram forces int8 regardless of GPU — reduces VRAM by ~30-40%
    if getattr(args, "low_vram", False):
        _state.compute_type = "int8"
        print("  [low-vram]        compute_type overridden to int8")


def _print_startup_banner(args):
    print("=" * 70)
    print(f"  YUME -- Whisper Server v{SERVER_VERSION}")
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
        print("  Romanization:     LLM-only (pip install pykakasi pypinyin for instant)")
    print("=" * 70)

    # Tool version checks — deferred to background so they don't block model load
    def _check_tool_versions():
        results = []
        try:
            if not _audio.check_ytdlp():
                results.append(("yt-dlp", None, ["  WARNING: yt-dlp not found in PATH!"]))
            else:
                v = subprocess.run([*_audio.ytdlp_cmd(), "--version"], capture_output=True, text=True, timeout=10)
                version = v.stdout.strip() or "?"
                results.append(("yt-dlp", _maybe_update_ytdlp(version), None))
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


def _maybe_update_ytdlp(version):
    """Self-update the standalone yt-dlp when it is older than a month (YouTube
    changes break old versions: a 7-month-old one got HTTP 403 on every video).
    pip installs (Deno auth) are left to the CLI. Returns the version to show."""
    import datetime

    try:
        released = datetime.date(*map(int, version.split(".")[:3]))
    except ValueError:
        return version
    age = (datetime.date.today() - released).days
    exe = shutil.which("yt-dlp")
    if age <= YTDLP_MAX_AGE_DAYS or not exe or _state.youtube_auth_method == "deno":
        return version
    # Releases can be weeks apart: ask at most once a day, not on every start
    stamp = Path(exe).with_name(".yt-dlp-update-check")
    try:
        if time.time() - stamp.stat().st_mtime < 86400:
            return version
    except OSError:
        pass
    print(f"  yt-dlp:           {version} is {age} days old — checking for an update...")
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


def _setup_deno_auth(args):
    """Check deno availability and start bgutil server if youtube_auth_method == 'deno'."""
    if _state.youtube_auth_method != "deno":
        return

    deno_found = False
    try:
        r = subprocess.run(["deno", "--version"], capture_output=True, text=True, timeout=5)
        deno_found = r.returncode == 0
        if deno_found:
            print(f"  Deno:             {r.stdout.split(chr(10))[0].strip()}")
    except Exception:
        pass

    if not deno_found:
        print("")
        print("  WARNING: youtube_auth_method is 'deno' but Deno is not installed.")
        print("  Auto-switching to 'cookies' auth.")
        print("  To fix: install Deno (https://deno.land), or set youtube_auth_method='cookies'.")
        _state.youtube_auth_method = "cookies"
        if _state.cookies_browser == "chrome" and platform.system() != "Windows":
            _state.cookies_browser = "firefox"
        print(f"  Now using: cookies ({_state.cookies_browser})")
        print("")
        return

    # The server never installs packages or downloads code at startup (that used
    # to happen silently, unpinned, on every launch). Setup is the CLI's job:
    # Tools -> Deno installs Deno, pip yt-dlp, the PO-token plugin and its server.
    missing = []
    try:
        import yt_dlp  # noqa: F401 — pip yt-dlp is what discovers the plugin
    except ImportError:
        missing.append("yt-dlp (pip)")
    try:
        import yt_dlp_plugins.extractor.getpot_bgutil  # noqa: F401  # type: ignore[import-not-found]
    except ImportError:
        missing.append("bgutil-ytdlp-pot-provider")
    if missing:
        print(f"  WARNING: Deno auth needs {', '.join(missing)} — run: python pocket_yume.py (Tools -> Deno)")

    if _bgutil.start_bgutil_server():
        atexit.register(_bgutil.stop_bgutil_server)
    else:
        print("  bgutil server:    not running — YouTube downloads will fall back to cookies")


def _write_token_file(args):
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


def _start_flask_thread(port):
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


def _set_low_priority():
    """Lower this thread's scheduling priority to avoid PC stutter during model load."""
    try:
        if sys.platform == "win32":
            import ctypes

            THREAD_PRIORITY_BELOW_NORMAL = -1
            ctypes.windll.kernel32.SetThreadPriority(
                ctypes.windll.kernel32.GetCurrentThread(), THREAD_PRIORITY_BELOW_NORMAL
            )
        else:
            os.nice(10)
    except Exception:
        pass


def _load_model(args):
    """Load the Whisper model; handle errors with actionable messages."""
    _set_low_priority()
    try:
        _state.model = WhisperModel(_state.model_name, device=_state.device, compute_type=_state.compute_type)

        # On CUDA a 1 s warm-up is cheap and is the only way to find out NOW that
        # cuBLAS/cuDNN are missing (the model itself loads fine without them).
        if not (args.prewarm or _state.device == "cuda"):
            print("  Prewarm:          skipped (use --prewarm to enable)")
        else:
            import numpy as np

            _dummy = np.zeros(16000, dtype=np.float32)
            try:
                list(_state.model.transcribe(_dummy, language="en"))
                print("  Prewarm:          done (CUDA kernels compiled)")
            except Exception as pw_err:
                pw_msg = str(pw_err)
                cuda_lib_missing = any(
                    lib in pw_msg.lower()
                    for lib in ["cublas", "cudnn", "cudart", "cufft", "cusolver", "is not found or cannot be loaded"]
                )
                if cuda_lib_missing and _state.device == "cuda":
                    print(f"  Prewarm:          CUDA library missing: {pw_msg[:80]}")
                    print("")
                    print("  WARNING: CUDA libraries are incomplete.")
                    print("  The model loaded but inference requires cuBLAS/cuDNN.")
                    print("  Falling back to CPU mode automatically.")
                    print("")
                    try:
                        # Assign, never `del`: other threads read _state.model concurrently
                        _state.model = None
                        _state.device = "cpu"
                        _state.compute_type = "int8"
                        _state.model = WhisperModel(_state.model_name, device="cpu", compute_type="int8")
                        list(_state.model.transcribe(_dummy, language="en"))
                        print("  Prewarm:          done (CPU fallback)")
                    except Exception as cpu_err:
                        print(f"  Prewarm:          CPU fallback also failed: {cpu_err}")
                        _state.model = None
                        _state.load_error = f"CUDA libraries missing and CPU fallback failed: {cpu_err}"
                        return
                else:
                    print(f"  Prewarm:          skipped ({pw_err})")

        print("=" * 70)
        print("  MODEL LOADED -- Server ready")
        print(f"  Listening on http://localhost:{args.port}")  # noqa: S5332 — display only, loopback
        print("=" * 70)
        print("")

    except Exception as e:
        _print_model_load_error(e)
        # sys.exit() here would only end this loader thread and leave /health
        # reporting "loading" forever. Publish the failure instead: /health then
        # returns status "error" with the message, which the extension and the
        # CLI launcher both surface to the user.
        _state.load_error = str(e) or type(e).__name__


def _print_model_load_error(e):
    err_msg = str(e).lower()
    print("")
    print("=" * 70)
    print(f"  FAILED TO LOAD MODEL: {e}")
    print("=" * 70)

    if "out of memory" in err_msg or "oom" in err_msg or ("cuda" in err_msg and "memory" in err_msg):
        print("")
        print("  CAUSE: Your GPU doesn't have enough VRAM for this model.")
        print("")
        print("  SOLUTIONS (pick one):")
        print("    1. Use a smaller model:")
        print("       python pocket_yume.py setup      → change Whisper model to 'small' or 'base'")
        print("    2. Use CPU instead (slower but works):")
        print("       python pocket_yume.py setup      → set device to 'cpu'")
        print("    3. Or restart with: --device cpu --compute-type int8")
    elif "cublas" in err_msg or "cudnn" in err_msg or "cudart" in err_msg:
        print("")
        print("  CAUSE: CUDA libraries are missing or incomplete.")
        print("")
        print("  SOLUTIONS (pick one):")
        print("    1. Install CUDA Toolkit: https://developer.nvidia.com/cuda-toolkit")
        print("    2. Or switch to CPU mode (no CUDA needed):")
        print("       python pocket_yume.py setup      → set device to 'cpu'")
        print("    3. Or restart with: --device cpu --compute-type int8")
    elif "no module" in err_msg or "modulenotfound" in err_msg:
        missing = str(e).split("'")[1] if "'" in str(e) else "unknown"
        print("")
        print(f"  CAUSE: Missing Python package: {missing}")
        print("")
        print("  SOLUTION: Run the setup wizard to install dependencies:")
        print("    python pocket_yume.py setup")
        print(f"    Or manually: pip install {missing}")
    elif _state.ERR_NO_SUCH_FILE in err_msg or "filenotfound" in err_msg:
        print("")
        print("  CAUSE: Model files not found on disk.")
        print("")
        print("  SOLUTIONS:")
        print("    1. The model will auto-download on first run (needs internet)")
        print("    2. Check your internet connection and try again")
        print("    3. Or choose a different model: python pocket_yume.py settings")
    elif "permission" in err_msg or "access" in err_msg:
        print("")
        print("  CAUSE: Permission denied — can't access model files or GPU.")
        print("")
        print("  SOLUTIONS:")
        print("    1. Try running as Administrator (Windows) or with sudo (Linux/macOS)")
        print("    2. Check that the model directory is not read-only")
    else:
        print("")
        print("  GENERAL SOLUTIONS:")
        print("    1. Switch to CPU:  --device cpu --compute-type int8")
        print("    2. Use smaller model:  --model small  or  --model tiny")
        print("    3. Re-run setup:  python pocket_yume.py setup")
        print("    4. Check logs:  logs/whisper_server.log")

    print("")
    print("  Need help? Run: python pocket_yume.py health")
    print("")


if __name__ == "__main__":
    main()
