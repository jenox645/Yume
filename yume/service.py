"""Headless Yume: `pocket_yume.py serve` and its control helpers.

The browser extension starts Yume through the native messaging host
(yume/native_host.py), which calls `spawn_detached()`. That runs
`pocket_yume.py serve` with no window: it starts the servers Yume owns
(Whisper, plus llama.cpp or Ollama when those are the backend), keeps their
output in logs/, and stops everything after `auto_stop_minutes` without a
video being subtitled (0 = never) or when asked to (`request_stop()`).

State shared with the native host lives in config/:
    service.json  {"pid", "state", "message", "started", "ports"}
    service.stop  presence asks the supervisor to shut down
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

from yume.hardware import IS_WIN
from yume.utils import BASE_DIR

_log = logging.getLogger("pocket_yume.service")

CONFIG_DIR = BASE_DIR / "config"
STATE_FILE = CONFIG_DIR / "service.json"
STOP_FILE = CONFIG_DIR / "service.stop"
LOCK_FILE = CONFIG_DIR / "service.lock"  # held by the one running supervisor
TOKEN_FILE = BASE_DIR / ".yume_token"  # written by the Whisper server
SERVICE_LOG = "service.log"

TICK_S = 5
WHISPER_START_TIMEOUT_S = 600  # first run downloads the Whisper model
TRANSLATION_START_TIMEOUT_S = 300

# Windows process creation flags (subprocess exposes most, not all, of them)
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_DETACHED_PROCESS = 0x00000008
_CREATE_NO_WINDOW = 0x08000000
_CREATE_BREAKAWAY_FROM_JOB = 0x01000000


# ── Process helpers ──────────────────────────────────────────────────────────


def pid_alive(pid: int | None) -> bool:
    """True if a process with this PID is running. Never signals the process.

    (On Windows `os.kill(pid, 0)` is not a probe: 0 is CTRL_C_EVENT, and any
    other signal number calls TerminateProcess.)
    """
    if not pid or pid <= 0:
        return False
    if IS_WIN:
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return ctypes.get_last_error() == 5  # access denied: exists, not ours
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(h, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def console_python() -> str:
    """python.exe next to the running interpreter (pythonw has no stdout)."""
    exe = Path(sys.executable)
    if exe.stem.lower() == "pythonw":
        cand = exe.with_name("python" + exe.suffix)
        if cand.exists():
            return str(cand)
    return str(exe)


def windowless_python() -> str:
    """pythonw.exe on Windows (no console flashes up), the interpreter elsewhere."""
    exe = Path(sys.executable)
    if IS_WIN and exe.stem.lower() == "python":
        cand = exe.with_name("pythonw" + exe.suffix)
        if cand.exists():
            return str(cand)
    return str(exe)


def spawn_detached(args: list[str]) -> int:
    """Start `pocket_yume.py <args>` with no window, outliving the caller.

    Chrome and Firefox run native messaging hosts inside a Windows job object
    that is killed with the host, so the child must break away from it. When
    the job forbids breakaway, WMI creates the process instead (its parent is
    then the WMI service, outside any job). Returns the PID (0 if unknown).
    """
    return _spawn_detached_cmd([windowless_python(), str(BASE_DIR / "pocket_yume.py"), *args])


def _spawn_detached_cmd(cmd: list[str]) -> int:
    common = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "cwd": str(BASE_DIR),
        "close_fds": True,
    }
    if not IS_WIN:
        return subprocess.Popen(cmd, start_new_session=True, **common).pid  # nosec B603 — fixed argv

    flags = _DETACHED_PROCESS | _CREATE_NEW_PROCESS_GROUP | _CREATE_NO_WINDOW
    try:
        p = subprocess.Popen(cmd, creationflags=flags | _CREATE_BREAKAWAY_FROM_JOB, **common)  # nosec B603
        # A breakaway can "succeed" and still leave the child in a job: a venv's
        # python.exe redirector runs the real interpreter in a job with SILENT
        # breakaway, so the child leaves that job but stays in the browser's,
        # and dies with the native host. Only a child outside every job is safe.
        if not _in_job(p.pid):
            return p.pid
        _log.info("[spawn_detached] child still inside a job — using WMI")
        p.kill()
    except OSError as e:
        _log.info("[spawn_detached] breakaway refused (%s) — using WMI", e)
    try:
        return _spawn_wmi(cmd)
    except Exception as e:
        _log.warning("[spawn_detached] WMI spawn failed (%s) — starting as a plain child", e)
    return subprocess.Popen(cmd, creationflags=flags, **common).pid  # nosec B603


def _in_job(pid: int) -> bool:
    """True if the process belongs to any Windows job object (False if unknown)."""
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k32.IsProcessInJob.argtypes = (wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL))
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    try:
        result = wintypes.BOOL()
        return bool(k32.IsProcessInJob(h, None, ctypes.byref(result))) and bool(result.value)
    finally:
        k32.CloseHandle(h)


def _spawn_wmi(cmd: list[str]) -> int:
    """Create a process through Win32_Process.Create (outside the caller's job)."""
    line = subprocess.list2cmdline(cmd).replace("'", "''")
    cwd = str(BASE_DIR).replace("'", "''")
    ps = (
        "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
        f"-Arguments @{{CommandLine='{line}'; CurrentDirectory='{cwd}'}}; "
        'Write-Output "$($r.ReturnValue) $($r.ProcessId)"'
    )
    from yume.utils import _run

    out = _run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], timeout=30, creationflags=_CREATE_NO_WINDOW
    )
    parts = (out.stdout or "").split()
    if len(parts) != 2 or parts[0] != "0":
        raise OSError(f"Win32_Process.Create failed: {out.stdout.strip() or out.stderr.strip()}")
    return int(parts[1])


# ── Shared state ─────────────────────────────────────────────────────────────


def read_state() -> dict:
    """The supervisor's state, or {"state": "stopped"} when none is running.

    A supervisor that failed leaves its error behind: {"state": "stopped",
    "last_error": "..."} until the next start.
    """
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"state": "stopped"}
    if not isinstance(data, dict):
        return {"state": "stopped"}
    if not pid_alive(data.get("pid")):
        if data.get("state") == "error":
            return {"state": "stopped", "last_error": data.get("message", "")}
        return {"state": "stopped"}
    return data


def _write_state(pid: int | None = None, **fields) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    data = {"pid": pid or os.getpid(), **fields}
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(STATE_FILE)


def mark_starting(pid: int) -> None:
    """Record a just-spawned supervisor, so a second start request in the
    second or two before it writes its own state does not spawn another."""
    if pid:
        _write_state(pid=pid, state="starting", message="Starting Yume…")


def _acquire_lock() -> bool:
    """One supervisor at a time: atomically create the lock file (a lock left
    by a process that no longer runs is taken over)."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                owner = int(LOCK_FILE.read_text(encoding="utf-8").strip() or 0)
            except (OSError, ValueError):
                owner = 0
            if owner != os.getpid() and pid_alive(owner):
                return False
            LOCK_FILE.unlink(missing_ok=True)  # stale
            continue
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
        return True
    return False


def _release_lock() -> None:
    try:
        if int(LOCK_FILE.read_text(encoding="utf-8").strip() or 0) == os.getpid():
            LOCK_FILE.unlink()
    except (OSError, ValueError):
        pass


def request_stop(timeout: float = 20) -> bool:
    """Ask a running supervisor to stop; returns True once it is gone."""
    st = read_state()
    if st["state"] == "stopped":
        return True
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    STOP_FILE.write_text(str(time.time()), encoding="utf-8")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not pid_alive(st.get("pid")):
            return True
        time.sleep(0.25)
    return not pid_alive(st.get("pid"))


# ── The supervisor ───────────────────────────────────────────────────────────


class _Supervisor:
    def __init__(self, cfg: dict, backend_info: dict):
        self.cfg = cfg
        self.backend_info = backend_info
        self.procs: list[tuple[str, subprocess.Popen]] = []
        self.logs: list = []
        self.started = time.time()

    # -- children --------------------------------------------------------------

    def _spawn(self, name: str, cmd: list[str], log_name: str, env: dict) -> subprocess.Popen:
        from yume.launch import _open_rotating_log

        if cmd and cmd[0] == sys.executable:
            cmd = [console_python(), *cmd[1:]]
        lh = _open_rotating_log(log_name)
        self.logs.append(lh)
        flags = _CREATE_NO_WINDOW if IS_WIN else 0
        p = subprocess.Popen(  # nosec B603 — argv built by yume.launch builders
            cmd, stdout=lh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env, creationflags=flags
        )
        self.procs.append((name, p))
        _log.info("started %s (pid %s): %s", name, p.pid, " ".join(cmd))
        return p

    def _wait(self, p: subprocess.Popen | None, ready, timeout: float) -> str | None:
        """None once ready(); otherwise an error message."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._stop_requested():
                return "stop requested"
            if p is not None and p.poll() is not None:
                return f"exited with code {p.returncode}"
            err = ready()
            if err is None:
                return None
            if err:  # a definitive failure reported by the server
                return err
            time.sleep(1)
        return "did not become ready in time"

    def _start_translation(self, env: dict) -> str | None:
        from yume.launch import llamacpp_command, resolve_gguf
        from yume.network import HEALTH_PATH_OLLAMA, check_server, check_translation_server
        from yume.ports import kill_port_process

        cfg = self.cfg
        bk = cfg.get("translation_backend", "llamacpp")
        host, port = cfg["translation_host"], cfg["translation_port"]
        bi = self.backend_info.get(bk, self.backend_info.get("custom", {}))
        if check_translation_server(host, port, bi)["up"]:
            return None
        if bk == "llamacpp":
            gguf = resolve_gguf(cfg)
            if gguf is None:
                return "no .gguf translation model in models/translation/"
            if not kill_port_process(port, interactive=False):
                return f"translation port {port} is used by another program"
            p = self._spawn("Translation", llamacpp_command(cfg, gguf, port), "translation_server.log",
                            {**env, "PYTHONUTF8": "1"})  # fmt: skip

            def ready():
                return None if check_translation_server(host, port, bi)["up"] else ""

            err = self._wait(p, ready, TRANSLATION_START_TIMEOUT_S)
            return f"translation server: {err}" if err else None
        if bk == "ollama":
            try:
                p = self._spawn("Ollama", ["ollama", "serve"], "ollama.log", env)
            except FileNotFoundError:
                return "Ollama is not installed"

            def ready():
                return None if check_server(host, port, HEALTH_PATH_OLLAMA)["up"] else ""

            err = self._wait(p, ready, 60)
            return f"Ollama: {err}" if err else None
        # LM Studio / text-generation-webui / custom: run by the user. Whisper
        # works without it (lines stay untranslated until it is reachable).
        _log.info("translation backend %s is not running at %s:%s", bk, host, port)
        return None

    def _start_whisper(self, env: dict) -> str | None:
        from yume.launch import whisper_command
        from yume.network import check_server
        from yume.ports import kill_port_process

        host, port = self.cfg["whisper_host"], self.cfg["whisper_port"]
        if check_server(host, port, "/health")["up"]:
            return None
        cmd = whisper_command(self.cfg)
        if cmd is None:
            return "server/faster_whisper_server.py is missing"
        if not kill_port_process(port, interactive=False):
            return f"Whisper port {port} is used by another program"
        p = self._spawn("Whisper", cmd, "whisper_server.log", env)

        def ready():
            data = check_server(host, port, "/health")["data"]
            if data.get("status") == "error":
                return f"model failed to load: {data.get('error') or 'unknown error'}"
            return None if data.get("status") == "ready" else ""

        err = self._wait(p, ready, WHISPER_START_TIMEOUT_S)
        return f"Whisper: {err}" if err else None

    # -- loop --------------------------------------------------------------------

    def _stop_requested(self) -> bool:
        return STOP_FILE.exists()

    def _state(self, state: str, message: str = "") -> None:
        _write_state(
            state=state,
            message=message,
            started=self.started,
            ports={"whisper": self.cfg["whisper_port"], "translation": self.cfg["translation_port"]},
        )
        _log.info("state: %s %s", state, message)

    def _active(self) -> bool:
        """True while a video is being subtitled (a job was polled recently)."""
        from yume.network import server_get

        stats = server_get(self.cfg["whisper_host"], self.cfg["whisper_port"], "/stats", timeout=5)
        return bool(stats and stats.get("active"))

    def run(self) -> int:
        from yume.launch import build_server_env

        from yume.utils import missing_requirements

        env = build_server_env()
        miss = missing_requirements()
        if miss:  # no one to ask here; the launcher and the health check offer the install
            _log.warning("missing server packages: %s", ", ".join(miss))
        self._state("starting", "Starting the translation server…")
        err = self._start_translation(env)
        if err is None:
            self._state("starting", "Loading the speech model…")
            err = self._start_whisper(env)
        if err:
            if err != "stop requested":
                self._state("error", err)
            return 1
        if not self.procs:
            _log.info("servers were already running (started elsewhere) — nothing to supervise")
            return 0

        self._state("running")
        idle_limit = max(0.0, float(self.cfg.get("auto_stop_minutes", 30))) * 60
        last_active = time.time()
        while not self._stop_requested():
            time.sleep(TICK_S)
            dead = [f"{n} (exit code {p.returncode})" for n, p in self.procs if p.poll() is not None]
            if dead:
                self._state("error", f"{', '.join(dead)} stopped unexpectedly — see logs/")
                return 1
            if self._active():
                last_active = time.time()
            elif idle_limit and time.time() - last_active > idle_limit:
                _log.info("idle for %g min — stopping", idle_limit / 60)
                break
        return 0

    def shutdown(self) -> None:
        for name, p in reversed(self.procs):
            try:
                p.terminate()
                p.wait(timeout=10)
            except Exception:
                try:
                    p.kill()
                except OSError:
                    pass
            _log.info("stopped %s", name)
        if any(name == "Whisper" for name, _ in self.procs):
            from yume.ports import stop_bgutil_server

            TOKEN_FILE.unlink(missing_ok=True)  # that server's token is dead now
            stop_bgutil_server()
        for lh in self.logs:
            try:
                lh.close()
            except OSError:
                pass


def serve(cfg: dict, backend_info: dict) -> int:
    """Run Yume headless until idle/stopped. Returns a process exit code."""
    from yume.launch import _open_rotating_log

    log_fh = _open_rotating_log(SERVICE_LOG)
    # pythonw has no stdout/stderr; anything printed by shared helpers goes to the log
    sys.stdout = sys.stderr = log_fh
    handler = logging.StreamHandler(log_fh)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    # Replace the root handlers: main() bound one to sys.stderr, which is None under pythonw
    logging.root.handlers = [handler]
    logging.getLogger("pocket_yume").setLevel(logging.INFO)

    if not _acquire_lock():
        _log.info("another supervisor is running — exiting")
        return 0
    STOP_FILE.unlink(missing_ok=True)
    sup = _Supervisor(cfg, backend_info)
    code = 1
    try:
        code = sup.run()
    except Exception as e:
        _log.exception("serve failed")
        sup._state("error", str(e))
    finally:
        failed = read_state().get("state") == "error"
        if not failed:
            sup._state("stopping")
        sup.shutdown()
        STOP_FILE.unlink(missing_ok=True)
        if not failed:  # an error stays readable (see read_state) until the next start
            STATE_FILE.unlink(missing_ok=True)
        _release_lock()
        _log.info("exit %d", code)
    return code
