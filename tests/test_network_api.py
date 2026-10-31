"""yume/network.py: calls to the Yume server (token, stale-token retry), server
discovery and how a failed download is reported. urlopen is faked."""

from __future__ import annotations

import email.message
import io
import json
import sys
import urllib.error
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import yume.network as net  # noqa: E402


class Reply(io.BytesIO):
    def __init__(self, body: bytes = b"", length: int | None = None):
        super().__init__(body)
        self.headers = {"content-length": str(len(body) if length is None else length)}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def http_error(code):
    return urllib.error.HTTPError("http://x", code, "nope", email.message.Message(), io.BytesIO(b""))


class Server:
    """Answers each request from `script` (an exception to raise, or a dict to send)."""

    def __init__(self, monkeypatch, *script, tokens=("t1", "t2")):
        self.script = list(script)
        self.tokens = list(tokens)
        self.requests = []
        monkeypatch.setattr(net, "_api_token", None)
        monkeypatch.setattr(net, "discover_api_token", self._discover)
        monkeypatch.setattr(net.urllib.request, "urlopen", self._open)

    def _discover(self, host, port):
        if net._api_token is None:
            net._api_token = self.tokens.pop(0)
        return net._api_token

    def _open(self, req, timeout=None):
        self.requests.append((req, timeout))
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return Reply(json.dumps(step).encode())

    def token_sent(self, i=0):
        return self.requests[i][0].get_header("X-api-token")


class TestServerGet:
    def test_sends_the_token_and_returns_the_json(self, monkeypatch):
        s = Server(monkeypatch, {"a": 1})
        assert net.server_get("127.0.0.1", 5001, "/stats", timeout=7) == {"a": 1}
        req, timeout = s.requests[0]
        assert req.full_url == "http://127.0.0.1:5001/stats" and req.get_method() == "GET" and timeout == 7
        assert s.token_sent() == "t1"

    def test_a_stale_token_is_dropped_and_retried_once(self, monkeypatch):
        s = Server(monkeypatch, http_error(401), {"a": 1})
        assert net.server_get("127.0.0.1", 5001, "/stats") == {"a": 1}
        assert [s.token_sent(0), s.token_sent(1)] == ["t1", "t2"]

    def test_a_second_refusal_gives_up(self, monkeypatch):
        s = Server(monkeypatch, http_error(403), http_error(403), tokens=("t1", "t2", "t3"))
        assert net.server_get("127.0.0.1", 5001, "/stats") is None
        assert len(s.requests) == 2

    def test_other_http_errors_are_not_retried(self, monkeypatch):
        s = Server(monkeypatch, http_error(500))
        assert net.server_get("127.0.0.1", 5001, "/stats") is None
        assert len(s.requests) == 1

    def test_no_connection(self, monkeypatch):
        Server(monkeypatch, OSError("refused"))
        assert net.server_get("127.0.0.1", 5001, "/stats") is None

    def test_the_token_never_goes_to_another_host(self, monkeypatch):
        s = Server(monkeypatch, http_error(401))
        assert net.server_get("192.168.1.5", 5001, "/stats") is None
        assert s.token_sent() is None and len(s.requests) == 1


class TestServerPost:
    def test_sends_json_with_the_token(self, monkeypatch):
        s = Server(monkeypatch, {"ok": True})
        assert net.server_post("127.0.0.1", 5001, "/x", {"text": "今日"}) == {"ok": True}
        req, timeout = s.requests[0]
        assert req.get_method() == "POST" and timeout == 30
        assert json.loads(req.data) == {"text": "今日"}
        assert req.get_header("Content-type") == "application/json" and s.token_sent() == "t1"

    def test_no_data_posts_an_empty_object(self, monkeypatch):
        s = Server(monkeypatch, {})
        net.server_post("127.0.0.1", 5001, "/x")
        assert s.requests[0][0].data == b"{}"

    def test_a_stale_token_is_dropped_and_retried_once(self, monkeypatch):
        s = Server(monkeypatch, http_error(401), {"ok": True})
        assert net.server_post("127.0.0.1", 5001, "/x") == {"ok": True}
        assert len(s.requests) == 2

    def test_errors_come_back_as_a_dict(self, monkeypatch):
        Server(monkeypatch, http_error(500))
        assert net.server_post("127.0.0.1", 5001, "/x") == {"error": "HTTP 500"}
        Server(monkeypatch, http_error(403), http_error(403), tokens=("t1", "t2", "t3"))
        assert net.server_post("127.0.0.1", 5001, "/x") == {"error": "HTTP 403"}
        Server(monkeypatch, OSError("refused"))
        assert net.server_post("127.0.0.1", 5001, "/x") == {"error": "refused"}


class TestDiscoverServers:
    def _discover(self, monkeypatch, cfg, up):
        calls = []

        def check(host, port, path="/health"):
            calls.append((host, port, path))
            return {"up": (port, path) in up, "data": {}}

        monkeypatch.setattr(net, "check_server", check)
        return net.discover_servers(cfg, net_backends()), calls

    def test_both_up(self, monkeypatch):
        cfg = {"translation_backend": "llamacpp"}
        found, _ = self._discover(monkeypatch, cfg, {(5001, "/health"), (5000, "/v1/models")})
        assert found == {"whisper": True, "translation": True}

    def test_ollama_on_a_port_other_than_the_configured_one_is_found(self, monkeypatch):
        cfg = {"translation_backend": "ollama", "translation_port": 5000}
        found, calls = self._discover(monkeypatch, cfg, {(8080, "/api/tags")})
        assert found == {"whisper": False, "translation": False, "ollama_found": 8080}
        assert (("127.0.0.1", 5000, "/api/tags")) in calls
        assert sum(c[2] == "/api/tags" and c[0] == "127.0.0.1" for c in calls) == 3  # configured + 11434 + 8080

    def test_a_server_answering_on_the_openai_path_counts(self, monkeypatch):
        cfg = {"translation_backend": "ollama"}
        found, _ = self._discover(monkeypatch, cfg, {(5000, "/v1/models")})
        assert found == {"whisper": False, "translation": True}


def net_backends():
    from yume.constants import BACKEND_INFO

    return BACKEND_INFO


class TestCheckOllamaModels:
    def test_names_of_the_installed_models(self, monkeypatch):
        monkeypatch.setattr(
            net.urllib.request, "urlopen", lambda req, timeout=None: Reply(b'{"models": [{"name": "m:latest"}]}')
        )
        assert net.check_ollama_models() == ["m:latest"]

    def test_no_server_means_no_models(self, monkeypatch):
        def refuse(req, timeout=None):
            raise OSError("refused")

        monkeypatch.setattr(net.urllib.request, "urlopen", refuse)
        assert net.check_ollama_models("127.0.0.1", 1) == []


class TestDownloadFailures:
    def _download(self, monkeypatch, tmp_path, answer, capsys):
        said = []
        monkeypatch.setattr(net, "info", said.append)

        def fake(req, timeout=None):
            if isinstance(answer, BaseException):
                raise answer
            return answer

        monkeypatch.setattr(net.urllib.request, "urlopen", fake)
        dest = tmp_path / "x.bin"
        ok = net.download_file("http://x/y", dest, "File")
        return ok, capsys.readouterr().out, said, dest

    def test_a_complete_download_lands(self, monkeypatch, tmp_path, capsys):
        ok, out, _, dest = self._download(monkeypatch, tmp_path, Reply(b"data"), capsys)
        assert ok is True and dest.read_bytes() == b"data" and "File — 0.0 MB in" in out
        assert not (tmp_path / "x.bin.part").exists()

    def test_a_404_suggests_updating(self, monkeypatch, tmp_path, capsys):
        ok, out, said, dest = self._download(monkeypatch, tmp_path, http_error(404), capsys)
        assert ok is False and "File — HTTP 404: nope" in out and any("Try updating" in s for s in said)
        assert not dest.exists()

    def test_another_http_error_says_only_the_code(self, monkeypatch, tmp_path, capsys):
        ok, out, said, _ = self._download(monkeypatch, tmp_path, http_error(503), capsys)
        assert ok is False and "HTTP 503" in out and said == []

    def test_no_connection_says_to_check_it(self, monkeypatch, tmp_path, capsys):
        error = urllib.error.URLError("no route")
        ok, out, said, _ = self._download(monkeypatch, tmp_path, error, capsys)
        assert ok is False and "Connection failed: no route" in out and any("internet" in s for s in said)

    def test_a_disk_error_is_a_failure_not_a_crash(self, monkeypatch, tmp_path, capsys):
        ok, out, _, _ = self._download(monkeypatch, tmp_path, OSError("disk full"), capsys)
        assert ok is False and "FAILED: disk full" in out

    def test_no_room_stops_before_writing(self, monkeypatch, tmp_path, capsys):
        monkeypatch.setattr(net, "_room_for", lambda dest, total, label: False)
        ok, _, _, dest = self._download(monkeypatch, tmp_path, Reply(b"data"), capsys)
        assert ok is False and not dest.exists() and not (tmp_path / "x.bin.part").exists()

    def test_ctrl_c_removes_the_partial_file_and_goes_on_up(self, monkeypatch, tmp_path):
        class Interrupted(Reply):
            def read(self, n=-1):
                raise KeyboardInterrupt

        monkeypatch.setattr(net.urllib.request, "urlopen", lambda req, timeout=None: Interrupted(b"data"))
        with pytest.raises(KeyboardInterrupt):
            net.download_file("http://x/y", tmp_path / "x.bin", "File")
        assert list(tmp_path.iterdir()) == []
