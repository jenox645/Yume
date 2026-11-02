"""whisper.cpp on Vulkan, for the GPUs CTranslate2 cannot use (AMD).

WhisperCpp has the two methods of faster_whisper.WhisperModel that _transcribe.py
calls, so nothing above it knows which engine hears. It runs the upstream server
as a child process (tools/whisper.cpp/whisper-server) on a free loopback port:
the build comes from .github/workflows/whisper-vulkan.yml.
"""

import hashlib
import io
import json
import secrets
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
import wave
from difflib import SequenceMatcher
from pathlib import Path
from types import SimpleNamespace

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
BINARY = ROOT / "tools" / "whisper.cpp" / ("whisper-server.exe" if sys.platform == "win32" else "whisper-server")
MODELS = ROOT / "models" / "whisper"
LOGS = ROOT / "logs"
# The build and the models, pinned by SHA-256; yume/whisper_cpp.py installs from it too
PINS = json.loads((Path(__file__).parent / "whisper_cpp.json").read_text(encoding="utf-8"))
SAMPLE_RATE = 16000
START_TIMEOUT_S = 180  # a first start compiles the Vulkan shaders
REQUEST_TIMEOUT_S = 600
TEMPERATURE_STEP = 0.2  # whisper.cpp's own fallback when a pass looks stuck: tests/gold/RESULTS.md
LOOP_MIN_LINES = 4
LOOP_SIMILARITY = 0.7


class EngineLost(RuntimeError):
    """whisper-server stopped and could not be started again."""


def model_file(name):
    """The ggml file of a Whisper model name (the quantised one: tests/gold/RESULTS.md)."""
    if name not in PINS["models"]:
        raise FileNotFoundError(f"{name} has no whisper.cpp build: pick one of {', '.join(PINS['models'])}")
    return MODELS / PINS["models"][name]["file"]


def ensure_model(name):
    """The model's file, downloaded on first use like faster-whisper's own models."""
    path = model_file(name)
    if path.exists():
        return path
    pin = PINS["models"][name]
    print(f"[Yume] Downloading {pin['file']} ({pin['mb']} MB)")
    MODELS.mkdir(parents=True, exist_ok=True)
    part = path.with_suffix(".part")
    digest = hashlib.sha256()
    req = urllib.request.Request(f"{PINS['model_repo']}/{pin['file']}", headers={"User-Agent": "Yume"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r, open(part, "wb") as out:  # nosec B310 — fixed https URL
            while chunk := r.read(1 << 20):
                out.write(chunk)
                digest.update(chunk)
        if digest.hexdigest() != pin["sha256"]:
            raise RuntimeError(f"{pin['file']} failed its checksum")
        part.replace(path)
    finally:
        part.unlink(missing_ok=True)
    return path


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wav(audio):
    """A float32 16 kHz mono array as a WAV file's bytes."""
    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm.tobytes())
    return out.getvalue()


def _multipart(wav, fields):
    """(body, content type): whisper-server reads every parameter as a form part."""
    boundary = uuid.uuid4().hex
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode() for k, v in fields.items()
    ]
    head = f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="a.wav"\r\n'
    parts.append(head.encode() + b"Content-Type: audio/wav\r\n\r\n" + wav + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _without_drifting_loops(lines):
    """Drop runs of nearly identical lines. Stuck on something it cannot follow,
    faster-whisper writes one long looping line, which _filter hides. whisper.cpp
    cuts the same loop into short lines that each differ a little, and those pass."""
    kept, start = [], 0
    while start < len(lines):
        end = start
        while (
            end + 1 < len(lines)
            and SequenceMatcher(None, lines[end]["text"], lines[end + 1]["text"]).ratio() >= LOOP_SIMILARITY
        ):
            end += 1
        if end - start + 1 < LOOP_MIN_LINES:
            kept.extend(lines[start : end + 1])
        start = end + 1
    return kept


def _die_with_this_process_linux():
    """preexec_fn: the kernel ends the child when this process dies, however it dies."""
    import ctypes
    import signal

    pr_set_pdeathsig = 1
    ctypes.CDLL(None).prctl(pr_set_pdeathsig, signal.SIGTERM)


def _die_with_this_process_windows(proc):
    """A job that kills its members when its last handle closes, which is when this
    process dies. Returns the handle: it must stay open as long as the child runs."""
    import ctypes
    from ctypes import wintypes

    class Basic(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class Extended(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", Basic),
            ("IoInfo", ctypes.c_uint64 * 6),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    kill_on_job_close, extended_limit_information = 0x2000, 9
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    job = kernel32.CreateJobObjectW(None, None)
    info = Extended()
    info.BasicLimitInformation.LimitFlags = kill_on_job_close
    set_ok = kernel32.SetInformationJobObject(job, extended_limit_information, ctypes.byref(info), ctypes.sizeof(info))
    if not (set_ok and kernel32.AssignProcessToJobObject(job, int(proc._handle))):
        print(f"[Yume] whisper-server is not tied to this process: {ctypes.get_last_error()}")
    return job


class WhisperCpp:
    def __init__(self, name, command=None):
        self._path = ensure_model(name)
        if command is None and not BINARY.exists():
            raise FileNotFoundError(f"whisper.cpp is not installed: {BINARY}")
        self._command = command or [str(BINARY)]
        self._start()

    def _start(self):
        """Port and path change on every start. whisper-server has no token and answers
        any origin, so a web page must not be able to guess where it listens."""
        self.port = _free_port()
        self._secret = "/" + secrets.token_urlsafe(32)
        LOGS.mkdir(exist_ok=True)
        # One log per model: during a hot swap the old server is still writing to its own
        self._log_path = LOGS / f"whisper-cpp-{self._path.stem}.log"
        self._log = open(self._log_path, "wb")  # noqa: SIM115 — stays open for the child's lifetime
        cmd = [*self._command, "-m", str(self._path), "--host", "127.0.0.1", "--port", str(self.port)]
        cmd += ["--request-path", self._secret]
        win = sys.platform == "win32"
        self._proc = subprocess.Popen(
            cmd,
            stdout=self._log,
            stderr=self._log,
            cwd=self._path.parent,
            creationflags=subprocess.CREATE_NO_WINDOW if win else 0,
            preexec_fn=None if win else _die_with_this_process_linux,  # nosec B606
        )
        self._job = _die_with_this_process_windows(self._proc) if win else None
        self._wait_until_up()
        self._check_on_the_gpu()

    def _wait_until_up(self):
        deadline = time.time() + START_TIMEOUT_S
        while time.time() < deadline:
            if self._proc.poll() is not None:
                self.close()
                raise RuntimeError(
                    f"whisper-server stopped at start (code {self._proc.returncode}), see {self._log_path}"
                )
            try:
                with socket.create_connection(("127.0.0.1", self.port), timeout=1):
                    return
            except OSError:
                time.sleep(0.2)
        self.close()
        raise RuntimeError(f"whisper-server did not start in {START_TIMEOUT_S} s, see {self._log_path}")

    def _check_on_the_gpu(self):
        """Without a working Vulkan device whisper.cpp runs on the CPU and says so only
        in its log. faster-whisper is the better engine there: the caller falls back."""
        self._log.flush()
        if "using Vulkan" not in self._log_path.read_text(encoding="utf-8", errors="replace"):
            self.close()
            raise RuntimeError("Vulkan found no GPU")

    def _stopped(self):
        """The connection can break a moment before the process is gone."""
        try:
            self._proc.wait(timeout=2)
            return True
        except subprocess.TimeoutExpired:
            return False

    def _post(self, audio, **fields):
        body, content_type = _multipart(_wav(audio), {"response_format": "verbose_json", **fields})
        for attempt in (1, 2):
            req = urllib.request.Request(
                f"http://127.0.0.1:{self.port}{self._secret}/inference",  # noqa: S5332 — loopback child process
                data=body,
                headers={"Content-Type": content_type},
            )
            try:
                with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as r:  # nosec B310 — fixed loopback URL
                    return json.loads(r.read())
            except (urllib.error.URLError, ConnectionError):
                # A GPU driver reset takes the child down; the region is simply heard again
                if attempt == 2 or not self._stopped():
                    raise
                print(f"[Yume] whisper-server stopped (code {self._proc.returncode}), starting it again")
                self.close()
                try:
                    self._start()
                except (OSError, RuntimeError) as e:
                    raise EngineLost(str(e)) from e
        return None

    def transcribe(
        self,
        audio,
        language=None,
        beam_size=5,
        temperature=0.0,
        log_prob_threshold=-2.0,
        no_speech_threshold=0.6,
        **_same_as_faster_whisper,  # no VAD, no word timestamps, no previous text: whisper-server's defaults here
    ):
        out = self._post(
            audio,
            language=language or "auto",
            beam_size=beam_size,
            best_of=5,
            temperature=temperature,
            temperature_inc=TEMPERATURE_STEP,
            logprob_thold=log_prob_threshold,
            no_speech_thold=no_speech_threshold,
            suppress_nst="true",  # faster-whisper's suppress_tokens=[-1]
            token_timestamps="false",  # on, the server would wrap every line at 60 characters, mid-word
            no_language_probabilities="true",  # costs an encoder pass
        )
        segments = [
            SimpleNamespace(text=s["text"], start=s["start"], end=s["end"], avg_logprob=s.get("avg_logprob", 0.0))
            for s in _without_drifting_loops(out.get("segments", []))
        ]
        return iter(segments), SimpleNamespace(language=language)

    def detect_language(self, audio):
        """(language, probability, all) like faster-whisper's."""
        out = self._post(audio, detect_language="true", no_language_probabilities="false")
        probs = out.get("language_probabilities") or {}
        if not probs:
            return None, 0.0, []
        ranked = sorted(probs.items(), key=lambda kv: kv[1], reverse=True)
        return ranked[0][0], ranked[0][1], ranked

    def close(self):
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._log.close()
        if self._job:
            import ctypes

            ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(self._job))
            self._job = None
