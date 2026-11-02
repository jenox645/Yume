"""whisper.cpp on Vulkan, against a fake whisper-server: a real child process speaking
the real protocol. The real one is measured by tests/gold (it needs a GPU)."""

import json
import sys
import textwrap
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "server"))
import _state
import _transcribe
import _whispercpp

from yume import servers, whisper_cpp

ROOT = Path(__file__).parent.parent

# argv: fake_server.py [mode] -m model --host h --port p --request-path /secret
FAKE_SERVER = textwrap.dedent(
    """
    import json, os, sys
    from email.parser import BytesParser
    from http.server import BaseHTTPRequestHandler, HTTPServer

    args = sys.argv[2:]
    mode = sys.argv[1]
    port = int(args[args.index("--port") + 1])
    secret = args[args.index("--request-path") + 1]
    if mode == "dies":
        sys.exit(3)
    print("using CPU" if mode == "no-vulkan" else "whisper_backend_init_gpu: using Vulkan0 backend", flush=True)

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            head = f"Content-Type: {self.headers['Content-Type']}\\r\\n\\r\\n".encode()
            parts = BytesParser().parsebytes(head + body).get_payload()
            fields = {p.get_param("name", header="content-disposition"): p.get_payload(decode=True) for p in parts}
            ok = self.path == secret + "/inference"
            sent = {k: v.decode() for k, v in fields.items() if k != "file"}
            out = {
                "segments": [{"text": " 안녕 ", "start": 1.0, "end": 2.5, "avg_logprob": -0.25}],
                "language_probabilities": {"ko": 0.9, "ja": 0.1},
                "sent": sent,
                "wav": fields["file"][:4].decode(),
            }
            if os.path.exists("crash-next"):
                os.remove("crash-next")
                os._exit(9)
            self.send_response(200 if ok else 404)
            self.end_headers()
            self.wfile.write(json.dumps(out).encode())

    HTTPServer(("127.0.0.1", port), H).serve_forever()
    """
)


@pytest.fixture
def engine(tmp_path, monkeypatch):
    fake = tmp_path / "fake_server.py"
    fake.write_text(FAKE_SERVER, encoding="utf-8")
    models = tmp_path / "models"
    models.mkdir()
    monkeypatch.setattr(_whispercpp, "MODELS", models)
    monkeypatch.setattr(_whispercpp, "LOGS", tmp_path / "logs")
    (models / _whispercpp.PINS["models"]["small"]["file"]).write_bytes(b"model")
    made = []

    def make(mode="ok"):
        made.append(_whispercpp.WhisperCpp("small", command=[sys.executable, str(fake), mode]))
        return made[-1]

    yield make
    for m in made:
        m.close()


AUDIO = np.zeros(16000, dtype=np.float32)


def test_lines_come_back_shaped_like_faster_whispers(engine):
    segments, _info = engine().transcribe(AUDIO, language="ko")
    seg = next(segments)
    assert (seg.text, seg.start, seg.end, seg.avg_logprob) == (" 안녕 ", 1.0, 2.5, -0.25)


def test_the_pipeline_reads_it_like_faster_whisper(engine):
    with patch.object(_state, "model", engine()):
        lines = _transcribe.transcribe_audio(AUDIO, "ko", offset=10.0)
    assert lines == [{"start": 11.0, "end": 12.5, "text": "안녕", "confidence": -0.25}]


def test_what_yume_asks_for_reaches_whisper_server(engine):
    model = engine()
    sent = model._post(AUDIO, beam_size=5)
    assert sent["wav"] == "RIFF" and sent["sent"]["beam_size"] == "5"
    with patch.object(model, "_post", return_value={"segments": []}) as post:
        model.transcribe(AUDIO, language="ja", beam_size=5, temperature=0.4, no_speech_threshold=0.7)
    asked = post.call_args.kwargs
    assert (asked["language"], asked["temperature"], asked["no_speech_thold"]) == ("ja", 0.4, 0.7)
    assert asked["token_timestamps"] == "false"  # or every line is cut at 60 characters
    assert asked["suppress_nst"] == "true"


def test_language_detection_has_faster_whispers_shape(engine):
    language, probability, _all = engine().detect_language(AUDIO)
    assert (language, probability) == ("ko", 0.9)


def test_the_server_answers_only_on_its_secret_path(engine):
    model = engine()
    secret, model._secret = model._secret, "/guessed"
    with pytest.raises(_whispercpp.urllib.error.HTTPError):
        model._post(AUDIO)
    model._secret = secret
    assert len(secret) > 40


def test_a_crash_mid_video_restarts_it_and_hears_the_region_again(engine):
    model = engine()
    (_whispercpp.MODELS / "crash-next").write_text("x")
    first_port = model.port
    segments, _info = model.transcribe(AUDIO, language="ko")
    assert next(segments).text == " 안녕 "
    assert model.port != first_port


def test_a_swap_never_reaches_the_model_it_closed(engine):
    old, new = MagicMock(), MagicMock()
    new.transcribe.return_value = (iter(()), None)

    def swap_first(*_a):
        _state.model = new
        return True

    lock = MagicMock(__enter__=swap_first, __exit__=lambda *a: False)
    with patch.object(_state, "model", old), patch.object(_state, "transcribe_lock", lock):
        _transcribe.transcribe_audio(AUDIO, "ko")
    old.transcribe.assert_not_called()
    new.transcribe.assert_called_once()


def test_an_engine_that_will_not_come_back_is_lost(engine):
    model = engine()
    (_whispercpp.MODELS / "crash-next").write_text("x")
    with patch.object(model, "_start", side_effect=RuntimeError("Vulkan found no GPU")):
        with pytest.raises(_whispercpp.EngineLost):
            model._post(AUDIO)


def test_a_lost_engine_hands_the_region_to_the_cpu():
    import _model

    gone, cpu = MagicMock(), MagicMock()
    gone.transcribe.side_effect = _whispercpp.EngineLost("no GPU")
    cpu.transcribe.return_value = (iter(()), None)

    def fall_back(_reason):
        _state.model = cpu
        return True

    with patch.object(_state, "model", gone), patch.object(_model, "cpu_after_losing_vulkan", fall_back):
        assert _transcribe.transcribe_audio(AUDIO, "ko") == []
    cpu.transcribe.assert_called_once()


def test_a_server_that_stops_at_start_is_an_error(engine):
    with pytest.raises(RuntimeError, match="stopped at start"):
        engine("dies")


def test_no_vulkan_device_is_an_error_not_a_silent_cpu_run(engine):
    with pytest.raises(RuntimeError, match="Vulkan found no GPU"):
        engine("no-vulkan")


def _lines(*texts):
    return [{"text": t, "start": i, "end": i + 1} for i, t in enumerate(texts)]


def test_a_loop_cut_into_drifting_lines_goes():
    loop = ["이 녀석에 연결되어 있습니다.", "이 녀석에 연결됩니다.", "이 녀석에 연결됩니다.", "그 녀석에 연결됩니다."]
    kept = _whispercpp._without_drifting_loops(_lines("먼저 보시죠.", *loop, "다음 얘기입니다."))
    assert [s["text"] for s in kept] == ["먼저 보시죠.", "다음 얘기입니다."]


def test_lyrics_that_come_back_stay():
    chorus = ["君が好きだよ", "君が好きだよ", "ずっと一緒にいたいから", "君が好きだよ"]
    assert len(_whispercpp._without_drifting_loops(_lines(*chorus))) == 4


def test_a_custom_model_has_no_whisper_cpp_file():
    with pytest.raises(FileNotFoundError, match="no whisper.cpp build"):
        _whispercpp.model_file("D:/models/mine")


def test_a_download_that_fails_its_checksum_leaves_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(_whispercpp, "MODELS", tmp_path)

    class Body:
        chunks = [b"not the model", b""]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self, _n):
            return self.chunks.pop(0)

    with patch.object(_whispercpp.urllib.request, "urlopen", return_value=Body()):
        with pytest.raises(RuntimeError, match="checksum"):
            _whispercpp.ensure_model("tiny")
    assert list(tmp_path.iterdir()) == []


@pytest.mark.skipif(sys.platform != "win32", reason="the job object is Windows' way")
def test_the_child_dies_with_this_process(tmp_path):
    """Whoever kills Yume hard must not leave a process holding the GPU's memory."""
    import subprocess

    parent = tmp_path / "parent.py"
    child = tmp_path / "child.py"
    child.write_text("import time\ntime.sleep(60)\n")
    parent.write_text(
        textwrap.dedent(
            f"""
            import subprocess, sys
            sys.path.insert(0, {str(ROOT / "server")!r})
            import _whispercpp
            child = subprocess.Popen([sys.executable, {str(child)!r}])
            job = _whispercpp._die_with_this_process_windows(child)
            print(child.pid, flush=True)
            sys.stdin.read()
            """
        )
    )
    proc = subprocess.Popen([sys.executable, str(parent)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    pid = int(proc.stdout.readline())
    proc.kill()
    proc.wait()
    deadline = time.time() + 10
    while (
        time.time() < deadline
        and str(pid)
        in subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
    ):
        time.sleep(0.2)
    assert (
        str(pid)
        not in subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
    )


class TestDevice:
    CFG = {"whisper_device": "auto", "whisper_compute_type": "auto"}
    AMD = {"has_nvidia": False, "has_amd": True, "vram_mb": 8192}
    NVIDIA = {"has_nvidia": True, "has_amd": False, "vram_mb": 12288}

    def _resolve(self, monkeypatch, gpu, installed, cfg=None):
        monkeypatch.setattr(servers, "detect_gpu", lambda: gpu)
        monkeypatch.setattr(whisper_cpp, "server_path", lambda: Path("whisper-server") if installed else None)
        return servers.resolve_whisper_device(cfg or self.CFG)

    def test_an_amd_card_hears_on_vulkan_once_it_is_installed(self, monkeypatch):
        assert self._resolve(monkeypatch, self.AMD, True) == ("vulkan", "q5")

    def test_until_then_the_cpu_hears(self, monkeypatch):
        assert self._resolve(monkeypatch, self.AMD, False) == ("cpu", "int8")

    def test_nvidia_stays_on_faster_whisper(self, monkeypatch):
        assert self._resolve(monkeypatch, self.NVIDIA, True)[0] == "cuda"

    def test_vulkan_can_be_chosen_by_hand_on_any_card(self, monkeypatch):
        cfg = {"whisper_device": "vulkan", "whisper_compute_type": "float16"}
        assert self._resolve(monkeypatch, self.NVIDIA, True, cfg) == ("vulkan", "q5")


class TestFallback:
    @pytest.fixture(autouse=True)
    def state(self):
        saved = (_state.model, _state.load_error, _state.device, _state.compute_type, _state.device_notice)
        _state.model, _state.load_error, _state.device_notice = None, "", ""
        _state.device, _state.compute_type = "vulkan", "q5"
        yield
        _state.model, _state.load_error, _state.device, _state.compute_type, _state.device_notice = saved

    def _load(self, whisper_cpp_error):
        import _model

        cpu = MagicMock()
        cpu.transcribe.return_value = (iter(()), None)
        with (
            patch.object(_model, "WhisperCpp", side_effect=whisper_cpp_error),
            patch.object(_model, "WhisperModel", return_value=cpu) as whisper_model,
            patch.object(_model, "set_low_priority"),
            patch("builtins.print"),
        ):
            _model.load_model(MagicMock(prewarm=False, port=5001))
        return cpu, whisper_model

    @pytest.mark.parametrize("error", [RuntimeError("Vulkan found no GPU"), FileNotFoundError("not installed")])
    def test_vulkan_that_cannot_run_leaves_the_cpu_with_a_notice(self, error):
        cpu, whisper_model = self._load(error)
        assert _state.model is cpu and _state.load_error == ""
        assert whisper_model.call_args.kwargs == {"device": "cpu", "compute_type": "int8"}
        assert (_state.device, _state.compute_type) == ("cpu", "int8")
        assert "graphics driver" in _state.device_notice

    def test_a_swapped_model_lets_its_process_go(self):
        import _model

        old = MagicMock()
        _model.close_whisper(old)
        old.close.assert_called_once()
        _model.close_whisper(object())  # faster-whisper's model has nothing to close


class TestInstall:
    def test_every_build_and_model_is_pinned(self):
        pins = json.loads((ROOT / "server" / "whisper_cpp.json").read_text(encoding="utf-8"))
        assert set(pins["builds"]) == {"win32", "linux"}
        for item in [*pins["builds"].values(), *pins["models"].values()]:
            assert len(item["sha256"]) == 64

    def test_the_tag_names_the_commit_the_workflow_builds(self):
        pins = json.loads((ROOT / "server" / "whisper_cpp.json").read_text(encoding="utf-8"))
        workflow = (ROOT / ".github" / "workflows" / "whisper-vulkan.yml").read_text(encoding="utf-8")
        version = pins["tag"].removeprefix("whisper-vulkan-")
        assert f"WHISPER_TAG: {version}" in workflow
        assert all(version in build["name"] for build in pins["builds"].values())

    def test_every_model_yume_offers_has_a_file(self):
        from yume.hardware import WHISPER_MODELS

        for name, *_rest in WHISPER_MODELS:
            assert whisper_cpp.model_path(name) is not None, name

    def test_the_download_is_checked_against_the_pin(self, tmp_path, monkeypatch):
        monkeypatch.setattr(whisper_cpp, "WHISPER_DIR", tmp_path / "whisper.cpp")
        monkeypatch.setattr(whisper_cpp.platform, "machine", lambda: "AMD64")
        monkeypatch.setattr(whisper_cpp.sys, "platform", "win32")
        with (
            patch.object(whisper_cpp, "download_file", return_value=False) as download,
            patch("yume.ui.error"),
        ):
            assert whisper_cpp.install() is False
        build = whisper_cpp._pins()["builds"]["win32"]
        assert download.call_args.kwargs == {"sha256": build["sha256"]}
        assert build["name"] in download.call_args.args[0]

    def test_no_build_for_arm(self, monkeypatch):
        monkeypatch.setattr(whisper_cpp.platform, "machine", lambda: "aarch64")
        with patch("yume.ui.error") as error:
            assert whisper_cpp.install() is False
        assert "stays on the CPU" in error.call_args.args[0]


class TestSetup:
    AMD = {"has_nvidia": False, "has_amd": True, "vram_mb": 8192}

    def test_only_an_amd_card_is_offered_the_engine(self):
        from yume import setup

        with patch.object(whisper_cpp, "server_path", return_value=None), patch.object(setup, "warn"):
            assert setup._missing_for_amd(self.AMD) == ["whisper_cpp"]
            assert setup._missing_for_amd({"has_nvidia": True, "has_amd": False}) == []

    def test_installed_it_is_not_offered_again(self):
        from yume import setup

        with patch.object(whisper_cpp, "server_path", return_value=Path("x")), patch.object(setup, "success"):
            assert setup._missing_for_amd(self.AMD) == []

    @pytest.mark.parametrize(("worked", "device"), [(True, "auto"), (False, "cpu")])
    def test_yes_to_the_engine_ends_a_saved_cpu_choice(self, worked, device):
        from yume import setup

        cfg = {"whisper_device": "cpu", "whisper_compute_type": "int8", "whisper_model": "small"}
        with (
            patch.object(whisper_cpp, "install", return_value=worked) as install,
            patch.object(setup, "detect_gpu", return_value=self.AMD),
        ):
            assert setup._install_whisper_cpp(cfg) is worked
        install.assert_called_once_with("large-v3-turbo")  # the card carries more than the CPU's pick
        assert cfg["whisper_device"] == device

    def test_no_build_for_this_system_is_not_offered(self):
        from yume import setup

        with patch.object(whisper_cpp, "available", return_value=False):
            assert setup._missing_for_amd(self.AMD) == []
