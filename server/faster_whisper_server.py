#!/usr/bin/env python3
"""
Yume -- Faster-Whisper Server v0.0.8
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
import logging
from pathlib import Path
from urllib.parse import urlparse
import unicodedata

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

CORS(
    app,
    resources={
        r"/*": {
            "origins": ["chrome-extension://*", "http://localhost:*", "http://127.0.0.1:*"],
            "methods": ["GET", "POST", "OPTIONS"],
            "allow_headers": ["Content-Type", "X-API-Token"],
        }
    },
)


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
    Rejects: dash-leading strings, non-http(s) schemes, shell metacharacters, empty URLs.
    Returns (is_valid, error_message).
    """
    if not url or not isinstance(url, str):
        return False, "Empty or invalid URL"
    url = url.strip()
    if url.startswith("-"):
        return False, "URL cannot start with '-' (argument injection)"
    # Reject shell metacharacters to prevent command injection
    if any(c in url for c in [";", "|", "`", "$", "(", ")", "\n", "\r"]):
        return False, "URL contains shell metacharacters"
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
        try:
            os.unlink(TOKEN_FILE)
        except Exception:
            pass
    sys.exit(0)


# Signal handlers registered in main()


@app.teardown_request
def _cleanup_request_temps(_exception):
    """Clean temp files created during this request (defense against unclean exits)."""
    for path in getattr(g, "_temp_files", []):
        try:
            if os.path.isfile(path):
                os.unlink(path)
            elif os.path.isdir(path):
                shutil.rmtree(path, ignore_errors=True)
        except OSError:
            pass


# Global state
model = None
model_name = "large-v3"
model_display_name = ""  # Friendly name for custom models (from config whisper_model_name)
device = "cuda"
compute_type = "float16"

# prevent garbage collection of the Windows console handler callback (set in main())
_win_console_handler_ref = None
use_word_timestamps = True
pause_threshold = 0.25  # seconds of silence to split segments (0.25 = better for songs)

# Duplicated string constants (SonarCloud S1192)
FFMPEG_PROTOCOL_WHITELIST = "file,http,https,tcp,tls,crypto"
FFMPEG_AUDIO_OPTS = "ffmpeg:-ar 16000 -ac 1"
YT_PLAYER_CLIENT_TV_WEB = "youtube:player_client=tv,web"
ERR_REQUESTED_FORMAT = "requested format"
ERR_NO_SUCH_FILE = "no such file"

# Server-side cache (LRU-limited to prevent memory leaks on long sessions)
subtitle_cache = {}
SUBTITLE_CACHE_MAX = 2000
AUDIO_CACHE_MAX = 50
STREAM_URL_CACHE_MAX = 100  # ~2000 chunks * ~10KB avg = ~20MB max
prefetch_lock = threading.Lock()

# Thread safety: waitress serves concurrent requests on multiple threads.
# Individual dict operations are GIL-atomic, but compound operations (check + insert,
# evict + add) are not. This lock serializes all cache mutations.
cache_lock = threading.Lock()

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
        parent = os.path.dirname(entry.get("path", ""))
        basename = os.path.basename(parent)
        if parent and os.path.isdir(parent) and (basename.startswith("yume_") or basename.startswith("tmp")):
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
youtube_auth_method = "cookies"  # "cookies" or "deno"
cookies_browser = "chrome"

# Translation server info (read from config, reported in /health so extension can auto-discover)
translation_host = "127.0.0.1"
translation_port = 5000
translation_backend = "llamacpp"
translation_prompt = ""  # Custom translation prompt from CLI config (passed to extension via /health)
romanization_prompt = ""  # Custom romanization prompt from CLI config (passed to extension via /health)

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
    "cache_misses": 0,
    "errors": 0,
    "last_chunk_whisper_time": 0.0,
    "last_chunk_segments": 0,
}
stats_lock = threading.Lock()


def _get_gpu_stats():
    """Get GPU VRAM and utilization via nvidia-smi. Returns dict or None."""
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


# ============================================================================
# HEALTH CHECK
# ============================================================================


@app.route("/health", methods=["GET"])
def health():
    # Minimal response for unauthenticated callers (bootstrap/discovery only)
    # Only expose token to Chrome extension or local callers (blocks CSRF from malicious sites)
    origin = request.headers.get("Origin", "")
    safe_caller = (not origin) or origin.startswith("chrome-extension://") or origin.startswith("moz-extension://")

    is_ready = model is not None
    base = {
        "status": "ready" if is_ready else "loading",
        "version": "0.0.8",
        "prepare_supported": True,
        "ytdlp_available": _check_ytdlp(),
    }
    if safe_caller:
        base["api_token"] = API_TOKEN

    # Full response only for authenticated callers — hides translation server details
    token = request.headers.get("X-API-Token", "")
    if token == API_TOKEN:
        base.update(
            {
                "model": model_name,
                "device": device,
                "compute_type": compute_type,
                "vad_filter": False,
                "translation_host": translation_host,
                "translation_port": translation_port,
                "translation_backend": translation_backend,
                "translation_url": f"http://{translation_host}:{translation_port}",
                "translation_prompt": translation_prompt,
                "romanization_prompt": romanization_prompt,
            }
        )

    # Return 503 while model is loading — extension checks response.ok
    status_code = 200 if is_ready else 503
    return jsonify(base), status_code


@app.route("/stats", methods=["GET"])
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
    s["model_display_name"] = model_display_name or ""
    s["device"] = device
    s["compute_type"] = compute_type

    return jsonify(s)


@app.route("/model/switch", methods=["POST"])
def switch_model():
    """Hot-swap the Whisper model without restarting the server."""
    global model, model_name, device, compute_type

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

    # Accept standard model names OR local directory paths (for custom/fine-tuned models)
    is_local_path = os.path.sep in new_model or "/" in new_model
    if is_local_path:
        if not os.path.isdir(new_model):
            return jsonify({"error": f"Directory not found: {new_model}"}), 400
        # Verify it looks like a CTranslate2 model
        required = ["model.bin", "config.json"]
        missing = [f for f in required if not os.path.exists(os.path.join(new_model, f))]
        if missing:
            return jsonify({"error": f"Not a valid CTranslate2 model — missing: {', '.join(missing)}"}), 400
    elif new_model not in valid_models:
        return jsonify({"error": f"Unknown model: {new_model}", "valid": valid_models}), 400

    # Normalize turbo alias
    if new_model == "turbo":
        new_model = "large-v3-turbo"

    if new_model == model_name:
        return jsonify({"status": "already_loaded", "model": model_name})

    old_model = model_name
    print(f"[Yume] Switching model: {old_model} -> {new_model}")

    try:
        with transcribe_lock:
            model_name = new_model
            model = WhisperModel(model_name, device=device, compute_type=compute_type)

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


@app.route("/config", methods=["GET"])
def get_config():
    return jsonify(
        {
            "model": model_name,
            "device": device,
            "compute_type": compute_type,
            "word_timestamps": use_word_timestamps,
            "pause_threshold": pause_threshold,
        }
    )


_ytdlp_cache = {"available": None, "checked_at": 0}


def _ytdlp_cmd():
    """Return the yt-dlp command prefix.

    When youtube_auth_method == 'deno', we MUST use the pip-installed yt-dlp
    (python -m yt_dlp) because only pip-installed yt-dlp discovers pip-installed
    plugins like bgutil-ytdlp-pot-provider.

    A standalone yt-dlp binary (tools/yt-dlp.exe) does NOT search site-packages
    for plugins — so the PO token plugin would be invisible to it.
    """
    if youtube_auth_method == "deno":
        # Use pip-installed yt-dlp so it finds pip-installed bgutil plugin
        return [sys.executable, "-m", "yt_dlp"]
    # Default: standalone binary (faster startup, works for cookies mode)
    return ["yt-dlp"]


def _check_ytdlp():
    """Check if yt-dlp is available (cached for 60s)."""
    now = time.time()
    if _ytdlp_cache["available"] is not None and now - _ytdlp_cache["checked_at"] < 60:
        return _ytdlp_cache["available"]
    try:
        result = subprocess.run(_ytdlp_cmd() + ["--version"], capture_output=True, timeout=10)
        available = result.returncode == 0
    except Exception:
        available = False
    _ytdlp_cache["available"] = available
    _ytdlp_cache["checked_at"] = now
    return available


@app.route("/translation/models", methods=["GET"])
def list_translation_models():
    """Query the translation backend for available models."""
    url = f"http://{translation_host}:{translation_port}"
    models = []

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
            ggufs.append({"name": f.name, "size_mb": round(f.stat().st_size / (1024 * 1024), 1)})

    return jsonify(
        {
            "backend": translation_backend,
            "translation_url": url,
            "models": models,
            "local_ggufs": ggufs,
            "note": "llama.cpp requires server restart to switch models" if translation_backend == "llamacpp" else "",
        }
    )


def _is_youtube_url(url):
    """Check if URL is a YouTube URL (for YouTube-specific args)."""
    if not url:
        return False
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        yt_domains = {
            "youtube.com",
            "www.youtube.com",
            "youtu.be",
            "youtube-nocookie.com",
            "www.youtube-nocookie.com",
            "music.youtube.com",
            "m.youtube.com",
        }
        return host in yt_domains or host.endswith(".youtube.com")
    except Exception:
        return False


# ============================================================================
# PREPARE VIDEO - Download full audio once
# ============================================================================


@app.route("/prepare", methods=["POST", "OPTIONS"])
def prepare():
    """Download full audio for a video. Called once before chunk transcription."""
    if request.method == "OPTIONS":
        return "", 204

    try:
        data = request.get_json()
        url = data.get("url")
        video_id = data.get("video_id", "unknown")

        if not url:
            return jsonify({"error": "Missing url"}), 400

        # SECURITY: Validate URL before passing to subprocess
        valid, err = _validate_url(url)
        if not valid:
            return jsonify({"error": f"Invalid URL: {err}"}), 400

        # Check cache
        with cache_lock:
            cached = full_audio_cache.get(video_id)
            if cached and time.time() - cached["timestamp"] < FULL_AUDIO_TTL and os.path.exists(cached["path"]):
                print(f"[Yume] Full audio cache hit for {video_id} ({cached['duration']:.0f}s)")
                return jsonify({"status": "ready", "duration": cached["duration"], "cached": True})

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

        with cache_lock:
            # Evict oldest if cache full — clean up its temp files
            if len(full_audio_cache) >= AUDIO_CACHE_MAX:
                oldest = next(iter(full_audio_cache))
                evicted = full_audio_cache.pop(oldest, None)
                if evicted:
                    _cleanup_audio_entry(evicted)
            full_audio_cache[video_id] = {"path": audio_path, "duration": duration, "timestamp": time.time()}

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
    if "drm protected" in lower:
        return (
            "YouTube blocked the download (DRM error). "
            "Fix: In yume_config.json set youtube_auth_method to 'cookies' "
            "and cookies_browser to your browser name (e.g. 'firefox'). "
            "Or paste a stream URL in the extension popup."
        )
    if "sign in to confirm" in lower or "confirm you" in lower:
        return (
            "YouTube requires sign-in to access this video. "
            "Fix: In yume_config.json set youtube_auth_method to 'cookies' "
            "and cookies_browser to your browser name."
        )
    if "http error 403" in lower or "403 forbidden" in lower:
        if "cloudflare" in lower:
            return (
                "Access denied (403) — Cloudflare anti-bot protection. "
                "This site blocks automated downloads. "
                "Try: copy the direct video/audio URL (often .m3u8 or .mp4) "
                "from the browser's Network tab and paste it as a Custom Stream URL "
                "in the Yume extension popup."
            )
        return (
            "Access denied (403). The site is blocking yt-dlp. "
            "For YouTube: try switching to cookie auth in yume_config.json. "
            "For other sites: use the Custom Stream URL option in the extension — "
            "open DevTools > Network > filter 'm3u8' or 'mp4' > copy the URL. "
            "Also try: pip install -U yt-dlp"
        )
    if "video unavailable" in lower or "private video" in lower:
        return "This video is unavailable or private."
    if "age" in lower and "restricted" in lower:
        return "Age-restricted video. Fix: Set youtube_auth_method to 'cookies' with a logged-in browser."
    if "geo" in lower and "block" in lower:
        return "This video is not available in your region."

    # Network / connectivity
    if "unable to download" in lower and ("webpage" in lower or "player" in lower):
        return "Cannot reach YouTube. Check your internet connection, or YouTube may be temporarily down."
    if "timed out" in lower or "timeout" in lower:
        return "Download timed out — the video may be too long or the connection too slow."

    # yt-dlp itself
    if ERR_NO_SUCH_FILE in lower and "yt-dlp" in lower:
        return "yt-dlp is not installed. Run the Yume setup wizard to install it."
    if "no video formats" in lower or ERR_REQUESTED_FORMAT in lower:
        return "No compatible audio format found. Try updating yt-dlp: pip install -U yt-dlp"

    # Deno-specific
    if "deno" in lower and ("not found" in lower or ERR_NO_SUCH_FILE in lower):
        return (
            "Deno is not installed (needed for YouTube auth). "
            "Fix: Switch youtube_auth_method to 'cookies' in yume_config.json, "
            "or install Deno: https://deno.land/#installation"
        )

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

    # Auth strategy:
    #   "deno" mode: bgutil-ytdlp-pot-provider generates PO tokens via deno.
    #     The plugin hooks into yt-dlp automatically — no extra args needed.
    #     Deno is found via PATH. If bgutil fails, we fall back to cookies.
    #   "cookies" mode: borrows the user's browser YouTube login session.
    #
    cookie_args = []
    try:
        cookie_args = ["--cookies-from-browser", _resolve_browser_cookies()]
    except Exception:
        pass

    if is_yt:
        if youtube_auth_method == "deno":
            # Try 1: bgutil PO token (no extra args — plugin handles everything)
            strategies.append(("deno+default", []))
            strategies.append(("deno+tv,web", ["--extractor-args", YT_PLAYER_CLIENT_TV_WEB]))
            # Try 2: cookies fallback (always available as backup)
            if cookie_args:
                strategies.append(("cookies-fallback", [*cookie_args]))
                strategies.append(("cookies+tv,web", ["--extractor-args", YT_PLAYER_CLIENT_TV_WEB, *cookie_args]))
            # Try 3: no auth (last resort, works for public non-restricted videos)
            strategies.append(("no-auth", []))
        else:
            # Pure cookies mode
            if cookie_args:
                strategies.append(("cookies+default", [*cookie_args]))
                strategies.append(("cookies+tv,web", ["--extractor-args", YT_PLAYER_CLIENT_TV_WEB, *cookie_args]))
                strategies.append(("cookies+mweb", ["--extractor-args", "youtube:player_client=mweb", *cookie_args]))
            # Always include no-auth as last resort (public videos work without cookies)
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
                        *_ytdlp_cmd(),
                        *fmt_args,
                        "-x",
                        "--audio-format",
                        "wav",
                        "--postprocessor-args",
                        FFMPEG_AUDIO_OPTS,
                        "--no-playlist",
                        "--no-cache-dir",
                        "--no-exec",
                        *extra_args,
                        "-o",
                        output_template,
                        "--",
                        url,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=300,
                )

                if result.returncode == 0 and os.path.exists(output_path):
                    print(f"[Yume] yt-dlp ({tag}) succeeded!")
                    return output_path, None

                stderr_lower = (result.stderr or "").lower()

                # If format error, skip to nofmt pass of same auth strategy
                if ERR_REQUESTED_FORMAT in stderr_lower and fmt_pass == "bestaudio":
                    continue

                # Extract error for reporting
                error_lines = [
                    ln.strip() for ln in (result.stderr or "").split("\n") if ln.strip() and "ERROR" in ln.upper()
                ]
                raw_err = error_lines[-1][:300] if error_lines else f"exit code {result.returncode}"
                last_error = _friendlify_ytdlp_error(raw_err)
                print(f"[Yume] yt-dlp ({tag}) failed: {last_error[:150]}")

                # If auth error, skip to next strategy (try cookies fallback)
                if (
                    "drm" in stderr_lower
                    or "sign in" in stderr_lower
                    or "forbidden" in stderr_lower
                    or "invalid token" in stderr_lower
                    or "bot" in stderr_lower
                ):
                    break  # skip nofmt pass, move to next auth strategy

            except subprocess.TimeoutExpired:
                last_error = "yt-dlp timed out (300s)"
                print(f"[Yume] yt-dlp ({label}) timed out")
            except Exception as e:
                last_error = f"yt-dlp error: {e}"

    # ---- Strategy 2: yt-dlp get-url → ffmpeg (works when yt-dlp download fails but URL extract works) ----
    if _is_youtube_url(url):
        try:
            print("[Yume] Trying yt-dlp get-url + ffmpeg fallback...")
            stream_url = _get_stream_url(url)
            if stream_url:
                ffmpeg_output = os.path.join(tmp_dir, "full_audio_stream.wav")
                result = subprocess.run(
                    [
                        "ffmpeg",
                        "-y",
                        "-protocol_whitelist",
                        FFMPEG_PROTOCOL_WHITELIST,
                        "-i",
                        stream_url,
                        "-vn",
                        "-ar",
                        "16000",
                        "-ac",
                        "1",
                        "-f",
                        "wav",
                        ffmpeg_output,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=300,
                )
                if result.returncode == 0 and os.path.exists(ffmpeg_output) and os.path.getsize(ffmpeg_output) > 10000:
                    print("[Yume] yt-dlp get-url + ffmpeg succeeded!")
                    return ffmpeg_output, None
                else:
                    print(f"[Yume] ffmpeg on stream URL failed: {(result.stderr or '')[-100:]}")
            else:
                print("[Yume] Could not get stream URL either")
        except Exception as e:
            print(f"[Yume] Strategy 2 error: {e}")

    # ---- Strategy 3: ffmpeg direct (for m3u8 / direct media URLs only) ----
    if url.endswith(".m3u8") or ".m3u8" in url or not _is_youtube_url(url):
        try:
            print("[Yume] Trying ffmpeg direct on URL...")
            ffmpeg_output = os.path.join(tmp_dir, "full_audio_ffmpeg.wav")
            result = subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-protocol_whitelist",
                    FFMPEG_PROTOCOL_WHITELIST,
                    "-i",
                    url,
                    "-vn",
                    "-ar",
                    "16000",
                    "-ac",
                    "1",
                    "-f",
                    "wav",
                    ffmpeg_output,
                ],
                capture_output=True,
                text=True,
                timeout=300,
            )

            if result.returncode == 0 and os.path.exists(ffmpeg_output) and os.path.getsize(ffmpeg_output) > 10000:
                print("[Yume] ffmpeg direct succeeded")
                return ffmpeg_output, None

            stderr = (result.stderr or "").strip()
            if stderr:
                ffmpeg_err = stderr.split("\n")[-1][:200]
                print(f"[Yume] ffmpeg also failed: {ffmpeg_err}")

        except subprocess.TimeoutExpired:
            print("[Yume] ffmpeg direct timed out (300s)")
        except Exception as e:
            print(f"[Yume] ffmpeg error: {e}")

    # All strategies failed — clean up temp dir before returning error
    try:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    except Exception:
        pass
    return None, last_error


def _get_audio_duration(audio_path):
    """Get duration of audio file in seconds using ffprobe."""
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                audio_path,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return float(result.stdout.strip())
    except Exception:
        return 0.0


# ============================================================================
# PREPARE DIRECT - Download from a direct stream URL (m3u8, mp4, etc.)
# ============================================================================


@app.route("/prepare_direct", methods=["POST", "OPTIONS"])
def prepare_direct():
    """Download audio from a direct stream URL (m3u8, mp4, etc).
    For when yt-dlp can't extract from the page URL."""
    if request.method == "OPTIONS":
        return "", 204

    try:
        data = request.get_json()
        stream_url = data.get("stream_url")
        video_id = data.get("video_id", "direct-" + str(int(time.time())))

        if not stream_url:
            return jsonify({"error": "Missing stream_url"}), 400

        # SECURITY: Validate URL
        valid, err = _validate_url(stream_url)
        if not valid:
            return jsonify({"error": f"Invalid URL: {err}"}), 400

        # Check cache
        with cache_lock:
            cached = full_audio_cache.get(video_id)
            if cached and time.time() - cached["timestamp"] < FULL_AUDIO_TTL and os.path.exists(cached["path"]):
                return jsonify({"status": "ready", "duration": cached["duration"], "cached": True})

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
                "ffmpeg",
                "-y",
                "-protocol_whitelist",
                FFMPEG_PROTOCOL_WHITELIST,
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
            # Fallback: try yt-dlp on the stream URL directly
            print("[Yume] ffmpeg failed, trying yt-dlp on stream URL...")
            output_template = os.path.join(tmp_dir, "full_audio.%(ext)s")
            result = subprocess.run(
                [
                    *_ytdlp_cmd(),
                    "-x",
                    "--audio-format",
                    "wav",
                    "--postprocessor-args",
                    FFMPEG_AUDIO_OPTS,
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
                return jsonify({"error": f"Direct download failed: {stderr[:200]}"}), 500

        duration = _get_audio_duration(output_path)
        size_kb = os.path.getsize(output_path) / 1024

        with cache_lock:
            full_audio_cache[video_id] = {"path": output_path, "duration": duration, "timestamp": time.time()}

        print(f"[Yume] Direct audio ready: {duration:.1f}s, {size_kb:.0f}KB")
        return jsonify({"status": "ready", "duration": duration, "cached": False})

    except Exception as e:
        import traceback

        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


def _slice_audio(full_audio_path, start_time, duration):
    """Slice a segment from a local audio file. Instant operation."""
    if not os.path.exists(full_audio_path):
        print(f"[Yume] Slice failed: source file missing ({full_audio_path})")
        return None

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".wav")
    tmp.close()

    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-ss",
                str(start_time),
                "-i",
                full_audio_path,
                "-t",
                str(duration),
                "-ar",
                "16000",
                "-ac",
                "1",
                "-f",
                "wav",
                tmp.name,
            ],
            capture_output=True,
            timeout=10,
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


@app.route("/transcribe_url", methods=["POST", "OPTIONS"])
def transcribe_url():
    if request.method == "OPTIONS":
        return "", 204

    try:
        data = request.get_json()
        if not data or "url" not in data:
            return jsonify({"error": "Missing 'url' field"}), 400

        url = data["url"]
        video_id = data.get("video_id", "unknown")

        # SECURITY: Validate URL
        valid, err = _validate_url(url)
        if not valid:
            return jsonify({"error": f"Invalid URL: {err}"}), 400

        chunk_index = int(data.get("chunk_index", 0))
        chunk_duration = int(data.get("chunk_duration", 30))  # Whisper window
        step_size = int(data.get("step_size", 25))  # Advance per chunk
        language = data.get("language") or None  # None = Whisper auto-detect

        # Cache key
        cache_key = f"{video_id}:{step_size}:{chunk_index}"

        with prefetch_lock:
            if cache_key in subtitle_cache:
                cached_result = subtitle_cache[cache_key]
                # Don't serve cached empty results — Whisper may have been wrong
                # (e.g., music intro confused VAD, or audio download was bad)
                if len(cached_result.get("segments", [])) > 0:
                    print(
                        f"[Yume] Cache hit for chunk {chunk_index} of {video_id} ({len(cached_result['segments'])} segments)"
                    )
                    cached_result["cached"] = True
                    with stats_lock:
                        server_stats["cache_hits"] += 1
                    return jsonify(cached_result)
                else:
                    # Empty result cached — re-transcribe to check if Whisper does better this time
                    print(f"[Yume] Stale empty cache for chunk {chunk_index} — re-transcribing")
                    del subtitle_cache[cache_key]

        # Calculate time windows
        # Audio sent to Whisper: starts at chunk_index * step_size, lasts chunk_duration
        whisper_start = chunk_index * step_size
        whisper_duration = chunk_duration

        # "Owned" window: only keep segments whose start falls here (deduplicates overlap)
        owned_start = whisper_start
        owned_end = whisper_start + step_size

        print(
            f"[Yume] Chunk {chunk_index}: whisper [{whisper_start}s-{whisper_start + whisper_duration}s], owns [{owned_start}s-{owned_end}s]"
        )

        with stats_lock:
            server_stats["cache_misses"] += 1

        # Try prepared full audio first (fast local slice), fall back to stream URL
        audio_path = None
        with cache_lock:
            prepared = full_audio_cache.get(video_id)
            prepared_path = prepared["path"] if prepared and os.path.exists(prepared["path"]) else None
        if prepared_path:
            audio_path = _slice_audio(prepared_path, whisper_start, whisper_duration)

        if audio_path is None:
            print("[Yume] No prepared audio, falling back to stream download")
            audio_path = _download_audio_segment(url, whisper_start, whisper_duration)

        if audio_path is None:
            return jsonify({"error": "Audio extraction failed"}), 500

        try:
            result = _transcribe_file(audio_path, language, whisper_start)
        finally:
            try:
                os.unlink(audio_path)
                # Also remove the parent temp dir (created by _download_audio_segment)
                parent = os.path.dirname(audio_path)
                if parent and os.path.isdir(parent) and os.path.basename(parent).startswith(("yume_", "tmp")):
                    shutil.rmtree(parent, ignore_errors=True)
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
        result["cached"] = False

        # Only cache results with actual segments — empty results may be wrong
        # (Whisper VAD miss, bad audio slice, etc.) and should be retried
        if len(trimmed) > 0:
            with prefetch_lock:
                if len(subtitle_cache) >= SUBTITLE_CACHE_MAX:
                    keys_to_remove = list(subtitle_cache.keys())[: len(subtitle_cache) - SUBTITLE_CACHE_MAX + 1]
                    for k in keys_to_remove:
                        del subtitle_cache[k]
                subtitle_cache[cache_key] = result

        print(
            f"[Yume] Chunk {chunk_index} done: {len(trimmed)} segments (from {raw_count} raw, {'cached' if len(trimmed) > 0 else 'not cached — empty'})"
        )
        return jsonify(result)

    except Exception as e:
        import traceback

        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


# ============================================================================
# RAW AUDIO TRANSCRIPTION (fallback)
# ============================================================================


@app.route("/transcribe", methods=["POST", "OPTIONS"])
def transcribe():
    if request.method == "OPTIONS":
        return "", 204

    try:
        data = request.get_json()
        if not data or "audio" not in data:
            return jsonify({"error": "No audio data provided"}), 400

        audio_base64 = data["audio"]
        language = data.get("language") or None  # auto-detect
        start_offset = float(data.get("start_offset", 0))

        try:
            audio_bytes = base64.b64decode(audio_base64)
        except Exception as e:
            return jsonify({"error": f"Invalid base64: {str(e)}"}), 400

        with tempfile.NamedTemporaryFile(delete=False, suffix=".webm") as f:
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


@app.route("/cache/clear", methods=["POST"])
def clear_cache():
    with prefetch_lock:
        count = len(subtitle_cache)
        subtitle_cache.clear()
    with cache_lock:
        stream_url_cache.clear()
        # Also clean up audio temp files
        for vid, entry in list(full_audio_cache.items()):
            _cleanup_audio_entry(entry)
        audio_count = len(full_audio_cache)
        full_audio_cache.clear()
    return jsonify({"cleared": count, "audio_cleared": audio_count})


@app.route("/cache/status", methods=["GET"])
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
    For deno mode: bgutil plugin works transparently (no args needed).
    We still add cookies as backup for non-download calls.
    """
    args = []

    if _is_youtube_url(url):
        if youtube_auth_method == "cookies":
            args.extend(["--cookies-from-browser", _resolve_browser_cookies()])
        elif youtube_auth_method == "deno":
            # bgutil plugin handles auth transparently — but add cookies as backup
            try:
                args.extend(["--cookies-from-browser", _resolve_browser_cookies()])
            except Exception:
                pass
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
                    print("[Yume] Flatpak Firefox is more recent, using its cookies")
                    return f"firefox:{flatpak_path}"
            elif os.path.exists(flat_ini):
                return f"firefox:{flatpak_path}"

    return browser


def _get_stream_url(url):
    """Get the direct audio stream URL, using cache to avoid repeated yt-dlp calls."""
    global stream_url_cache

    valid, err = _validate_url(url)
    if not valid:
        print(f"[Yume] Rejected invalid URL: {err} — {url[:80]}")
        return None

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

    last_stderr = ""
    for fmt_args in format_attempts:
        try:
            result = subprocess.run(
                [*_ytdlp_cmd(), "--get-url", *fmt_args, "--no-playlist", "--no-exec", *auth_args, "--", url],
                capture_output=True,
                text=True,
                timeout=30,
            )
        except subprocess.TimeoutExpired:
            print("[Yume] yt-dlp get-url timed out (30s)")
            return None
        if result.returncode == 0 and result.stdout.strip().startswith("http"):
            stream_url = result.stdout.strip().split("\n")[0]
            print("[Yume] Stream URL obtained and cached")
            # Evict oldest if cache full
            with cache_lock:
                if len(stream_url_cache) >= STREAM_URL_CACHE_MAX:
                    oldest_key = min(stream_url_cache, key=lambda k: stream_url_cache[k].get("timestamp", 0))
                    stream_url_cache.pop(oldest_key, None)
                stream_url_cache[url] = {"stream_url": stream_url, "timestamp": time.time()}
            return stream_url
        last_stderr = result.stderr or ""
        stderr_lower = last_stderr.lower()
        if ERR_REQUESTED_FORMAT in stderr_lower:
            continue  # try next format
        break  # non-format error, stop trying

    print(f"[Yume] yt-dlp get-url failed: {last_stderr[:200]}")
    return None


def _download_audio_segment(url, start_time, duration):
    """Download a specific time segment of audio using yt-dlp + ffmpeg.
    Stream URL is cached so yt-dlp is only called once per video."""
    valid, err = _validate_url(url)
    if not valid:
        print(f"[Yume] Rejected invalid URL: {err} — {url[:80]}")
        return None

    tmp_dir = tempfile.mkdtemp()
    output_path = os.path.join(tmp_dir, "segment.wav")

    try:
        stream_url = _get_stream_url(url)

        if stream_url is None:
            return _download_audio_segment_fallback(url, start_time, duration, output_path)

        # Validate stream URL from yt-dlp before passing to ffmpeg
        stream_valid, _stream_err = _validate_url(stream_url)
        if not stream_valid:
            print("[Yume] Invalid stream URL from yt-dlp, using fallback")
            stream_url_cache.pop(url, None)
            return _download_audio_segment_fallback(url, start_time, duration, output_path)

        print(f"[Yume] Extracting {duration}s from {start_time}s via ffmpeg...")

        ffmpeg_result = subprocess.run(
            [
                "ffmpeg",
                "-protocol_whitelist",
                FFMPEG_PROTOCOL_WHITELIST,
                "-ss",
                str(start_time),
                "-i",
                stream_url,
                "-t",
                str(duration),
                "-ar",
                "16000",
                "-ac",
                "1",
                "-f",
                "wav",
                "-y",
                output_path,
            ],
            capture_output=True,
            timeout=60,
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
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass
        return None
    except Exception as e:
        print(f"[Yume] Download error: {e}")
        try:
            shutil.rmtree(tmp_dir, ignore_errors=True)
        except Exception:
            pass
        return None


def _download_audio_segment_fallback(url, start_time, duration, output_path):
    """Fallback: use yt-dlp's --download-sections directly."""
    try:
        print("[Yume] Using yt-dlp fallback download method...")
        tmp_template = output_path.replace(".wav", ".%(ext)s")

        auth_args = _build_auth_args(url)

        result = subprocess.run(
            [
                *_ytdlp_cmd(),
                "--download-sections",
                f"*{start_time}-{start_time + duration}",
                "--force-keyframes-at-cuts",
                "-x",
                "--audio-format",
                "wav",
                "--postprocessor-args",
                FFMPEG_AUDIO_OPTS,
                "--no-playlist",
                "--no-exec",
                *auth_args,
                "-o",
                tmp_template,
                "--",
                url,
            ],
            capture_output=True,
            timeout=90,
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
_kakasi = None  # pykakasi.kakasi instance or None
_kakasi_checked = False  # Whether we've attempted to load pykakasi
_kakasi_lock = threading.Lock()
_pinyin_available = False


def _get_kakasi():
    """Lazy-load pykakasi (Japanese kanji→romaji converter). Thread-safe."""
    global _kakasi, _kakasi_checked
    with _kakasi_lock:
        if not _kakasi_checked:
            _kakasi_checked = True
            try:
                import pykakasi

                print(f"[Yume] pykakasi found at {pykakasi.__file__}")
                _kakasi = pykakasi.kakasi()
                print("[Yume] pykakasi loaded — deterministic Japanese romanization enabled")
            except ImportError:
                _kakasi = None
                print("[Yume] pykakasi not installed — Japanese romanization falls back to LLM")
            except Exception as e:
                _kakasi = None
                print(f"[Yume] pykakasi failed: {type(e).__name__}: {e}")
                print("[Yume] Japanese romanization falls back to LLM")
    return _kakasi


def _romanize_japanese(text):
    """Convert Japanese text (kanji/kana) to romaji using pykakasi. ~1ms."""
    kakasi = _get_kakasi()
    if not kakasi:
        return None
    try:
        result = kakasi.convert(text)
        parts = []
        for item in result:
            r = item.get("hepburn", "") or item.get("passport", "") or item.get("orig", "")
            parts.append(r)
        return " ".join(parts).strip()
    except Exception as e:
        print(f"[Yume] pykakasi error: {e}")
        return None


def _romanize_chinese(text):
    """Convert Chinese text to pinyin using pypinyin. ~1ms."""
    try:
        from pypinyin import pinyin, Style

        result = pinyin(text, style=Style.TONE)
        return " ".join(p[0] for p in result).strip()
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


@app.route("/romanize", methods=["POST", "OPTIONS"])
def romanize():
    """Deterministic romanization for ja/zh/ko. Returns in <5ms vs 1-10s for LLM.
    Falls back to {supported: false} if library not available."""
    if request.method == "OPTIONS":
        return "", 204

    data = request.get_json() or {}
    text = data.get("text", "").strip()
    lang = data.get("language") or None  # auto-detect

    if not text:
        return jsonify({"romanization": "", "method": "empty"})

    result = None
    if lang == "ja":
        result = _romanize_japanese(text)
    elif lang == "zh":
        result = _romanize_chinese(text)
    elif lang == "ko":
        result = _romanize_korean(text)

    if result is not None:
        return jsonify({"romanization": result, "method": "deterministic", "language": lang})
    else:
        return jsonify({"supported": False, "language": lang}), 501


@app.route("/romanize_batch", methods=["POST", "OPTIONS"])
def romanize_batch():
    """Batch deterministic romanization — single round trip for N texts.
    Accepts: {"texts": ["text1", "text2", ...], "language": "ja"}
    Returns: {"romanizations": ["roma1", "roma2", ...], "method": "deterministic"}
    Falls back to empty strings for unsupported languages."""
    if request.method == "OPTIONS":
        return "", 204

    data = request.get_json() or {}
    texts = data.get("texts", [])
    lang = data.get("language") or None

    if not texts or not isinstance(texts, list):
        return jsonify({"romanizations": [], "method": "empty"})

    # Pick the right romanizer
    romanizer = None
    if lang == "ja":
        romanizer = _romanize_japanese
    elif lang == "zh":
        romanizer = _romanize_chinese
    elif lang == "ko":
        romanizer = _romanize_korean

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


HALLUCINATION_PATTERNS = [
    "\u3054\u8996\u8074\u3042\u308a\u304c\u3068\u3046\u3054\u3056\u3044\u307e\u3057\u305f",
    "\u3054\u8996\u8074\u3042\u308a\u304c\u3068\u3046",
    "\u304a\u75b2\u308c\u69d8\u3067\u3057\u305f",
    "\u304a\u75b2\u308c\u69d8",
    "\u5b57\u5e55\u306f\u81ea\u52d5\u751f\u6210",
    "\u5b57\u5e55\u5236\u4f5c",
    "\u4f5c\u8a5e",
    "\u4f5c\u66f2",
    "\u7de8\u66f2",
    "\u6b4c\uff1a",
    "feat.",
    "\u8a5e\u66f2",
    "Sound Hodori",
    "\uc0ac\uc6b4\ub4dc \ud638\ub3cc\uc774",
    "\u30db\u30c9\u30ea",
    "Instagram",
    "Twitter",
    "\u30c1\u30e3\u30f3\u30cd\u30eb\u767b\u9332",
    "\u9ad8\u8a55\u4fa1",
    "\u30b5\u30d6\u30b9\u30af\u30e9\u30a4\u30d6",
    "Subscribe",
    "Like and subscribe",
    "Thank you for watching",
    "Thanks for watching",
    "Please subscribe",
    "[Music]",
    "[Applause]",
    "[Laughter]",
    "(Music)",
    # Chinese common hallucinations
    "请订阅",
    "感谢观看",
    "感谢收看",
    "字幕制作",
    "字幕组",
    "谢谢大家的支持",
    "记得点赞",
    "关注我",
    "一键三连",
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
    "Like",
    "Share",
    "Comment",
    "Follow",
]

# User-reported hallucinations (populated via /blacklist/update from extension popup)
user_blacklist = []


@app.route("/blacklist/update", methods=["POST"])
def update_blacklist():
    global user_blacklist
    data = request.get_json() or {}
    incoming = data.get("blacklist", [])
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


@app.route("/blacklist", methods=["GET"])
def get_blacklist():
    return jsonify({"blacklist": user_blacklist, "count": len(user_blacklist)})


CREDITS_PATTERNS = [
    "\u4f5c\u8a5e\u30fb\u4f5c\u66f2",
    "\u4f5c\u66f2\u30fb\u7de8\u66f2",
    "\u4f5c\u8a5e",
    "\u4f5c\u66f2",
    "\u7de8\u66f2",
    "vocals",
    "vocal",
    "guitar",
    "bass",
    "drums",
    "piano",
    "illustration",
    "illust",
    "animation",
    "video",
    "mix",
    "mastering",
]

SINGLE_WORD_BLOCKLIST = ["music", "mm", "hmm"]  # No vocal sounds (la/na/da/oh/ah are real lyrics)


@app.route("/hallucination_patterns", methods=["GET"])
def get_hallucination_patterns():
    """Server-authoritative hallucination patterns. Client fetches at startup
    instead of maintaining a duplicate list. Eliminates drift bugs."""
    return jsonify(
        {
            "builtin": HALLUCINATION_PATTERNS,
            "credits": CREDITS_PATTERNS,
            "user_blacklist": user_blacklist,
            "single_word_blocklist": SINGLE_WORD_BLOCKLIST,
            "repeat_threshold": 6,  # words >= this with <=2 unique = spam
            "concat_min_len": 4,  # min clean length for concatenated repetition check
            "concat_coverage": 0.95,  # coverage threshold for concat repetition (high to avoid dropping real choruses)
        }
    )


def _is_hallucination(text):
    t = text.strip()
    if not t:
        return True
    # Normalize Unicode (NFC) to catch alternate representations of JA/ZH/AR text
    t = unicodedata.normalize("NFC", t)
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
            if sub * (len(clean) // len(sub)) == clean[: len(sub) * (len(clean) // len(sub))]:
                repeats = len(clean) // len(sub)
                if repeats >= 2 and len(sub) * repeats >= len(clean) * 0.95:
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
        vad_filter=False,  # MUST be False - Silero VAD drops singing
        word_timestamps=False,  # v2.0.7: False (different decode path when True)
        condition_on_previous_text=False,
        temperature=0.0,
        compression_ratio_threshold=2.4,  # v2.0.7 value
        log_prob_threshold=-2.0,  # widened from -1.5: catch more speech at low confidence
        no_speech_threshold=0.3,  # lowered from 0.45: catch speech after silence/intros
    )

    # CRITICAL: Serialize model access. CTranslate2 is NOT thread-safe.
    with transcribe_lock:
        segments_iter, info = model.transcribe(audio_path, **params)  # type: ignore[arg-type]
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
        # NOTE: No secondary logprob filter here. Whisper's internal log_prob_threshold=-2.0
        # handles truly low-confidence segments. A stricter server-side filter drops
        # valid vocals mixed with background music (the primary use case for Yume).
        # Hallucination patterns + user blacklist are the correct quality filter.

        segments.append(
            {
                "start": round(seg.start + start_offset, 2),
                "end": round(seg.end + start_offset, 2),
                "text": text,
                "confidence": round(seg.avg_logprob, 2) if hasattr(seg, "avg_logprob") else 0,
            }
        )
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
        "start_offset": start_offset,
    }


# ============================================================================
# STARTUP  (ALL ASCII -- no em-dashes, no box-drawing chars)
# ============================================================================

# ============================================================================
# BGUTIL PO TOKEN SERVER MANAGEMENT
# ============================================================================
# bgutil-ytdlp-pot-provider needs a local HTTP server on port 4416 that
# solves YouTube's BotGuard challenge and generates PO tokens.
# Yume manages this server automatically when youtube_auth_method == "deno".
#
# Architecture:
#   yt-dlp -> bgutil plugin (pip) -> HTTP request to 127.0.0.1:4416
#   bgutil server (deno) -> runs BotGuard JS -> returns PO token
# ============================================================================

BGUTIL_PORT = 4416
_bgutil_proc = None  # Managed subprocess


def _bgutil_server_dir():
    """Where the bgutil server files live."""
    return Path(__file__).parent.parent / "tools" / "bgutil-ytdlp-pot-provider" / "server"


def _is_bgutil_server_ready():
    """Check if bgutil HTTP server is responding."""
    try:
        import urllib.request

        resp = urllib.request.urlopen(f"http://127.0.0.1:{BGUTIL_PORT}/ping", timeout=3)
        return resp.status == 200
    except Exception:
        return False


def _setup_bgutil_server():
    """Download and set up the bgutil server if not already present.
    Downloads the repo as a zip from GitHub, extracts, runs deno install.
    Returns True if the server directory is ready.
    """
    server_dir = _bgutil_server_dir()
    main_ts = server_dir / "src" / "main.ts"

    if main_ts.exists():
        print(f"  bgutil server:    found at {server_dir}")
        return True

    print("  bgutil server:    not found — downloading...")
    repo_parent = server_dir.parent.parent  # tools/
    repo_parent.mkdir(parents=True, exist_ok=True)

    # Download the repo zip from GitHub
    zip_path = repo_parent / "bgutil-ytdlp-pot-provider.zip"
    try:
        import urllib.request

        url = "https://github.com/Brainicism/bgutil-ytdlp-pot-provider/archive/refs/tags/1.3.1.zip"
        print("  bgutil server:    downloading from GitHub...")
        urllib.request.urlretrieve(url, str(zip_path))
    except Exception as e:
        print(f"  bgutil server:    download failed: {e}")
        return False

    # Extract
    try:
        import zipfile

        with zipfile.ZipFile(str(zip_path), "r") as zf:
            zf.extractall(str(repo_parent))
        zip_path.unlink(missing_ok=True)

        # Rename extracted dir (bgutil-ytdlp-pot-provider-1.3.1 -> bgutil-ytdlp-pot-provider)
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
        return False

    # Install dependencies with deno
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
            timeout=180,
        )
        if r.returncode == 0:
            print("  bgutil server:    dependencies installed")
        else:
            # Try without --frozen flag (may not exist in all deno versions)
            r2 = subprocess.run(
                ["deno", "install", "--allow-scripts=npm:canvas"],
                cwd=str(server_dir),
                capture_output=True,
                text=True,
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


def _start_bgutil_server():
    """Start the bgutil HTTP server on port 4416 as a background process.
    Returns True if the server starts successfully.
    """
    global _bgutil_proc

    # Already running?
    if _is_bgutil_server_ready():
        print(f"  bgutil server:    already running on port {BGUTIL_PORT}")
        return True

    server_dir = _bgutil_server_dir()
    node_modules = server_dir / "node_modules"
    main_ts = server_dir / "src" / "main.ts"

    if not main_ts.exists():
        print("  bgutil server:    main.ts not found — cannot start")
        return False

    # Determine working directory — deno needs to run from node_modules
    # to resolve npm dependencies
    cwd = str(node_modules) if node_modules.exists() else str(server_dir)

    # Compute relative path from cwd to main.ts
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

        # Wait for server to be ready (max 30s)
        for i in range(30):
            time.sleep(1)
            if _is_bgutil_server_ready():
                print(f"  bgutil server:    ready on port {BGUTIL_PORT} (PO token generation active)")
                return True
            # Check if process died
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


def _stop_bgutil_server():
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


def main():
    _cleanup_stale_temps()
    signal.signal(signal.SIGTERM, _shutdown_handler)
    signal.signal(signal.SIGINT, _shutdown_handler)
    atexit.register(_cleanup_all_audio)

    # Windows: register console control handler to intercept CLOSE/LOGOFF events.
    # Without this, Intel MKL (bundled with numpy/CTranslate2) catches the
    # CTRL_CLOSE_EVENT first and calls abort(), producing:
    #   "forrtl: error (200): program aborting due to window-CLOSE event"
    # Our handler fires before MKL's and exits cleanly.
    if sys.platform == "win32":
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
                    return True  # handled — don't pass to MKL
                return False

            kernel32.SetConsoleCtrlHandler(_win_console_handler, True)
            # prevent garbage collection of the callback (must survive until process exit)
            global _win_console_handler_ref
            _win_console_handler_ref = _win_console_handler
        except Exception:
            pass  # non-critical — worst case is the old MKL abort behavior

    global model, model_name, model_display_name, device, compute_type
    global use_word_timestamps, pause_threshold
    global youtube_auth_method, cookies_browser
    global translation_host, translation_port, translation_backend
    global translation_prompt, romanization_prompt
    global TOKEN_FILE

    parser = argparse.ArgumentParser(description="Yume Whisper Server")
    parser.add_argument("--model", default="large-v3")
    parser.add_argument("--device", default="cuda")
    parser.add_argument("--compute-type", default="float16")
    parser.add_argument("--port", type=int, default=5001)
    parser.add_argument("--no-word-timestamps", action="store_true", help="Disable word-level timestamps")
    parser.add_argument("--pause-threshold", type=float, default=0.25, help="Seconds of silence to split segments")
    parser.add_argument("--config", type=str, default=None, help="Path to yume_config.json")
    parser.add_argument(
        "--prewarm", action="store_true", help="Run CUDA kernel warmup at startup (slower startup, faster first chunk)"
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    # Configure logging level
    if args.verbose or os.environ.get("LOG_LEVEL", "").upper() == "DEBUG":
        logging.basicConfig(level=logging.DEBUG, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    else:
        logging.basicConfig(level=logging.WARNING)
        # Suppress Flask/werkzeug access logs and dev server warning when not verbose
        logging.getLogger("werkzeug").setLevel(logging.ERROR)

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
        model_display_name = cfg.get("whisper_model_name", "")
        # NOTE: whisper_device and whisper_compute_type are NOT loaded from config here.
        # The CLI resolves "auto" → "cuda"/"cpu" before launching the server,
        # and passes the resolved value via --device and --compute-type.
        # Loading from config would overwrite the CLI's resolved value back to "auto".
        use_word_timestamps = cfg.get("word_timestamps", use_word_timestamps)
        pause_threshold = cfg.get("pause_threshold", pause_threshold)
        port = cfg.get("whisper_port", port)
        youtube_auth_method = cfg.get("youtube_auth_method", youtube_auth_method)
        cookies_browser = cfg.get("cookies_browser", cookies_browser)
        translation_host = cfg.get("translation_host", translation_host)
        translation_port = cfg.get("translation_port", translation_port)
        translation_backend = cfg.get("translation_backend", translation_backend)
        translation_prompt = cfg.get("translation_prompt", "")
        romanization_prompt = cfg.get("romanization_prompt", "")

    # Handle 'auto' device — use CTranslate2's detection (not torch!)
    # torch.cuda.is_available() can be False even when CTranslate2 has CUDA support
    if device == "auto":
        try:
            import ctranslate2

            if "cuda" in ctranslate2.get_supported_compute_types("cuda"):
                device = "cuda"
            else:
                device = "cpu"
        except Exception:
            # Fallback: try torch, then nvidia-smi
            try:
                import torch

                device = "cuda" if torch.cuda.is_available() else "cpu"
            except ImportError:
                try:
                    # subprocess is already imported at module level
                    r = subprocess.run(["nvidia-smi"], capture_output=True, timeout=5)
                    device = "cuda" if r.returncode == 0 else "cpu"
                except Exception:
                    device = "cpu"
    if compute_type == "auto":
        compute_type = "float16" if device == "cuda" else "int8"

    print("=" * 70)
    print("  YUME -- Whisper Server v0.0.8")
    print("=" * 70)
    print(f"  Model:            {model_name}")
    print(f"  Device:           {device}")
    print(f"  Compute Type:     {compute_type}")
    print(f"  Port:             {port}")
    print("  Architecture:     download-once + local slice")
    print("  Whisper Params:   v2.0.7 (proven for music)")
    print("  VAD Filter:       OFF (required for music)")
    yt_info = youtube_auth_method
    if youtube_auth_method == "cookies":
        yt_info += f" ({cookies_browser})"
    print(f"  YouTube Auth:     {yt_info}")

    # Deterministic romanization: lightweight check only (no dictionary loading at startup).
    # Full initialization happens lazily on first /romanize request.
    # Using import-only (not kakasi() constructor) because the dictionary load
    # can take 30-120s on Windows with real-time antivirus scanning.
    roma_parts = []
    print(f"  Python exe:       {sys.executable}")
    try:
        import pykakasi  # noqa: F401

        roma_parts.append("ja(pykakasi)")
    except Exception as e:
        # Show where Python is looking for packages
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
        from romanization import romanize  # noqa: F401

        roma_parts.append("ko(romanization)")
    except ImportError:
        pass  # optional, don't spam
    if roma_parts:
        print(f"  Romanization:     {', '.join(roma_parts)} (instant)")
    else:
        print("  Romanization:     LLM-only (pip install pykakasi pypinyin for instant)")

    print("=" * 70)

    # --- Tool version checks (deferred to background — don't block model load) ---
    def _check_tool_versions():
        """Check yt-dlp and ffmpeg versions in background. Prints after model loads."""
        results = []
        try:
            if not _check_ytdlp():
                results.append(("yt-dlp", None, ["  WARNING: yt-dlp not found in PATH!"]))
            else:
                v = subprocess.run(
                    [*_ytdlp_cmd(), "--version"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
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
                v = subprocess.run(
                    ["ffmpeg", "-version"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
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

    # Auth method viability check
    #
    # How "deno" auth works:
    #   YouTube requires a "proof-of-origin" (PO) token to prove you're a real browser.
    #   The bgutil-ytdlp-pot-provider plugin solves YouTube's BotGuard challenge using
    #   Deno as a JavaScript runtime, then passes the PO token to yt-dlp automatically.
    #   Once installed, yt-dlp uses it transparently — no extra yt-dlp arguments needed.
    #
    # Requirements: deno >= 2.0 in PATH + bgutil-ytdlp-pot-provider pip package
    #
    if youtube_auth_method == "deno":
        deno_found = False
        try:
            r = subprocess.run(["deno", "--version"], capture_output=True, text=True, timeout=5)
            deno_found = r.returncode == 0
            if deno_found:
                deno_ver = r.stdout.split("\n")[0].strip()
                print(f"  Deno:             {deno_ver}")
        except Exception:
            pass

        if deno_found:
            # Deno mode requires pip-installed yt-dlp (not standalone binary)
            # because only pip yt-dlp discovers pip-installed plugins (like bgutil).
            pip_ytdlp_ok = False
            try:
                r = subprocess.run(
                    [sys.executable, "-m", "yt_dlp", "--version"], capture_output=True, text=True, timeout=10
                )
                pip_ytdlp_ok = r.returncode == 0
                if pip_ytdlp_ok:
                    print(f"  yt-dlp (pip):     {r.stdout.strip()}")
            except Exception:
                pass

            if not pip_ytdlp_ok:
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

            # Check if bgutil PO token plugin is installed
            # bgutil is a yt-dlp plugin — detect via pip show, not importlib
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

            if not bgutil_installed:
                print("  PO Token plugin:  not found — installing bgutil-ytdlp-pot-provider...")
                try:
                    subprocess.run(
                        [
                            sys.executable,
                            "-m",
                            "pip",
                            "install",
                            "-q",
                            "--no-warn-script-location",
                            "bgutil-ytdlp-pot-provider",
                        ],
                        capture_output=True,
                        timeout=120,
                    )
                    print("  PO Token plugin:  installed (YouTube BotGuard bypass via Deno)")
                except Exception as e:
                    print(f"  PO Token plugin:  install failed ({e})")
                    print("  YouTube may require sign-in. Fallback: set youtube_auth_method='cookies'")
            else:
                print("  PO Token plugin:  bgutil-ytdlp-pot-provider (active)")

            # Setup and start the bgutil HTTP server (port 4416)
            # This is the actual PO token generation server that the plugin connects to
            if _setup_bgutil_server():
                _start_bgutil_server()
                atexit.register(_stop_bgutil_server)
            else:
                print("  bgutil server:    setup failed — YouTube downloads may fail")
                print("  bgutil server:    will try cookies fallback automatically")
        else:
            print("")
            print("  WARNING: youtube_auth_method is 'deno' but Deno is not installed.")
            print("  Auto-switching to 'cookies' auth.")
            print("  To fix: install Deno (https://deno.land), or set youtube_auth_method='cookies'.")
            youtube_auth_method = "cookies"
            if cookies_browser == "chrome":
                if platform.system() != "Windows":
                    cookies_browser = "firefox"
            print(f"  Now using: cookies ({cookies_browser})")
            print("")

    # Write API token file for extension discovery (before model load so
    # the extension can discover the server while model is still loading)
    base_dir = Path(__file__).parent.parent.resolve()
    TOKEN_FILE = str(base_dir / ".yume_token")
    try:
        with open(TOKEN_FILE, "w") as f:
            f.write(API_TOKEN)
        os.chmod(TOKEN_FILE, 0o600)  # owner-only read/write
        print(f"  API token:        written to {TOKEN_FILE}")
    except Exception as e:
        print(f"  API token:        file write FAILED ({e})")
        print("                    Extension may not auto-discover the server.")

    print("  Security:         Host validation + API token + URL validation")

    # ── Parallel startup: start HTTP server FIRST, then load model ──
    # The /health endpoint returns {"status": "loading"} while model is None,
    # so the extension can connect immediately and show "Loading model..." to
    # the user instead of "Server not reachable".
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

    server_thread = threading.Thread(target=_start_server, daemon=True)
    server_thread.start()

    print("")
    print(f"  Listening on http://localhost:{port}  (status: loading)")
    print("  Loading Whisper model...")
    print("")

    # Pre-load pykakasi dictionary in background thread BEFORE model load.
    # The dictionary load can take 30-120s on Windows (Defender scans each file).
    # By starting it now, it runs in parallel with the ~15s Whisper model load,
    # so it's usually ready before the extension's first romanization probe.
    def _init_kakasi_background():
        try:
            _get_kakasi()
        except Exception as e:
            print(f"[Yume] Background kakasi init failed: {e}")

    threading.Thread(target=_init_kakasi_background, daemon=True).start()

    try:
        model = WhisperModel(model_name, device=device, compute_type=compute_type)

        # Prewarm: run a tiny dummy inference to trigger CUDA kernel compilation
        # and KV cache allocation. Adds ~10-15s to startup but makes first real
        # chunk faster. Also detects missing CUDA libraries before user's first request.
        # Skip with --no-prewarm for faster startup (first chunk takes the hit instead).
        if not args.prewarm:
            print("  Prewarm:          skipped (use --prewarm to enable)")
        else:
            import numpy as np

            _dummy = np.zeros(16000, dtype=np.float32)  # 1 second of silence
            try:
                list(model.transcribe(_dummy, language="en"))
                print("  Prewarm:          done (CUDA kernels compiled)")
            except Exception as pw_err:
                pw_msg = str(pw_err)
                cuda_lib_missing = any(
                    lib in pw_msg.lower()
                    for lib in [
                        "cublas",
                        "cudnn",
                        "cudart",
                        "cufft",
                        "cusolver",
                        "is not found or cannot be loaded",
                    ]
                )
                if cuda_lib_missing and device == "cuda":
                    print(f"  Prewarm:          CUDA library missing: {pw_msg[:80]}")
                    print("")
                    print("  WARNING: CUDA libraries are incomplete.")
                    print("  The model loaded but inference requires cuBLAS/cuDNN.")
                    print("  Falling back to CPU mode automatically.")
                    print("")
                    try:
                        del model
                        device = "cpu"
                        compute_type = "int8"
                        model = WhisperModel(model_name, device="cpu", compute_type="int8")
                        list(model.transcribe(_dummy, language="en"))
                        print("  Prewarm:          done (CPU fallback)")
                    except Exception as cpu_err:
                        print(f"  Prewarm:          CPU fallback also failed: {cpu_err}")
                else:
                    print(f"  Prewarm:          skipped ({pw_err})")

        print("=" * 70)
        print("  MODEL LOADED -- Server ready")
        print(f"  Listening on http://localhost:{port}")
        print("=" * 70)
        print("")

        # Keep main thread alive (server runs in daemon thread)
        server_thread.join()

    except Exception as e:
        err_msg = str(e).lower()
        print("")
        print("=" * 70)
        print(f"  FAILED TO LOAD MODEL: {e}")
        print("=" * 70)

        # ── Actionable error messages with easy solutions ──
        if "out of memory" in err_msg or "oom" in err_msg or "cuda" in err_msg and "memory" in err_msg:
            print("")
            print("  CAUSE: Your GPU doesn't have enough VRAM for this model.")
            print("")
            print("  SOLUTIONS (pick one):")
            print("    1. Use a smaller model:")
            print("       python pocket_yume.py settings  → change Whisper model to 'small' or 'base'")
            print("    2. Use CPU instead (slower but works):")
            print("       python pocket_yume.py settings  → set device to 'cpu'")
            print("    3. Or restart with: --device cpu --compute-type int8")
        elif "cublas" in err_msg or "cudnn" in err_msg or "cudart" in err_msg:
            print("")
            print("  CAUSE: CUDA libraries are missing or incomplete.")
            print("")
            print("  SOLUTIONS (pick one):")
            print("    1. Install CUDA Toolkit: https://developer.nvidia.com/cuda-toolkit")
            print("    2. Or switch to CPU mode (no CUDA needed):")
            print("       python pocket_yume.py settings  → set device to 'cpu'")
            print("    3. Or restart with: --device cpu --compute-type int8")
        elif "no module" in err_msg or "modulenotfound" in err_msg:
            missing = str(e).split("'")[1] if "'" in str(e) else "unknown"
            print("")
            print(f"  CAUSE: Missing Python package: {missing}")
            print("")
            print("  SOLUTION: Run the setup wizard to install dependencies:")
            print("    python pocket_yume.py setup")
            print(f"    Or manually: pip install {missing}")
        elif ERR_NO_SUCH_FILE in err_msg or "filenotfound" in err_msg:
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
        sys.exit(1)


if __name__ == "__main__":
    main()
