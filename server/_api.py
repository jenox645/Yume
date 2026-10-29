"""HTTP API of the Yume server: the Flask app, its security checks, the routes.

Security: only browser-extension origins get CORS headers; every endpoint but
/health needs the per-session API token; the Host header must be localhost
(DNS rebinding). The work behind the routes lives in _jobs (subtitles),
_store (cache), _settings (config, blacklist) and _model (Whisper).
"""

import json
import os
import re
import secrets
import subprocess
import time
from pathlib import Path

from flask import Flask, jsonify, request

import _audio
import _jobs
import _model
import _separate
import _settings
import _state
import _translate
from _security import validate_url

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


# Generated at startup, written to .yume_token so the extension can discover it.
_state.API_TOKEN = secrets.token_urlsafe(32)


ALLOWED_HOSTS = {"127.0.0.1", "localhost"}


def token_ok(token):
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
        if not token_ok(request.headers.get("X-API-Token", "")):
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


def get_gpu_stats():
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
        "version": _state.SERVER_VERSION,
        "api": 2,  # 2 = job API (/jobs); the old per-chunk API is gone
        "ytdlp_available": _audio.check_ytdlp(),
    }
    if _state.load_error:
        base["error"] = _state.load_error
    if safe_caller:
        base["api_token"] = _state.API_TOKEN

    if token_ok(request.headers.get("X-API-Token", "")):
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
    s["gpu"] = get_gpu_stats()
    s["model"] = _state.model_name
    s["model_display_name"] = _state.model_display_name or ""
    s["device"] = _state.device
    s["compute_type"] = _state.compute_type
    s["vocal_isolation"] = vocal_isolation_status()

    return jsonify(s)


def vocal_isolation_status():
    if not _state.vocal_isolation:
        return "off"
    ok, why = _separate.available()
    return f"on ({_separate.MODEL}, CUDA)" if ok else f"unavailable: {why}"


@app.route("/model/switch", methods=["POST"])
def switch_model():
    """Hot-swap the Whisper model without restarting the server."""
    new_model, err = _requested_model((request.get_json(silent=True) or {}).get("model"))
    if err:
        return jsonify(err), 400
    if new_model == _state.model_name:
        return jsonify({"status": "already_loaded", "model": _state.model_name})
    if not _state.model_switch_lock.acquire(blocking=False):
        return jsonify({"error": "A model switch is already in progress"}), 409
    try:
        return _swap_model(new_model)
    finally:
        _state.model_switch_lock.release()


# Multilingual models only: *.en and distil-* models are English-only and
# cannot transcribe the languages Yume exists for.
_VALID_MODELS = ["tiny", "base", "small", "medium", "large-v1", "large-v2", "large-v3", "turbo", "large-v3-turbo"]


def _requested_model(name):
    """A model name or local CTranslate2 folder from a switch request → (model, None) or (None, error)."""
    if not name:
        return None, {"error": "Missing 'model' field"}
    if os.path.sep not in name and "/" not in name:
        if name not in _VALID_MODELS:
            return None, {"error": f"Unknown model: {name}", "valid": _VALID_MODELS}
        return ("large-v3-turbo" if name == "turbo" else name), None
    raw_path = Path(name)
    if not raw_path.is_absolute() or ".." in raw_path.parts:
        return None, {"error": "Model path must be absolute and must not contain '..'"}
    model_path = raw_path.resolve()
    if not model_path.is_dir():
        return None, {"error": f"Directory not found: {model_path}"}
    missing = [f for f in ("model.bin", "config.json") if not (model_path / f).exists()]
    if missing:
        return None, {"error": f"Not a valid CTranslate2 model — missing: {', '.join(missing)}"}
    return str(model_path), None


def _swap_model(new_model):
    """Load new_model, then swap it in (call with model_switch_lock held)."""
    old_model = _state.model_name
    print(f"[Yume] Switching model: {old_model} -> {new_model}")
    try:
        # Load outside transcribe_lock so in-flight transcriptions finish on the old model
        new_whisper = _model.load_whisper(new_model)
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
    _settings.persist_whisper_model(new_model)
    print(f"[Yume] Model switched to {_state.model_name}")
    return jsonify({"status": "ok", "model": _state.model_name, "previous": old_model})


@app.route("/translation/models", methods=["GET"])
def list_translation_models():
    """Query the translation backend for available models."""
    _settings.reload_config()
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


# Whisper language codes are 2-3 lowercase letters ("ja", "yue", "haw")
_LANG_RE = re.compile(r"^[a-z]{2,3}$")


def _job_params(data):
    """(params for JobManager.create, None), or (None, error message) for a bad request.

    Durations and the playhead are sanitized by the job itself (_jobs.seconds)."""
    url = str(data.get("url") or "")
    stream_url = str(data.get("stream_url") or "")
    if not url and not stream_url:
        return None, "Missing url"
    for u in (url, stream_url):
        valid, err = validate_url(u) if u else (True, "")
        if not valid:
            return None, f"Invalid URL: {err}"
    language = str(data.get("language") or "").lower()
    if language in ("", "auto"):
        language = None
    elif not _LANG_RE.match(language):
        return None, f"Invalid language code: {language!r}"
    return {
        "video_id": data.get("video_id"),
        "url": url,
        "stream_url": stream_url,
        "language": language,
        "target": str(data.get("target") or "").strip()[:40] or None,
        "romanize": bool(data.get("romanize")),
        "title": str(data.get("title") or ""),
        "duration": data.get("duration"),
        "playhead": data.get("t"),
    }, None


def _job_or_404(job_id):
    job = _state.jobs.get(job_id) if _state.jobs else None
    if job is None:
        return None, (jsonify({"error": "Unknown job (server restarted or model switched) — create it again"}), 404)
    return job, None


@app.route("/jobs", methods=["POST"])
def create_job():
    """Start (or rejoin) the subtitle job for one video + language pair."""
    _settings.reload_config()  # vocal_isolation may have changed
    if _state.load_error:
        return jsonify({"error": f"Whisper could not load its model: {_state.load_error}"}), 503
    params, err = _job_params(request.get_json(silent=True) or {})
    if err:
        return jsonify({"error": err}), 400
    try:
        job = _state.jobs.create(params)
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


def _set_blacklist(items):
    clean = _settings.save_blacklist(items)
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
    except Exception as e:  # anything else was an HTML 500 the health check could not read
        detail = str(e) if isinstance(e, _translate.TranslationError) else f"{type(e).__name__}: {e}"
        return jsonify({"success": False, "error": detail}), 502


@app.route("/cache/clear", methods=["POST"])
def clear_cache():
    """Wipe the durable cache (transcripts, translations, library) and live jobs."""
    _state.store.clear()
    with _state.cache_lock:
        _state.stream_url_cache.clear()
    _state.jobs.drop_all("Cache cleared")
    return jsonify({"success": True})
