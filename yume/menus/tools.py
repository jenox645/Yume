"""Tools & Fonts menu: installers, the translation engine, Python dependencies."""

from __future__ import annotations

import logging
import sys

from yume.menus.hf_browser import browse_hf
from yume.menus.translation import _menu_backend, _test_translation
from yume.menus.youtube_auth import _menu_deno, _menu_yt_auth
from yume.ui import C, ask_arrow, ask_yn, header, info, pause, section, success, warn
from yume.utils import find_tool

_log = logging.getLogger("pocket_yume")


# ── Tools menu ─────────────────────────────────────────────────────────────────


def _vocals_menu(cfg: dict) -> None:
    from yume import vocals

    vocals.menu(cfg)


def _benchmark(cfg: dict) -> None:
    from yume.benchmark import benchmark_whisper

    benchmark_whisper(cfg)


def _fonts(cfg: dict) -> None:
    from yume.health import detect_fonts

    detect_fonts()
    pause()


def _extension_help(cfg: dict) -> None:
    from yume.setup import _extension_guide

    header("Browser Extension")
    _extension_guide()
    pause()


def tools_menu(cfg: dict) -> None:
    """Tools management menu."""
    from yume import llama_server

    while True:
        # Same name as the main-menu entry that leads here
        header("Tools & Fonts")
        # ✓/✗ carry the meaning; colour is reinforcement only (colourblind-safe)
        ok = f"{C.GREEN}✓ installed{C.RESET}"
        miss = f"{C.RED}✗ missing{C.RESET}"
        opt_miss = f"{C.DIM}– not installed{C.RESET}"
        build = llama_server.installed_build() if llama_server.server_path() else {}
        engine = f"{C.GREEN}✓ {build.get('variant', '?')}{C.RESET}" if build else opt_miss
        entries = [
            (f"yt-dlp          {ok if find_tool('yt-dlp') else miss}", "Downloads audio from YouTube and 1000+ video sites", _menu_ytdlp),
            (f"FFmpeg          {ok if find_tool('ffmpeg') else miss}", "Converts audio between formats (required)", lambda cfg: _menu_ffmpeg()),
            (f"Deno            {ok if find_tool('deno') else opt_miss}", "Helps bypass YouTube bot detection (optional)", _menu_deno),
            ("Translation Backend", "Choose how Yume translates: llama.cpp / Ollama / LM Studio / Custom", _menu_backend),
            (f"Translation Engine  {engine}", "Install / update llama.cpp's prebuilt GPU server (llama-server)", lambda cfg: _menu_llama_server()),
            ("Vocal Isolation", "Separate the singer from the music before transcribing (better lyrics)", _vocals_menu),
            ("Download Translation Model", "Browse and download GGUF models (the files your translator uses)", browse_hf),
            ("Python Dependencies", "Install required Python packages + romanization libraries", lambda cfg: _menu_pydeps()),
            ("Test Translation", "Send a test sentence to check if translation is working", _test_translation),
            ("Benchmark Whisper", "Measure how fast each speech recognition model runs on your hardware", _benchmark),
            ("Detect Fonts", "Find subtitle-compatible fonts installed on your system", _fonts),
            ("Browser Extension", "How to load Yume in Chrome, Edge, Brave or Firefox", _extension_help),
        ]  # fmt: skip
        ch = ask_arrow(
            "Select a tool:", [(label, desc) for label, desc, _ in entries] + [("Back", None)], default=len(entries)
        )
        if not 0 <= ch < len(entries):
            return
        entries[ch][2](cfg)


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


def cfg_backend() -> str:
    from config import load_config

    return load_config().get("translation_backend", "llamacpp")


# (import name, pip name, what it does) — the libraries server/_romanize.py uses
_ROMA_LIBS = [
    ("fugashi", "fugashi", "Japanese → romaji by word (MeCab), with unidic-lite and jaconv"),
    ("unidic_lite", "unidic-lite", "the dictionary MeCab reads words with"),
    ("jaconv", "jaconv", "kana conversion used by the romaji step"),
    ("pykakasi", "pykakasi", "Japanese → romaji fallback when the three above are missing"),
    ("pypinyin", "pypinyin", "Chinese hanzi → pinyin"),
]


def _importable(module: str) -> bool:
    try:
        __import__(module)
        return True
    except ImportError:
        return False


def _menu_pydeps() -> None:
    header("Python Dependencies")
    _core_deps()
    print()
    _romanization_deps()
    pause()


def _core_deps() -> None:
    from yume.installers import install_llamacpp_python, install_python_deps

    section("Core (required)")
    core = ["faster_whisper", "flask", "waitress"]
    if cfg_backend() == "llamacpp":  # only llama.cpp runs inside Yume's Python
        core += ["llama_cpp", "uvicorn", "fastapi"]
    deps = {p: _importable(p) for p in core}
    for p, ok in deps.items():
        (success if ok else warn)(f"{p}: {'installed' if ok else 'NOT installed'}")
    if all(deps.values()):
        success("All core deps installed!")
    elif ask_yn("Install missing core deps?"):
        install_python_deps()
        if not deps.get("llama_cpp", False):
            install_llamacpp_python()


def _romanization_deps() -> None:
    from yume.installers import _check_pip
    from yume.utils import _run, requirement_name, server_requirements

    section("Romanization (optional — instant romaji/pinyin)")
    missing = []
    for module, name, desc in _ROMA_LIBS:
        if _importable(module):
            success(f"{name}: installed — {desc}")
        else:
            info(f"{name}: {C.DIM}not installed{C.RESET} — {desc}")
            missing.append(name)
    if not missing:
        success("All romanization libs installed — instant romaji and pinyin!")
        return
    print()
    info(f"{C.DIM}Without these, Japanese romaji and Chinese pinyin come from the LLM (slower).{C.RESET}")
    info(f"{C.DIM}Korean and Russian always use Yume's built-in romanization.{C.RESET}")
    if not ask_yn("Install romanization libraries? (recommended)"):
        return
    _check_pip()
    # The versions server/requirements.txt pins
    specs = [s for s in server_requirements() if requirement_name(s) in missing]
    r = _run([sys.executable, "-m", "pip", "install", *specs, "-q", "--no-warn-script-location"], timeout=300)
    if r.returncode == 0:
        success("Romanization libraries installed! Restart server to activate.")
    else:
        warn("Some libraries failed to install. Check pip output above.")
