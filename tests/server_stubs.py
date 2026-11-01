"""Minimal flask and faster_whisper, installed on import, so server modules that
need them can be imported without those packages. Replaces flask for the whole
test process: import this only from modules collected after the ones that need
the real one (see test_api_security.py)."""

from __future__ import annotations

import sys
import types


# Must happen BEFORE any test touches faster_whisper_server.


def _install_flask_stub():
    if "flask" in sys.modules:
        return
    flask_mod = types.ModuleType("flask")

    class _FakeFlask:
        def __init__(self, name):
            self.name = name
            self.config = {}

        def route(self, *args, **kwargs):
            return lambda f: f

        def before_request(self, f):
            return f

        def after_request(self, f):
            return f

        def teardown_request(self, f):
            return f

        def test_client(self):
            return None

    # A minimal request stand-in reused across tests (tests that need different
    # values patch it locally via unittest.mock.patch).
    class _FakeRequest:
        method = "GET"
        path = "/health"
        host = "127.0.0.1"
        headers: dict = {}

        def get_json(self):
            return {}

    flask_mod.Flask = _FakeFlask
    flask_mod.g = types.SimpleNamespace(_temp_files=[])
    flask_mod.request = _FakeRequest()
    flask_mod.jsonify = lambda d=None, **kw: d  # return the dict directly
    sys.modules["flask"] = flask_mod


def _install_faster_whisper_stub():
    if "faster_whisper" in sys.modules:
        return
    fw = types.ModuleType("faster_whisper")
    fw.WhisperModel = type("WhisperModel", (), {})
    sys.modules["faster_whisper"] = fw


_install_flask_stub()
_install_faster_whisper_stub()
