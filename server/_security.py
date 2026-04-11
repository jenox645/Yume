"""URL validation — defence against argument injection (CWE-88).

Keep this module import-free of Flask and _state so it can be unit-tested
without standing up the full server.
"""

from urllib.parse import urlparse


def validate_url(url):
    """Validate a URL before passing to a subprocess.

    Rejects: dash-leading strings, non-http(s) schemes, shell metacharacters,
    empty/None URLs.

    Returns (is_valid: bool, error_message: str).
    """
    if not url or not isinstance(url, str):
        return False, "Empty or invalid URL"
    url = url.strip()
    if url.startswith("-"):
        return False, "URL cannot start with '-' (argument injection)"
    # Reject shell metacharacters — all subprocess calls use shell=False (list args),
    # but defence-in-depth: reject chars that indicate injection attempts.
    # Note: '&' is intentionally allowed — it is a standard URL query separator.
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
