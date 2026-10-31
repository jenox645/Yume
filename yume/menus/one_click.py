"""One-click start: the browser extension starts and stops Yume (native messaging host)."""

from __future__ import annotations

import sys

from yume.ui import C, ask_arrow, ask_input, error, header, info, pause, success


def _show_state(browsers: list, mins: int) -> None:
    state = f"{C.GREEN}✓ on{C.RESET}  ({', '.join(browsers)})" if browsers else f"{C.DIM}– off{C.RESET}"
    info(f"Status:     {state}")
    info(f"Auto-stop:  {f'after {mins} min without a video' if mins else 'never'}")


def menu_one_click(cfg: dict) -> None:
    """Let the browser extension start/stop Yume (native messaging host)."""
    from yume import native_host

    while True:
        header("One-click start")
        info("When this is on, the extension's Enable button starts Yume by itself —")
        info("no launcher window. Yume then stops on its own when you stop watching.")
        print()
        browsers = native_host.registered_browsers()
        _show_state(browsers, int(cfg.get("auto_stop_minutes", 30)))
        print()
        entries = [
            (
                "Turn on" if not browsers else "Repair / re-register",
                "Register Yume with Chrome, Edge, Brave and Firefox",
                _register,
            ),
            ("Turn off", "Remove the registration", _unregister),
            ("Auto-stop delay", "Minutes without a video before Yume stops (0 = never)", _ask_auto_stop),
        ]
        ch = ask_arrow(
            "Options:",
            [(label, desc) for label, desc, _ in entries] + [("Back", None)],
            default=0 if not browsers else len(entries),
        )
        if not 0 <= ch < len(entries):
            return
        entries[ch][2](cfg)


def _register(cfg: dict) -> None:
    from yume import native_host

    try:
        done = native_host.register()
        success(f"Registered for: {', '.join(done) or 'no browser found'}")
        info("Reload the Yume extension (or restart the browser) once to pick it up.")
    except Exception as e:
        error(f"Registration failed: {e}")
    pause()


def _unregister(cfg: dict) -> None:
    from yume import native_host

    native_host.unregister()
    success("One-click start is off.")
    pause()


def _ask_auto_stop(cfg: dict) -> None:
    from config import save_config

    try:
        n = int(ask_input("Minutes (0 = never)", str(int(cfg.get("auto_stop_minutes", 30)))))
        if not 0 <= n <= 24 * 60:
            raise ValueError
    except ValueError:
        error("Enter a number of minutes between 0 and 1440.")
        pause()
        return
    cfg["auto_stop_minutes"] = n
    save_config(cfg)


def cli_one_click(cfg: dict, args: list) -> None:
    """`pocket_yume.py autostart [on|off|status]`."""
    from yume import native_host

    sub = args[0].lower() if args else "status"
    if sub == "on":
        done = native_host.register()
        success(f"One-click start on for: {', '.join(done) or 'no browser found'}")
        info("Reload the Yume extension once to pick it up.")
    elif sub == "off":
        native_host.unregister()
        success("One-click start off.")
    elif sub == "status":
        browsers = native_host.registered_browsers()
        info(f"One-click start: {'on (' + ', '.join(browsers) + ')' if browsers else 'off'}")
        mins = int(cfg.get("auto_stop_minutes", 30))
        info(f"Auto-stop: {f'after {mins} min idle' if mins else 'never'}")
    else:
        error(f"Unknown: autostart {sub}. Use: autostart on | off | status")
        sys.exit(2)
