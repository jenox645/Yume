"""yt-dlp itself: which command runs it, browser cookies and auth arguments,
what its errors mean.
"""

import os
import platform
import subprocess
import sys
import time
from urllib.parse import urlparse

import _state


# Browser cookies that yt-dlp could not read. Chromium browsers on Windows lock
# their cookie database while running and encrypt it with app-bound keys, so
# every cookie attempt fails the same way; skip them for a while once seen.
_COOKIE_DB_ERRORS = ("could not copy chrome cookie database", "failed to decrypt", "cookies database", "app-bound")
COOKIES_RETRY_S = 600
_cookies_failed_at = 0.0
_cookies_error = ""


def cookies_unreadable():
    return time.time() - _cookies_failed_at < COOKIES_RETRY_S


def note_cookie_failure(stderr):
    global _cookies_failed_at, _cookies_error
    low = (stderr or "").lower()
    if any(e in low for e in _COOKIE_DB_ERRORS):
        _cookies_failed_at = time.time()
        _cookies_error = next((ln.strip() for ln in stderr.splitlines() if "ERROR" in ln), "cookie error")[:200]
        return True
    return False


def cookies_hint():
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


# ── What its errors mean ──────────────────────────────────────────────────────


# Where users change it: the CLI menu, not the JSON file by hand
_SETTINGS = "python pocket_yume.py settings"
_COOKIES_FIX = (
    f"Fix: Settings → YouTube auth → Browser cookies, and pick a browser you are logged in with ({_SETTINGS})"
)

# (conditions, message): the first rule whose every condition matches wins.
# A condition is a tuple of phrases, any of which may appear in the error.
_YTDLP_ERRORS = [
    (
        (("drm protected",),),
        f"YouTube blocked the download (DRM error). {_COOKIES_FIX} (e.g. 'firefox'). "
        "Or paste a stream URL in the extension popup.",
    ),
    ((("sign in to confirm", "confirm you"),), f"YouTube requires sign-in to access this video. {_COOKIES_FIX}."),
    (
        (("http error 403", "403 forbidden"), ("cloudflare",)),
        "Access denied (403) — Cloudflare anti-bot protection. This site blocks automated downloads. "
        "Try: copy the direct video/audio URL (often .m3u8 or .mp4) from the browser's Network tab "
        "and paste it as a Custom Stream URL in the Yume extension popup.",
    ),
    (
        (("http error 403", "403 forbidden"),),
        "Access denied (403). The site is blocking yt-dlp. "
        f"For YouTube: switch to browser cookies in Settings → YouTube auth ({_SETTINGS}). "
        "For other sites: use the Custom Stream URL option in the extension — "
        "open DevTools > Network > filter 'm3u8' or 'mp4' > copy the URL. "
        "Also update yt-dlp (Tools → yt-dlp → Install / Update).",
    ),
    ((("video unavailable", "private video"),), "This video is unavailable or private."),
    (
        (("age",), ("restricted",)),
        f"Age-restricted video. {_COOKIES_FIX}.",
    ),
    ((("geo",), ("block",)), "This video is not available in your region."),
    (
        (("unable to download",), ("webpage", "player")),
        "Cannot reach YouTube. Check your internet connection, or YouTube may be temporarily down.",
    ),
    ((("timed out", "timeout"),), "Download timed out — the video may be too long or the connection too slow."),
    (
        ((_state.ERR_NO_SUCH_FILE,), ("yt-dlp",)),
        "yt-dlp is not installed. Run the Yume setup wizard to install it.",
    ),
    (
        (("no video formats", _state.ERR_REQUESTED_FORMAT),),
        "No compatible audio format found. Update yt-dlp (Tools → yt-dlp → Install / Update).",
    ),
    (
        (("deno",), ("not found", _state.ERR_NO_SUCH_FILE)),
        "Deno is not installed (needed for YouTube auth). "
        f"Fix: Settings → YouTube auth → Browser cookies ({_SETTINGS}), "
        "or install Deno: https://deno.land/#installation",
    ),
]


def friendlify_ytdlp_error(raw_error):
    """Translate raw yt-dlp errors into actionable user-facing messages."""
    lower = raw_error.lower()
    for conditions, message in _YTDLP_ERRORS:
        if all(any(phrase in lower for phrase in phrases) for phrases in conditions):
            return message
    return raw_error
