#!/usr/bin/env python3
"""
Yume -- Faster-Whisper Server v5.4.0
Word-level timestamps + pause re-splitting + security hardening
Parallel startup: Flask starts before model loads. Prewarm inference on load.
All output is ASCII-safe for Windows cp932/cp1252 locales.
"""

import os
import sys
import io
import base64
import tempfile
import argparse
import subprocess
import json
import time
import threading
import shutil
import atexit
import platform
import secrets
import signal
import re
from pathlib import Path
from urllib.parse import urlparse

# === CRITICAL: Force UTF-8 stdout to avoid cp932 UnicodeEncodeError on Windows ===
if sys.platform == "win32":
    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    except Exception:
        pass

from flask import Flask, request, jsonify, g
from flask_cors import CORS

try:
    from faster_whisper import WhisperModel
except ImportError:
    print("ERROR: faster-whisper not installed!")
    print("Run: pip install faster-whisper")
    sys.exit(1)

app = Flask(__name__)

# ============================================================================
# SECURITY: Shared secret token (defense against DNS rebinding & CSRF)
# Generated at startup, written to a token file the extension reads.
# ============================================================================
API_TOKEN = secrets.token_urlsafe(32)
TOKEN_FILE = None  # Set in main() after BASE_DIR is known

CORS(app, resources={
    r"/*": {
        "origins": ["chrome-extension://*", "http://localhost:*", "http://127.0.0.1:*"],
        "methods": ["GET", "POST", "OPTIONS"],
        "allow_headers": ["Content-Type", "X-API-Token"]
    }
})


# ============================================================================
# SECURITY: Request validation (Host header + API token)
# Defends against DNS rebinding (CVE-style) and CSRF from malicious pages.
# ============================================================================
ALLOWED_HOSTS = {"127.0.0.1", "localhost"}

@app.before_request
def _security_checks():
    # Skip preflight OPTIONS
    if request.method == "OPTIONS":
        return None

    # 1. Host header validation — blocks DNS rebinding
    host = request.host.split(":")[0].lower()
    if host not in ALLOWED_HOSTS:
        print(f"[Yume] BLOCKED: DNS rebinding attempt from Host: {request.host}")
        return jsonify({"error": "Forbidden: invalid host"}), 403

    # 2. API token validation — blocks blind CSRF from malicious pages
    # Health endpoint is exempt (extension uses it to discover server)
    if request.path not in ("/health", "/favicon.ico"):
        token = request.headers.get("X-API-Token", "")
        if token != API_TOKEN:
            print(f"[Yume] BLOCKED: invalid/missing API token on {request.method} {request.path}")
            return jsonify({"error": "Forbidden: invalid token"}), 403

    return None


# ============================================================================
# SECURITY: URL validation (defense against argument injection / CWE-88)
# ============================================================================
def _validate_url(url):
    """Validate a URL before passing to subprocess.
    Rejects: dash-leading strings, non-http(s) schemes, empty URLs.
    Returns (is_valid, error_message).
    """
    if not url or not isinstance(url, str):
        return False, "Empty or invalid URL"
    url = url.strip()
    if url.startswith("-"):
        return False, "URL cannot start with '-' (argument injection)"
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return False, f"Unsupported URL scheme: {parsed.scheme!r} (only http/https allowed)"
        if not parsed.netloc and not parsed.path:
            return False, "URL has no host or path"
    except Exception as e:
        return False, f"URL parse error: {e}"
    return True, ""


# ============================================================================
# SECURITY: Signal handlers + startup cleanup (temp file hardening)
# ============================================================================
def _cleanup_stale_temps():
    """Clean orphaned yume_* temp dirs from previous crashed instances."""
    tmp = tempfile.gettempdir()
    cleaned = 0
    try:
        for entry in os.listdir(tmp):
            if entry.startswith("yume_") and os.path.isdir(os.path.join(tmp, entry)):
                path = os.path.join(tmp, entry)
                # Only clean dirs older than 1 hour (avoid racing with active server)
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

# Run startup cleanup immediately
# _cleanup_stale_temps() called in main()

def _shutdown_handler(signum, frame):
    """Handle SIGTERM/SIGINT — clean up and exit gracefully."""
    print(f"\n[Yume] Received signal {signum}, cleaning up...")
    _cleanup_all_audio()
    # Remove token file
    if TOKEN_FILE and os.path.exists(TOKEN_FILE):
        try: os.unlink(TOKEN_FILE)
        except Exception: pass
    sys.exit(0)

# Signal handlers registered in main()


@app.teardown_request
def _cleanup_request_temps(exception):
    """Clean temp files created during this request (defense against unclean exits)."""
    for path in getattr(g, '_temp_files', []):
        try:
            if os.path.isfile(path):
                os.unlink(path)
            elif os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass

# Global state
model = None
batched_model = None    # BatchedInferencePipeline wrapper (optional, for multi-chunk)
model_name = "large-v3"
device = "cuda"
compute_type = "float16"
use_word_timestamps = True
pause_threshold = 0.25  # seconds of silence to split segments (0.25 = better for songs)

# Server-side cache (LRU-limited to prevent memory leaks on long sessions)
subtitle_cache = {}
SUBTITLE_CACHE_MAX = 2000
AUDIO_CACHE_MAX = 50
STREAM_URL_CACHE_MAX = 100  # ~2000 chunks * ~10KB avg = ~20MB max
prefetch_lock = threading.Lock()

# CRITICAL: Whisper model is NOT thread-safe. Concurrent transcribe() calls
# return corrupted/empty results. Serialize all transcription through this lock.
transcribe_lock = threading.Lock()

# Full audio cache: download once, slice locally for each chunk
# { video_id: { "path": str, "duration": float, "timestamp": float } }
full_audio_cache = {}  # bounded: max AUDIO_CACHE_MAX entries
FULL_AUDIO_TTL = 600  # 10 minutes


def _cleanup_audio_entry(entry):
    """Delete the temp directory for a cached audio file."""
    try:
        parent = os.path.dirname(entry.get('path', ''))
        if parent and os.path.isdir(parent) and 'yume' in parent.lower():
            shutil.rmtree(parent, ignore_errors=True)
    except Exception:
        pass


def _cleanup_all_audio():
    """Called at server shutdown — clean up all cached audio temp files."""
    for vid, entry in list(full_audio_cache.items()):
        _cleanup_audio_entry(entry)
    full_audio_cache.clear()

# atexit registered in main()

# Stream URL cache: avoids calling yt-dlp --get-url for every chunk
# { video_url: {"stream_url": "...", "timestamp": time.time()} }
stream_url_cache = {}  # bounded: max STREAM_URL_CACHE_MAX entries
STREAM_URL_TTL = 300  # 5 minutes (YouTube stream URLs expire)

# YouTube auth (set from config at startup)
youtube_auth_method = "deno"    # "deno" or "cookies"
cookies_browser = "chrome"

# Translation server info (read from config, reported in /health so extension can auto-discover)
translation_host = "127.0.0.1"
translation_port = 5000
translation_backend = "llamacpp"

# ============================================================================
# SESSION STATISTICS (accumulated, reset on server restart)
# ============================================================================

server_stats = {
    "start_time": time.time(),
    "chunks_transcribed": 0,
    "segments_produced": 0,
    "hallucinations_filtered": 0,
    "total_audio_seconds": 0.0,
    "total_whisper_time": 0.0,
    "downloads_completed": 0,
    "cache_hits": 0,
    "errors": 0,
    "last_chunk_whisper_time": 0.0,
    "last_chunk_segments": 0,
}
stats_lock = threading.Lock()


def _get_gpu_stats():
    """Get GPU VRAM and utilization via nvidia-smi. Returns dict or None."""
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total,utilization.gpu,temperature.gpu,name",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5
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


# ============================================================================
# HEALTH CHECK
# ============================================================================

@app.route('/health', methods=['GET'])
def health():
    # Minimal response for unauthenticated callers (bootstrap/discovery only)
    # Only expose token to Chrome extension or local callers (blocks CSRF from malicious sites)
    origin = request.headers.get("Origin", "")
    safe_caller = (not origin) or origin.startswith("chrome-extension://") or origin.startswith("moz-extension://")

    is_ready = model is not None
    base = {
        "status": "ready" if is_ready else "loading",
        "version": "5.4.0",
        "prepare_supported": True,
        "ytdlp_available": _check_ytdlp(),
    }
    if safe_caller:
        base["api_token"] = API_TOKEN

    # Full response only for authenticated callers — hides translation server details
    token = request.headers.get("X-API-Token", "")
    if token == API_TOKEN:
        base.update({
            "model": model_name,
            "device": device,
            "compute_type": compute_type,
            "vad_filter": False,
            "translation_host": translation_host,
            "translation_port": translation_port,
            "translation_backend": translation_backend,
            "translation_url": f"http://{translation_host}:{translation_port}",
        })

    # Return 503 while model is loading — extension checks response.ok
    status_code = 200 if is_ready else 503
    return jsonify(base), status_code


@app.route('/stats', methods=['GET'])
def stats():
    """Session statistics + live GPU info for the popup dashboard."""
    with stats_lock:
        s = dict(server_stats)

    uptime = time.time() - s["start_time"]
    s["uptime_seconds"] = round(uptime)
    s["uptime_human"] = f"{int(uptime // 3600)}h{int((uptime % 3600) // 60)}m"

    # Avg chunk time
    if s["chunks_transcribed"] > 0:
        s["avg_whisper_time"] = round(s["total_whisper_time"] / s["chunks_transcribed"], 1)
    else:
        s["avg_whisper_time"] = 0

    # Cache info
    with prefetch_lock:
        s["subtitle_cache_size"] = len(subtitle_cache)
    s["audio_cache_size"] = len(full_audio_cache)
    s["blacklist_size"] = len(user_blacklist)

    # GPU stats (None if no NVIDIA GPU)
    s["gpu"] = _get_gpu_stats()

    # Whisper model info
    s["model"] = model_name
    s["device"] = device
    s["compute_type"] = compute_type

    return jsonify(s)


@app.route('/model/switch', methods=['POST'])
def switch_model():
    """Hot-swap the Whisper model without restarting the server."""
    global model, model_name, device, compute_type

    data = request.get_json() or {}
    new_model = data.get('model')
    if not new_model:
        return jsonify({"error": "Missing 'model' field"}), 400

    valid_models = [
        'tiny', 'tiny.en', 'base', 'base.en', 'small', 'small.en',
        'medium', 'medium.en', 'large-v1', 'large-v2', 'large-v3',
        'turbo', 'large-v3-turbo',
        'distil-large-v2', 'distil-large-v3',
    ]
    if new_model not in valid_models:
        return jsonify({"error": f"Unknown model: {new_model}", "valid": valid_models}), 400

    # Normalize turbo alias
    if new_model == 'turbo':
        new_model = 'large-v3-turbo'

    if new_model == model_name:
        return jsonify({"status": "already_loaded", "model": model_name})

    old_model = model_name
    print(f"[Yume] Switching model: {old_model} -> {new_model}")

    try:
        with transcribe_lock:
            model_name = new_model
            model = WhisperModel(model_name, device=device, compute_type=compute_type)
            # Rebuild batched pipeline wrapper
            try:
                from faster_whisper import BatchedInferencePipeline
                batched_model = BatchedInferencePipeline(model=model)
            except (ImportError, Exception):
                batched_model = None

        # Clear subtitle cache (old model's results are stale)
        with prefetch_lock:
            subtitle_cache.clear()

        print(f"[Yume] Model switched to {model_name}")
        return jsonify({"status": "ok", "model": model_name, "previous": old_model})

    except Exception as e:
        print(f"[Yume] Model switch failed: {e}")
        # Try to reload old model
        try:
            with transcribe_lock:
                model_name = old_model
                model = WhisperModel(model_name, device=device, compute_type=compute_type)
        except Exception:
            pass
        return jsonify({"error": f"Switch failed: {str(e)}"}), 500


@app.route('/config', methods=['GET'])
def get_config():
    return jsonify({
        "model": model_name,
        "device": device,
        "compute_type": compute_type,
        "word_timestamps": use_word_timestamps,
        "pause_threshold": pause_threshold,
    })


_ytdlp_cache = {"available": None, "checked_at": 0}

def _check_ytdlp():
    """Check if yt-dlp is available (cached for 60s)."""
    now = time.time()
    if _ytdlp_cache["available"] is not None and now - _ytdlp_cache["checked_at"] < 60:
        return _ytdlp_cache["available"]
    try:
        result = subprocess.run(["yt-dlp", "--version"], capture_output=True, timeout=5)
        available = result.returncode == 0
    except Exception:
        available = False
    _ytdlp_cache["available"] = available
    _ytdlp_cache["checked_at"] = now
    return available


@app.route('/translation/models', methods=['GET'])
def list_translation_models():
    """Query the translation backend for available models."""
    url = f"http://{translation_host}:{translation_port}"
    models = []
    current = ""

    try:
        if translation_backend == "ollama":
            # Ollama: /api/tags
            import urllib.request
            req = urllib.request.Request(f"http://{translation_host}:{translation_port}/api/tags")
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
                for m in data.get("models", []):
                    models.append({"id": m["name"], "name": m["name"], "size": m.get("size", 0)})
        else:
            # OpenAI-compatible: /v1/models
            import urllib.request
            req = urllib.request.Request(f"{url}/v1/models")
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = json.loads(resp.read())
                for m in data.get("data", []):
                    models.append({"id": m.get("id", "?"), "name": m.get("id", "?")})
    except Exception as e:
        print(f"[Yume] Model list query failed: {e}")

    # Also list local GGUFs if applicable
    gguf_dir = Path(__file__).parent.parent / "models" / "translation"
    ggufs = []
    if gguf_dir.exists():
        for f in gguf_dir.glob("*.gguf"):
            ggufs.append({"name": f.name, "size_mb": round(f.stat().st_size / (1024*1024), 1)})

    return jsonify({
        "backend": translation_backend,
        "translation_url": url,
        "models": models,
        "local_ggufs": ggufs,
        "note": "llama.cpp requires server restart to switch models" if translation_backend == "llamacpp" else ""
    })


def _is_youtube_url(url):
    """Check if URL is a YouTube URL (for YouTube-specific args)."""
    if not url:
        return False
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or '').lower()
        yt_domains = {"youtube.com", "www.youtube.com", "youtu.be",
                      "youtube-nocookie.com", "www.youtube-nocookie.com",
                      "music.youtube.com", "m.youtube.com"}
        return host in yt_domains or host.endswith(".youtube.com")
    except Exception:
        return False


# ============================================================================
# PREPARE VIDEO - Download full audio once
# ============================================================================

@app.route('/prepare', methods=['POST', 'OPTIONS'])
def prepare():
    """Download full audio for a video. Called once before chunk transcription."""
    if request.method == 'OPTIONS':
        return '', 204

    try:
        data = request.get_json()
        url = data.get('url')
        video_id = data.get('video_id', 'unknown')

        if not url:
            return jsonify({"error": "Missing url"}), 400

        # SECURITY: Validate URL before passing to subprocess
        valid, err = _validate_url(url)
        if not valid:
            return jsonify({"error": f"Invalid URL: {err}"}), 400

        # Check cache
        cached = full_audio_cache.get(video_id)
        if cached and time.time() - cached['timestamp'] < FULL_AUDIO_TTL and os.path.exists(cached['path']):
            print(f"[Yume] Full audio cache hit for {video_id} ({cached['duration']:.0f}s)")
            return jsonify({"status": "ready", "duration": cached['duration'], "cached": True})

        # Clean up expired entry if it exists
        if cached:
            _cleanup_audio_entry(cached)
            full_audio_cache.pop(video_id, None)

        print(f"[Yume] Downloading full audio for {video_id}...")
        audio_path, error_msg = _download_full_audio(url)

        if not audio_path:
            return jsonify({"error": error_msg or "Audio download failed"}), 500

        duration = _get_audio_duration(audio_path)
        size_kb = os.path.getsize(audio_path) / 1024

        # Evict oldest if cache full
        if len(full_audio_cache) >= AUDIO_CACHE_MAX:
            oldest = next(iter(full_audio_cache))
            full_audio_cache.pop(oldest, None)
        full_audio_cache[video_id] = {
            "path": audio_path,
            "duration": duration,
            "timestamp": time.time()
        }

        print(f"[Yume] Full audio ready: {duration:.1f}s, {size_kb:.0f}KB")
        return jsonify({"status": "ready", "duration": duration, "cached": False})

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


def _friendlify_ytdlp_error(raw_error):
    """Translate raw yt-dlp errors into actionable user-facing messages."""
    lower = raw_error.lower()

    # YouTube DRM / bot detection / sign-in errors
    if 'drm protected' in lower:
        return ("YouTube blocked the download (DRM error). "
                "Fix: In yume_config.json set youtube_auth_method to 'cookies' "
                "and cookies_browser to your browser name (e.g. 'firefox'). "
                "Or paste a stream URL in the extension popup.")
    if 'sign in to confirm' in lower or 'confirm you' in lower:
        return ("YouTube requires sign-in to access this video. "
                "Fix: In yume_config.json set youtube_auth_method to 'cookies' "
                "and cookies_browser to your browser name.")
    if 'http error 403' in lower or '403 forbidden' in lower:
        if 'cloudflare' in lower:
            return ("Access denied (403) — Cloudflare anti-bot protection. "
                    "This site blocks automated downloads. "
                    "Try: copy the direct video/audio URL (often .m3u8 or .mp4) "
                    "from the browser's Network tab and paste it as a Custom Stream URL "
                    "in the Yume extension popup.")
        return ("Access denied (403). The site is blocking yt-dlp. "
                "For YouTube: try switching to cookie auth in yume_config.json. "
                "For other sites: use the Custom Stream URL option in the extension — "
                "open DevTools > Network > filter 'm3u8' or 'mp4' > copy the URL. "
                "Also try: pip install -U yt-dlp")
    if 'video unavailable' in lower or 'private video' in lower:
        return "This video is unavailable or private."
    if 'age' in lower and 'restricted' in lower:
        return ("Age-restricted video. "
                "Fix: Set youtube_auth_method to 'cookies' with a logged-in browser.")
    if 'geo' in lower and 'block' in lower:
        return "This video is not available in your region."

    # Network / connectivity
    if 'unable to download' in lower and ('webpage' in lower or 'player' in lower):
        return ("Cannot reach YouTube. Check your internet connection, "
                "or YouTube may be temporarily down.")
    if 'timed out' in lower or 'timeout' in lower:
        return "Download timed out — the video may be too long or the connection too slow."

    # yt-dlp itself
    if 'no such file' in lower and 'yt-dlp' in lower:
        return "yt-dlp is not installed. Run the Yume setup wizard to install it."
    if 'no video formats' in lower or 'requested format' in lower:
        return ("No compatible audio format found. Try updating yt-dlp: "
                "pip install -U yt-dlp")

    # Deno-specific
    if 'deno' in lower and ('not found' in lower or 'no such file' in lower):
        return ("Deno is not installed (needed for YouTube auth). "
                "Fix: Switch youtube_auth_method to 'cookies' in yume_config.json, "
                "or install Deno: https://deno.land/#installation")

    return raw_error


def _download_full_audio(url):
    """Download the complete audio track as 16kHz mono WAV.
    Strategy 1: yt-dlp (with multiple auth/format combos)
    Strategy 2: yt-dlp get-url → ffmpeg (stream URL extraction + direct download)
    Strategy 3: ffmpeg direct (for m3u8 / direct media URLs)
    Returns (path, None) on success or (None, error_message) on failure.
    """
    tmp_dir = tempfile.mkdtemp(prefix="yume_")
    output_template = os.path.join(tmp_dir, "full_audio.%(ext)s")
    output_path = os.path.join(tmp_dir, "full_audio.wav")
    last_error = "Unknown error"

    # Build list of yt-dlp strategies to try in order
    is_yt = _is_youtube_url(url)
    strategies = []

    # Base auth: cookies only (no player_client override) — most compatible
    cookie_args = []
    if youtube_auth_method == "cookies":
        cookie_args = ["--cookies-from-browser", _resolve_browser_cookies()]

    if is_yt:
        # Strategy A: cookies + default player (most compatible)
        strategies.append(("cookies+default", [*cookie_args]))
        # Strategy B: cookies + tv,web player (works for some restricted videos)
        strategies.append(("cookies+tv,web", [
            "--extractor-args", "youtube:player_client=tv,web", *cookie_args
        ]))
        # Strategy C: cookies + mweb player (mobile fallback)
        strategies.append(("cookies+mweb", [
            "--extractor-args", "youtube:player_client=mweb", *cookie_args
        ]))
        # Strategy D: no auth at all (works for non-restricted videos)
        if cookie_args:
            strategies.append(("no-auth", []))
    else:
        strategies.append(("default", [*cookie_args]))

    # ---- Strategy 1: yt-dlp download (try multiple auth combos) ----
    for label, extra_args in strategies:
        for fmt_pass, fmt_args in [("bestaudio", ["-f", "bestaudio*/best"]), ("nofmt", [])]:
            try:
                tag = f"{label}/{fmt_pass}"
                print(f"[Yume] Trying yt-dlp ({tag}): {url[:80]}...")
                result = subprocess.run(
                    [
                        "yt-dlp",
                        *fmt_args,
                        "-x", "--audio-format", "wav",
                        "--postprocessor-args", "ffmpeg:-ar 16000 -ac 1",
                        "--no-playlist", "--no-cache-dir",
                        "--no-exec",
                        *extra_args,
                        "-o", output_template,
                        "--", url
                    ],
                    capture_output=True, text=True, timeout=300
                )

                if result.returncode == 0 and os.path.exists(output_path):
                    print(f"[Yume] yt-dlp ({tag}) succeeded!")
                    return output_path, None

                stderr_lower = (result.stderr or '').lower()

                # If format error, skip to nofmt pass of same auth strategy
                if 'requested format' in stderr_lower and fmt_pass == "bestaudio":
                    continue

                # Extract error for reporting
                error_lines = [l.strip() for l in (result.stderr or '').split('\n')
                               if l.strip() and 'ERROR' in l.upper()]
                raw_err = error_lines[-1][:300] if error_lines else f"exit code {result.returncode}"
                last_error = _friendlify_ytdlp_error(raw_err)
                print(f"[Yume] yt-dlp ({tag}) failed: {last_error[:150]}")

                # If DRM/auth error and we haven't tried cookies yet, skip to next strategy
                if 'drm' in stderr_lower or 'sign in' in stderr_lower:
                    break  # skip nofmt pass, move to next auth strategy

            except subprocess.TimeoutExpired:
                last_error = "yt-dlp timed out (300s)"
                print(f"[Yume] yt-dlp ({label}) timed out")
            except Exception as e:
                last_error = f"yt-dlp error: {e}"

    # ---- Strategy 2: yt-dlp get-url → ffmpeg (works when yt-dlp download fails but URL extract works) ----
    if _is_youtube_url(url):
        try:
            print(f"[Yume] Trying yt-dlp get-url + ffmpeg fallback...")
            stream_url = _get_stream_url(url)
            if stream_url:
                ffmpeg_output = os.path.join(tmp_dir, "full_audio_stream.wav")
                result = subprocess.run(
                    [
                        "ffmpeg", "-y",
                        "-protocol_whitelist", "file,http,https,tcp,tls,crypto",
                        "-i", stream_url,
                        "-vn", "-ar", "16000", "-ac", "1",
                        "-f", "wav",
                        ffmpeg_output
                    ],
                    capture_output=True, text=True, timeout=300
                )
                if result.returncode == 0 and os.path.exists(ffmpeg_output) and os.path.getsize(ffmpeg_output) > 10000:
                    print(f"[Yume] yt-dlp get-url + ffmpeg succeeded!")
                    return ffmpeg_output, None
                else:
                    print(f"[Yume] ffmpeg on stream URL failed: {(result.stderr or '')[-100:]}")
            else:
                print(f"[Yume] Could not get stream URL either")
        except Exception as e:
            print(f"[Yume] Strategy 2 error: {e}")

    # ---- Strategy 3: ffmpeg direct (for m3u8 / direct media URLs only) ----
    if url.endswith('.m3u8') or '.m3u8' in url or not _is_youtube_url(url):
        try:
            print(f"[Yume] Trying ffmpeg direct on URL...")
            ffmpeg_output = os.path.join(tmp_dir, "full_audio_ffmpeg.wav")
            result = subprocess.run(
                [
                    "ffmpeg", "-y",
                    "-protocol_whitelist", "file,http,https,tcp,tls,crypto",
                    "-i", url,
                    "-vn", "-ar", "16000", "-ac", "1",
                    "-f", "wav",
                    ffmpeg_output
                ],
                capture_output=True, text=True, timeout=300
            )

            if result.returncode == 0 and os.path.exists(ffmpeg_output) and os.path.getsize(ffmpeg_output) > 10000:
                print(f"[Yume] ffmpeg direct succeeded")
                return ffmpeg_output, None

            stderr = (result.stderr or '').strip()
            if stderr:
                ffmpeg_err = stderr.split('\n')[-1][:200]
                print(f"[Yume] ffmpeg also failed: {ffmpeg_err}")

        except subprocess.TimeoutExpired:
            print("[Yume] ffmpeg direct timed out (300s)")
        except Exception as e:
            print(f"[Yume] ffmpeg error: {e}")

    return None, last_error


def _get_audio_duration(audio_path):
    """Get duration of audio file in seconds using ffprobe."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                audio_path
            ],
            capture_output=True, text=True, timeout=10
        )
        return float(result.stdout.strip())
    except Exception:
        return 0.0


# ============================================================================
# PREPARE DIRECT - Download from a direct stream URL (m3u8, mp4, etc.)
# ============================================================================

@app.route('/prepare_direct', methods=['POST', 'OPTIONS'])
def prepare_direct():
    """Download audio from a direct stream URL (m3u8, mp4, etc).
    For when yt-dlp can't extract from the page URL."""
    if request.method == 'OPTIONS':
        return '', 204

    try:
        data = request.get_json()
        stream_url = data.get('stream_url')
        video_id = data.get('video_id', 'direct-' + str(int(time.time())))

        if not stream_url:
            return jsonify({"error": "Missing stream_url"}), 400

        # SECURITY: Validate URL
        valid, err = _validate_url(stream_url)
        if not valid:
            return jsonify({"error": f"Invalid URL: {err}"}), 400

        # Check cache
        cached = full_audio_cache.get(video_id)
        if cached and time.time() - cached['timestamp'] < FULL_AUDIO_TTL and os.path.exists(cached['path']):
            return jsonify({"status": "ready", "duration": cached['duration'], "cached": True})

        # Clean up expired entry if it exists
        if cached:
            _cleanup_audio_entry(cached)
            full_audio_cache.pop(video_id, None)

        print(f"[Yume] Direct download: {stream_url[:120]}...")

        tmp_dir = tempfile.mkdtemp(prefix="yume_direct_")
        output_path = os.path.join(tmp_dir, "full_audio.wav")

        # Try ffmpeg first (best for m3u8 and direct media URLs)
        result = subprocess.run(
            [
                "ffmpeg", "-y",
                "-protocol_whitelist", "file,http,https,tcp,tls,crypto",
                "-i", stream_url,
                "-vn", "-ar", "16000", "-ac", "1",
                "-f", "wav", output_path
            ],
            capture_output=True, text=True, timeout=600
        )

        if result.returncode != 0 or not os.path.exists(output_path) or os.path.getsize(output_path) < 10000:
            # Fallback: try yt-dlp on the stream URL directly
            print(f"[Yume] ffmpeg failed, trying yt-dlp on stream URL...")
            output_template = os.path.join(tmp_dir, "full_audio.%(ext)s")
            result = subprocess.run(
                ["yt-dlp", "-x", "--audio-format", "wav",
                 "--postprocessor-args", "ffmpeg:-ar 16000 -ac 1",
                 "--no-playlist", "--no-cache-dir", "--no-exec",
                 "-o", output_template, "--", stream_url],
                capture_output=True, text=True, timeout=300
            )
            if result.returncode != 0 or not os.path.exists(output_path):
                stderr = (result.stderr or '')[-300:]
                return jsonify({"error": f"Direct download failed: {stderr[:200]}"}), 500

        duration = _get_audio_duration(output_path)
        size_kb = os.path.getsize(output_path) / 1024

        full_audio_cache[video_id] = {
            "path": output_path,
            "duration": duration,
            "timestamp": time.time()
        }

        print(f"[Yume] Direct audio ready: {duration:.1f}s, {size_kb:.0f}KB")
        return jsonify({"status": "ready", "duration": duration, "cached": False})

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


def _slice_audio(full_audio_path, start_time, duration):
    """Slice a segment from a local audio file. Instant operation."""
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.wav')
    tmp.close()

    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y",
                "-ss", str(start_time),
                "-i", full_audio_path,
                "-t", str(duration),
                "-ar", "16000", "-ac", "1",
                "-f", "wav",
                tmp.name
            ],
            capture_output=True, timeout=10
        )

        if result.returncode == 0 and os.path.exists(tmp.name) and os.path.getsize(tmp.name) > 1000:
            size_kb = os.path.getsize(tmp.name) / 1024
            print(f"[Yume] Sliced {start_time}s+{duration}s -> {size_kb:.0f}KB")
            return tmp.name
        else:
            print(f"[Yume] Slice failed for {start_time}s+{duration}s")
    except Exception as e:
        print(f"[Yume] Slice error: {e}")

    try:
        os.unlink(tmp.name)
    except Exception:
        pass
    return None


# ============================================================================
# URL-BASED PRE-FETCH TRANSCRIPTION
# ============================================================================

@app.route('/transcribe_url', methods=['POST', 'OPTIONS'])
def transcribe_url():
    if request.method == 'OPTIONS':
        return '', 204

    try:
        data = request.get_json()
        if not data or 'url' not in data:
            return jsonify({"error": "Missing 'url' field"}), 400

        url = data['url']
        video_id = data.get('video_id', 'unknown')

        # SECURITY: Validate URL
        valid, err = _validate_url(url)
        if not valid:
            return jsonify({"error": f"Invalid URL: {err}"}), 400

        chunk_index = int(data.get('chunk_index', 0))
        chunk_duration = int(data.get('chunk_duration', 30))  # Whisper window
        step_size = int(data.get('step_size', 25))            # Advance per chunk
        language = data.get('language') or None  # None = Whisper auto-detect

        # Cache key
        cache_key = f"{video_id}:{step_size}:{chunk_index}"

        with prefetch_lock:
            if cache_key in subtitle_cache:
                print(f"[Yume] Cache hit for chunk {chunk_index} of {video_id}")
                with stats_lock:
                    server_stats["cache_hits"] += 1
                return jsonify(subtitle_cache[cache_key])

        # Calculate time windows
        # Audio sent to Whisper: starts at chunk_index * step_size, lasts chunk_duration
        whisper_start = chunk_index * step_size
        whisper_duration = chunk_duration

        # "Owned" window: only keep segments whose start falls here (deduplicates overlap)
        owned_start = whisper_start
        owned_end = whisper_start + step_size

        print(f"[Yume] Chunk {chunk_index}: whisper [{whisper_start}s-{whisper_start + whisper_duration}s], owns [{owned_start}s-{owned_end}s]")

        # Try prepared full audio first (fast local slice), fall back to stream URL
        audio_path = None
        prepared = full_audio_cache.get(video_id)
        if prepared and os.path.exists(prepared['path']):
            audio_path = _slice_audio(prepared['path'], whisper_start, whisper_duration)

        if audio_path is None:
            print(f"[Yume] No prepared audio, falling back to stream download")
            audio_path = _download_audio_segment(url, whisper_start, whisper_duration)

        if audio_path is None:
            return jsonify({"error": "Audio extraction failed"}), 500

        try:
            result = _transcribe_file(audio_path, language, whisper_start)
        finally:
            try:
                os.unlink(audio_path)
            except Exception:
                pass

        # TRIM: only keep segments whose start falls within owned window
        raw_count = len(result.get("segments", []))
        trimmed = []
        for seg in result.get("segments", []):
            s = seg["start"]
            if s >= (owned_start - 0.3) and s < (owned_end + 0.5):
                trimmed.append(seg)

        result["segments"] = trimmed
        result["text"] = " ".join(s["text"] for s in trimmed)

        with prefetch_lock:
            # Evict oldest entries if cache is full
            if len(subtitle_cache) >= SUBTITLE_CACHE_MAX:
                keys_to_remove = list(subtitle_cache.keys())[:len(subtitle_cache) - SUBTITLE_CACHE_MAX + 1]
                for k in keys_to_remove:
                    del subtitle_cache[k]
            subtitle_cache[cache_key] = result

        print(f"[Yume] Chunk {chunk_index} ready: {len(trimmed)} segments (from {raw_count} raw)")
        return jsonify(result)

    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


# ============================================================================
# RAW AUDIO TRANSCRIPTION (fallback)
# ============================================================================

@app.route('/transcribe', methods=['POST', 'OPTIONS'])
def transcribe():
    if request.method == 'OPTIONS':
        return '', 204

    try:
        data = request.get_json()
        if not data or 'audio' not in data:
            return jsonify({"error": "No audio data provided"}), 400

        audio_base64 = data['audio']
        language = data.get('language') or None  # auto-detect
        start_offset = float(data.get('start_offset', 0))

        try:
            audio_bytes = base64.b64decode(audio_base64)
        except Exception as e:
            return jsonify({"error": f"Invalid base64: {str(e)}"}), 400

        with tempfile.NamedTemporaryFile(delete=False, suffix='.webm') as f:
            f.write(audio_bytes)
            temp_path = f.name

        try:
            result = _transcribe_file(temp_path, language, start_offset)
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


# ============================================================================
# CACHE MANAGEMENT
# ============================================================================

@app.route('/cache/clear', methods=['POST'])
def clear_cache():
    global stream_url_cache
    with prefetch_lock:
        count = len(subtitle_cache)
        subtitle_cache.clear()
    stream_url_cache.clear()
    # Also clean up audio temp files
    for vid, entry in list(full_audio_cache.items()):
        _cleanup_audio_entry(entry)
    audio_count = len(full_audio_cache)
    full_audio_cache.clear()
    return jsonify({"cleared": count, "audio_cleared": audio_count})

@app.route('/cache/status', methods=['GET'])
def cache_status():
    with prefetch_lock:
        keys = list(subtitle_cache.keys())
    return jsonify({"chunks_cached": len(keys), "keys": keys})


# ============================================================================
# AUDIO DOWNLOAD (supports ALL yt-dlp sites, not just YouTube)
# ============================================================================

def _build_auth_args(url):
    """Build yt-dlp auth arguments for non-download calls (get-url, prepare).
    Download calls handle their own multi-strategy retries.
    """
    args = []

    if _is_youtube_url(url):
        # Use default player client for stream URL extraction (most compatible)
        if youtube_auth_method == "cookies":
            args.extend(["--cookies-from-browser", _resolve_browser_cookies()])
    elif youtube_auth_method == "cookies":
        args.extend(["--cookies-from-browser", _resolve_browser_cookies()])

    return args


def _resolve_browser_cookies():
    """Resolve the correct cookies-from-browser string.
    On Fedora/Linux, Firefox is often installed as Flatpak and cookies
    live at ~/.var/app/org.mozilla.firefox/.mozilla/firefox/ instead of
    ~/.mozilla/firefox/. yt-dlp can't find them without the path hint.
    """
    browser = cookies_browser or "firefox"

    # Only apply Flatpak detection for Firefox on Linux
    if browser.lower() == "firefox" and platform.system() == "Linux":
        flatpak_path = os.path.expanduser("~/.var/app/org.mozilla.firefox/.mozilla/firefox")
        native_path = os.path.expanduser("~/.mozilla/firefox")

        # If Flatpak Firefox exists but native doesn't, use Flatpak path
        if os.path.isdir(flatpak_path):
            if not os.path.isdir(native_path):
                print(f"[Yume] Detected Flatpak Firefox, using cookie path: {flatpak_path}")
                return f"firefox:{flatpak_path}"
            # Both exist — check which has more recent cookies (profiles.ini modified time)
            flat_ini = os.path.join(flatpak_path, "profiles.ini")
            native_ini = os.path.join(native_path, "profiles.ini")
            if os.path.exists(flat_ini) and os.path.exists(native_ini):
                if os.path.getmtime(flat_ini) > os.path.getmtime(native_ini):
                    print(f"[Yume] Flatpak Firefox is more recent, using its cookies")
                    return f"firefox:{flatpak_path}"
            elif os.path.exists(flat_ini):
                return f"firefox:{flatpak_path}"

    return browser


def _get_stream_url(url):
    """Get the direct audio stream URL, using cache to avoid repeated yt-dlp calls."""
    global stream_url_cache

    # Check cache first
    cached = stream_url_cache.get(url)
    if cached and (time.time() - cached["timestamp"]) < STREAM_URL_TTL:
        print(f"[Yume] Using cached stream URL (age: {time.time() - cached['timestamp']:.0f}s)")
        return cached["stream_url"]

    auth_args = _build_auth_args(url)

    # Try multiple format selectors
    format_attempts = [
        ["--format", "bestaudio*/bestaudio/best"],
        ["--format", "bestaudio/best"],
        [],  # no format = let yt-dlp pick
    ]

    for fmt_args in format_attempts:
        result = subprocess.run(
            ["yt-dlp", "--get-url", *fmt_args, "--no-playlist", "--no-exec",
             *auth_args, "--", url],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0 and result.stdout.strip().startswith("http"):
            stream_url = result.stdout.strip().split('\n')[0]
            print(f"[Yume] Stream URL obtained and cached")
            stream_url_cache[url] = {"stream_url": stream_url, "timestamp": time.time()}
            return stream_url
        stderr_lower = (result.stderr or '').lower()
        if 'requested format' in stderr_lower:
            continue  # try next format
        break  # non-format error, stop trying

    print(f"[Yume] yt-dlp get-url failed: {(result.stderr or '')[:200]}")
    return None


def _download_audio_segment(url, start_time, duration):
    """Download a specific time segment of audio using yt-dlp + ffmpeg.
    Stream URL is cached so yt-dlp is only called once per video."""
    tmp_dir = tempfile.mkdtemp()
    output_path = os.path.join(tmp_dir, "segment.wav")

    try:
        stream_url = _get_stream_url(url)

        if stream_url is None:
            return _download_audio_segment_fallback(url, start_time, duration, output_path)

        print(f"[Yume] Extracting {duration}s from {start_time}s via ffmpeg...")

        ffmpeg_result = subprocess.run(
            [
                "ffmpeg",
                "-protocol_whitelist", "file,http,https,tcp,tls,crypto",
                "-ss", str(start_time),
                "-i", stream_url,
                "-t", str(duration),
                "-ar", "16000",
                "-ac", "1",
                "-f", "wav",
                "-y",
                output_path
            ],
            capture_output=True, timeout=60
        )

        if ffmpeg_result.returncode == 0 and os.path.exists(output_path):
            size = os.path.getsize(output_path)
            print(f"[Yume] Audio segment downloaded: {size / 1024:.1f} KB")
            return output_path
        else:
            err_msg = ffmpeg_result.stderr[-200:] if ffmpeg_result.stderr else b""
            print(f"[Yume] ffmpeg failed: {err_msg.decode('utf-8', errors='ignore')}")
            # If ffmpeg fails, the cached stream URL might be expired
            stream_url_cache.pop(url, None)
            return None

    except subprocess.TimeoutExpired:
        print("[Yume] Download timed out")
        return None
    except Exception as e:
        print(f"[Yume] Download error: {e}")
        return None


def _download_audio_segment_fallback(url, start_time, duration, output_path):
    """Fallback: use yt-dlp's --download-sections directly."""
    try:
        print("[Yume] Using yt-dlp fallback download method...")
        tmp_template = output_path.replace(".wav", ".%(ext)s")

        auth_args = _build_auth_args(url)

        result = subprocess.run(
            [
                "yt-dlp",
                "--download-sections", f"*{start_time}-{start_time + duration}",
                "--force-keyframes-at-cuts",
                "-x", "--audio-format", "wav",
                "--postprocessor-args", "ffmpeg:-ar 16000 -ac 1",
                "--no-playlist", "--no-exec",
                *auth_args,
                "-o", tmp_template,
                "--", url
            ],
            capture_output=True, timeout=90
        )

        if result.returncode == 0 and os.path.exists(output_path):
            return output_path

        err_msg = result.stderr[-200:] if result.stderr else b""
        print(f"[Yume] Fallback also failed: {err_msg.decode('utf-8', errors='ignore')}")
        return None

    except Exception as e:
        print(f"[Yume] Fallback error: {e}")
        return None


# ============================================================================
# HALLUCINATION FILTER
# ============================================================================

# ============================================================================
# DETERMINISTIC ROMANIZATION (replaces LLM for ja/zh — instant, no GPU)
# ============================================================================

# Lazy-loaded romanizers (only import when first needed)
_kakasi = None
_pinyin_available = False

def _get_kakasi():
    """Lazy-load pykakasi (Japanese kanji→romaji converter)."""
    global _kakasi
    if _kakasi is None:
        try:
            import pykakasi
            _kakasi = pykakasi.kakasi()
            print("[Yume] pykakasi loaded — deterministic Japanese romanization enabled")
        except ImportError:
            _kakasi = False  # Mark as unavailable
            print("[Yume] pykakasi not installed — Japanese romanization falls back to LLM")
    return _kakasi if _kakasi is not False else None


def _romanize_japanese(text):
    """Convert Japanese text (kanji/kana) to romaji using pykakasi. ~1ms."""
    kakasi = _get_kakasi()
    if not kakasi:
        return None
    try:
        result = kakasi.convert(text)
        parts = []
        for item in result:
            r = item.get('hepburn', '') or item.get('passport', '') or item.get('orig', '')
            parts.append(r)
        return ' '.join(parts).strip()
    except Exception as e:
        print(f"[Yume] pykakasi error: {e}")
        return None


def _romanize_chinese(text):
    """Convert Chinese text to pinyin using pypinyin. ~1ms."""
    try:
        from pypinyin import pinyin, Style
        result = pinyin(text, style=Style.TONE)
        return ' '.join(p[0] for p in result).strip()
    except ImportError:
        return None
    except Exception:
        return None


def _romanize_korean(text):
    """Convert Korean text to Revised Romanization using `romanization` (MIT). ~1ms."""
    try:
        from romanization import romanize as kr_romanize
        return kr_romanize(text)
    except ImportError:
        return None
    except Exception as e:
        print(f"[Yume] Korean romanization error: {e}")
        return None


@app.route('/romanize', methods=['POST', 'OPTIONS'])
def romanize():
    """Deterministic romanization for ja/zh/ko. Returns in <5ms vs 1-10s for LLM.
    Falls back to {supported: false} if library not available."""
    if request.method == 'OPTIONS':
        return '', 204

    data = request.get_json() or {}
    text = data.get('text', '').strip()
    lang = data.get('language') or None  # auto-detect

    if not text:
        return jsonify({"romanization": "", "method": "empty"})

    result = None
    if lang == 'ja':
        result = _romanize_japanese(text)
    elif lang == 'zh':
        result = _romanize_chinese(text)
    elif lang == 'ko':
        result = _romanize_korean(text)

    if result is not None:
        return jsonify({"romanization": result, "method": "deterministic", "language": lang})
    else:
        return jsonify({"supported": False, "language": lang}), 501


@app.route('/romanize_batch', methods=['POST', 'OPTIONS'])
def romanize_batch():
    """Batch deterministic romanization — single round trip for N texts.
    Accepts: {"texts": ["text1", "text2", ...], "language": "ja"}
    Returns: {"romanizations": ["roma1", "roma2", ...], "method": "deterministic"}
    Falls back to empty strings for unsupported languages."""
    if request.method == 'OPTIONS':
        return '', 204

    data = request.get_json() or {}
    texts = data.get('texts', [])
    lang = data.get('language') or None

    if not texts or not isinstance(texts, list):
        return jsonify({"romanizations": [], "method": "empty"})

    # Pick the right romanizer
    romanizer = None
    if lang == 'ja':
        romanizer = _romanize_japanese
    elif lang == 'zh':
        romanizer = _romanize_chinese
    elif lang == 'ko':
        romanizer = _romanize_korean

    if romanizer is None:
        return jsonify({"supported": False, "language": lang}), 501

    results = []
    for text in texts:
        try:
            r = romanizer(text.strip()) if text.strip() else ''
            results.append(r or '')
        except Exception:
            results.append('')

    return jsonify({"romanizations": results, "method": "deterministic", "language": lang})


HALLUCINATION_PATTERNS = [
    "\u3054\u8996\u8074\u3042\u308a\u304c\u3068\u3046\u3054\u3056\u3044\u307e\u3057\u305f",
    "\u3054\u8996\u8074\u3042\u308a\u304c\u3068\u3046",
    "\u304a\u75b2\u308c\u69d8\u3067\u3057\u305f",
    "\u304a\u75b2\u308c\u69d8",
    "\u5b57\u5e55\u306f\u81ea\u52d5\u751f\u6210",
    "\u5b57\u5e55\u5236\u4f5c",
    "\u4f5c\u8a5e", "\u4f5c\u66f2", "\u7de8\u66f2", "\u6b4c\uff1a", "feat.",
    "\u8a5e\u66f2",
    "Sound Hodori",
    "\uc0ac\uc6b4\ub4dc \ud638\ub3cc\uc774",
    "\u30db\u30c9\u30ea",
    "Instagram", "Twitter",
    "\u30c1\u30e3\u30f3\u30cd\u30eb\u767b\u9332",
    "\u9ad8\u8a55\u4fa1",
    "\u30b5\u30d6\u30b9\u30af\u30e9\u30a4\u30d6",
    "Subscribe", "Like and subscribe",
    "Thank you for watching", "Thanks for watching", "Please subscribe",
    "[Music]", "[Applause]", "[Laughter]", "(Music)",
    # Chinese common hallucinations
    "请订阅", "感谢观看", "感谢收看", "字幕制作", "字幕组",
    "谢谢大家的支持", "记得点赞", "关注我", "一键三连",
    # Russian common hallucinations
    "Подписывайтесь на канал",
    "Спасибо за просмотр",
    "Ставьте лайк",
    "Нажимайте колокольчик",
    # Arabic common hallucinations
    "اشترك في القناة",
    "شكرا للمشاهدة",
    "لا تنسى الاعجاب",  # bare alef form
    "لا تنسى الإعجاب",  # hamza-below form (Whisper standard)
    # Universal social media
    "Like", "Share", "Comment", "Follow",
]

# User-reported hallucinations (populated via /blacklist/update from extension popup)
user_blacklist = []

@app.route('/blacklist/update', methods=['POST'])
def update_blacklist():
    global user_blacklist
    data = request.get_json() or {}
    incoming = data.get('blacklist', [])
    if not isinstance(incoming, list):
        return jsonify({"error": "blacklist must be a list"}), 400
    user_blacklist = [str(item).strip() for item in incoming if str(item).strip()]
    count = len(user_blacklist)
    print(f"[Yume] User blacklist updated: {count} items")
    for item in user_blacklist[:10]:
        print(f"[Yume]   - {item!r}")
    if count > 10:
        print(f"[Yume]   ... and {count - 10} more")
    return jsonify({"success": True, "count": count})

@app.route('/blacklist', methods=['GET'])
def get_blacklist():
    return jsonify({"blacklist": user_blacklist, "count": len(user_blacklist)})


CREDITS_PATTERNS = [
    "\u4f5c\u8a5e\u30fb\u4f5c\u66f2", "\u4f5c\u66f2\u30fb\u7de8\u66f2",
    "\u4f5c\u8a5e", "\u4f5c\u66f2", "\u7de8\u66f2",
    "vocals", "vocal", "guitar", "bass", "drums", "piano",
    "illustration", "illust", "animation", "video",
    "mix", "mastering",
]

SINGLE_WORD_BLOCKLIST = ['music', 'la', 'na', 'da', 'oh', 'ah', 'mm', 'hmm']

@app.route('/hallucination_patterns', methods=['GET'])
def get_hallucination_patterns():
    """Server-authoritative hallucination patterns. Client fetches at startup
    instead of maintaining a duplicate list. Eliminates drift bugs."""
    return jsonify({
        "builtin": HALLUCINATION_PATTERNS,
        "credits": CREDITS_PATTERNS,
        "user_blacklist": user_blacklist,
        "single_word_blocklist": SINGLE_WORD_BLOCKLIST,
        "repeat_threshold": 6,     # words >= this with <=2 unique = spam
        "concat_min_len": 4,       # min clean length for concatenated repetition check
        "concat_coverage": 0.8,    # coverage threshold for concat repetition
    })

def _is_hallucination(text):
    t = text.strip()
    if not t:
        return True
    t_lower = t.lower()
    for pat in HALLUCINATION_PATTERNS:
        if pat.lower() in t_lower:
            return True

    # Check user-reported blacklist (sent from extension popup)
    for bl_item in user_blacklist:
        if bl_item and bl_item.lower() in t_lower:
            print(f"[Yume] User blacklist match: {bl_item!r} in {t!r}")
            return True

    # Repeated word spam: "30k 30k 30k 30k" (6+ words, <=2 unique)
    words = t.split()
    if len(words) >= 6:
        unique = set(w.lower().strip(".,!?") for w in words)
        if len(unique) <= 2:
            return True

    # Detect concatenated repetition: "musicmusic", "aaaaa", "la la la la"
    # Check if a short substring (2-8 chars) repeats to fill most of the text
    clean = t_lower.replace(" ", "")
    if len(clean) >= 4:
        for sub_len in range(2, min(9, len(clean) // 2 + 1)):
            sub = clean[:sub_len]
            if sub * (len(clean) // len(sub)) == clean[:len(sub) * (len(clean) // len(sub))]:
                repeats = len(clean) // len(sub)
                if repeats >= 2 and len(sub) * repeats >= len(clean) * 0.8:
                    print(f"[Yume] Hallucination: repeated '{sub}' x{repeats} in '{t}'")
                    return True

    # Single common noise words
    if t_lower in SINGLE_WORD_BLOCKLIST:
        return True

    return False

def _is_credits_line(text):
    t = text.strip()
    t_lower = t.lower()
    for pat in CREDITS_PATTERNS:
        if pat.lower() in t_lower:
            return True
    if "\u30fb" in t and len(t) < 60:
        return True
    return False


# ============================================================================
# SEGMENT RE-SPLITTING (word_timestamps + pause detection)
# ============================================================================


def _transcribe_file(audio_path, language, start_offset=0.0):
    """Transcribe audio file. Uses v2.0.7 parameters proven to work for music."""
    if model is None:
        raise RuntimeError("Model is still loading — try again in a few seconds")

    print(f"[Yume] Transcribing {audio_path} (offset: {start_offset}s)")
    t_start = time.time()

    file_size = os.path.getsize(audio_path)
    if file_size < 10_000:
        print(f"[Yume] Skipping tiny file ({file_size} bytes)")
        return {"text": "", "segments": [], "language": language, "duration": 0, "start_offset": start_offset}

    # v2.0.7 parameters - proven to work for Japanese music
    # DO NOT use word_timestamps - different decode path, drops segments
    params = dict(
        language=language,
        beam_size=5,
        vad_filter=False,              # MUST be False - Silero VAD drops singing
        word_timestamps=False,         # v2.0.7: False (different decode path when True)
        condition_on_previous_text=False,
        temperature=0.0,
        compression_ratio_threshold=2.4,  # v2.0.7 value
        log_prob_threshold=-1.5,          # widened from -1.0: sung Japanese has lower confidence
        no_speech_threshold=0.6,          # raised from 0.45: music vocals have high no-speech prob
    )

    # CRITICAL: Serialize model access. CTranslate2 is NOT thread-safe.
    with transcribe_lock:
        segments_iter, info = model.transcribe(audio_path, **params)
        raw_segments = list(segments_iter)  # consume inside lock

    segments = []
    full_text_parts = []
    dropped = 0

    print(f"[Yume] Whisper returned {len(raw_segments)} raw segments")

    for seg in raw_segments:
        text = seg.text.strip()
        if not text:
            continue
        if _is_hallucination(text):
            print(f"[Yume] Dropped hallucination: {text!r}")
            dropped += 1
            continue
        if _is_credits_line(text):
            print(f"[Yume] Dropped credits: {text!r}")
            dropped += 1
            continue
        if hasattr(seg, "avg_logprob") and seg.avg_logprob < -1.5:  # widened: music content
            print(f"[Yume] Dropped low-conf ({seg.avg_logprob:.2f}): {text!r}")
            dropped += 1
            continue

        segments.append({
            "start": round(seg.start + start_offset, 2),
            "end":   round(seg.end   + start_offset, 2),
            "text":  text,
            "confidence": round(seg.avg_logprob, 2) if hasattr(seg, "avg_logprob") else 0
        })
        full_text_parts.append(text)

    print(f"[Yume] Transcription done: {len(segments)} segments ({dropped} dropped)")

    whisper_elapsed = time.time() - t_start
    with stats_lock:
        server_stats["chunks_transcribed"] += 1
        server_stats["segments_produced"] += len(segments)
        server_stats["hallucinations_filtered"] += dropped
        server_stats["total_audio_seconds"] += info.duration
        server_stats["total_whisper_time"] += whisper_elapsed
        server_stats["last_chunk_whisper_time"] = round(whisper_elapsed, 1)
        server_stats["last_chunk_segments"] = len(segments)

    return {
        "text": " ".join(full_text_parts),
        "segments": segments,
        "language": info.language,
        "duration": info.duration,
        "start_offset": start_offset
    }


# ============================================================================
# STARTUP  (ALL ASCII -- no em-dashes, no box-drawing chars)
# ============================================================================

def main():
    _cleanup_stale_temps()
    signal.signal(signal.SIGTERM, _shutdown_handler)
    signal.signal(signal.SIGINT, _shutdown_handler)
    atexit.register(_cleanup_all_audio)

    global model, model_name, device, compute_type
    global use_word_timestamps, pause_threshold
    global youtube_auth_method, cookies_browser
    global translation_host, translation_port, translation_backend
    global TOKEN_FILE

    parser = argparse.ArgumentParser(description="Yume Whisper Server")
    parser.add_argument('--model', default='large-v3')
    parser.add_argument('--device', default='cuda')
    parser.add_argument('--compute-type', default='float16')
    parser.add_argument('--port', type=int, default=5001)
    parser.add_argument('--no-word-timestamps', action='store_true', help='Disable word-level timestamps')
    parser.add_argument('--pause-threshold', type=float, default=0.25, help='Seconds of silence to split segments')
    parser.add_argument('--config', type=str, default=None, help='Path to yume_config.json')
    args = parser.parse_args()

    # Start with CLI defaults, then overlay config file values
    model_name = args.model
    device = args.device
    compute_type = args.compute_type
    use_word_timestamps = not args.no_word_timestamps
    pause_threshold = args.pause_threshold
    port = args.port

    if args.config and os.path.exists(args.config):
        with open(args.config, encoding="utf-8") as f:
            cfg = json.load(f)
        model_name = cfg.get("whisper_model", model_name)
        device = cfg.get("whisper_device", device)
        compute_type = cfg.get("whisper_compute_type", compute_type)
        use_word_timestamps = cfg.get("word_timestamps", use_word_timestamps)
        pause_threshold = cfg.get("pause_threshold", pause_threshold)
        port = cfg.get("whisper_port", port)
        youtube_auth_method = cfg.get("youtube_auth_method", youtube_auth_method)
        cookies_browser = cfg.get("cookies_browser", cookies_browser)
        translation_host = cfg.get("translation_host", translation_host)
        translation_port = cfg.get("translation_port", translation_port)
        translation_backend = cfg.get("translation_backend", translation_backend)

    # Handle 'auto' device
    if device == "auto":
        try:
            import torch
            device = "cuda" if torch.cuda.is_available() else "cpu"
        except ImportError:
            device = "cpu"
    if compute_type == "auto":
        compute_type = "float16" if device == "cuda" else "int8"

    print("=" * 70)
    print("  YUME -- Whisper Server v5.4.0")
    print("=" * 70)
    print(f"  Model:            {model_name}")
    print(f"  Device:           {device}")
    print(f"  Compute Type:     {compute_type}")
    print(f"  Port:             {port}")
    print(f"  Architecture:     download-once + local slice")
    print(f"  Whisper Params:   v2.0.7 (proven for music)")
    print(f"  VAD Filter:       OFF (required for music)")
    yt_info = youtube_auth_method
    if youtube_auth_method == "cookies":
        yt_info += f" ({cookies_browser})"
    print(f"  YouTube Auth:     {yt_info}")

    # Deterministic romanization availability
    roma_parts = []
    try:
        import pykakasi; roma_parts.append("ja(pykakasi)")
    except ImportError: pass
    try:
        from pypinyin import pinyin; roma_parts.append("zh(pypinyin)")
    except ImportError: pass
    try:
        from romanization import romanize as _kr; roma_parts.append("ko(romanization)")
    except ImportError: pass
    if roma_parts:
        print(f"  Romanization:     {', '.join(roma_parts)} (instant)")
    else:
        print(f"  Romanization:     LLM-only (pip install pykakasi pypinyin romanization for instant)")

    print("=" * 70)

    if not _check_ytdlp():
        print("")
        print("  WARNING: yt-dlp not found in PATH!")
        print("  Pre-fetch mode will not work without it.")
        print("")
    else:
        v = subprocess.run(["yt-dlp", "--version"], capture_output=True, text=True)
        print(f"  yt-dlp:           {v.stdout.strip()}")

    # Auth method viability check
    if youtube_auth_method == "deno":
        deno_found = False
        try:
            subprocess.run(["deno", "--version"], capture_output=True, timeout=5)
            deno_found = True
        except Exception:
            pass
        if not deno_found:
            print("")
            print("  WARNING: youtube_auth_method is 'deno' but Deno is not installed.")
            print("  Auto-switching to 'cookies' auth with browser: firefox")
            print("  To fix: install Deno, or set youtube_auth_method='cookies' in config.")
            youtube_auth_method = "cookies"
            if cookies_browser == "chrome":
                # If still default chrome, switch to firefox (more common on Linux)
                import platform
                if platform.system() != "Windows":
                    cookies_browser = "firefox"
            print(f"  Now using: cookies ({cookies_browser})")
            print("")

    # Write API token file for extension discovery (before model load so
    # the extension can discover the server while model is still loading)
    base_dir = Path(__file__).parent.parent.resolve()
    TOKEN_FILE = str(base_dir / ".yume_token")
    try:
        with open(TOKEN_FILE, 'w') as f:
            f.write(API_TOKEN)
        os.chmod(TOKEN_FILE, 0o600)  # owner-only read/write
        print(f"  API token:        written to {TOKEN_FILE}")
    except Exception as e:
        print(f"  API token:        {API_TOKEN[:12]}... (file write failed: {e})")

    print(f"  Security:         Host validation + API token + URL validation")

    # ── Parallel startup: start HTTP server FIRST, then load model ──
    # The /health endpoint returns {"status": "loading"} while model is None,
    # so the extension can connect immediately and show "Loading model..." to
    # the user instead of "Server not reachable".
    def _start_server():
        try:
            from waitress import serve
            print("  Server:           Waitress (production)")
            print("")
            serve(app, host='127.0.0.1', port=port, threads=4,
                  channel_timeout=300, recv_bytes=65536)
        except ImportError:
            print("  Server:           Flask dev (install waitress for production)")
            print("")
            app.run(host='127.0.0.1', port=port, debug=False, threaded=True)

    server_thread = threading.Thread(target=_start_server, daemon=True)
    server_thread.start()

    print("")
    print(f"  Listening on http://localhost:{port}  (status: loading)")
    print("  Loading Whisper model...")
    print("")

    try:
        model = WhisperModel(model_name, device=device, compute_type=compute_type)

        # Try to create BatchedInferencePipeline wrapper for potential multi-chunk batching
        try:
            from faster_whisper import BatchedInferencePipeline
            batched_model = BatchedInferencePipeline(model=model)
            print(f"  Batched pipeline: available (faster-whisper BatchedInferencePipeline)")
        except (ImportError, Exception) as bp_err:
            batched_model = None
            print(f"  Batched pipeline: unavailable ({bp_err})")

        # Prewarm: run a tiny dummy inference to trigger CUDA kernel compilation
        # and KV cache allocation. This moves the first-inference penalty from
        # the user's first real request to startup time.
        try:
            import numpy as np
            _dummy = np.zeros(16000, dtype=np.float32)  # 1 second of silence
            list(model.transcribe(_dummy, language="en"))
            print("  Prewarm:          done (CUDA kernels compiled)")
        except Exception as pw_err:
            print(f"  Prewarm:          skipped ({pw_err})")

        print("=" * 70)
        print("  MODEL LOADED -- Server ready")
        print(f"  Listening on http://localhost:{port}")
        print("=" * 70)
        print("")

        # Keep main thread alive (server runs in daemon thread)
        server_thread.join()

    except Exception as e:
        print(f"\n  FAILED TO LOAD MODEL: {e}")
        print("\n  If you see CUDA errors, try:")
        print("    --device cpu --compute-type int8")
        sys.exit(1)


if __name__ == '__main__':
    main()
