"""Shared mutable state for the Yume Whisper server.

All modules import this as:  import _state
Access globals as:           _state.model   (NOT 'from _state import model')

Python module objects are singletons.  Mutating _state.model in one module
is immediately visible to every other module that did 'import _state'.
Using 'from _state import model' creates a local binding — mutations are NOT
shared.  Always use attribute access: _state.model = x, not model = x.
"""

import threading
import time

# ── Whisper model ────────────────────────────────────────────────────────────
model = None
model_name = "large-v3"
model_display_name = ""  # Friendly name for custom models (from config)
device = "cuda"
compute_type = "float16"

# Prevent garbage collection of Windows console handler ctypes callback
_win_console_handler_ref = None
use_word_timestamps = True
pause_threshold = 0.25  # seconds of silence to split segments

# ── String constants (SonarCloud S1192 — no duplicated literals) ─────────────
FFMPEG_PROTOCOL_WHITELIST = "file,http,https,tcp,tls,crypto"
FFMPEG_AUDIO_OPTS = "ffmpeg:-ar 16000 -ac 1"
YT_PLAYER_CLIENT_TV_WEB = "youtube:player_client=tv,web"
ERR_REQUESTED_FORMAT = "requested format"
ERR_NO_SUCH_FILE = "no such file"

# ── Subtitle cache ────────────────────────────────────────────────────────────
subtitle_cache: dict = {}
SUBTITLE_CACHE_MAX = 2000
prefetch_lock = threading.Lock()

# ── Full audio cache ──────────────────────────────────────────────────────────
# { video_id: { "path": str, "duration": float, "timestamp": float } }
# bounded: max AUDIO_CACHE_MAX entries
full_audio_cache: dict = {}
AUDIO_CACHE_MAX = 50
FULL_AUDIO_TTL = 600  # 10 minutes

# ── Stream URL cache ──────────────────────────────────────────────────────────
# { video_url: {"stream_url": "...", "timestamp": float} }
# bounded: max STREAM_URL_CACHE_MAX entries (~20 MB max)
stream_url_cache: dict = {}
STREAM_URL_CACHE_MAX = 100
STREAM_URL_TTL = 300  # 5 minutes (YouTube stream URLs expire)

# ── Thread locks ──────────────────────────────────────────────────────────────
# Individual dict operations are GIL-atomic, but compound ops (check+insert,
# evict+add) are not.  This lock serialises all cache mutations.
cache_lock = threading.Lock()

# CRITICAL: Whisper model is NOT thread-safe.  Concurrent transcribe() calls
# produce corrupted/empty results.  Serialise all transcription through this lock.
transcribe_lock = threading.Lock()

# ── YouTube auth ──────────────────────────────────────────────────────────────
youtube_auth_method = "cookies"  # "cookies" or "deno"
cookies_browser = "chrome"

# ── Translation server settings ───────────────────────────────────────────────
translation_host = "127.0.0.1"
translation_port = 5000
translation_backend = "llamacpp"
translation_prompt = ""   # Custom prompt forwarded to extension via /health
romanization_prompt = ""  # Custom romanization prompt forwarded via /health

# ── Session statistics ────────────────────────────────────────────────────────
server_stats: dict = {
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

# ── Hallucination filter state ────────────────────────────────────────────────
# Populated via /blacklist/update from the extension popup.
user_blacklist: list = []

# ── API token ─────────────────────────────────────────────────────────────────
# Generated at startup in main(); empty string is never valid.
API_TOKEN = ""
TOKEN_FILE = None
