"""A video's stream: its URL, cached so yt-dlp does not run for every region,
and single segments read from a download. The stream preview and streamed
regions use these; a full download goes through _download.
"""

import os
import shutil
import subprocess
import tempfile
import time

import _state
import _ytdlp
from _security import validate_url


_WAV_16K_MONO = ("-ar", "16000", "-ac", "1", "-f", "wav")
_WAV_HEADER = 78  # ffmpeg's, with nothing after it when the download gave no audio
PIPE_TIMEOUT_S = 60


def _is_valid(url):
    valid, err = validate_url(url)
    if not valid:
        print(f"[Yume] Rejected invalid URL: {err} — {url[:80]}")
    return valid


# ── Stream URL ────────────────────────────────────────────────────────────────

_FORMAT_ATTEMPTS = (
    # Audio-only first (see _download._FORMAT_PASSES): avoid pulling a combined
    # video+audio stream when an audio-only track exists.
    ["--format", "bestaudio/bestaudio*/best"],
    ["--format", "bestaudio/best"],
    [],
)


def stream_category(url):
    """The site's category for a video whose stream URL is known ("" otherwise)."""
    return (_state.stream_url_cache.get(url) or {}).get("category", "")


def _cached_stream_url(url):
    cached = _state.stream_url_cache.get(url)
    if cached and (time.time() - cached["timestamp"]) < _state.STREAM_URL_TTL:
        print(f"[Yume] Using cached stream URL (age: {time.time() - cached['timestamp']:.0f}s)")
        return cached["stream_url"]
    return None


def _remember_stream_url(url, stdout):
    """Cache what yt-dlp printed (the URL, then the category); returns the URL."""
    lines = stdout.strip().split("\n")
    print("[Yume] Stream URL obtained and cached")
    with _state.cache_lock:
        if len(_state.stream_url_cache) >= _state.STREAM_URL_CACHE_MAX:
            oldest_key = min(_state.stream_url_cache, key=lambda k: _state.stream_url_cache[k].get("timestamp", 0))
            _state.stream_url_cache.pop(oldest_key, None)
        _state.stream_url_cache[url] = {
            "stream_url": lines[0],
            "category": lines[-1].strip(),
            "timestamp": time.time(),
        }
    return lines[0]


def _forget_stream_url(url):
    with _state.cache_lock:
        _state.stream_url_cache.pop(url, None)


def _ask_ytdlp(url, fmt_args, auth_args):
    cmd = [
        *_ytdlp.ytdlp_cmd(), "--print", "urls", "--print", "%(categories.0)s", *fmt_args,
        "--no-playlist", "--no-exec", *auth_args, "--", url,
    ]  # fmt: skip
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)


def get_stream_url(url):
    """Get the direct audio stream URL, caching to avoid repeated yt-dlp calls."""
    if not _is_valid(url):
        return None
    return _cached_stream_url(url) or _resolve_stream_url(url)


def _resolve_stream_url(url):
    auth_args = _ytdlp.build_auth_args(url)
    last_stderr = ""
    for fmt_args in _FORMAT_ATTEMPTS:
        try:
            result = _ask_ytdlp(url, fmt_args, auth_args)
        except subprocess.TimeoutExpired:
            print("[Yume] yt-dlp get-url timed out (30s)")
            return None
        except OSError as e:  # not installed
            print(f"[Yume] yt-dlp get-url could not run: {e}")
            return None
        if result.returncode == 0 and result.stdout.strip().startswith("http"):
            return _remember_stream_url(url, result.stdout)
        last_stderr = result.stderr or ""
        if auth_args and _ytdlp.note_cookie_failure(last_stderr):
            # The stream preview often hits the cookie error before the full
            # download does: retry without cookies (build_auth_args skips them now)
            print("[Yume] Browser cookies unreadable — retrying without them")
            return get_stream_url(url)
        if _state.ERR_REQUESTED_FORMAT not in last_stderr.lower():
            break

    print(f"[Yume] yt-dlp get-url failed: {last_stderr[:200]}")
    return None


# ── Segments ──────────────────────────────────────────────────────────────────


def download_audio_segment(url, start_time, duration):
    """A stretch of the audio, read from an ordinary download through a pipe: yt-dlp
    writes the file from its start, ffmpeg keeps the part asked for, and closing the
    pipe ends the download. 30 s took 4-11 s, also at 10:00 and 20:00, on 8 videos of
    8. It replaced fetching the stream by byte range, which stopped at about 900 KB
    and failed on 3 tries in 8, and ffmpeg seeking in the stream, which failed always."""
    if not _is_valid(url):
        return None

    # yume_ prefix is required: the caller's cleanup and the startup sweep
    # only remove yume_* directories — an unprefixed dir leaks on every call.
    tmp_dir = tempfile.mkdtemp(prefix="yume_")
    output_path = os.path.join(tmp_dir, "segment.wav")

    path = None
    try:
        path = _pipe_segment(url, start_time, duration, output_path)
    except subprocess.TimeoutExpired:
        print("[Yume] Download timed out")
    except Exception as e:
        print(f"[Yume] Download error: {e}")
    if path is None:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return path


def _pipe_segment(url, start_time, duration, output_path):
    print(f"[Yume] Reading {duration}s from {start_time:.0f}s of the download...")
    ytdlp = [
        *_ytdlp.ytdlp_cmd(), "--quiet", "--no-warnings", "--format", "bestaudio/best", "--no-playlist",
        "--output", "-", "--", url,
    ]  # fmt: skip
    # After -i: ffmpeg decodes up to the start, it does not seek in the stream
    ffmpeg = [
        "ffmpeg", "-y", "-loglevel", "error", "-i", "pipe:0", "-ss", str(start_time), "-t", str(duration),
        *_WAV_16K_MONO, output_path,
    ]  # fmt: skip
    download = subprocess.Popen(ytdlp, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        result = subprocess.run(ffmpeg, stdin=download.stdout, capture_output=True, timeout=PIPE_TIMEOUT_S)
    finally:
        download.stdout.close()
        download.terminate()
        download.wait(timeout=10)
    if result.returncode == 0 and os.path.getsize(output_path) > _WAV_HEADER:
        return output_path
    print(f"[Yume] Could not read the download: {result.stderr[-200:].decode('utf-8', 'replace')}")
    return None
