"""Audio download and slicing helpers.

Handles all yt-dlp and ffmpeg interactions:
  - Full-video audio download (Strategy 1: yt-dlp, Strategy 2: get-url+ffmpeg,
    Strategy 3: ffmpeg direct)
  - Stream URL caching (avoids calling yt-dlp --get-url for every chunk)
  - Per-chunk slicing from a cached local file
  - Auth strategy selection (cookies vs deno/bgutil)
"""

import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlparse

import _state
from _security import validate_url


# ── yt-dlp availability cache (module-private) ────────────────────────────────
_ytdlp_cache: dict = {"available": None, "checked_at": 0}


# ── yt-dlp command selection ──────────────────────────────────────────────────


def ytdlp_cmd():
    """Return the yt-dlp command prefix.

    When youtube_auth_method == 'deno', we MUST use the pip-installed yt-dlp
    (python -m yt_dlp) because only pip-installed yt-dlp discovers pip-installed
    plugins like bgutil-ytdlp-pot-provider.  A standalone binary does NOT search
    site-packages for plugins.
    """
    if _state.youtube_auth_method == "deno":
        return [sys.executable, "-m", "yt_dlp"]
    return ["yt-dlp"]


def check_ytdlp():
    """Check if yt-dlp is available (cached for 60 s)."""
    now = time.time()
    if _ytdlp_cache["available"] is not None and now - _ytdlp_cache["checked_at"] < 60:
        return _ytdlp_cache["available"]
    try:
        result = subprocess.run(ytdlp_cmd() + ["--version"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10)
        available = result.returncode == 0
    except Exception:
        available = False
    _ytdlp_cache["available"] = available
    _ytdlp_cache["checked_at"] = now
    return available


# ── URL helpers ───────────────────────────────────────────────────────────────


def is_youtube_url(url):
    """Return True if url points to YouTube (for YouTube-specific yt-dlp args)."""
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


# ── Cookie / auth helpers ─────────────────────────────────────────────────────


def resolve_browser_cookies():
    """Resolve the correct --cookies-from-browser string.

    On Fedora/Linux, Firefox is often installed as Flatpak and cookies live at
    ~/.var/app/org.mozilla.firefox/.mozilla/firefox/ instead of ~/.mozilla/firefox/.
    yt-dlp can't find them without the path hint.
    """
    browser = _state.cookies_browser or "firefox"

    if browser.lower() == "firefox" and platform.system() == "Linux":
        flatpak_path = os.path.expanduser("~/.var/app/org.mozilla.firefox/.mozilla/firefox")
        native_path = os.path.expanduser("~/.mozilla/firefox")

        if os.path.isdir(flatpak_path):
            if not os.path.isdir(native_path):
                print(f"[Yume] Detected Flatpak Firefox, using cookie path: {flatpak_path}")
                return f"firefox:{flatpak_path}"
            flat_ini = os.path.join(flatpak_path, "profiles.ini")
            native_ini = os.path.join(native_path, "profiles.ini")
            if os.path.exists(flat_ini) and os.path.exists(native_ini):
                if os.path.getmtime(flat_ini) > os.path.getmtime(native_ini):
                    print("[Yume] Flatpak Firefox is more recent, using its cookies")
                    return f"firefox:{flatpak_path}"
            elif os.path.exists(flat_ini):
                return f"firefox:{flatpak_path}"

    return browser


def build_auth_args(url):
    """Build yt-dlp auth arguments for non-download calls (get-url, prepare).

    Download calls handle their own multi-strategy retries.
    For deno mode: bgutil plugin works transparently (no args needed).
    We still add cookies as backup for non-download calls.
    """
    args = []
    if is_youtube_url(url):
        if _state.youtube_auth_method == "cookies":
            args.extend(["--cookies-from-browser", resolve_browser_cookies()])
        elif _state.youtube_auth_method == "deno":
            try:
                args.extend(["--cookies-from-browser", resolve_browser_cookies()])
            except Exception:
                pass
    elif _state.youtube_auth_method == "cookies":
        args.extend(["--cookies-from-browser", resolve_browser_cookies()])
    return args


# ── Audio temp file cleanup ───────────────────────────────────────────────────


def cleanup_audio_entry(entry):
    """Delete the temp directory for a cached audio file."""
    try:
        parent = os.path.dirname(entry.get("path", ""))
        basename = os.path.basename(parent)
        if parent and os.path.isdir(parent) and (basename.startswith("yume_") or basename.startswith("tmp")):
            shutil.rmtree(parent, ignore_errors=True)
    except Exception:
        pass


def cleanup_all_audio():
    """Called at server shutdown — clean up all cached audio temp files."""
    for _vid, entry in list(_state.full_audio_cache.items()):
        cleanup_audio_entry(entry)
    _state.full_audio_cache.clear()


# ── Stream URL (single-chunk fallback) ───────────────────────────────────────


def get_stream_url(url):
    """Get the direct audio stream URL, caching to avoid repeated yt-dlp calls."""
    valid, err = validate_url(url)
    if not valid:
        print(f"[Yume] Rejected invalid URL: {err} — {url[:80]}")
        return None

    cached = _state.stream_url_cache.get(url)
    if cached and (time.time() - cached["timestamp"]) < _state.STREAM_URL_TTL:
        print(f"[Yume] Using cached stream URL (age: {time.time() - cached['timestamp']:.0f}s)")
        return cached["stream_url"]

    auth_args = build_auth_args(url)

    format_attempts = [
        ["--format", "bestaudio*/bestaudio/best"],
        ["--format", "bestaudio/best"],
        [],
    ]

    last_stderr = ""
    for fmt_args in format_attempts:
        try:
            result = subprocess.run(
                [*ytdlp_cmd(), "--get-url", *fmt_args, "--no-playlist", "--no-exec", *auth_args, "--", url],
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
            with _state.cache_lock:
                if len(_state.stream_url_cache) >= _state.STREAM_URL_CACHE_MAX:
                    oldest_key = min(
                        _state.stream_url_cache,
                        key=lambda k: _state.stream_url_cache[k].get("timestamp", 0),
                    )
                    _state.stream_url_cache.pop(oldest_key, None)
                _state.stream_url_cache[url] = {"stream_url": stream_url, "timestamp": time.time()}
            return stream_url
        last_stderr = result.stderr or ""
        if _state.ERR_REQUESTED_FORMAT in last_stderr.lower():
            continue
        break

    print(f"[Yume] yt-dlp get-url failed: {last_stderr[:200]}")
    return None


# ── Chunk download (fallback when no prepared full audio) ─────────────────────


def download_audio_segment(url, start_time, duration):
    """Download a specific time segment using yt-dlp + ffmpeg (stream URL cached)."""
    valid, err = validate_url(url)
    if not valid:
        print(f"[Yume] Rejected invalid URL: {err} — {url[:80]}")
        return None

    # yume_ prefix is required: the caller's cleanup and the startup sweep
    # only remove yume_* directories — an unprefixed dir leaks on every call.
    tmp_dir = tempfile.mkdtemp(prefix="yume_")
    output_path = os.path.join(tmp_dir, "segment.wav")

    try:
        stream_url = get_stream_url(url)

        if stream_url is None:
            result = _download_audio_segment_fallback(url, start_time, duration, output_path)
            if result is None:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            return result

        stream_valid, _stream_err = validate_url(stream_url)
        if not stream_valid:
            print("[Yume] Invalid stream URL from yt-dlp, using fallback")
            with _state.cache_lock:
                _state.stream_url_cache.pop(url, None)
            result = _download_audio_segment_fallback(url, start_time, duration, output_path)
            if result is None:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            return result

        print(f"[Yume] Extracting {duration}s from {start_time}s via ffmpeg...")

        ffmpeg_result = subprocess.run(
            [
                "ffmpeg",
                "-protocol_whitelist",
                _state.FFMPEG_PROTOCOL_WHITELIST,
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
            with _state.cache_lock:
                _state.stream_url_cache.pop(url, None)
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return None

    except subprocess.TimeoutExpired:
        print("[Yume] Download timed out")
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return None
    except Exception as e:
        print(f"[Yume] Download error: {e}")
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return None


def _download_audio_segment_fallback(url, start_time, duration, output_path):
    """Fallback: use yt-dlp --download-sections directly."""
    try:
        print("[Yume] Using yt-dlp fallback download method...")
        tmp_template = output_path.replace(".wav", ".%(ext)s")
        auth_args = build_auth_args(url)

        result = subprocess.run(
            [
                *ytdlp_cmd(),
                "--download-sections",
                f"*{start_time}-{start_time + duration}",
                "--force-keyframes-at-cuts",
                "-x",
                "--audio-format",
                "wav",
                "--postprocessor-args",
                _state.FFMPEG_AUDIO_OPTS,
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


# ── Full audio download ───────────────────────────────────────────────────────


def friendlify_ytdlp_error(raw_error):
    """Translate raw yt-dlp errors into actionable user-facing messages."""
    lower = raw_error.lower()

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
    if "unable to download" in lower and ("webpage" in lower or "player" in lower):
        return "Cannot reach YouTube. Check your internet connection, or YouTube may be temporarily down."
    if "timed out" in lower or "timeout" in lower:
        return "Download timed out — the video may be too long or the connection too slow."
    if _state.ERR_NO_SUCH_FILE in lower and "yt-dlp" in lower:
        return "yt-dlp is not installed. Run the Yume setup wizard to install it."
    if "no video formats" in lower or _state.ERR_REQUESTED_FORMAT in lower:
        return "No compatible audio format found. Try updating yt-dlp: pip install -U yt-dlp"
    if "deno" in lower and ("not found" in lower or _state.ERR_NO_SUCH_FILE in lower):
        return (
            "Deno is not installed (needed for YouTube auth). "
            "Fix: Switch youtube_auth_method to 'cookies' in yume_config.json, "
            "or install Deno: https://deno.land/#installation"
        )
    return raw_error


def download_full_audio(url):
    """Download the complete audio track as 16 kHz mono WAV.

    Strategy 1: yt-dlp (multiple auth/format combos)
    Strategy 2: yt-dlp get-url → ffmpeg (stream URL + direct download)
    Strategy 3: ffmpeg direct (for m3u8 / direct media URLs)

    Returns (path, None) on success or (None, error_message) on failure.
    """
    tmp_dir = tempfile.mkdtemp(prefix="yume_")
    output_template = os.path.join(tmp_dir, "full_audio.%(ext)s")
    output_path = os.path.join(tmp_dir, "full_audio.wav")
    last_error = "Unknown error"

    is_yt = is_youtube_url(url)
    strategies = _build_download_strategies(url, is_yt)

    # ── Strategy 1: yt-dlp download (try multiple auth combos) ───────────────
    for label, extra_args in strategies:
        for fmt_pass, fmt_args in [("bestaudio", ["-f", "bestaudio*/best"]), ("nofmt", [])]:
            try:
                tag = f"{label}/{fmt_pass}"
                print(f"[Yume] Trying yt-dlp ({tag}): {url[:80]}...")
                result = subprocess.run(
                    [
                        *ytdlp_cmd(),
                        *fmt_args,
                        "-x",
                        "--audio-format",
                        "wav",
                        "--postprocessor-args",
                        _state.FFMPEG_AUDIO_OPTS,
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

                if _state.ERR_REQUESTED_FORMAT in stderr_lower and fmt_pass == "bestaudio":
                    continue  # skip to nofmt pass of same auth strategy

                error_lines = [
                    ln.strip() for ln in (result.stderr or "").split("\n") if ln.strip() and "ERROR" in ln.upper()
                ]
                raw_err = error_lines[-1][:300] if error_lines else f"exit code {result.returncode}"
                last_error = friendlify_ytdlp_error(raw_err)
                print(f"[Yume] yt-dlp ({tag}) failed: {last_error[:150]}")

                if (
                    "drm" in stderr_lower
                    or "sign in" in stderr_lower
                    or "forbidden" in stderr_lower
                    or "invalid token" in stderr_lower
                    or "bot" in stderr_lower
                ):
                    break  # auth error — skip nofmt pass, move to next auth strategy

            except subprocess.TimeoutExpired:
                last_error = "yt-dlp timed out (300s)"
                print(f"[Yume] yt-dlp ({label}) timed out")
            except Exception as e:
                last_error = f"yt-dlp error: {e}"

    # ── Strategy 2: yt-dlp get-url → ffmpeg ──────────────────────────────────
    if is_youtube_url(url):
        try:
            print("[Yume] Trying yt-dlp get-url + ffmpeg fallback...")
            stream_url = get_stream_url(url)
            if stream_url:
                ffmpeg_output = os.path.join(tmp_dir, "full_audio_stream.wav")
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

    # ── Strategy 3: ffmpeg direct (m3u8 / direct media URLs) ─────────────────
    if url.endswith(".m3u8") or ".m3u8" in url or not is_youtube_url(url):
        try:
            print("[Yume] Trying ffmpeg direct on URL...")
            ffmpeg_output = os.path.join(tmp_dir, "full_audio_ffmpeg.wav")
            result = subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-protocol_whitelist",
                    _state.FFMPEG_PROTOCOL_WHITELIST,
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

    shutil.rmtree(tmp_dir, ignore_errors=True)
    return None, last_error


def _build_download_strategies(url, is_yt):
    """Return the ordered list of (label, extra_args) yt-dlp auth strategies."""
    cookie_args = []
    try:
        cookie_args = ["--cookies-from-browser", resolve_browser_cookies()]
    except Exception:
        pass

    strategies = []
    if is_yt:
        if _state.youtube_auth_method == "deno":
            strategies.append(("deno+default", []))
            strategies.append(("deno+tv,web", ["--extractor-args", _state.YT_PLAYER_CLIENT_TV_WEB]))
            if cookie_args:
                strategies.append(("cookies-fallback", [*cookie_args]))
                strategies.append(
                    ("cookies+tv,web", ["--extractor-args", _state.YT_PLAYER_CLIENT_TV_WEB, *cookie_args])
                )
            strategies.append(("no-auth", []))
        else:
            if cookie_args:
                strategies.append(("cookies+default", [*cookie_args]))
                strategies.append(
                    ("cookies+tv,web", ["--extractor-args", _state.YT_PLAYER_CLIENT_TV_WEB, *cookie_args])
                )
                strategies.append(("cookies+mweb", ["--extractor-args", "youtube:player_client=mweb", *cookie_args]))
            strategies.append(("no-auth", []))
    else:
        # Non-YouTube URLs (direct media, other sites). Try browser cookies first
        # for login-gated sites, but ALWAYS fall back to no-auth: a missing or
        # locked cookie DB must not hard-fail a URL that never needed cookies
        # (e.g. a direct .webm/.mp4, or any cookie-less environment).
        if cookie_args:
            strategies.append(("default", [*cookie_args]))
        strategies.append(("no-auth", []))

    return strategies


# ── Audio utility ─────────────────────────────────────────────────────────────


def get_audio_duration(audio_path):
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


def slice_audio(full_audio_path, start_time, duration):
    """Slice a segment from a local audio file. Near-instant operation."""
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
