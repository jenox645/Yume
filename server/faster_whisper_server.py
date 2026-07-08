#!/usr/bin/env python3
"""
Yume -- Faster-Whisper Server v0.1.0
Word-level timestamps + pause re-splitting + security hardening
Parallel startup: Flask starts before model loads. Prewarm inference on load.
All output is ASCII-safe for Windows cp932/cp1252 locales.
"""

import atexit
import io
import json
import logging
import os
import platform
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

# === CRITICAL: Force UTF-8 stdout to avoid cp932 UnicodeEncodeError on Windows ===
if sys.platform == "win32":
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    except Exception:
        pass

from flask import Flask, g, jsonify, request

try:
    from faster_whisper import WhisperModel
except ImportError:
    print("ERROR: faster-whisper not installed!")
    print("Run: pip install faster-whisper")
    sys.exit(1)

import _state
import _audio
import _bgutil
import _filter
import _romanize
import _transcribe
from _security import validate_url

# ── Flask app ─────────────────────────────────────────────────────────────────
app = Flask(__name__)

# Trusted origins for CORS — only local servers and the browser extension.
_CORS_ORIGINS = frozenset(
    [
        "chrome-extension://",  # prefix match — any Chrome/Edge/Brave/Opera extension
        "moz-extension://",  # prefix match — any Firefox extension
    ]
)
_CORS_HEADERS = "Content-Type, X-API-Token"
_CORS_METHODS = "GET, POST, OPTIONS"


def _is_trusted_origin(origin) -> bool:
    """Return True for chrome-extension:// origins and all loopback origins."""
    if not origin:
        return False
    if any(origin.startswith(pfx) for pfx in _CORS_ORIGINS):
        return True
    # Accept http://localhost:* and http://127.0.0.1:* (loopback only)
    import urllib.parse

    parsed = urllib.parse.urlparse(origin)
    return parsed.scheme == "http" and parsed.hostname in ("localhost", "127.0.0.1")


# ── Security: shared secret token ────────────────────────────────────────────
# Generated at startup, written to .yume_token so the extension can discover it.
_state.API_TOKEN = secrets.token_urlsafe(32)

ALLOWED_HOSTS = {"127.0.0.1", "localhost"}


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
        token = request.headers.get("X-API-Token", "")
        if not secrets.compare_digest(token, _state.API_TOKEN):
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


@app.teardown_request
def _cleanup_request_temps(_exception):
    """Clean temp files created during this request (defence against unclean exits)."""
    for path in getattr(g, "_temp_files", []):
        try:
            if os.path.isfile(path):
                os.unlink(path)
            elif os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass


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
    _audio.cleanup_all_audio()
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
            parts = result.stdout.strip().split(", ")
            if len(parts) >= 5:
                return {
                    "vram_used_mb": int(parts[0].strip()),
                    "vram_total_mb": int(parts[1].strip()),
                    "gpu_util_pct": int(parts[2].strip()),
                    "gpu_temp_c": int(parts[3].strip()),
                    "gpu_name": parts[4].strip(),
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
    base = {
        "status": "ready" if is_ready else "loading",
        "version": "0.1.0",
        "prepare_supported": True,
        "ytdlp_available": _audio.check_ytdlp(),
    }
    if safe_caller:
        base["api_token"] = _state.API_TOKEN

    token = request.headers.get("X-API-Token", "")
    if token == _state.API_TOKEN:
        base.update(
            {
                "model": _state.model_name,
                "device": _state.device,
                "compute_type": _state.compute_type,
                "vad_filter": False,
                "translation_host": _state.translation_host,
                "translation_port": _state.translation_port,
                "translation_backend": _state.translation_backend,
                "translation_url": f"http://{_state.translation_host}:{_state.translation_port}",  # noqa: S5332 — local LLM backend, no TLS
                "translation_prompt": _state.translation_prompt,
                "romanization_prompt": _state.romanization_prompt,
            }
        )

    return jsonify(base), (200 if is_ready else 503)


@app.route("/stats", methods=["GET"])
def stats():
    """Session statistics + live GPU info for the popup dashboard."""
    with _state.stats_lock:
        s = dict(_state.server_stats)

    uptime = time.time() - s["start_time"]
    s["uptime_seconds"] = round(uptime)
    s["uptime_human"] = f"{int(uptime // 3600)}h{int((uptime % 3600) // 60)}m"

    if s["chunks_transcribed"] > 0:
        s["avg_whisper_time"] = round(s["total_whisper_time"] / s["chunks_transcribed"], 1)
    else:
        s["avg_whisper_time"] = 0

    with _state.prefetch_lock:
        s["subtitle_cache_size"] = len(_state.subtitle_cache)
    s["audio_cache_size"] = len(_state.full_audio_cache)
    s["blacklist_size"] = len(_state.user_blacklist)
    s["gpu"] = _get_gpu_stats()
    s["model"] = _state.model_name
    s["model_display_name"] = _state.model_display_name or ""
    s["device"] = _state.device
    s["compute_type"] = _state.compute_type

    return jsonify(s)


@app.route("/config", methods=["GET"])
def get_config():
    return jsonify(
        {
            "model": _state.model_name,
            "device": _state.device,
            "compute_type": _state.compute_type,
            "word_timestamps": _state.use_word_timestamps,
            "pause_threshold": _state.pause_threshold,
        }
    )


# ── Route: model hot-swap ─────────────────────────────────────────────────────


@app.route("/model/switch", methods=["POST"])
def switch_model():
    """Hot-swap the Whisper model without restarting the server."""
    data = request.get_json() or {}
    new_model = data.get("model")
    if not new_model:
        return jsonify({"error": "Missing 'model' field"}), 400

    valid_models = [
        "tiny",
        "tiny.en",
        "base",
        "base.en",
        "small",
        "small.en",
        "medium",
        "medium.en",
        "large-v1",
        "large-v2",
        "large-v3",
        "turbo",
        "large-v3-turbo",
        "distil-large-v2",
        "distil-large-v3",
    ]

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
        with _state.prefetch_lock:
            _state.subtitle_cache.clear()
        print(f"[Yume] Model switched to {_state.model_name}")
        return jsonify({"status": "ok", "model": _state.model_name, "previous": old_model})
    finally:
        _state.model_switch_lock.release()


# ── Route: translation model discovery ───────────────────────────────────────


@app.route("/translation/models", methods=["GET"])
def list_translation_models():
    """Query the translation backend for available models."""
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


# ── Routes: prepare (download full audio) ────────────────────────────────────


@app.route("/prepare", methods=["POST"])
def prepare():
    """Download full audio for a video. Called once before chunk transcription."""

    try:
        data = request.get_json()
        url = data.get("url")
        video_id = data.get("video_id", "unknown")

        if not url:
            return jsonify({"error": "Missing url"}), 400

        valid, err = validate_url(url)
        if not valid:
            return jsonify({"error": f"Invalid URL: {err}"}), 400

        with _state.cache_lock:
            cached = _state.full_audio_cache.get(video_id)
            if cached and time.time() - cached["timestamp"] < _state.FULL_AUDIO_TTL and os.path.exists(cached["path"]):
                print(f"[Yume] Full audio cache hit for {video_id} ({cached['duration']:.0f}s)")
                return jsonify({"status": "ready", "duration": cached["duration"], "cached": True})
            if cached:
                _audio.cleanup_audio_entry(cached)
                _state.full_audio_cache.pop(video_id, None)

        print(f"[Yume] Downloading full audio for {video_id}...")
        audio_path, error_msg = _audio.download_full_audio(url)

        if not audio_path:
            return jsonify({"error": error_msg or "Audio download failed"}), 500

        duration = _audio.get_audio_duration(audio_path)
        size_kb = os.path.getsize(audio_path) / 1024

        with _state.cache_lock:
            if len(_state.full_audio_cache) >= _state.AUDIO_CACHE_MAX:
                oldest = next(iter(_state.full_audio_cache))
                evicted = _state.full_audio_cache.pop(oldest, None)
                if evicted:
                    _audio.cleanup_audio_entry(evicted)
            _state.full_audio_cache[video_id] = {
                "path": audio_path,
                "duration": duration,
                "timestamp": time.time(),
            }

        print(f"[Yume] Full audio ready: {duration:.1f}s, {size_kb:.0f}KB")
        return jsonify({"status": "ready", "duration": duration, "cached": False})

    except Exception as e:
        import traceback

        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/prepare_direct", methods=["POST"])
def prepare_direct():
    """Download audio from a direct stream URL (m3u8, mp4, etc.)."""

    try:
        data = request.get_json()
        stream_url = data.get("stream_url")
        video_id = data.get("video_id", "direct-" + str(int(time.time())))

        if not stream_url:
            return jsonify({"error": "Missing stream_url"}), 400

        valid, err = validate_url(stream_url)
        if not valid:
            return jsonify({"error": f"Invalid URL: {err}"}), 400

        with _state.cache_lock:
            cached = _state.full_audio_cache.get(video_id)
            if cached and time.time() - cached["timestamp"] < _state.FULL_AUDIO_TTL and os.path.exists(cached["path"]):
                return jsonify({"status": "ready", "duration": cached["duration"], "cached": True})
            if cached:
                _audio.cleanup_audio_entry(cached)
                _state.full_audio_cache.pop(video_id, None)

        print(f"[Yume] Direct download: {stream_url[:120]}...")

        tmp_dir = tempfile.mkdtemp(prefix="yume_direct_")
        output_path = os.path.join(tmp_dir, "full_audio.wav")

        result = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-protocol_whitelist",
                _state.FFMPEG_PROTOCOL_WHITELIST,
                "-i",
                stream_url,
                "-vn",
                "-ar",
                "16000",
                "-ac",
                "1",
                "-f",
                "wav",
                output_path,
            ],
            capture_output=True,
            text=True,
            timeout=600,
        )

        if result.returncode != 0 or not os.path.exists(output_path) or os.path.getsize(output_path) < 10000:
            print("[Yume] ffmpeg failed, trying yt-dlp on stream URL...")
            output_template = os.path.join(tmp_dir, "full_audio.%(ext)s")
            result = subprocess.run(
                [
                    *_audio.ytdlp_cmd(),
                    "-x",
                    "--audio-format",
                    "wav",
                    "--postprocessor-args",
                    _state.FFMPEG_AUDIO_OPTS,
                    "--no-playlist",
                    "--no-cache-dir",
                    "--no-exec",
                    "-o",
                    output_template,
                    "--",
                    stream_url,
                ],
                capture_output=True,
                text=True,
                timeout=300,
            )
            if result.returncode != 0 or not os.path.exists(output_path):
                stderr = (result.stderr or "")[-300:]
                shutil.rmtree(tmp_dir, ignore_errors=True)
                return jsonify({"error": f"Direct download failed: {stderr[:200]}"}), 500

        duration = _audio.get_audio_duration(output_path)
        size_kb = os.path.getsize(output_path) / 1024

        with _state.cache_lock:
            if len(_state.full_audio_cache) >= _state.AUDIO_CACHE_MAX:
                oldest = next(iter(_state.full_audio_cache))
                evicted = _state.full_audio_cache.pop(oldest, None)
                if evicted:
                    _audio.cleanup_audio_entry(evicted)
            _state.full_audio_cache[video_id] = {
                "path": output_path,
                "duration": duration,
                "timestamp": time.time(),
            }

        print(f"[Yume] Direct audio ready: {duration:.1f}s, {size_kb:.0f}KB")
        return jsonify({"status": "ready", "duration": duration, "cached": False})

    except Exception as e:
        import traceback

        traceback.print_exc()
        _tmp = locals().get("tmp_dir")
        if _tmp and os.path.isdir(_tmp):
            shutil.rmtree(_tmp, ignore_errors=True)
        return jsonify({"error": str(e)}), 500


# ── Routes: transcription ─────────────────────────────────────────────────────


@app.route("/transcribe_url", methods=["POST"])
def transcribe_url():

    if _state.model is None:
        return jsonify({"error": "Model is still loading. Retry shortly."}), 503

    try:
        data = request.get_json()
        if not data or "url" not in data:
            return jsonify({"error": "Missing 'url' field"}), 400

        url = data["url"]
        video_id = data.get("video_id", "unknown")

        valid, err = validate_url(url)
        if not valid:
            return jsonify({"error": f"Invalid URL: {err}"}), 400

        try:
            chunk_index = int(data.get("chunk_index", 0))
            chunk_duration = int(data.get("chunk_duration", 30))
            step_size = int(data.get("step_size", 25))
        except (TypeError, ValueError):
            return jsonify({"error": "chunk_index, chunk_duration and step_size must be integers"}), 400
        if chunk_index < 0 or not (1 <= chunk_duration <= 300) or not (1 <= step_size <= 300):
            return jsonify({"error": "chunk_index/chunk_duration/step_size out of range"}), 400
        language = data.get("language") or None

        cache_key = f"{video_id}:{step_size}:{chunk_index}"

        with _state.prefetch_lock:
            if cache_key in _state.subtitle_cache:
                cached_result = _state.subtitle_cache[cache_key]
                if len(cached_result.get("segments", [])) > 0:
                    print(
                        f"[Yume] Cache hit for chunk {chunk_index} of {video_id} "
                        f"({len(cached_result['segments'])} segments)"
                    )
                    cached_result["cached"] = True
                    with _state.stats_lock:
                        _state.server_stats["cache_hits"] += 1
                    return jsonify(cached_result)
                else:
                    print(f"[Yume] Stale empty cache for chunk {chunk_index} — re-transcribing")
                    del _state.subtitle_cache[cache_key]

        whisper_start = chunk_index * step_size
        is_first_chunk = chunk_index == 0

        # Chunk 0 has no pre-roll audio: chunks 1+ start 5 s before their owned
        # region (the last 5 s of the previous chunk's window), giving Whisper
        # warm-up context.  Chunk 0 starts cold at t=0.  Extending its window by
        # 5 s gives Whisper proportionally more vocal content to work with when
        # a song opens with an instrumental intro, reducing false no-speech drops.
        whisper_duration = chunk_duration + (5 if is_first_chunk else 0)
        owned_start = whisper_start
        owned_end = whisper_start + step_size

        print(
            f"[Yume] Chunk {chunk_index}: whisper [{whisper_start}s-{whisper_start + whisper_duration}s], "
            f"owns [{owned_start}s-{owned_end}s]"
        )

        with _state.stats_lock:
            _state.server_stats["cache_misses"] += 1

        audio_path = None
        with _state.cache_lock:
            prepared = _state.full_audio_cache.get(video_id)
            prepared_path = prepared["path"] if prepared and os.path.exists(prepared["path"]) else None
        if prepared_path:
            audio_path = _audio.slice_audio(prepared_path, whisper_start, whisper_duration)

        if audio_path is None:
            print("[Yume] No prepared audio, falling back to stream download")
            audio_path = _audio.download_audio_segment(url, whisper_start, whisper_duration)

        if audio_path is None:
            return jsonify({"error": "Audio extraction failed"}), 500

        try:
            result = _transcribe.transcribe_file(audio_path, language, whisper_start, is_first_chunk=is_first_chunk)
        finally:
            try:
                os.unlink(audio_path)
                parent = os.path.dirname(audio_path)
                if parent and os.path.isdir(parent) and os.path.basename(parent).startswith("yume_") and parent != tempfile.gettempdir():
                    shutil.rmtree(parent, ignore_errors=True)
            except Exception:
                pass

        raw_count = len(result.get("segments", []))
        trimmed = [seg for seg in result.get("segments", []) if (owned_start - 0.3) <= seg["start"] < (owned_end + 0.5)]

        result["segments"] = trimmed
        result["text"] = " ".join(s["text"] for s in trimmed)
        result["cached"] = False

        if len(trimmed) > 0:
            with _state.prefetch_lock:
                if len(_state.subtitle_cache) >= _state.SUBTITLE_CACHE_MAX:
                    keys_to_remove = list(_state.subtitle_cache.keys())[
                        : len(_state.subtitle_cache) - _state.SUBTITLE_CACHE_MAX + 1
                    ]
                    for k in keys_to_remove:
                        del _state.subtitle_cache[k]
                _state.subtitle_cache[cache_key] = result

        print(
            f"[Yume] Chunk {chunk_index} done: {len(trimmed)} segments "
            f"(from {raw_count} raw, {'cached' if len(trimmed) > 0 else 'not cached — empty'})"
        )
        return jsonify(result)

    except Exception as e:
        import traceback

        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/transcribe", methods=["POST"])
def transcribe():

    if _state.model is None:
        return jsonify({"error": "Model is still loading. Retry shortly."}), 503

    try:
        import base64

        data = request.get_json()
        if not data or "audio" not in data:
            return jsonify({"error": "No audio data provided"}), 400

        audio_base64 = data["audio"]
        language = data.get("language") or None
        start_offset = float(data.get("start_offset", 0))

        try:
            audio_bytes = base64.b64decode(audio_base64)
        except Exception as e:
            return jsonify({"error": f"Invalid base64: {str(e)}"}), 400

        with tempfile.NamedTemporaryFile(delete=False, suffix=".webm") as f:
            f.write(audio_bytes)
            temp_path = f.name

        try:
            result = _transcribe.transcribe_file(temp_path, language, start_offset)
        finally:
            try:
                os.unlink(temp_path)
            except Exception:
                pass

        return jsonify(result)

    except Exception as e:
        import traceback

        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


# ── Routes: romanization ──────────────────────────────────────────────────────


@app.route("/romanize", methods=["POST"])
def romanize():
    """Deterministic romanization for ja/zh/ko. <5 ms vs 1-10 s for LLM."""

    data = request.get_json() or {}
    text = data.get("text", "").strip()
    lang = data.get("language") or None

    if not text:
        return jsonify({"romanization": "", "method": "empty"})

    result = None
    if lang == "ja":
        result = _romanize.romanize_japanese(text)
    elif lang == "zh":
        result = _romanize.romanize_chinese(text)
    elif lang == "ko":
        result = _romanize.romanize_korean(text)

    if result is not None:
        return jsonify({"romanization": result, "method": "deterministic", "language": lang})
    return jsonify({"supported": False, "language": lang}), 501


@app.route("/romanize_batch", methods=["POST"])
def romanize_batch():
    """Batch deterministic romanization — single round trip for N texts."""

    data = request.get_json() or {}
    texts = data.get("texts", [])
    lang = data.get("language") or None

    if not texts or not isinstance(texts, list):
        return jsonify({"romanizations": [], "method": "empty"})

    romanizer = None
    if lang == "ja":
        romanizer = _romanize.romanize_japanese
    elif lang == "zh":
        romanizer = _romanize.romanize_chinese
    elif lang == "ko":
        romanizer = _romanize.romanize_korean

    if romanizer is None:
        return jsonify({"supported": False, "language": lang}), 501

    results = []
    for text in texts:
        try:
            r = romanizer(text.strip()) if text.strip() else ""
            results.append(r or "")
        except Exception:
            results.append("")

    return jsonify({"romanizations": results, "method": "deterministic", "language": lang})


# ── Routes: hallucination filter ──────────────────────────────────────────────


@app.route("/hallucination_patterns", methods=["GET"])
def get_hallucination_patterns():
    """Server-authoritative hallucination patterns — client fetches at startup."""
    return jsonify(
        {
            "builtin": _filter.HALLUCINATION_PATTERNS,
            "credits": _filter.CREDITS_PATTERNS,
            "user_blacklist": _state.user_blacklist,
            "single_word_blocklist": _filter.SINGLE_WORD_BLOCKLIST,
            "repeat_threshold": 6,
            "concat_min_len": 4,
            "concat_coverage": 0.95,
        }
    )


@app.route("/blacklist/update", methods=["POST"])
def update_blacklist():
    data = request.get_json() or {}
    incoming = data.get("blacklist", [])
    if not isinstance(incoming, list):
        return jsonify({"error": "blacklist must be a list"}), 400
    _state.user_blacklist = [str(item).strip() for item in incoming if str(item).strip()]
    count = len(_state.user_blacklist)
    print(f"[Yume] User blacklist updated: {count} items")
    for item in _state.user_blacklist[:10]:
        print(f"[Yume]   - {item!r}")
    if count > 10:
        print(f"[Yume]   ... and {count - 10} more")
    # Persist so reported hallucinations survive server restarts
    try:
        if _state.blacklist_file:
            with open(_state.blacklist_file, "w", encoding="utf-8") as f:
                json.dump(_state.user_blacklist, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[Yume] Blacklist save failed: {e}")
    return jsonify({"success": True, "count": count})


@app.route("/blacklist", methods=["GET"])
def get_blacklist():
    return jsonify({"blacklist": _state.user_blacklist, "count": len(_state.user_blacklist)})


# ── Routes: cache management ──────────────────────────────────────────────────


@app.route("/cache/clear", methods=["POST"])
def clear_cache():
    with _state.prefetch_lock:
        count = len(_state.subtitle_cache)
        _state.subtitle_cache.clear()
    with _state.cache_lock:
        _state.stream_url_cache.clear()
        for _vid, entry in list(_state.full_audio_cache.items()):
            _audio.cleanup_audio_entry(entry)
        audio_count = len(_state.full_audio_cache)
        _state.full_audio_cache.clear()
    return jsonify({"cleared": count, "audio_cleared": audio_count})


@app.route("/cache/status", methods=["GET"])
def cache_status():
    with _state.prefetch_lock:
        keys = list(_state.subtitle_cache.keys())
    return jsonify({"chunks_cached": len(keys), "keys": keys})


# ── main() ────────────────────────────────────────────────────────────────────


def main():
    _cleanup_stale_temps()
    signal.signal(signal.SIGTERM, _shutdown_handler)
    signal.signal(signal.SIGINT, _shutdown_handler)
    atexit.register(_audio.cleanup_all_audio)

    _setup_windows_console_handler()

    args = _parse_args()
    _configure_logging(args)
    _apply_config(args)

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

    # Load model in a background thread so the server is immediately responsive.
    # /health returns {"status": "loading"} until the model is ready.
    # Transcription endpoints return 503 while the model is loading.
    threading.Thread(target=_load_model, args=(args, server_thread), daemon=True, name="model-loader").start()

    # Block the main thread to keep the process alive (Flask runs in server_thread).
    try:
        server_thread.join()
    except KeyboardInterrupt:
        pass


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
    parser.add_argument("--model", default="large-v3")
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--compute-type", default="float16")
    parser.add_argument("--port", type=int, default=5001)
    parser.add_argument("--no-word-timestamps", action="store_true")
    parser.add_argument("--pause-threshold", type=float, default=0.25)
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
    _state.use_word_timestamps = not args.no_word_timestamps
    _state.pause_threshold = args.pause_threshold

    if args.config and os.path.exists(args.config):
        with open(args.config, encoding="utf-8") as f:
            cfg = json.load(f)
        _state.model_name = cfg.get("whisper_model", _state.model_name)
        _state.model_display_name = cfg.get("whisper_model_name", "")
        # NOTE: whisper_device / whisper_compute_type are intentionally NOT loaded
        # from config here.  The CLI resolves "auto" → "cuda"/"cpu" before launching
        # the server and passes the resolved value via --device / --compute-type.
        _state.use_word_timestamps = cfg.get("word_timestamps", _state.use_word_timestamps)
        _state.pause_threshold = cfg.get("pause_threshold", _state.pause_threshold)
        args.port = cfg.get("whisper_port", args.port)
        _state.youtube_auth_method = cfg.get("youtube_auth_method", _state.youtube_auth_method)
        _state.cookies_browser = cfg.get("cookies_browser", _state.cookies_browser)
        _state.translation_host = cfg.get("translation_host", _state.translation_host)
        _state.translation_port = cfg.get("translation_port", _state.translation_port)
        _state.translation_backend = cfg.get("translation_backend", _state.translation_backend)
        _state.translation_prompt = cfg.get("translation_prompt", "")
        _state.romanization_prompt = cfg.get("romanization_prompt", "")

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

    # Resolve 'auto' device using CTranslate2's own detection (not torch)
    if _state.device == "auto":
        try:
            import ctranslate2

            _state.device = "cuda" if "cuda" in ctranslate2.get_supported_compute_types("cuda") else "cpu"
        except Exception:
            try:
                import torch

                _state.device = "cuda" if torch.cuda.is_available() else "cpu"
            except ImportError:
                try:
                    r = subprocess.run(["nvidia-smi"], capture_output=True, timeout=5)
                    _state.device = "cuda" if r.returncode == 0 else "cpu"
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
    print("  YUME -- Whisper Server v0.1.0")
    print("=" * 70)
    print(f"  Model:            {_state.model_name}")
    print(f"  Device:           {_state.device}")
    print(f"  Compute Type:     {_state.compute_type}")
    print(f"  Port:             {args.port}")
    print("  Architecture:     download-once + local slice")
    print("  Whisper Params:   v2.0.7 (proven for music)")
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
    try:
        from romanization import romanize  # noqa: F401  # type: ignore[import-not-found]

        roma_parts.append("ko(romanization)")
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
                results.append(("yt-dlp", v.stdout.strip() or "?", None))
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

    # Ensure pip yt-dlp is available (needed for plugin discovery)
    _ensure_pip_ytdlp()

    # Ensure bgutil PO token plugin is installed
    _ensure_bgutil_plugin()

    # Setup and start the bgutil HTTP server
    if _bgutil.setup_bgutil_server():
        _bgutil.start_bgutil_server()
        atexit.register(_bgutil.stop_bgutil_server)
    else:
        print("  bgutil server:    setup failed — YouTube downloads may fail")
        print("  bgutil server:    will try cookies fallback automatically")


def _ensure_pip_ytdlp():
    try:
        r = subprocess.run([sys.executable, "-m", "yt_dlp", "--version"], capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            print(f"  yt-dlp (pip):     {r.stdout.strip()}")
            return
    except Exception:
        pass
    print("  yt-dlp (pip):     not found — installing...")
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-q", "--no-warn-script-location", "yt-dlp"],
            capture_output=True,
            timeout=120,
        )
        print("  yt-dlp (pip):     installed")
    except Exception as e:
        print(f"  yt-dlp (pip):     install failed ({e})")


def _ensure_bgutil_plugin():
    bgutil_installed = False
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pip", "show", "bgutil-ytdlp-pot-provider"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        bgutil_installed = r.returncode == 0
    except Exception:
        pass

    if bgutil_installed:
        print("  PO Token plugin:  bgutil-ytdlp-pot-provider (active)")
        return

    print("  PO Token plugin:  not found — installing bgutil-ytdlp-pot-provider...")
    try:
        subprocess.run(
            [sys.executable, "-m", "pip", "install", "-q", "--no-warn-script-location", "bgutil-ytdlp-pot-provider"],
            capture_output=True,
            timeout=120,
        )
        print("  PO Token plugin:  installed (YouTube BotGuard bypass via Deno)")
    except Exception as e:
        print(f"  PO Token plugin:  install failed ({e})")
        print("  YouTube may require sign-in. Fallback: set youtube_auth_method='cookies'")


def _write_token_file(args):
    """Write the API token to .yume_token before model load (extension needs it early)."""
    base_dir = Path(__file__).parent.parent.resolve()
    _state.TOKEN_FILE = str(base_dir / ".yume_token")
    try:
        with open(_state.TOKEN_FILE, "w") as f:
            f.write(_state.API_TOKEN)
        os.chmod(_state.TOKEN_FILE, 0o600)
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


def _load_model(args, server_thread):
    """Load the Whisper model; handle errors with actionable messages."""
    _set_low_priority()
    try:
        _state.model = WhisperModel(_state.model_name, device=_state.device, compute_type=_state.compute_type)

        if not args.prewarm:
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
                        del _state.model
                        _state.device = "cpu"
                        _state.compute_type = "int8"
                        _state.model = WhisperModel(_state.model_name, device="cpu", compute_type="int8")
                        list(_state.model.transcribe(_dummy, language="en"))
                        print("  Prewarm:          done (CPU fallback)")
                    except Exception as cpu_err:
                        print(f"  Prewarm:          CPU fallback also failed: {cpu_err}")
                else:
                    print(f"  Prewarm:          skipped ({pw_err})")

        print("=" * 70)
        print("  MODEL LOADED -- Server ready")
        print(f"  Listening on http://localhost:{args.port}")  # noqa: S5332 — display only, loopback
        print("=" * 70)
        print("")

        server_thread.join()

    except Exception as e:
        _print_model_load_error(e)
        sys.exit(1)


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
