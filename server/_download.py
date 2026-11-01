"""A video's whole audio track, by every way there is; custom stream URLs too."""

import os
import shutil
import subprocess
import tempfile

import _state
import _stream
import _ytdlp
from _security import validate_url

# A full-audio download of a long video on a slow line can take a while; a
# timeout means retrying with other auth/format options would only waste the
# same time again.
DOWNLOAD_TIMEOUT_S = 900
DIRECT_TIMEOUT_S = 600  # per tool: ffmpeg, then yt-dlp

LIVE_ERROR = (
    "This is a live stream. Yume transcribes a video's whole audio, so it works on the "
    "recording once the stream has ended."
)


def _is_live(stderr_lower):
    """yt-dlp failed on a live stream: its HLS URLs say so (ffmpeg only reports
    "exited with code 3199971767")."""
    return "yt_live_broadcast" in stderr_lower or "/live/1/" in stderr_lower


def _run_tool(cmd, timeout=DOWNLOAD_TIMEOUT_S):
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)


def _ffmpeg_to_wav(src, out_path, rate, channels, timeout=DOWNLOAD_TIMEOUT_S):
    """ffmpeg `src` (a URL) into a WAV; returns (ok, stderr)."""
    cmd = [
        "ffmpeg", "-y", "-protocol_whitelist", _state.FFMPEG_PROTOCOL_WHITELIST, "-i", src,
        "-vn", "-ar", rate, "-ac", channels, "-f", "wav", out_path,
    ]  # fmt: skip
    result = _run_tool(cmd, timeout)
    ok = result.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 10000
    return ok, (result.stderr or "")


# Prefer an AUDIO-ONLY stream ("bestaudio", no "*"): "bestaudio*/best" lets
# yt-dlp pick a combined video+audio format (e.g. 360p mp4) when its total
# bitrate beats the audio-only streams, so it downloads the whole VIDEO just
# to extract audio — several times more data for no benefit. Fall back to any
# format-with-audio, then best, only if no audio-only track exists.
_FORMAT_PASSES = (("bestaudio", ["-f", "bestaudio/bestaudio*/best"]), ("nofmt", []))
_AUTH_ERROR_WORDS = ("drm", "sign in", "forbidden", "invalid token", "not a bot")


class _Live(Exception):
    """yt-dlp says the URL is a live stream."""


class _TimedOut(Exception):
    pass


def _ytdlp_audio_cmd(url, fmt_args, auth_args, output_template, pp_opts):
    return [
        *_ytdlp.ytdlp_cmd(), *fmt_args, "-x", "--audio-format", "wav", "--postprocessor-args", pp_opts,
        "--no-playlist", "--no-cache-dir", "--no-exec", *auth_args, "-o", output_template, "--", url,
    ]  # fmt: skip


def _last_error_line(result):
    lines = [ln.strip() for ln in (result.stderr or "").split("\n") if "ERROR" in ln.upper()]
    return lines[-1] if lines else ""


def _ytdlp_error(result):
    """A failed run's last ERROR line, as advice for the user."""
    return _ytdlp.friendlify_ytdlp_error(_last_error_line(result)[:300] or f"exit code {result.returncode}")


def _ytdlp_strategies(url, strategies, output_template, output_path, pp_opts):
    """Strategy 1: yt-dlp with each auth strategy × format pass.

    Returns (path, last_error); path is None when every attempt failed.
    Raises _Live for a live stream and _TimedOut when a download timed out.
    """
    last_error = "Unknown error"
    for label, extra_args in strategies:
        if "--cookies-from-browser" in extra_args and _ytdlp.cookies_unreadable():
            continue  # the cookie DB could not be read a moment ago: it won't be now
        path, error = _ytdlp_format_passes(url, label, extra_args, output_template, output_path, pp_opts)
        if path:
            return path, None
        last_error = error or last_error
    return None, last_error


def _ytdlp_format_passes(url, label, extra_args, output_template, output_path, pp_opts):
    """One auth strategy, each format pass: (path, None) or (None, last error if any)."""
    last_error = None
    for fmt_pass, fmt_args in _FORMAT_PASSES:
        tag = f"{label}/{fmt_pass}"
        print(f"[Yume] Trying yt-dlp ({tag}): {url[:80]}...")
        try:
            result = _run_tool(_ytdlp_audio_cmd(url, fmt_args, extra_args, output_template, pp_opts))
        except subprocess.TimeoutExpired:
            print(f"[Yume] yt-dlp ({label}) timed out")
            raise _TimedOut from None
        except FileNotFoundError:
            last_error = _ytdlp.NOT_INSTALLED
            continue
        except Exception as e:
            last_error = f"yt-dlp error: {e}"
            continue

        if result.returncode == 0 and os.path.exists(output_path):
            print(f"[Yume] yt-dlp ({tag}) succeeded!")
            return output_path, None

        next_pass, error = _failed_pass(result, fmt_pass, tag)
        last_error = error or last_error
        if not next_pass:
            break
    return None, last_error


def _failed_pass(result, fmt_pass, tag):
    """What a failed attempt means: (try the next format pass?, its error if any)."""
    stderr_lower = (result.stderr or "").lower()
    if _ytdlp.note_cookie_failure(result.stderr):
        print(f"[Yume] Browser cookies unreadable — skipping cookie strategies for {_ytdlp.COOKIES_RETRY_S // 60} min")
        return False, None
    if _is_live(stderr_lower):
        raise _Live
    if _state.ERR_REQUESTED_FORMAT in stderr_lower and fmt_pass == "bestaudio":
        return True, None
    error = _ytdlp_error(result)
    print(f"[Yume] yt-dlp ({tag}) failed: {error[:150]}")
    # An auth error fails on every format: on to the next auth strategy
    return not any(w in stderr_lower for w in _AUTH_ERROR_WORDS), error


def _stream_url_fallback(url, tmp_dir, rate, channels):
    """Strategy 2 (YouTube): yt-dlp resolves the stream URL, ffmpeg downloads it."""
    try:
        print("[Yume] Trying yt-dlp get-url + ffmpeg fallback...")
        stream_url = _stream.get_stream_url(url)
        if not stream_url:
            print("[Yume] Could not get stream URL either")
            return None
        out = os.path.join(tmp_dir, "full_audio_stream.wav")
        ok, stderr = _ffmpeg_to_wav(stream_url, out, rate, channels)
        if ok:
            print("[Yume] yt-dlp get-url + ffmpeg succeeded!")
            return out
        print(f"[Yume] ffmpeg on stream URL failed: {stderr[-100:]}")
    except Exception as e:
        print(f"[Yume] Strategy 2 error: {e}")
    return None


def _ffmpeg_direct(url, tmp_dir, rate, channels):
    """Strategy 3 (other sites): ffmpeg on the URL itself (m3u8 / direct media)."""
    try:
        print("[Yume] Trying ffmpeg direct on URL...")
        out = os.path.join(tmp_dir, "full_audio_ffmpeg.wav")
        ok, stderr = _ffmpeg_to_wav(url, out, rate, channels)
        if ok:
            print("[Yume] ffmpeg direct succeeded")
            return out
        if stderr.strip():
            last_line = stderr.strip().splitlines()[-1]
            print(f"[Yume] ffmpeg also failed: {last_line[:200]}")
    except subprocess.TimeoutExpired:
        print(f"[Yume] ffmpeg direct timed out ({DOWNLOAD_TIMEOUT_S}s)")
    except Exception as e:
        print(f"[Yume] ffmpeg error: {e}")
    return None


def download_full_audio(url, stereo=False):
    """Download the complete audio track as 16 kHz mono WAV.

    Strategy 1: yt-dlp (multiple auth/format combos)
    Strategy 2: yt-dlp get-url → ffmpeg (stream URL + direct download)
    Strategy 3: ffmpeg direct (for m3u8 / direct media URLs)

    Returns (path, None) on success or (None, error_message) on failure.
    16 kHz mono (what Whisper takes), or 44.1 kHz stereo for vocal isolation
    (_separate: separating the 16 kHz mono made transcription worse).
    """
    rate, channels = ("44100", "2") if stereo else ("16000", "1")
    tmp_dir = tempfile.mkdtemp(prefix="yume_")
    output_path = os.path.join(tmp_dir, "full_audio.wav")
    is_yt = _ytdlp.is_youtube_url(url)

    try:
        path, last_error = _ytdlp_strategies(
            url,
            _build_download_strategies(is_yt),
            os.path.join(tmp_dir, "full_audio.%(ext)s"),
            output_path,
            f"ffmpeg:-ar {rate} -ac {channels}",
        )
    except _Live:
        # The fallbacks would record the live stream until the timeout
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return None, LIVE_ERROR
    except _TimedOut:
        path = None
        last_error = f"Download timed out after {DOWNLOAD_TIMEOUT_S // 60} min — the video may be too long or the connection too slow"
    else:
        fallback = _stream_url_fallback if is_yt else _ffmpeg_direct
        path = path or fallback(url, tmp_dir, rate, channels)
    if path:
        return path, None

    shutil.rmtree(tmp_dir, ignore_errors=True)
    return None, _with_cookies_hint(last_error) if is_yt else last_error


def _with_cookies_hint(error):
    """An auth error on YouTube, when the cookies are what failed."""
    auth_error = any(k in error.lower() for k in ("403", "sign-in", "sign in", "age-restricted", "access denied"))
    if _ytdlp.cookies_unreadable() and auth_error:
        return error.split(" For YouTube:")[0] + _ytdlp.cookies_hint()
    return error


def _build_download_strategies(is_yt):
    """Return the ordered list of (label, extra_args) yt-dlp auth strategies."""
    cookie_args = []
    try:
        cookie_args = ["--cookies-from-browser", _ytdlp.resolve_browser_cookies()]
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


def download_direct(stream_url):
    """Download a direct media / m3u8 URL as 16 kHz mono WAV.
    Returns (path, None) or (None, error_message)."""
    valid, err = validate_url(stream_url)
    if not valid:
        return None, f"Invalid URL: {err}"
    tmp_dir = tempfile.mkdtemp(prefix="yume_direct_")
    output_path = os.path.join(tmp_dir, "full_audio.wav")
    try:
        if _ffmpeg_to_wav(stream_url, output_path, "16000", "1", DIRECT_TIMEOUT_S)[0]:
            return output_path, None
        print("[Yume] ffmpeg failed on stream URL, trying yt-dlp...")
        template = os.path.join(tmp_dir, "full_audio.%(ext)s")
        result = _run_tool(_ytdlp_audio_cmd(stream_url, [], [], template, _state.FFMPEG_AUDIO_OPTS), DIRECT_TIMEOUT_S)
        if result.returncode == 0 and os.path.exists(output_path):
            return output_path, None
        reason = _last_error_line(result).replace("ERROR: ", "")[:150] or f"exit code {result.returncode}"
        error = (
            f"Could not download the custom stream URL ({reason}). Stream URLs copied from the browser "
            "usually expire within hours: copy a fresh one (DevTools > Network) and paste it again."
        )
    except subprocess.TimeoutExpired:
        error = "Direct download timed out"
    except Exception as e:
        error = f"Direct download error: {e}"
    shutil.rmtree(tmp_dir, ignore_errors=True)
    return None, error
