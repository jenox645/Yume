"""The CLI: `python pocket_yume.py [command]` and the interactive main menu.

Each command is one entry in COMMANDS: the `help` text is generated from the
same table, so a command cannot be listed without existing, or the reverse.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import traceback
from dataclasses import dataclass
from typing import Callable

from config import CONFIG_DIR, config_export, config_import, load_config, save_config
from yume.constants import BACKEND_INFO, GGUF_DIR, LOGS_DIR, MODELS_DIR, SERVER_DIR, TOOLS_DIR, VERSION
from yume.ui import C, ask_arrow, enable_ansi, error, header, info, panel, pause

_log = logging.getLogger("pocket_yume")


# ── Commands ──────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Command:
    run: Callable[[dict, list[str]], object]  # (config, arguments after the command)
    group: str  # heading in `help`
    help: tuple[tuple[str, str], ...]  # (usage, description) lines in `help`


def _launch(cfg, args):
    from yume.launch import launch_services

    launch_services(cfg)


def _serve(cfg, args):
    from yume.service import serve

    sys.exit(serve(cfg))


def _stop(cfg, args):
    from yume.service import read_state, request_stop

    if read_state()["state"] == "stopped":
        info("Yume is not running in the background.")
    elif request_stop():
        info("Stopped.")
    else:
        error("Yume did not stop in time — see logs/service.log")


def _autostart(cfg, args):
    from yume.menus import cli_one_click

    cli_one_click(cfg, args)


def _setup(cfg, args):
    from yume.setup import setup_wizard

    cfg["first_run_complete"] = False
    save_config(cfg)
    return setup_wizard(cfg)


def _settings(cfg, args):
    from yume.menus import settings_menu

    settings_menu(cfg)


def _guide(cfg, args):
    from yume.guides import guides_menu

    guides_menu(cfg)


def _status(cfg, args):
    from yume.health import show_status

    show_status(cfg)


def _health(cfg, args):
    from yume.health import health_check

    health_check(cfg)


def _stats(cfg, args):
    from yume.menus import cli_server_stats

    cli_server_stats(cfg)


def _ports(cfg, args):
    from yume.ports import show_ports_status

    show_ports_status(cfg)


def _recommend(cfg, args):
    from yume.hardware import recommend_whisper_model

    model, reason = recommend_whisper_model()
    info(f"Recommended: {model}")
    info(f"Reason: {reason}")


def _fonts(cfg, args):
    from yume.health import detect_fonts

    detect_fonts()


def _benchmark(cfg, args):
    from yume.benchmark import benchmark_whisper

    benchmark_whisper(cfg)


def _model(cfg, args):
    from yume.menus import cli_model

    cli_model(cfg, args)


def _blacklist(cfg, args):
    from yume.menus import cli_blacklist

    cli_blacklist(cfg, args)


def _export(cfg, args):
    config_export(cfg, args[0] if args else None)


def _import(cfg, args):
    if not args:
        error("Usage: python pocket_yume.py import <path>")
        return
    config_import(args[0])  # merges with defaults and saves


COMMANDS: dict[str, Command] = {
    "launch": Command(_launch, "Run", (("launch", "Start servers (interactive runtime menu)"),)),
    "serve": Command(_serve, "Run", (("serve", "Start servers in the background (no window, auto-stops)"),)),
    "stop": Command(_stop, "Run", (("stop", "Stop servers started in the background"),)),
    "autostart": Command(
        _autostart, "Run", (("autostart on|off", "Let the browser extension start/stop Yume by itself"),)
    ),
    "setup": Command(_setup, "Run", (("setup", "Run setup wizard"),)),
    "settings": Command(
        _settings, "Run", (("settings", "Settings menu (models, translation, addresses, one-click start)"),)
    ),
    "guide": Command(_guide, "Run", (("guide", "Step-by-step how-to guides (cookbook)"),)),
    "status": Command(_status, "Inspect", (("status", "Check components"),)),
    "health": Command(_health, "Inspect", (("health", "Run full health check"),)),
    "stats": Command(_stats, "Inspect", (("stats", "Live server statistics (GPU, session)"),)),
    "ports": Command(_ports, "Inspect", (("ports", "Show port availability"),)),
    "recommend": Command(_recommend, "Inspect", (("recommend", "Suggest best Whisper model for your hardware"),)),
    "fonts": Command(_fonts, "Inspect", (("fonts", "Detect installed CJK and system fonts"),)),
    "benchmark": Command(_benchmark, "Inspect", (("benchmark", "Test Whisper model speeds on your hardware"),)),
    "model": Command(
        _model,
        "Manage",
        (
            ("model", "Show current Whisper model"),
            ("model list", "List available models with VRAM"),
            ("model switch <n>", "Hot-swap Whisper model"),
        ),
    ),
    "blacklist": Command(
        _blacklist,
        "Manage",
        (
            ("blacklist list", "Show server blacklist"),
            ("blacklist add <t>", "Block a phrase"),
            ("blacklist remove <t>", "Unblock a phrase"),
            ("blacklist clear", "Clear all entries"),
        ),
    ),
    "export": Command(_export, "Manage", (("export [path]", "Export config to backup file"),)),
    "import": Command(_import, "Manage", (("import <path>", "Import config from backup file"),)),
}
ALIASES = {"guides": "guide"}
HELP_FLAGS = ("help", "-h", "--help")


def print_help() -> None:
    print(f"\n  {C.BOLD}Pocket Yume v{VERSION}{C.RESET}")
    print("  Cross-platform installer for Yume AI Subtitles\n")
    print(f"  {C.BOLD}Usage:{C.RESET} python pocket_yume.py [command]\n")
    lines: dict[str, list[tuple[str, str]]] = {"Run": [("(none)", "Interactive menu")]}
    for cmd in COMMANDS.values():
        lines.setdefault(cmd.group, []).extend(cmd.help)
    lines["Flags"] = [
        ("help / -h / --help", "This message"),
        ("--verbose / -v", "Enable debug logging to stderr"),
        ("--version", "Print version and exit"),
        ("--no-color", "Disable colored output"),
    ]
    for group, rows in lines.items():
        if group != "Run":
            print()
        print(f"  {C.GOLD}{group}:{C.RESET}")
        for usage, desc in rows:
            print(f"    {usage:<20} {desc}")
    print()


# ── Main menu ─────────────────────────────────────────────────────────────────

# Background update check: [latest, url, done], started on the first menu render
_update_result: list | None = None


def _hardware_label() -> str:
    from yume.hardware import _detect_cpu_name, detect_gpu

    gpu = detect_gpu()
    if gpu["has_nvidia"]:
        return f"{C.GREEN}{gpu['name']}{C.RESET}"
    if gpu["has_amd"]:
        return f"{C.RED}{gpu['name']} (AMD){C.RESET}"
    cpu_name = _detect_cpu_name()
    color = C.BLUE if "intel" in cpu_name.lower() else C.RED if "amd" in cpu_name.lower() else C.YELLOW
    return f"{color}CPU: {cpu_name}{C.RESET}"


def _servers_label(cfg: dict) -> str:
    """An instant local port probe (not a health check): enough to tell a
    returning user whether they still need to Launch."""
    from yume.ports import is_port_free

    w_up = not is_port_free(cfg["whisper_port"], cfg.get("whisper_host", "127.0.0.1"))
    t_up = not is_port_free(cfg["translation_port"], cfg.get("translation_host", "127.0.0.1"))
    if w_up and t_up:
        return f"{C.GREEN}● running{C.RESET}"
    if w_up or t_up:
        return f"{C.YELLOW}● partially running{C.RESET}  {C.DIM}(one server is down — use Launch Yume){C.RESET}"
    return f"{C.DIM}○ stopped — choose Launch Yume to start{C.RESET}"


def _update_notice() -> None:
    """Start the update check once; show its result on a later render."""
    global _update_result
    if _update_result is None:
        from yume.utils import check_for_updates

        _update_result = [None, None, False]

        def _bg_check() -> None:
            global _update_result
            try:
                latest, url = check_for_updates(VERSION)
                _update_result = [latest, url, True]
            except Exception:
                _update_result = [None, None, True]

        threading.Thread(target=_bg_check, daemon=True).start()
    if _update_result[2] and _update_result[0]:
        print(f"  {C.YELLOW}⬆{C.RESET}  Update available: v{_update_result[0]}  {C.DIM}{_update_result[1]}{C.RESET}")


def _tools(cfg, args):
    from yume.menus import tools_menu

    tools_menu(cfg)


def _uninstall(cfg, args):
    from yume.setup import uninstall_yume

    uninstall_yume()


# (label, description, handler); a handler returning a dict replaces the config
MAIN_MENU = [
    ("Launch Yume", "Start servers + runtime menu", _launch),
    ("System Status", "Hardware, tools, packages, ports", _status),
    ("Health Check", "Test every component end-to-end", _health),
    ("Settings", "Whisper, translation, addresses, subtitles", _settings),
    ("Tools & Fonts", "Install, update, manage components, detect fonts", _tools),
    ("Help & Guides", "Step-by-step recipes: first subtitles, models, fixes", _guide),
    ("Re-run Setup", "Guided first-time configuration", _setup),
    ("Uninstall", "Remove Yume and all data", _uninstall),
]


def main_menu() -> None:
    cfg = load_config()
    if not cfg.get("first_run_complete"):
        from yume.setup import setup_wizard

        cfg = setup_wizard(cfg)

    while True:
        header(version=VERSION)
        bn = BACKEND_INFO.get(cfg.get("translation_backend", "llamacpp"), {}).get("name", "?")
        panel(
            f"{C.DIM}Hardware   {C.RESET}  {_hardware_label()}\n"
            f"{C.DIM}Speech AI  {C.RESET}  {C.CYAN}{cfg['whisper_model']}{C.RESET}  on  {cfg['whisper_host']}:{cfg['whisper_port']}\n"
            f"{C.DIM}Translator {C.RESET}  {C.MAGENTA}{bn}{C.RESET}  on  {cfg['translation_host']}:{cfg['translation_port']}\n"
            f"{C.DIM}Servers    {C.RESET}  {_servers_label(cfg)}",
            style=C.DIM,
        )
        _update_notice()
        print(
            f"  {C.DIM}Tip: 'python pocket_yume.py help' lists direct CLI commands (launch, status, stats...){C.RESET}"
        )

        ch = ask_arrow(
            "What would you like to do?",
            [(label, desc) for label, desc, _fn in MAIN_MENU] + [("Exit", None)],
            default=0,
            allow_back=False,
        )
        if ch >= len(MAIN_MENU):
            print(f"\n  {C.GOLD}Goodbye!{C.RESET}\n")
            break
        result = MAIN_MENU[ch][2](cfg, [])
        if isinstance(result, dict):
            cfg = result


# ── Entry point ───────────────────────────────────────────────────────────────


def _utf8_output() -> None:
    """Piped or redirected, Python writes the ANSI code page (cp932, cp1252),
    which cannot encode the CLI's ✓ — …: `pocket_yume.py health > log.txt`
    crashed with UnicodeEncodeError. (A console is UTF-8 already.)"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def _configure_logging() -> None:
    verbose = "--verbose" in sys.argv or "-v" in sys.argv
    env_level = os.environ.get("LOG_LEVEL", "").upper()
    if verbose or env_level == "DEBUG":
        level = logging.DEBUG
    else:
        level = max(getattr(logging, env_level, logging.WARNING), logging.DEBUG)
    logging.basicConfig(level=level, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s", stream=sys.stderr)
    sys.argv = [a for a in sys.argv if a not in ("--verbose", "-v")]


def _configure_color() -> None:
    if "--no-color" in sys.argv or os.environ.get("NO_COLOR"):
        C.disable()
        sys.argv = [a for a in sys.argv if a != "--no-color"]
    else:
        enable_ansi()


def run_command(name: str, args: list[str]) -> None:
    name = ALIASES.get(name, name)
    if name in HELP_FLAGS:
        print_help()
    elif name in COMMANDS:
        COMMANDS[name].run(load_config(), args)
    else:
        print(f"  Unknown: {name}. Try: python pocket_yume.py help")


def main() -> None:
    _utf8_output()
    _configure_logging()
    _configure_color()
    if "--version" in sys.argv:
        print(f"Yume v{VERSION} (Pocket Yume CLI)")
        return

    for d in [TOOLS_DIR, SERVER_DIR, CONFIG_DIR, MODELS_DIR, GGUF_DIR, LOGS_DIR]:
        d.mkdir(parents=True, exist_ok=True)

    try:
        if len(sys.argv) > 1:
            run_command(sys.argv[1].lower(), sys.argv[2:])
        else:
            main_menu()
    except KeyboardInterrupt:
        print(f"\n\n  {C.GOLD}Goodbye!{C.RESET}\n")
    except Exception as e:
        if len(sys.argv) > 1:
            raise
        error(f"Error: {e}")
        traceback.print_exc()
        pause()
