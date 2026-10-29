"""The server's security layer through Flask's real request handling: the Host
check (DNS rebinding), the API token, CORS for extension origins only, and what
/health hands out (docs/SECURITY.md).

The unit tests call the routes as plain functions with a stub Flask, so the
before/after-request hooks never ran in them. These run the real app with
Flask's test client, in a child process: test_server_modules replaces `flask`
with a stub for the rest of the session.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
pytestmark = pytest.mark.skipif(importlib.util.find_spec("flask") is None, reason="flask not installed")

EXT = "chrome-extension://eagcngkomlidpdaggkkgefjobcjgjaio"
FIREFOX = "moz-extension://0f1e2d3c-4b5a-6978-8091-a2b3c4d5e6f7"
PAGE = "https://evil.example"

_CHILD = r"""
import json, sys, types
sys.path.insert(0, sys.argv[1])
CASES = json.loads(sys.argv[2])
sys.argv = sys.argv[:1]  # the server modules read their own options from argv
try:
    import faster_whisper  # noqa: F401
except ImportError:
    sys.modules["faster_whisper"] = types.SimpleNamespace(WhisperModel=None)
import _api, _state

_api._audio.check_ytdlp = lambda: True  # no yt-dlp subprocess
_state.model = object()  # Whisper "loaded": /health answers ready
client = _api.app.test_client()

def call(method, path, token=False, origin=None, host="127.0.0.1:5001", raw_token=None, huge=False):
    data = json.dumps({"text": "x" * (3 * 1024 * 1024)}) if huge else None  # over the 2 MB cap
    headers = {}
    if token:
        headers["X-API-Token"] = _state.API_TOKEN
    if raw_token is not None:
        headers["X-API-Token"] = raw_token
    if origin:
        headers["Origin"] = origin
    if data:
        headers["Content-Type"] = "application/json"
    r = client.open(path, method=method, headers=headers, base_url="http://" + host, data=data)
    body = r.get_json(silent=True) or {}
    return {
        "code": r.status_code,
        "token": body.get("api_token") == _state.API_TOKEN,
        "keys": sorted(body),
        "acao": r.headers.get("Access-Control-Allow-Origin"),
    }

print(json.dumps({name: call(*args, **kw) for name, args, kw in CASES}))
"""

CASES = [
    ("no_token", ("GET", "/blacklist"), {}),
    ("wrong_token", ("GET", "/blacklist"), {"raw_token": "not-the-token"}),
    ("non_ascii_token", ("GET", "/blacklist"), {"raw_token": "été"}),
    ("token", ("GET", "/blacklist"), {"token": True}),
    ("localhost", ("GET", "/blacklist"), {"token": True, "host": "localhost:5001"}),
    ("rebinding", ("GET", "/blacklist"), {"token": True, "host": "evil.example:5001"}),
    ("rebinding_suffix", ("GET", "/blacklist"), {"token": True, "host": "127.0.0.1.evil.example"}),
    ("health_page", ("GET", "/health"), {"origin": PAGE}),
    ("health_null_origin", ("GET", "/health"), {"origin": "null"}),
    ("health_extension", ("GET", "/health"), {"origin": EXT}),
    ("health_firefox", ("GET", "/health"), {"origin": FIREFOX}),
    ("health_local_tool", ("GET", "/health"), {}),
    ("health_rebinding", ("GET", "/health"), {"host": "evil.example:5001"}),
    ("health_full", ("GET", "/health"), {"token": True}),
    ("page_with_token", ("GET", "/blacklist"), {"token": True, "origin": PAGE}),
    ("preflight_page", ("OPTIONS", "/jobs"), {"origin": PAGE}),
    ("preflight_extension", ("OPTIONS", "/jobs"), {"origin": EXT}),
    ("post_page_no_token", ("POST", "/cache/clear"), {"origin": PAGE}),
    ("huge_body", ("POST", "/blacklist/add"), {"token": True, "huge": True}),
]


@pytest.fixture(scope="module")
def results():
    out = subprocess.run(
        [sys.executable, "-c", _CHILD, str(ROOT / "server"), json.dumps(CASES)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        cwd=ROOT,
    )
    assert out.returncode == 0, out.stderr[-2000:]
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_every_endpoint_but_health_needs_the_token(results):
    assert results["no_token"]["code"] == 403
    assert results["wrong_token"]["code"] == 403
    assert results["non_ascii_token"]["code"] == 403  # not a 500 (compare_digest on str)
    assert results["post_page_no_token"]["code"] == 403  # a page cannot clear the cache
    assert results["token"]["code"] == 200
    assert results["localhost"]["code"] == 200


def test_a_foreign_host_header_is_refused_dns_rebinding(results):
    assert results["rebinding"]["code"] == 403
    assert results["rebinding_suffix"]["code"] == 403
    assert results["health_rebinding"]["code"] == 403 and not results["health_rebinding"]["token"]


def test_health_hands_the_token_only_to_extensions_and_local_tools(results):
    for name in ("health_page", "health_null_origin"):
        assert results[name]["code"] == 200, name
        assert not results[name]["token"], name
        assert results[name]["acao"] is None, name
    for name, origin in (("health_extension", EXT), ("health_firefox", FIREFOX)):
        assert results[name]["token"], name
        assert results[name]["acao"] == origin, name
    assert results["health_local_tool"]["token"]
    # Model and backend details only for token holders
    assert "model" not in results["health_page"]["keys"]
    assert "model" in results["health_full"]["keys"]


def test_cors_is_only_for_extension_origins(results):
    assert results["page_with_token"]["acao"] is None  # the browser hides the answer from a page
    assert results["preflight_page"]["code"] == 403
    assert results["preflight_extension"]["code"] == 204
    assert results["preflight_extension"]["acao"] == EXT


def test_request_bodies_are_capped(results):
    assert results["huge_body"]["code"] == 413
