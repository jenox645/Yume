"""The menu shown while the servers run: status, stats, filter, model, logs, stop."""

from __future__ import annotations

from yume import servers
from yume.constants import LOGS_DIR, backend_info
from yume.network import check_server, check_translation_server
from yume.ui import C, ask_arrow, ask_yn, clear, error, info, panel, pause, section, success, warn


def _stop_all(cfg: dict, procs: servers.Processes) -> None:
    print(f"\n  {C.YELLOW}Shutting down...{C.RESET}")
    if not procs:
        # Servers this menu did not start: if the background service runs
        # them (one-click start), stop it; otherwise leave them alone.
        from yume.service import read_state, request_stop

        if read_state()["state"] != "stopped":
            success("Background Yume stopped" if request_stop() else "Asked background Yume to stop")
        else:
            info("These servers were started elsewhere — they keep running.")
        return
    names = procs.names()
    procs.stop(cfg, timeout=1)
    for n in names:
        success(f"{n} stopped")


def _worth_reading(line: str) -> bool:
    return "[Yume]" in line or any(w in line.lower() for w in ("error", "fail", "warn"))


def _is_request(line: str) -> bool:
    return "HTTP/" in line and "/health" not in line


def _log_excerpt(lines: list[str]) -> list[str]:
    """The lines of a server log worth showing: Yume's own messages and
    problems, then requests, then anything else (health checks left out)."""
    lines = lines[-2000:]  # logs reach 5 MB; only the tail matters
    yume_lines = [ln for ln in lines if _worth_reading(ln)]
    access_lines = [ln for ln in lines if _is_request(ln)]
    seen = set(yume_lines) | set(access_lines)
    other_lines = [ln for ln in lines if ln not in seen and "GET /health" not in ln]
    shown = yume_lines[-10:] + access_lines[-5:] + other_lines[-5:]
    return (shown or lines[-15:])[-20:]


def _show_logs() -> None:
    section("Recent Logs")
    for name in [servers.WHISPER_LOG, servers.TRANSLATION_LOG]:
        lp = LOGS_DIR / name
        if not lp.exists():
            continue
        info(f"{C.BOLD}{name}{C.RESET}")
        try:
            with open(lp, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            for line in _log_excerpt(lines):
                print(f"    {C.DIM}{line.rstrip()}{C.RESET}")
        except Exception as e:
            warn(f"Could not read: {e}")
        print()


def _handle_dead_servers(cfg: dict, procs: servers.Processes) -> bool:
    """Report servers that exited on their own. True: the user chose to stop."""
    dead = [(n, p) for n, p in procs if p.poll() is not None]
    if not dead:
        return False
    warn("A server has stopped unexpectedly.")
    for name, p in dead:
        error(f"{name} exited with code {p.returncode}")
    info(f"Its log: {C.CYAN}{LOGS_DIR}{C.RESET}")
    if ask_yn("Stop the other servers and go back to the main menu (choose Launch to restart)?", True):
        _stop_all(cfg, procs)
        return True
    for item in dead:  # keep going with what still runs; don't ask again
        procs.procs.remove(item)
    return False


def _status_panel(cfg: dict, bi: dict) -> None:
    ws_up = check_server(cfg["whisper_host"], cfg["whisper_port"], "/health")["up"]
    ts_up = check_translation_server(cfg["translation_host"], cfg["translation_port"], bi)["up"]

    # Word + colour so status is readable without colour vision
    def state(up: bool) -> str:
        return f"{C.GREEN}● up{C.RESET}  " if up else f"{C.RED}● DOWN{C.RESET}"

    print()
    panel(
        f"  {state(ws_up)} Whisper     {cfg['whisper_host']}:{cfg['whisper_port']}\n"
        f"  {state(ts_up)} Translation {cfg['translation_host']}:{cfg['translation_port']}",
        title=f"{C.GREEN}Yume Running{C.RESET}",
        style=C.GREEN,
    )


def _runtime_actions() -> list[tuple[str, str, object]]:
    """(label, description, action(cfg)) for the runtime menu, Stop excluded."""
    from yume.benchmark import benchmark_whisper
    from yume.menus import _menu_blacklist, _menu_whisper_model, _test_translation, cli_server_stats

    def stats(cfg: dict) -> None:
        cli_server_stats(cfg)
        pause()

    def logs(_cfg: dict) -> None:
        _show_logs()
        pause()

    return [
        ("Server Stats", "GPU usage, memory, how many chunks have been processed", stats),
        ("Subtitle Filter", "Block phrases Whisper hallucinates (fake 'Subscribe' etc.)", _menu_blacklist),
        ("Whisper Model", "Switch speech recognition model without restarting", _menu_whisper_model),
        ("Test Translation", "Send a test sentence to verify the translation pipeline", _test_translation),
        ("Benchmark Whisper", "Measure how fast each model runs on your hardware", benchmark_whisper),
        ("View Logs", "Recent server output (for troubleshooting)", logs),
    ]


def runtime_menu(cfg: dict, procs: servers.Processes, bk: str) -> None:
    """Live menu: stats, blacklist, model swap, logs."""
    bi = backend_info(bk)
    actions = _runtime_actions()
    stop = len(actions)
    try:
        while True:
            clear()
            print()
            if _handle_dead_servers(cfg, procs):
                return
            _status_panel(cfg, bi)
            ch = ask_arrow(
                "Runtime:",
                [(label, desc) for label, desc, _ in actions]
                + [("Stop & Return", "Shut down all servers and go back to main menu")],
                default=stop,
                allow_back=False,
            )
            if ch is not None and ch < stop:
                actions[ch][2](cfg)
            # Default No: this menu opens with Stop highlighted, so two
            # accidental Enters must not kill running servers.
            elif ch == stop and ask_yn("Stop all servers?", default=False):
                _stop_all(cfg, procs)
                return
    except KeyboardInterrupt:
        print()
        _stop_all(cfg, procs)
