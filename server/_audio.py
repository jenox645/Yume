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


# A full-audio download of a long video on a slow line can take a while; a
# timeout means retrying with other auth/format options would only waste the
# same time again.
DOWNLOAD_TIMEOUT_S = 900

# Browser cookies that yt-dlp could not read. Chromium browsers on Windows lock
# their cookie database while running and encrypt it with app-bound keys, so
# every cookie attempt fails the same way; skip them for a while once seen.
_COOKIE_DB_ERRORS = ("could not copy chrome cookie database", "failed to decrypt", "cookies database", "app-bound")
_COOKIES_RETRY_S = 600
_cookies_failed_at = 0.0
_cookies_error = ""


def cookies_unreadable():
    return time.time() - _cookies_failed_at < _COOKIES_RETRY_S


def _note_cookie_failure(stderr):
    global _cookies_failed_at, _cookies_error
    low = (stderr or "").lower()
    if any(e in low for e in _COOKIE_DB_ERRORS):
        _cookies_failed_at = time.time()
        _cookies_error = next((ln.strip() for ln in stderr.splitlines() if "ERROR" in ln), "cookie error")[:200]
        return True
    return False


def _cookies_hint():
    browser = (_state.cookies_browser or "your browser").capitalize()
    return (
        f" Yume could not read {browser}'s cookies either ({browser} locks and encrypts them while it runs"
        " on Windows). What helps: the newest yt-dlp (Tools → yt-dlp), Deno instead of cookies"
        " (Settings → YouTube auth), or Firefox as the cookie browser."
    )


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
        result = subprocess.run(
            ytdlp_cmd() + ["--version"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=10
        )
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
    if cookies_unreadable():
        return args
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
        # Audio-only first (see download_full_audio): avoid pulling a combined
        # video+audio stream when an audio-only track exists.
        ["--format", "bestaudio/bestaudio*/best"],
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
                encoding="utf-8",
                errors="replace",
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
        if auth_args and _note_cookie_failure(last_stderr):
            # The stream preview often hits the cookie error before the full
            # download does: retry without cookies (build_auth_args skips them now)
            print("[Yume] Browser cookies unreadable — retrying without them")
            return get_stream_url(url)
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


def _download_audio_segment_fallback(url, start_time, duration, output_path, _retry=True):
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

        stderr = (result.stderr or b"").decode("utf-8", errors="replace")
        if _retry and auth_args and _note_cookie_failure(stderr):
            return _download_audio_segment_fallback(url, start_time, duration, output_path, _retry=False)
        print(f"[Yume] Fallback also failed: {stderr[-200:]}")
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
    timed_out = False
    for label, extra_args in strategies:
        if timed_out:
            break
        if "--cookies-from-browser" in extra_args and cookies_unreadable():
            continue  # the cookie DB could not be read a moment ago: it won't be now
        # Prefer an AUDIO-ONLY stream ("bestaudio", no "*"): "bestaudio*/best" lets
        # yt-dlp pick a combined video+audio format (e.g. 360p mp4) when its total
        # bitrate beats the audio-only streams, so it downloads the whole VIDEO just
        # to extract audio — several times more data for no benefit. Fall back to any
        # format-with-audio, then best, only if no audio-only track exists.
        for fmt_pass, fmt_args in [("bestaudio", ["-f", "bestaudio/bestaudio*/best"]), ("nofmt", [])]:
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
                    encoding="utf-8",
                    errors="replace",
                    timeout=DOWNLOAD_TIMEOUT_S,
                )

                if result.returncode == 0 and os.path.exists(output_path):
                    print(f"[Yume] yt-dlp ({tag}) succeeded!")
                    return output_path, None

                stderr_lower = (result.stderr or "").lower()
                if _note_cookie_failure(result.stderr):
                    print(
                        f"[Yume] Browser cookies unreadable — skipping cookie strategies for {_COOKIES_RETRY_S // 60} min"
                    )
                    break

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
                last_error = f"Download timed out after {DOWNLOAD_TIMEOUT_S // 60} min — the video may be too long or the connection too slow"
                print(f"[Yume] yt-dlp ({label}) timed out")
                timed_out = True
                break
            except Exception as e:
                last_error = f"yt-dlp error: {e}"

    # ── Strategy 2: yt-dlp get-url → ffmpeg ──────────────────────────────────
    if is_yt and not timed_out:
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
                    encoding="utf-8",
                    errors="replace",
                    timeout=DOWNLOAD_TIMEOUT_S,
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
    if not is_yt and not timed_out:
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
                encoding="utf-8",
                errors="replace",
                timeout=DOWNLOAD_TIMEOUT_S,
            )
            if result.returncode == 0 and os.path.exists(ffmpeg_output) and os.path.getsize(ffmpeg_output) > 10000:
                print("[Yume] ffmpeg direct succeeded")
                return ffmpeg_output, None
            stderr = (result.stderr or "").strip()
            if stderr:
                ffmpeg_err = stderr.split("\n")[-1][:200]
                print(f"[Yume] ffmpeg also failed: {ffmpeg_err}")
        except subprocess.TimeoutExpired:
            print(f"[Yume] ffmpeg direct timed out ({DOWNLOAD_TIMEOUT_S}s)")
        except Exception as e:
            print(f"[Yume] ffmpeg error: {e}")

    shutil.rmtree(tmp_dir, ignore_errors=True)
    auth_error = any(k in last_error.lower() for k in ("403", "sign-in", "sign in", "age-restricted", "access denied"))
    if cookies_unreadable() and is_yt and auth_error:
        last_error = last_error.split(" For YouTube: try switching to cookie auth")[0] + _cookies_hint()
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


# ── Audio loading / temp cleanup ──────────────────────────────────────────────


def remove_temp(path):
    """Delete a downloaded file and its yume_* temp directory."""
    if not path:
        return
    parent = os.path.dirname(path)
    try:
        if parent and os.path.basename(parent).startswith("yume_"):
            shutil.rmtree(parent, ignore_errors=True)
        elif os.path.isfile(path):
            os.unlink(path)
    except OSError:
        pass


def load_audio(path):
    """Load audio as float32 mono 16 kHz numpy array.

    Downloads are already 16 kHz mono 16-bit WAV (yt-dlp/ffmpeg are told so), which
    the stdlib reads directly; anything else goes through faster-whisper's decoder.
    """
    import wave

    import numpy as np

    try:
        with wave.open(path, "rb") as wf:
            if wf.getframerate() == 16000 and wf.getnchannels() == 1 and wf.getsampwidth() == 2:
                pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
                return pcm.astype(np.float32) / 32768.0
    except (wave.Error, EOFError, OSError):
        pass
    from faster_whisper.audio import decode_audio

    return decode_audio(path, sampling_rate=16000)


def download_direct(stream_url):
    """Download a direct media / m3u8 URL as 16 kHz mono WAV.
    Returns (path, None) or (None, error_message)."""
    valid, err = validate_url(stream_url)
    if not valid:
        return None, f"Invalid URL: {err}"
    tmp_dir = tempfile.mkdtemp(prefix="yume_direct_")
    output_path = os.path.join(tmp_dir, "full_audio.wav")
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-protocol_whitelist", _state.FFMPEG_PROTOCOL_WHITELIST,
                "-i", stream_url, "-vn", "-ar", "16000", "-ac", "1", "-f", "wav", output_path,
            ],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600,
        )  # fmt: skip
        if result.returncode == 0 and os.path.exists(output_path) and os.path.getsize(output_path) > 10000:
            return output_path, None
        print("[Yume] ffmpeg failed on stream URL, trying yt-dlp...")
        result = subprocess.run(
            [
                *ytdlp_cmd(), "-x", "--audio-format", "wav", "--postprocessor-args", _state.FFMPEG_AUDIO_OPTS,
                "--no-playlist", "--no-cache-dir", "--no-exec",
                "-o", os.path.join(tmp_dir, "full_audio.%(ext)s"), "--", stream_url,
            ],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600,
        )  # fmt: skip
        if result.returncode == 0 and os.path.exists(output_path):
            return output_path, None
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return None, f"Direct download failed: {(result.stderr or '')[-200:]}"
    except subprocess.TimeoutExpired:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return None, "Direct download timed out"
    except Exception as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return None, f"Direct download error: {e}"
