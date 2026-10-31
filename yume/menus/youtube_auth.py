"""YouTube authentication menus: browser cookies or Deno with the PO token plugin."""

from __future__ import annotations

import sys

from yume.constants import TOOLS_DIR
from yume.ui import C, ask_arrow, ask_yn, error, header, info, pause, success, warn
from yume.utils import find_tool

PO_TOKEN_PLUGIN = "bgutil-ytdlp-pot-provider"
_COOKIE_BROWSERS = ["chrome", "firefox", "edge", "brave", "opera", "chromium", "safari"]


def _explain_auth() -> None:
    info("YouTube blocks automated downloads to prevent bots.")
    info("Yume needs a way to prove you're a real person.")
    print()
    info(f"{C.BOLD}Browser Cookies (recommended, default){C.RESET}")
    info(f"{C.DIM}  Borrows your YouTube login from Chrome/Firefox/Edge.{C.RESET}")
    info(f"{C.DIM}  Requirement: be logged into YouTube in your browser.{C.RESET}")
    info(f"{C.DIM}  No extra software needed. Works offline after login.{C.RESET}")
    print()
    info(f"{C.BOLD}Deno (advanced, no YouTube account needed){C.RESET}")
    info(f"{C.DIM}  Uses a small program (Deno) to solve YouTube's bot challenge.{C.RESET}")
    info(f"{C.DIM}  Generates a 'proof-of-origin' token without any login.{C.RESET}")
    info(f"{C.DIM}  Requires: internet connection + Deno installed (~35 MB).{C.RESET}")
    info(f"{C.DIM}  Yume runs a local server (port 4416) to generate tokens.{C.RESET}")
    print()


def _menu_yt_auth(cfg: dict) -> None:
    while True:
        header("YouTube Authentication")
        _explain_auth()
        cur = cfg.get("youtube_auth_method", "cookies")
        info(f"Current method: {C.BOLD}{cur}{C.RESET}")
        print()
        ch = ask_arrow(
            "Select method:",
            [
                ("Browser Cookies (recommended)", "Uses your browser's YouTube login. No extra software."),
                ("Deno (no account needed)", "Solves YouTube's bot challenge via a local server. Needs internet."),
                ("Back", None),
            ],
            default=0 if cur == "cookies" else 1,
        )
        if ch == -1 or ch == 2:
            return
        elif ch == 0:
            _use_cookies(cfg)
        elif ch == 1:
            _use_deno(cfg)


def _use_cookies(cfg: dict) -> None:
    from config import save_config

    cfg["youtube_auth_method"] = "cookies"
    save_config(cfg)
    bc = ask_arrow(
        "Which browser are you logged into YouTube with?",
        [(b.capitalize(), None) for b in _COOKIE_BROWSERS] + [("Back", None)],
        default=0,
    )
    if 0 <= bc < len(_COOKIE_BROWSERS):
        cfg["cookies_browser"] = _COOKIE_BROWSERS[bc]
        save_config(cfg)
        success(f"Using cookies from: {_COOKIE_BROWSERS[bc]}")
        info("Make sure you're logged into YouTube in that browser.")
    pause()


def _use_deno(cfg: dict) -> None:
    from config import save_config
    from yume.installers import install_deno

    cfg["youtube_auth_method"] = "deno"
    save_config(cfg)
    success("Set to Deno")
    if not find_tool("deno"):
        if ask_yn("Deno not installed. Download and set up now?"):
            install_deno()
    elif not (TOOLS_DIR / PO_TOKEN_PLUGIN / "server" / "src" / "main.ts").exists():
        info("PO token server not set up yet.")
        if ask_yn("Set up now? (downloads ~5 MB from GitHub)"):
            install_deno()
    pause()


def _plugin_installed() -> bool:
    from yume.utils import _run

    try:
        return _run([sys.executable, "-m", "pip", "show", PO_TOKEN_PLUGIN], timeout=10).returncode == 0
    except Exception:
        return False


def _show_deno_state(cfg: dict) -> None:
    p = find_tool("deno")
    (success if p else info)(f"{'Installed: ' + p if p else 'Not installed'}")
    print()
    info("YouTube blocks automated downloads with a 'bot detection' challenge.")
    info("Deno is a JavaScript runtime that solves this challenge automatically.")
    info(f"{C.DIM}How it works: Deno runs YouTube's BotGuard script to generate a{C.RESET}")
    info(f"{C.DIM}'proof-of-origin' (PO) token that proves you're a real browser.{C.RESET}")
    info(f"{C.DIM}The bgutil-ytdlp-pot-provider plugin connects this to yt-dlp.{C.RESET}")
    print()

    if p and _plugin_installed():
        success("PO token plugin: installed (YouTube auth fully working)")
    elif p:
        warn("Deno installed but PO token plugin missing")
        info(f"{C.DIM}Install it: pip install {PO_TOKEN_PLUGIN}{C.RESET}")
    else:
        warn("Deno not installed — YouTube may block downloads")

    info(f"Current YouTube auth method: {C.BOLD}{cfg.get('youtube_auth_method', 'cookies')}{C.RESET}")


def _menu_deno(cfg: dict) -> None:
    from config import save_config
    from yume.installers import install_deno

    while True:
        header("Deno (YouTube Authentication)")
        _show_deno_state(cfg)
        ch = ask_arrow(
            "Options:",
            [
                ("Install Deno + PO token plugin", "Downloads Deno (~35 MB) and installs the YouTube auth plugin"),
                ("Install PO token plugin only", "If Deno is already installed, just add the yt-dlp plugin"),
                ("Switch to browser cookies", "Use your browser's YouTube login instead of Deno"),
                ("Back", None),
            ],
            default=3,
        )
        if ch == -1 or ch == 3:
            return
        elif ch == 0:
            install_deno()
            cfg["youtube_auth_method"] = "deno"
            save_config(cfg)
            pause()
        elif ch == 1:
            _install_plugin(cfg)
        elif ch == 2:
            cfg["youtube_auth_method"] = "cookies"
            save_config(cfg)
            success("Switched to cookies")
            pause()


def _install_plugin(cfg: dict) -> None:
    from config import save_config
    from yume.utils import _run

    info("Installing PO token plugin...")
    try:
        cmd = [sys.executable, "-m", "pip", "install", "-q", "--no-warn-script-location", PO_TOKEN_PLUGIN]
        if _run(cmd, timeout=120).returncode == 0:
            success("PO token plugin installed")
            cfg["youtube_auth_method"] = "deno"
            save_config(cfg)
        else:
            error("Install failed")
    except Exception as e:
        error(f"Failed: {e}")
    pause()
