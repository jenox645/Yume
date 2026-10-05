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

# Set by the model-loader thread when loading fails; /health reports it as
# status "error" instead of "loading" forever.
load_error = ""

# Prevent garbage collection of Windows console handler ctypes callback
_win_console_handler_ref = None

# ── String constants (SonarCloud S1192 — no duplicated literals) ─────────────
FFMPEG_PROTOCOL_WHITELIST = "file,http,https,tcp,tls,crypto"
FFMPEG_AUDIO_OPTS = "ffmpeg:-ar 16000 -ac 1"
YT_PLAYER_CLIENT_TV_WEB = "youtube:player_client=tv,web"
ERR_REQUESTED_FORMAT = "requested format"
ERR_NO_SUCH_FILE = "no such file"

# ── Stream URL cache (yt-dlp --get-url results, for streaming previews) ───────
# { video_url: {"stream_url": "...", "timestamp": float} }
stream_url_cache: dict = {}
STREAM_URL_CACHE_MAX = 100
STREAM_URL_TTL = 300  # 5 minutes (YouTube stream URLs expire)

# ── Thread locks ──────────────────────────────────────────────────────────────
# Serialises compound mutations of stream_url_cache.
cache_lock = threading.Lock()

# CRITICAL: Whisper model is NOT thread-safe.  Concurrent transcribe() calls
# produce corrupted/empty results.  Serialise all transcription through this lock.
transcribe_lock = threading.Lock()

# Serialises /model/switch — two concurrent switches would each load a model,
# briefly tripling VRAM and leaving whichever finishes last as the winner.
model_switch_lock = threading.Lock()

# ── YouTube auth ──────────────────────────────────────────────────────────────
youtube_auth_method = "cookies"  # "cookies" or "deno"
cookies_browser = "chrome"

# ── Translation server settings (the server calls the LLM itself) ─────────────
translation_host = "127.0.0.1"
translation_port = 5000
translation_backend = "llamacpp"
translation_model = ""  # required by Ollama/LM Studio; llama.cpp serves one model
translation_prompt = ""  # custom template with {src}/{tgt}; "" = built-in
romanization_prompt = ""  # custom template with {src}/{sys}; "" = built-in

# Config file the translation settings come from; re-read when it changes so
# CLI edits (backend, address, model, prompts) apply without a restart.
config_file = ""
config_mtime = 0.0

# ── Durable cache (set in main(): _store.Store) and job manager ───────────────
store = None
jobs = None

# ── Session statistics ────────────────────────────────────────────────────────
server_stats: dict = {
    "start_time": time.time(),
    "regions_transcribed": 0,
    "segments_produced": 0,
    "hallucinations_filtered": 0,
    "total_audio_seconds": 0.0,
    "total_whisper_time": 0.0,
    "downloads_completed": 0,
    "lines_translated": 0,
    "translation_cache_hits": 0,
    "errors": 0,
    "last_region_whisper_time": 0.0,
    "last_region_segments": 0,
}
stats_lock = threading.Lock()

# ── Hallucination filter state ────────────────────────────────────────────────
# Single source of truth for the user blacklist (the CLI and the extension
# popup both edit it through the server, or this file while it is down).
user_blacklist: list = []
# Persistence file (config/blacklist.json) — set in _apply_config.
blacklist_file: str = ""

# ── API token ─────────────────────────────────────────────────────────────────
# Generated at startup in main(); empty string is never valid.
API_TOKEN = ""
TOKEN_FILE = None
