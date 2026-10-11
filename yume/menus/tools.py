"""Tools & Fonts menu: installers, translation backend and engine, YouTube auth, one-click start."""

from __future__ import annotations

import json
import logging
import sys
import urllib.request

from yume.network import (
    check_ollama_models,
    check_translation_server,
)
from yume.ports import find_free_port, get_port_process, is_port_free
from yume.ui import (
    C,
    ask_arrow,
    ask_input,
    ask_yn,
    bullet,
    error,
    header,
    info,
    pause,
    section,
    success,
    warn,
)
from yume.utils import GGUF_DIR, TOOLS_DIR, GiB, find_gguf_models, find_tool

from yume.menus import _shared
from yume.menus.hf_browser import browse_hf

_log = logging.getLogger("pocket_yume")


# ── Tools menu ─────────────────────────────────────────────────────────────────


def tools_menu(cfg: dict) -> None:
    """Tools management menu."""
    from yume.benchmark import benchmark_whisper
    from yume.health import detect_fonts

    while True:
        # Same name as the main-menu entry that leads here
        header("Tools & Fonts")
        yt = find_tool("yt-dlp")
        ff = find_tool("ffmpeg")
        dn = find_tool("deno")
        # ✓/✗ carry the meaning; colour is reinforcement only (colourblind-safe)
        ok = f"{C.GREEN}✓ installed{C.RESET}"
        miss = f"{C.RED}✗ missing{C.RESET}"
        opt_miss = f"{C.DIM}– not installed{C.RESET}"
        from yume import llama_server

        build = llama_server.installed_build() if llama_server.server_path() else {}
        engine = f"{C.GREEN}✓ {build.get('variant', '?')}{C.RESET}" if build else opt_miss
        ch = ask_arrow(
            "Select a tool:",
            [
                (f"yt-dlp          {ok if yt else miss}", "Downloads audio from YouTube and 1000+ video sites"),
                (f"FFmpeg          {ok if ff else miss}", "Converts audio between formats (required)"),
                (f"Deno            {ok if dn else opt_miss}", "Helps bypass YouTube bot detection (optional)"),
                ("Translation Backend", "Choose how Yume translates: llama.cpp / Ollama / LM Studio / Custom"),
                (f"Translation Engine  {engine}", "Install / update llama.cpp's prebuilt GPU server (llama-server)"),
                ("Vocal Isolation", "Separate the singer from the music before transcribing (better lyrics)"),
                ("Download Translation Model", "Browse and download GGUF models (the files your translator uses)"),
                ("Python Dependencies", "Install required Python packages + romanization libraries"),
                ("Test Translation", "Send a test sentence to check if translation is working"),
                ("Benchmark Whisper", "Measure how fast each speech recognition model runs on your hardware"),
                ("Detect Fonts", "Find subtitle-compatible fonts installed on your system"),
                ("Browser Extension", "How to load Yume in Chrome, Edge, Brave or Firefox"),
                ("Back", None),
            ],
            default=12,
        )
        if ch == -1 or ch == 12:
            return
        elif ch == 0:
            _menu_ytdlp(cfg)
        elif ch == 1:
            _menu_ffmpeg()
        elif ch == 2:
            _menu_deno(cfg)
        elif ch == 3:
            _menu_backend(cfg)
        elif ch == 4:
            _menu_llama_server()
        elif ch == 5:
            from yume import vocals

            vocals.menu(cfg)
        elif ch == 6:
            browse_hf(cfg)
        elif ch == 7:
            _menu_pydeps()
        elif ch == 8:
            _test_translation(cfg)
        elif ch == 9:
            benchmark_whisper(cfg)
        elif ch == 10:
            detect_fonts()
        elif ch == 11:
            from yume.setup import _extension_guide

            header("Browser Extension")
            _extension_guide()
            pause()


def _menu_llama_server() -> None:
    from yume import llama_server

    header("Translation Engine (llama.cpp)")
    info("llama.cpp's own server runs your GGUF translation model — prebuilt for")
    info("NVIDIA (CUDA), AMD/Intel (Vulkan), Apple (Metal) or the CPU. Yume prefers it")
    info("over llama-cpp-python, which often ends up CPU-only (~10x slower).")
    print()
    if llama_server.server_path():
        build = llama_server.installed_build()
        success(
            f"Installed: {build.get('tag', '?')} ({build.get('variant', '?')})  {C.DIM}{llama_server.server_path()}{C.RESET}"
        )
        label = "Update to the newest build"
    else:
        info("Not installed — translation uses llama-cpp-python.")
        label = "Install"
    print()
    if ask_yn(f"{label}? (restart Yume afterwards)", not llama_server.server_path()):
        llama_server.install()
    pause()


def _menu_ytdlp(cfg: dict) -> None:
    from yume.installers import install_ytdlp

    while True:
        header("yt-dlp")
        p = find_tool("yt-dlp")
        if p:
            success(f"Installed: {p}")
            try:
                from yume.utils import _run

                v = _run([p, "--version"], timeout=5)
                info(f"Version: {v.stdout.strip()}")
            except Exception as e:
                _log.debug("[_menu_ytdlp] version-check failed: %s", e)

            info("yt-dlp supports 1000+ sites: YouTube, NicoNico, Bilibili, Twitch, etc.")
        else:
            warn("Not installed")
        ch = ask_arrow(
            "Options:",
            [
                ("Install / Update", "Download latest binary"),
                ("YouTube Auth", "Deno vs browser cookies"),
                ("Back", None),
            ],
            default=2,
        )
        if ch == -1 or ch == 2:
            return
        elif ch == 0:
            install_ytdlp()
            pause()
        elif ch == 1:
            _menu_yt_auth(cfg)


def menu_one_click(cfg: dict) -> None:
    """Let the browser extension start/stop Yume (native messaging host)."""
    from config import save_config
    from yume import native_host

    while True:
        header("One-click start")
        info("When this is on, the extension's Enable button starts Yume by itself —")
        info("no launcher window. Yume then stops on its own when you stop watching.")
        print()
        browsers = native_host.registered_browsers()
        mins = int(cfg.get("auto_stop_minutes", 30))
        state = f"{C.GREEN}✓ on{C.RESET}  ({', '.join(browsers)})" if browsers else f"{C.DIM}– off{C.RESET}"
        info(f"Status:     {state}")
        info(f"Auto-stop:  {f'after {mins} min without a video' if mins else 'never'}")
        print()
        ch = ask_arrow(
            "Options:",
            [
                (
                    "Turn on" if not browsers else "Repair / re-register",
                    "Register Yume with Chrome, Edge, Brave and Firefox",
                ),
                ("Turn off", "Remove the registration"),
                ("Auto-stop delay", "Minutes without a video before Yume stops (0 = never)"),
                ("Back", None),
            ],
            default=0 if not browsers else 3,
        )
        if ch == -1 or ch == 3:
            return
        elif ch == 0:
            try:
                done = native_host.register()
                success(f"Registered for: {', '.join(done) or 'no browser found'}")
                info("Reload the Yume extension (or restart the browser) once to pick it up.")
            except Exception as e:
                error(f"Registration failed: {e}")
            pause()
        elif ch == 1:
            native_host.unregister()
            success("One-click start is off.")
            pause()
        elif ch == 2:
            v = ask_input("Minutes (0 = never)", str(mins))
            try:
                n = int(v)
                if not 0 <= n <= 24 * 60:
                    raise ValueError
            except ValueError:
                error("Enter a number of minutes between 0 and 1440.")
                pause()
                continue
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


def _menu_yt_auth(cfg: dict) -> None:
    from config import save_config
    from yume.installers import install_deno

    while True:
        header("YouTube Authentication")
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
            cfg["youtube_auth_method"] = "cookies"
            save_config(cfg)
            browsers = ["chrome", "firefox", "edge", "brave", "opera", "chromium", "safari"]
            bc = ask_arrow(
                "Which browser are you logged into YouTube with?",
                [(b.capitalize(), None) for b in browsers] + [("Back", None)],
                default=0,
            )
            if 0 <= bc < len(browsers):
                cfg["cookies_browser"] = browsers[bc]
                save_config(cfg)
                success(f"Using cookies from: {browsers[bc]}")
                info("Make sure you're logged into YouTube in that browser.")
            pause()
        elif ch == 1:
            cfg["youtube_auth_method"] = "deno"
            save_config(cfg)
            success("Set to Deno")
            if not find_tool("deno"):
                if ask_yn("Deno not installed. Download and set up now?"):
                    install_deno()
            else:
                bgutil_main = TOOLS_DIR / "bgutil-ytdlp-pot-provider" / "server" / "src" / "main.ts"
                if not bgutil_main.exists():
                    info("PO token server not set up yet.")
                    if ask_yn("Set up now? (downloads ~5 MB from GitHub)"):
                        install_deno()
            pause()


def _menu_ffmpeg() -> None:
    from yume.installers import install_ffmpeg

    while True:
        header("FFmpeg")
        p = find_tool("ffmpeg")
        (success if p else warn)(f"{'Installed: ' + p if p else 'Not installed'}")
        ch = ask_arrow("Options:", [("Install / Update", "Download latest static build"), ("Back", None)], default=1)
        if ch == -1 or ch == 1:
            return
        elif ch == 0:
            install_ffmpeg()
            pause()


def _menu_deno(cfg: dict) -> None:
    from config import save_config
    from yume.installers import install_deno
    from yume.utils import _run

    while True:
        header("Deno (YouTube Authentication)")
        p = find_tool("deno")
        (success if p else info)(f"{'Installed: ' + p if p else 'Not installed'}")
        print()
        info("YouTube blocks automated downloads with a 'bot detection' challenge.")
        info("Deno is a JavaScript runtime that solves this challenge automatically.")
        info(f"{C.DIM}How it works: Deno runs YouTube's BotGuard script to generate a{C.RESET}")
        info(f"{C.DIM}'proof-of-origin' (PO) token that proves you're a real browser.{C.RESET}")
        info(f"{C.DIM}The bgutil-ytdlp-pot-provider plugin connects this to yt-dlp.{C.RESET}")
        print()

        bgutil_ok = False
        try:
            r = _run([sys.executable, "-m", "pip", "show", "bgutil-ytdlp-pot-provider"], timeout=10)
            bgutil_ok = r.returncode == 0
        except Exception:
            pass

        if p and bgutil_ok:
            success("PO token plugin: installed (YouTube auth fully working)")
        elif p:
            warn("Deno installed but PO token plugin missing")
            info(f"{C.DIM}Install it: pip install bgutil-ytdlp-pot-provider{C.RESET}")
        else:
            warn("Deno not installed — YouTube may block downloads")

        info(f"Current YouTube auth method: {C.BOLD}{cfg.get('youtube_auth_method', 'cookies')}{C.RESET}")
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
            info("Installing PO token plugin...")
            try:
                r = _run(
                    [
                        sys.executable,
                        "-m",
                        "pip",
                        "install",
                        "-q",
                        "--no-warn-script-location",
                        "bgutil-ytdlp-pot-provider",
                    ],
                    timeout=120,
                )
                if r.returncode == 0:
                    success("PO token plugin installed")
                    cfg["youtube_auth_method"] = "deno"
                    save_config(cfg)
                else:
                    error("Install failed")
            except Exception as e:
                error(f"Failed: {e}")
            pause()
        elif ch == 2:
            cfg["youtube_auth_method"] = "cookies"
            save_config(cfg)
            success("Switched to cookies")
            pause()


def _menu_backend(cfg: dict) -> None:
    from config import DEFAULT_TRANSLATION_PORT
    from yume.installers import install_llamacpp_python, install_ollama

    while True:
        header("Translation Backend")
        cur = cfg.get("translation_backend", "llamacpp")
        bi = _shared.BI.get(cur, _shared.BI.get("custom", {}))
        info(f"Current: {C.BOLD}{bi.get('name', cur)}{C.RESET}")
        info(
            f"Address: {C.CYAN}{cfg.get('translation_host', '127.0.0.1')}:"
            f"{cfg.get('translation_port', DEFAULT_TRANSLATION_PORT)}{C.RESET}"
        )
        st = check_translation_server(
            cfg.get("translation_host", "127.0.0.1"),
            cfg.get("translation_port", DEFAULT_TRANSLATION_PORT),
            bi,
        )
        (success if st["up"] else warn)(f"Status: {'RUNNING' if st['up'] else 'Not running'}")

        ch = ask_arrow(
            "Options:",
            [
                ("Change backend", "Switch between llama.cpp/Ollama/LM Studio/WebUI/Custom"),
                ("Change address", f"Currently {cfg.get('translation_host')}:{cfg.get('translation_port')}"),
                ("Install instructions", f"How to set up {bi.get('name', cur)}"),
                ("Manage model", "Pull, change, browse, or download models"),
                ("Back", None),
            ],
            default=4,
        )
        if ch == -1 or ch == 4:
            return
        elif ch == 0:
            _select_backend(cfg)
        elif ch == 1:
            _change_addr(cfg, "translation")
        elif ch == 2:
            header(f"Install {bi.get('name', cur)}")
            print(
                f"\n  {C.BOLD}{bi.get('name', cur)}{C.RESET}\n  {bi.get('desc', '')}\n\n  {C.BOLD}Installation:{C.RESET}"
            )
            for line in bi.get("inst", "").split("\n"):
                print(f"  {line}")
            if cur == "llamacpp":
                print()
                if ask_yn("Install llama-cpp-python now?"):
                    install_llamacpp_python()
            elif cur == "ollama":
                print()
                if ask_yn("Auto-install Ollama now?"):
                    install_ollama()
            pause()
        elif ch == 3:
            _manage_model(cfg)


def _select_backend(cfg: dict) -> None:
    from config import save_config

    header("Select Backend")
    keys = list(_shared.BI.keys())
    opts = [(_shared.BI[k]["name"], _shared.BI[k]["desc"]) for k in keys] + [("Back", None)]
    cur = cfg.get("translation_backend", "llamacpp")
    di = keys.index(cur) if cur in keys else 0
    ch = ask_arrow("Choose:", opts, default=di)
    if ch == -1 or ch == len(keys):
        return
    k = keys[ch]
    bi = _shared.BI[k]
    cfg["translation_backend"] = k
    cfg["translation_host"] = bi["dh"]
    cfg["translation_port"] = bi["dp"]
    save_config(cfg)
    success(f"Backend: {bi['name']}  ({bi['dh']}:{bi['dp']})")
    print(f"\n  {C.BOLD}Installation:{C.RESET}")
    for line in bi["inst"].split("\n"):
        print(f"  {line}")
    pause()


def _change_addr(cfg: dict, prefix: str) -> None:
    from config import save_config, validate_host, validate_port

    ch = cfg.get(f"{prefix}_host", "127.0.0.1")
    cp = cfg.get(f"{prefix}_port", 5000)
    print(f"\n  Current: {C.CYAN}{ch}:{cp}{C.RESET}\n")
    if prefix == "whisper":
        # The Whisper server binds 127.0.0.1 only and rejects any other Host
        # header (DNS-rebinding defence); the extension can only reach localhost.
        host = "127.0.0.1"
        info(f"{C.DIM}The Whisper server always runs on this computer (127.0.0.1) — only the port can change.{C.RESET}")
    else:
        raw_host = ask_input("Host", ch)
        host = validate_host(raw_host)
        if host is None:
            warn(f"Keeping current host: {ch}")
            host = ch
    raw_port = ask_input("Port", str(cp))
    port = validate_port(raw_port, f"{prefix.title()} port")
    # A busy port only matters for servers Yume starts itself (Whisper, llama.cpp);
    # Ollama / LM Studio are SUPPOSED to be listening on theirs.
    launches_it = prefix == "whisper" or cfg.get("translation_backend") == "llamacpp"
    if port is None:
        warn(f"Keeping current port: {cp}")
        port = cp
    elif port != cp and launches_it:
        if not is_port_free(port, host):
            pid, name = get_port_process(port)
            warn(f"Port {port} is in use by {name or 'unknown'} (PID {pid or '?'})")
            if not ask_yn(f"Use port {port} anyway?", False):
                free = find_free_port(port + 1)
                if free:
                    info(f"Suggestion: port {free} is available")
                    if ask_yn(f"Use {free} instead?"):
                        port = free
                    else:
                        port = cp
                else:
                    port = cp
    cfg[f"{prefix}_host"] = host
    cfg[f"{prefix}_port"] = port
    save_config(cfg)
    success(f"Set to {host}:{port}")
    if prefix == "whisper" and port != cp:
        warn(f"Also set the Whisper port to {port} in the browser extension popup (Server Settings).")
    pause()


def _manage_model(cfg: dict) -> None:
    import subprocess

    from config import DEFAULT_OLLAMA_PORT, save_config

    while True:
        header("Manage Translation Model")
        bk = cfg.get("translation_backend", "llamacpp")
        mdl = cfg.get("translation_model", "")
        gp = cfg.get("gguf_model_path", "")
        bi = _shared.BI.get(bk, {})
        info(f"Backend: {C.BOLD}{bi.get('name', bk)}{C.RESET}")
        if mdl:
            info(f"Model:   {C.BOLD}{mdl}{C.RESET}")
        if gp:
            info(f"GGUF:    {gp}")

        gf = find_gguf_models()
        if gf:
            print()
            info(f"GGUF files in {GGUF_DIR}:")
            for f in gf:
                sg = f.stat().st_size / GiB
                act = f" {C.GREEN}<- active{C.RESET}" if str(f) == gp else ""
                bullet(f"{f.name}  ({sg:.2f} GB){act}")

        if bk == "ollama":
            ms = check_ollama_models(
                cfg.get("translation_host", "127.0.0.1"),
                cfg.get("translation_port", DEFAULT_OLLAMA_PORT),
            )
            if ms:
                print()
                info("Ollama models:")
                for m in ms:
                    act = (
                        f" {C.GREEN}<- active{C.RESET}"
                        if m == mdl or m.startswith(mdl.split(":")[0] if ":" in mdl else mdl)
                        else ""
                    )
                    bullet(f"{m}{act}")

        ch = ask_arrow(
            "Options:",
            [
                ("Change model name", "Enter model name manually"),
                ("Pull Ollama model", "Download via ollama pull"),
                ("Download GGUF from HuggingFace", "Browse repos and pick files"),
                ("Select local GGUF file", f"{len(gf)} file(s) in models/translation/"),
                ("Back", None),
            ],
            default=4,
        )
        if ch == -1 or ch == 4:
            return
        elif ch == 0:
            nm = ask_input("Model name", mdl)
            if nm:
                cfg["translation_model"] = nm
                save_config(cfg)
                success(f"Model: {nm}")
            pause()
        elif ch == 1:
            from yume.installers import pull_ollama_model

            mn = ask_input("Ollama model to pull", mdl or "qwen2.5:7b")
            if mn:
                if not check_ollama_models(
                    cfg.get("translation_host", "127.0.0.1"),
                    cfg.get("translation_port", DEFAULT_OLLAMA_PORT),
                ):
                    info("Starting Ollama...")
                    try:
                        subprocess.Popen(  # nosec B603
                            ["ollama", "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                        )
                        import time

                        time.sleep(3)
                    except Exception:
                        error("Could not start Ollama")
                        pause()
                        continue
                pull_ollama_model(mn)
                cfg["translation_model"] = mn
                save_config(cfg)
            pause()
        elif ch == 2:
            browse_hf(cfg)
        elif ch == 3:
            if not gf:
                warn(f"No .gguf files in {GGUF_DIR}")
                pause()
                continue
            fo = [(f"{f.name} ({f.stat().st_size / GiB:.2f} GB)", None) for f in gf] + [("Back", None)]
            fc = ask_arrow("Select:", fo, default=len(fo) - 1)
            if 0 <= fc < len(gf):
                cfg["gguf_model_path"] = str(gf[fc])
                cfg["translation_model"] = gf[fc].stem
                save_config(cfg)
                success(f"Selected: {gf[fc].name}")
                if cfg["translation_backend"] != "llamacpp":
                    if ask_yn("Switch to llama.cpp backend?"):
                        bi2 = _shared.BI.get("llamacpp", {})
                        cfg["translation_backend"] = "llamacpp"
                        cfg["translation_host"] = bi2.get("dh", "127.0.0.1")
                        cfg["translation_port"] = bi2.get("dp", 5000)
                        save_config(cfg)
            pause()


def cfg_backend() -> str:
    from config import load_config

    return load_config().get("translation_backend", "llamacpp")


def _menu_pydeps() -> None:
    from yume.installers import _check_pip, install_llamacpp_python, install_python_deps
    from yume.utils import _run

    header("Python Dependencies")
    section("Core (required)")
    deps: dict[str, bool] = {}
    core = ["faster_whisper", "flask", "waitress"]
    if cfg_backend() == "llamacpp":  # only llama.cpp runs inside Yume's Python
        core += ["llama_cpp", "uvicorn", "fastapi"]
    for p in core:
        try:
            __import__(p)
            deps[p] = True
        except ImportError:
            deps[p] = False
    for p, ok in deps.items():
        (success if ok else warn)(f"{p}: {'installed' if ok else 'NOT installed'}")
    if all(deps.values()):
        success("All core deps installed!")
    elif ask_yn("Install missing core deps?"):
        install_python_deps()
        if not deps.get("llama_cpp", False):
            install_llamacpp_python()

    print()
    section("Romanization (optional — faster romaji/pinyin)")
    roma_deps: dict[str, bool] = {"pykakasi": False, "pypinyin": False}
    roma_desc = {
        "pykakasi": "Japanese kanji → romaji (instant, no LLM needed)",
        "pypinyin": "Chinese hanzi → pinyin (instant, no LLM needed)",
    }
    for p in roma_deps:
        try:
            __import__(p)
            roma_deps[p] = True
        except ImportError:
            pass
    for p, ok in roma_deps.items():
        if ok:
            success(f"{p}: installed — {roma_desc[p]}")
        else:
            info(f"{p}: {C.DIM}not installed{C.RESET} — {roma_desc[p]}")

    if all(roma_deps.values()):
        success("All romanization libs installed — instant romaji/pinyin/romanization!")
    else:
        print()
        info(f"{C.DIM}Without these, Japanese romaji and Chinese pinyin come from the LLM (slower).{C.RESET}")
        info(f"{C.DIM}Korean and Russian always use Yume's built-in romanization.{C.RESET}")
        if ask_yn("Install romanization libraries? (recommended)"):
            _check_pip()
            r = _run(
                [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "pykakasi==2.3.0",
                    "pypinyin==0.55.0",
                    "-q",
                    "--no-warn-script-location",
                ],
                timeout=120,
            )
            if r.returncode == 0:
                success("Romanization libraries installed! Restart server to activate.")
            else:
                warn("Some libraries failed to install. Check pip output above.")
    pause()


def _test_translation(cfg: dict) -> None:
    from config import DEFAULT_TRANSLATION_PORT
    from yume.network import HEALTH_PATH_OPENAI  # type: ignore[attr-defined]

    header("Test Translation")
    bk = cfg.get("translation_backend", "llamacpp")
    h = cfg.get("translation_host", "127.0.0.1")
    p = cfg.get("translation_port", DEFAULT_TRANSLATION_PORT)
    m = cfg.get("translation_model", "")
    bi = _shared.BI.get(bk, _shared.BI.get("custom", {"ap": "/v1/chat/completions", "hp": HEALTH_PATH_OPENAI}))

    info(f"Backend: {bi.get('name', bk)} ({h}:{p})")
    if m:
        info(f"Model: {m}")
    print()

    info("Checking server connectivity...")
    st = check_translation_server(h, p, bi)
    if not st["up"]:
        error(f"Server not reachable at {h}:{p}")
        if bk == "llamacpp":
            warn("Make sure you launched Yume first (main menu → Launch Yume)")
            info(f"{C.DIM}The translation server starts automatically when you launch.{C.RESET}")
        pause()
        return
    success("Server reachable!")

    txt = ask_input("Test sentence (Japanese)", "\u4eca\u65e5\u306f\u3044\u3044\u5929\u6c17\u3067\u3059\u306d")
    info(f"Sending: {txt}")
    print()
    try:
        body = {
            "messages": [
                {"role": "system", "content": "You are a translation system. Output ONLY the English translation."},
                {"role": "user", "content": txt},
            ],
            "max_tokens": 200,
            "temperature": 0.1,
            "stream": False,
        }
        # llama-cpp-python serves one model; every other backend needs the name
        if bk != "llamacpp" and m:
            body["model"] = m
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            f"http://{h}:{p}{bi.get('ap', '/v1/chat/completions')}",  # noqa: S5332 — local LLM backend
            data=data,
            headers={"Content-Type": "application/json", "User-Agent": "Yume"},
            method="POST",
        )
        info("Waiting...")
        with urllib.request.urlopen(req, timeout=60) as resp:  # nosec B310
            result = json.loads(resp.read())
        tr = ""
        if "choices" in result and result["choices"]:
            tr = result["choices"][0].get("message", {}).get("content", "")
        elif "message" in result:
            tr = result["message"].get("content", "")
        if tr:
            print()
            success(f"Translation: {C.BOLD}{tr.strip()}{C.RESET}")
            print()
            success("Pipeline working!")
        else:
            warn("Got response but no translation text")
    except Exception as e:
        error(f"Failed: {e}")
    pause()
