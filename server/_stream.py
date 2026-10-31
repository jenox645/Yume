"""A video's stream: its URL, cached so yt-dlp does not run for every region,
and single segments cut from it. The stream preview and streamed regions use
these; a full download goes through _download.
"""

import http.client
import os
import shutil
import subprocess
import tempfile
import time
import urllib.request
from urllib.parse import urlparse

import _state
import _ytdlp
from _security import validate_url


_WAV_16K_MONO = ("-ar", "16000", "-ac", "1", "-f", "wav")


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
    """Download a specific time segment using yt-dlp + ffmpeg (stream URL cached)."""
    if not _is_valid(url):
        return None

    # yume_ prefix is required: the caller's cleanup and the startup sweep
    # only remove yume_* directories — an unprefixed dir leaks on every call.
    tmp_dir = tempfile.mkdtemp(prefix="yume_")
    output_path = os.path.join(tmp_dir, "segment.wav")

    path = None
    try:
        path = _segment_from_stream(url, start_time, duration, output_path) or _download_audio_segment_fallback(
            url, start_time, duration, output_path
        )
    except subprocess.TimeoutExpired:
        print("[Yume] Download timed out")
    except Exception as e:
        print(f"[Yume] Download error: {e}")
    if path is None:
        shutil.rmtree(tmp_dir, ignore_errors=True)
    return path


def _segment_from_stream(url, start_time, duration, output_path):
    """The segment cut from the stream, or None: use the fallback."""
    stream_url = get_stream_url(url)
    if stream_url is None:
        return None
    if not validate_url(stream_url)[0]:
        print("[Yume] Invalid stream URL from yt-dlp, using fallback")
        _forget_stream_url(url)
        return None
    if start_time == 0 and _fetch_stream_start(stream_url, duration, output_path):
        return output_path
    if _ffmpeg_segment(stream_url, start_time, duration, output_path):
        return output_path
    _forget_stream_url(url)
    return None


def _ffmpeg_segment(stream_url, start_time, duration, output_path):
    print(f"[Yume] Extracting {duration}s from {start_time}s via ffmpeg...")
    cmd = [
        "ffmpeg", "-protocol_whitelist", _state.FFMPEG_PROTOCOL_WHITELIST, "-ss", str(start_time),
        "-i", stream_url, "-t", str(duration), *_WAV_16K_MONO, "-y", output_path,
    ]  # fmt: skip
    result = subprocess.run(cmd, capture_output=True, timeout=60)
    if result.returncode == 0 and os.path.exists(output_path):
        size = os.path.getsize(output_path)
        print(f"[Yume] Audio segment downloaded: {size / 1024:.1f} KB")
        return True
    err_msg = result.stderr[-200:] if result.stderr else b""
    print(f"[Yume] ffmpeg failed: {err_msg.decode('utf-8', errors='ignore')}")
    return False


# 200 kbps: more than audio-only YouTube streams use (251 is ~110-160). YouTube
# answers 403 to a range above roughly 800-950 KB, so 30 s must stay under that.
_STREAM_BYTES_PER_S = 25_000


def _fetch_stream_start(stream_url, duration, output_path):
    """The first `duration` seconds of a YouTube stream, as yt-dlp's own
    downloader fetches it: by byte range. ffmpeg asking for the whole URL gets
    403 Forbidden, and so does yt-dlp's --download-sections, which runs ffmpeg:
    the preview failed on 3 videos out of 5 and subtitles waited 33-70 s for the
    full download. False: use another way."""
    if not (urlparse(stream_url).hostname or "").endswith(".googlevideo.com"):
        return False
    size = int(duration * _STREAM_BYTES_PER_S)
    part = output_path + ".part"
    try:
        req = urllib.request.Request(f"{stream_url}&range=0-{size}", headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=20) as resp:  # nosec B310 — https, checked by validate_url
            data = resp.read(size + 1)
        with open(part, "wb") as f:
            f.write(data)
        result = subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-i", part, "-t", str(duration), *_WAV_16K_MONO, output_path],
            capture_output=True,
            timeout=30,
        )
    except (OSError, http.client.HTTPException, subprocess.TimeoutExpired) as e:  # urllib's errors are OSErrors
        print(f"[Yume] Could not fetch the start of the stream: {e}")
        return False
    finally:
        if os.path.exists(part):
            os.remove(part)
    return result.returncode == 0 and os.path.exists(output_path)


def _download_audio_segment_fallback(url, start_time, duration, output_path, _retry=True):
    """Fallback: use yt-dlp --download-sections directly."""
    try:
        print("[Yume] Using yt-dlp fallback download method...")
        tmp_template = output_path.replace(".wav", ".%(ext)s")
        auth_args = _ytdlp.build_auth_args(url)

        cmd = [
            *_ytdlp.ytdlp_cmd(), "--download-sections", f"*{start_time}-{start_time + duration}",
            "--force-keyframes-at-cuts", "-x", "--audio-format", "wav",
            "--postprocessor-args", _state.FFMPEG_AUDIO_OPTS, "--no-playlist", "--no-exec", *auth_args,
            "-o", tmp_template, "--", url,
        ]  # fmt: skip
        result = subprocess.run(cmd, capture_output=True, timeout=90)

        if result.returncode == 0 and os.path.exists(output_path):
            return output_path

        stderr = (result.stderr or b"").decode("utf-8", errors="replace")
        if _retry and auth_args and _ytdlp.note_cookie_failure(stderr):
            return _download_audio_segment_fallback(url, start_time, duration, output_path, _retry=False)
        print(f"[Yume] Fallback also failed: {stderr[-200:]}")
        return None

    except Exception as e:
        print(f"[Yume] Fallback error: {e}")
        return None
