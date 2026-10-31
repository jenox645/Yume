"""Health check and font detection."""

from __future__ import annotations

import concurrent.futures
import os
import re
import sys
from pathlib import Path

from yume.constants import BASE_DIR, EXT_DIR, IS_MAC, IS_WIN, SERVER_DIR, backend_info
from yume.hardware import detect_gpu, detect_ram_gb, disk_free_gb, recommend_whisper_model
from yume.network import check_server, check_translation_server
from yume.ports import get_port_process, is_port_free
from yume.ui import C, bullet, header, info, panel, section, success, table, warn
from yume.utils import find_gguf_models, find_tool


# ── Health check ──────────────────────────────────────────────────────────────
# Each check returns one (name, passed, detail) row, a list of rows, or None.


def _chk_system():
    gpu = detect_gpu()
    ram = detect_ram_gb()
    disk = disk_free_gb()
    return [
        ("GPU detection", True, gpu["name"] or "CPU mode"),
        ("RAM ≥ 4 GB", ram >= 4, f"{ram:.1f} GB"),
        ("Disk ≥ 5 GB", disk >= 5, f"{disk:.1f} GB free"),
    ]


def _chk_tool(t):
    from yume.utils import _run

    p = find_tool(t)
    if not p:
        return (f"{t} installed", False, "not found")
    r = _run([p, "--version" if t == "yt-dlp" else "-version"], timeout=5)
    ok = r.returncode == 0
    return (f"{t} installed", ok, p if ok else f"found at {p} but fails to run")


def _chk_deno(cfg):
    if cfg.get("youtube_auth_method") == "cookies":
        return None
    dn = find_tool("deno")
    return ("deno installed", dn is not None, dn or "not found (needed for YouTube)")


def _chk_pip():
    from yume.utils import _run

    r = _run([sys.executable, "-m", "pip", "--version"], timeout=10)
    if r.returncode != 0:
        return ("pip available", False, "not found — install python3-pip")
    parts = r.stdout.strip().split()
    return ("pip available", True, parts[1] if len(parts) >= 2 else "unknown")


def _chk_pkg(pkg, name):
    try:
        __import__(pkg)
        return (name, True, "installed")
    except ImportError:
        return (name, False, "not installed")


def _chk_requirements():
    from yume.utils import missing_requirements

    miss = missing_requirements()
    return ("Server packages", not miss, "all installed" if not miss else "missing: " + ", ".join(miss))


def _chk_llama_cuda():
    from yume import llama_server

    if llama_server.server_path():
        build = llama_server.installed_build()
        return ("Translation engine", True, f"llama-server {build.get('tag', '')} ({build.get('variant', '?')})")
    gpu = detect_gpu()
    if not (gpu.get("has_nvidia") or gpu.get("has_amd")):
        return None  # not relevant on CPU-only machines
    try:
        import llama_cpp.llama_cpp as _lib  # type: ignore[import]
    except ImportError:
        return ("llama-cpp CUDA support", False, "llama-cpp-python not installed")
    if callable(getattr(_lib, "ggml_backend_cuda_reg", None)):
        return ("llama-cpp CUDA support", True, "GPU offloading available")
    return (
        "llama-cpp CUDA support",
        False,
        "CPU-only build — model will use RAM, not VRAM. Re-run setup to reinstall with CUDA",
    )


def _chk_roma(pkg, desc):
    # Optional lib: always PASS, but the detail must not read like a failure
    try:
        __import__(pkg)
        return (pkg, True, f"installed — {desc}")
    except ImportError:
        return (pkg, True, f"optional, not installed — {desc} will use the LLM (slower)")


def _chk_cutlet():
    # Optional like the others: without it, pykakasi romanizes Japanese
    try:
        import fugashi  # noqa: F401
        import unidic_lite  # noqa: F401

        return ("Japanese by word", True, "installed — cutlet on MeCab + UniDic (best romaji)")
    except ImportError as e:
        return ("Japanese by word", True, f"optional, not installed ({e.name}) — pykakasi is used instead")


def _port_row(label, port, responding, launched_by_yume):
    """A busy port is only a problem when something OTHER than the expected
    server holds it (the old check failed whenever Yume was running)."""
    if is_port_free(port):
        ok = launched_by_yume  # free is fine if Yume will start it; an external LLM should be up
        return (f"{label} port {port}", ok, "free — starts on Launch" if ok else "nothing listening")
    if responding:
        return (f"{label} port {port}", True, "in use by the running server")
    pid, name = get_port_process(port)
    return (f"{label} port {port}", False, f"held by {name or 'unknown'} (PID {pid or '?'}), not by the server")


def _chk_end_to_end(wp):
    """Translate a sentence THROUGH the Whisper server, exactly like subtitles do."""
    from yume.network import server_post

    r = server_post("127.0.0.1", wp, "/translation/test", {"text": "今日はいい天気ですね"}, timeout=150)
    if r.get("success"):
        return ("Translation end-to-end", True, f"今日はいい天気ですね → {r.get('translation', '')[:50]}")
    return ("Translation end-to-end", False, (r.get("error") or "failed")[:90])


def _chk_whisper_srv(wp):
    ws = check_server("127.0.0.1", wp, "/health")
    status = ws["data"].get("status")
    rows = [_port_row("Whisper", wp, bool(status), True)]
    if status:
        detail = f"status: {status}" + (f" — {ws['data'].get('error')}" if status == "error" else "")
        rows.append(("Whisper server responding", status in ("ready", "loading"), detail))
        if status == "ready":
            rows.append(_chk_end_to_end(wp))
    return rows


def _chk_translation_srv(c, tp):
    bk = c.get("translation_backend", "llamacpp")
    bi = backend_info(bk)
    ts = check_translation_server(c.get("translation_host", "127.0.0.1"), tp, bi)
    rows = [_port_row("Translation", tp, ts["up"], bk == "llamacpp")]
    if ts["up"]:
        busy = ts["data"].get("status") == "busy"
        rows.append(("Translation server responding", True, "busy (generating)" if busy else "OK"))
    return rows


def _chk_server_script():
    ss = SERVER_DIR / "faster_whisper_server.py"
    if not ss.exists():
        ss = BASE_DIR / "faster_whisper_server.py"
    return ("Whisper server script", ss.exists(), str(ss) if ss.exists() else "MISSING")


def _as_rows(result) -> list:
    if result is None:
        return []
    return result if isinstance(result, list) else [result]


def _run_checks(cfg: dict) -> list:
    """Run every check in parallel; rows come back in display order."""
    from config import DEFAULT_TRANSLATION_PORT, DEFAULT_WHISPER_PORT, load_config

    from yume import llama_server, vocals

    c = load_config()
    wp = c.get("whisper_port", DEFAULT_WHISPER_PORT)
    tp = c.get("translation_port", DEFAULT_TRANSLATION_PORT)
    uses_llamacpp = cfg.get("translation_backend", "llamacpp") == "llamacpp"
    native = llama_server.server_path() is not None  # llama-server replaces llama-cpp-python

    checks = [
        (_chk_system,),
        (_chk_tool, "yt-dlp"),
        (_chk_tool, "ffmpeg"),
        (_chk_tool, "ffprobe"),
        (_chk_deno, cfg),
        (_chk_pip,),
        (_chk_pkg, "faster_whisper", "faster-whisper"),
        (_chk_pkg, "flask", "Flask"),
        (_chk_requirements,),
    ]
    if uses_llamacpp and not native:
        checks.append((_chk_pkg, "llama_cpp", "llama-cpp-python"))
    if uses_llamacpp:
        checks.append((_chk_llama_cuda,))
    roma = [
        (_chk_cutlet,),
        (_chk_roma, "pykakasi", "Japanese romaji fallback"),
        (_chk_roma, "pypinyin", "Chinese pinyin"),
    ]
    checks += [*roma, (vocals.health_row, cfg), (_chk_translation_srv, c, tp), (_chk_whisper_srv, wp)]

    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as ex:
        futures = [ex.submit(fn, *args) for fn, *args in checks]
        results = [row for f in futures for row in _as_rows(f.result())]

    roma_names = {"Japanese by word", "pykakasi", "pypinyin"}
    if not any(name in roma_names and "not installed" not in detail for name, _ok, detail in results):
        info(f"\n  {C.DIM}Tip: Install romanization libraries for instant romaji/pinyin:{C.RESET}")
        info(f"  {C.CYAN}pip install pykakasi pypinyin{C.RESET}")
        info(f"  {C.DIM}Without them, Yume uses the LLM for romanization (slower).{C.RESET}\n")

    # The config rows go between vocal isolation and the servers
    at = next(i for i, r in enumerate(results) if r[0].startswith("Translation port"))
    results[at:at] = [
        ("Config loadable", bool(c), "OK" if c else "corrupted"),
        ("Ports different", wp != tp, f"whisper:{wp} translation:{tp}"),
    ]
    results.append(_chk_server_script())
    if cfg.get("translation_backend") == "llamacpp":
        gf = find_gguf_models()
        results.append(("GGUF model present", len(gf) > 0, gf[0].name if gf else "none in models/translation/"))
    return results


def tools_menu_path() -> str:
    return f"{C.CYAN}python pocket_yume.py{C.RESET} → Tools & Fonts"


def _advice_tool(name: str, detail: str) -> list[str]:
    url = "https://github.com/yt-dlp/yt-dlp/releases" if "yt-dlp" in name else "https://ffmpeg.org/download.html"
    return [f"Run: {C.CYAN}python pocket_yume.py setup{C.RESET}", f"  Or download manually: {C.CYAN}{url}{C.RESET}"]


def _advice_packages(name: str, detail: str) -> list[str]:
    return [
        detail,
        f"  Install: {C.CYAN}{sys.executable} -m pip install -r server/requirements.txt{C.RESET}",
        "  Or: Main Menu → Tools & Fonts → Python Dependencies",
    ]


def _advice_port(name: str, detail: str) -> list[str]:
    port = re.search(r"\d+", name)
    return [
        detail,
        f"  Free port {port.group() if port else '?'} or change it in: {C.CYAN}python pocket_yume.py{C.RESET}"
        " → Settings → Server addresses",
    ]


def _advice_config(name: str, detail: str) -> list[str]:
    from config import CONFIG_FILE

    return [f"Delete {C.CYAN}{CONFIG_FILE}{C.RESET} and re-run setup."]


# (does it apply to this check name?, advice lines; the first follows "<name> -> ")
_ADVICE = [
    (lambda n: "yt-dlp" in n or "ffmpeg" in n or "ffprobe" in n, _advice_tool),
    (lambda n: n == "Server packages", _advice_packages),
    (
        lambda n: n == "llama-cpp CUDA support",
        lambda n, d: [
            "Translation will run on the CPU (slow).",
            f"  Install the GPU translation engine: {tools_menu_path()} → Translation Engine",
        ],
    ),
    (lambda n: "faster-whisper" in n or "Flask" in n or "llama-cpp" in n,
     lambda n, d: [f"Run: {C.CYAN}python pocket_yume.py setup{C.RESET}"]),
    (lambda n: " port " in n, _advice_port),
    (
        lambda n: "end-to-end" in n,
        lambda n, d: [
            "The Whisper server could not translate through your LLM:",
            f"  {d}",
            "  Check Settings → Translation settings (backend, address, model name).",
        ],
    ),
    (
        lambda n: "Whisper server responding" in n,
        lambda n, d: [
            f"Restart: {C.CYAN}python pocket_yume.py launch{C.RESET}",
            f"  Or check logs: {C.CYAN}logs/whisper_server.log{C.RESET}",
        ],
    ),
    (lambda n: "Whisper server script" in n,
     lambda n, d: [f"Re-extract Yume or run: {C.CYAN}python pocket_yume.py setup{C.RESET}"]),
    (
        lambda n: n == "GGUF model present",
        lambda n, d: [
            "llama.cpp needs a translation model (a .gguf file).",
            f"  Download one: {tools_menu_path()} → Download Translation Model",
            "  Or put a .gguf file in models/translation/",
        ],
    ),
    (lambda n: "Config" in n, _advice_config),
    (
        lambda n: "RAM" in n,
        lambda n, d: [
            "Close other applications to free up memory.",
            f"  Or use a smaller Whisper model: {C.CYAN}python pocket_yume.py settings{C.RESET}",
        ],
    ),
    (lambda n: "Disk" in n, lambda n, d: ["Free up disk space. Yume needs ~5-10 GB for models and tools."]),
]  # fmt: skip


def _fix_advice(name: str, detail: str) -> list[str]:
    """How to fix one failed check (empty when there is nothing to suggest)."""
    for applies, advice in _ADVICE:
        if applies(name):
            first, *rest = advice(name, detail)
            return [f"{C.BOLD}{name}{C.RESET} -> {first}", *rest]
    return []


def health_check(cfg: dict) -> bool:
    """Comprehensive health check — all checks run in parallel for speed.
    True when every check passed."""
    header("Health Check")
    info(f"{C.DIM}Running checks in parallel...{C.RESET}")
    print()
    results = _run_checks(cfg)

    ok = sum(1 for _, p, _ in results if p)
    total = len(results)
    table(
        ["Check", "Result", "Details"],
        [
            [name, f"{C.GREEN}PASS{C.RESET}" if passed else f"{C.RED}FAIL{C.RESET}", detail]
            for name, passed, detail in results
        ],
        col_styles=[C.RESET, C.RESET, C.DIM],
        title=f"Health Check — {ok}/{total} passed",
    )
    print()
    if ok == total:
        success("All checks passed! Yume is ready to launch.")
    else:
        fails = [(name, detail) for name, passed, detail in results if not passed]
        warn(f"{total - ok} issue(s) found: {', '.join(name for name, _ in fails)}")
        print()
        for name, detail in fails:
            for line in _fix_advice(name, detail):
                info(line)
        print()

    # Quick perf estimate
    rec_model, rec_reason = recommend_whisper_model(detect_gpu())
    print()
    panel(
        f"Recommended model: {C.BOLD}{rec_model}{C.RESET}\n{C.DIM}{rec_reason}{C.RESET}",
        title="Performance Estimate",
        style=C.CYAN,
    )
    return ok == total


_FONT_EXTENSIONS = {".ttf", ".otf", ".ttc", ".woff", ".woff2"}

# script -> (file-name patterns, the free font to suggest). Patterns match the
# FILE name, which on Windows is not the font's name: Microsoft YaHei is
# msyh.ttc, Yu Gothic YuGothR.ttc, MS Gothic msgothic.ttc.
_SCRIPT_FONTS = {
    "Japanese": (
        ["NotoSansJP", "NotoSerifJP", "MPLUS", "YuGoth", "YuMin", "Meiryo", "MSGothic", "MSMincho", "Hiragino",
         "Kosugi", "ZenMaru"],
        "Noto Sans JP",
    ),
    "Chinese": (
        ["NotoSansSC", "NotoSansTC", "SourceHanSans", "msyh", "MicrosoftYaHei", "msjh", "SimHei", "SimSun", "MingLiU",
         "DengXian", "PingFang", "STHeiti"],
        "Noto Sans SC",
    ),
    "Korean": (["NotoSansKR", "Malgun", "NanumGothic", "AppleSD", "Batang", "Gulim"], "Noto Sans KR"),
    "Arabic": (
        ["NotoNaskhArabic", "NotoSansArabic", "Amiri", "Scheherazade", "trado", "arabtype", "majalla"],
        "Noto Naskh Arabic",
    ),
}  # fmt: skip
_GENERIC_FONTS = ["Arial", "Georgia", "Verdana", "Consolas", "Times New Roman", "Courier New"]


def _font_files() -> tuple[int, list[Path]]:
    """(number of font folders, font files in them) for this OS."""
    if IS_WIN:
        dirs = [Path(os.environ.get("WINDIR", "C:\\Windows")) / "Fonts"]
    elif IS_MAC:
        dirs = [Path("/Library/Fonts"), Path.home() / "Library/Fonts", Path("/System/Library/Fonts")]
    else:
        dirs = [Path("/usr/share/fonts"), Path.home() / ".local/share/fonts", Path("/usr/local/share/fonts")]
    files = [f for d in dirs if d.exists() for f in d.rglob("*") if f.suffix.lower() in _FONT_EXTENSIONS]
    return len(dirs), files


def _matching(files: list[Path], patterns: list[str]) -> list[str]:
    """Names of the font files whose name contains one of the patterns (case and spaces ignored)."""
    pats = [p.lower().replace(" ", "") for p in patterns]
    return sorted({f.name for f in files if any(p in f.stem.lower().replace(" ", "") for p in pats)}, key=str.lower)


def _show_bundled_fonts() -> None:
    ext_fonts = EXT_DIR / "fonts"
    if not ext_fonts.exists():
        warn(f"Extension fonts folder missing: {ext_fonts}")
        info("Create it and add .ttf/.otf files for custom subtitle fonts")
        return
    bundled = [f.name for f in ext_fonts.iterdir() if f.suffix.lower() in _FONT_EXTENSIONS]
    if not bundled:
        info(f"Extension fonts folder: empty (add .ttf/.otf files to {ext_fonts})")
        return
    section("Bundled Fonts (extension/fonts/)")
    for f in bundled:
        bullet(f)


_FONT_HELP = [
    "How to use fonts in Yume:",
    "  1. Yume automatically detects your system fonts",
    "  2. Pick a font in the extension popup (Settings > Appearance)",
    "",
    "To add a custom font that isn't installed system-wide:",
    "  1. Copy the .ttf or .otf file into the extension/fonts/ folder",
    "  2. Open extension/js/bundled-fonts.js and find the BUNDLED_FONTS list",
    "  3. Add your font: { file: 'MyFont.ttf', name: 'My Font' }",
    "  4. Reload the extension in chrome://extensions",
    "  The font will appear with '(bundled)' next to its name in the dropdown",
]


def detect_fonts() -> None:
    """Detect CJK and common fonts available on this system."""
    header("Font Detection")
    info("Scanning for subtitle-compatible fonts (Chinese, Japanese, Korean, Arabic)...")
    print()
    n_dirs, files = _font_files()
    info(f"Scanned {n_dirs} font directories, found {len(files)} font files")
    print()

    for script, (patterns, suggestion) in _SCRIPT_FONTS.items():
        matches = _matching(files, patterns)
        if not matches:
            warn(f"{script}: No dedicated fonts found")
            info(f"  Install: {suggestion} from Google Fonts")
            continue
        success(f"{script}: {len(matches)} font(s) found")
        for m in matches[:5]:
            bullet(m)
        if len(matches) > 5:
            info(f"  ... and {len(matches) - 5} more")
    print()

    generic = [g for g in _GENERIC_FONTS if _matching(files, [g])]
    info(f"Generic fonts: {', '.join(sorted(generic)) if generic else 'none detected'}")
    _show_bundled_fonts()
    print()
    for line in _FONT_HELP:
        info(line)
