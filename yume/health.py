"""Health check, system status, and font detection."""

from __future__ import annotations

import concurrent.futures
import os
import re
import sys
import time
from pathlib import Path

from yume.constants import (
    BASE_DIR,
    SERVER_DIR,
    EXT_DIR,
    PLAT,
    IS_WIN,
    IS_MAC,
    ARCH,
    GiB,
    HEALTH_PATH_OPENAI,
    backend_info,
)
from yume.hardware import detect_gpu, detect_ram_gb, disk_free_gb, recommend_whisper_model
from yume.network import check_server, check_translation_server
from yume.ports import get_port_process, is_port_free
from yume.ui import C, bullet, header, info, panel, pause, section, success, table, warn
from yume.utils import find_gguf_models, find_tool


# 30-second hardware info cache — avoids re-calling nvidia-smi on every status render
_hw_cache: dict | None = None
_hw_cache_ts: float = 0.0
_HW_CACHE_TTL = 30.0


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


def _fix_advice(name: str, detail: str) -> list[str]:
    """How to fix one failed check (empty when there is nothing to suggest)."""
    from config import CONFIG_FILE

    head = f"{C.BOLD}{name}{C.RESET} -> "
    run_setup = f"Run: {C.CYAN}python pocket_yume.py setup{C.RESET}"
    if "yt-dlp" in name or "ffmpeg" in name or "ffprobe" in name:
        url = "https://github.com/yt-dlp/yt-dlp/releases" if "yt-dlp" in name else "https://ffmpeg.org/download.html"
        return [head + run_setup, f"  Or download manually: {C.CYAN}{url}{C.RESET}"]
    if name == "Server packages":
        return [
            head + detail,
            f"  Install: {C.CYAN}{sys.executable} -m pip install -r server/requirements.txt{C.RESET}",
            "  Or: Main Menu → Tools & Fonts → Python Dependencies",
        ]
    if "faster-whisper" in name or "Flask" in name or "llama-cpp" in name:
        return [head + run_setup]
    if " port " in name:
        port = re.search(r"\d+", name)
        return [
            head + detail,
            f"  Free port {port.group() if port else '?'} or change it in: {C.CYAN}python pocket_yume.py{C.RESET}"
            " → Settings → Server addresses",
        ]
    if "end-to-end" in name:
        return [
            head + "The Whisper server could not translate through your LLM:",
            f"  {detail}",
            "  Check Settings → Translation settings (backend, address, model name).",
        ]
    if "Whisper server responding" in name:
        return [
            head + f"Restart: {C.CYAN}python pocket_yume.py launch{C.RESET}",
            f"  Or check logs: {C.CYAN}logs/whisper_server.log{C.RESET}",
        ]
    if "Whisper server script" in name:
        return [head + f"Re-extract Yume or run: {C.CYAN}python pocket_yume.py setup{C.RESET}"]
    if "Config" in name:
        return [head + f"Delete {C.CYAN}{CONFIG_FILE}{C.RESET} and re-run setup."]
    if "RAM" in name:
        return [
            head + "Close other applications to free up memory.",
            f"  Or use a smaller Whisper model: {C.CYAN}python pocket_yume.py settings{C.RESET}",
        ]
    if "Disk" in name:
        return [head + "Free up disk space. Yume needs ~5-10 GB for models and tools."]
    return []


def health_check(cfg: dict) -> None:
    """Comprehensive health check — all checks run in parallel for speed."""
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
    pause()


def show_status(cfg: dict) -> None:
    """Display system status overview."""
    global _hw_cache, _hw_cache_ts

    from config import DEFAULT_TRANSLATION_PORT
    from yume.benchmark import _is_whisper_model_cached

    header("System Status")

    # Use cached hardware info (30 s TTL) to avoid slow re-detection on every render
    now = time.monotonic()
    if _hw_cache is None or (now - _hw_cache_ts) > _HW_CACHE_TTL:
        _hw_cache = {
            "gpu": detect_gpu(),
            "ram": detect_ram_gb(),
            "disk": disk_free_gb(),
        }
        _hw_cache_ts = now
    gpu = _hw_cache["gpu"]
    ram = _hw_cache["ram"]
    disk = _hw_cache["disk"]

    if gpu["has_nvidia"]:
        gpu_txt = f"{C.GREEN}{gpu['name']} ({gpu['vram_mb']} MB){C.RESET}"
    elif gpu["has_amd"]:
        vr = f" ({gpu['vram_mb']} MB)" if gpu["vram_mb"] else ""
        gpu_txt = f"{C.CYAN}{gpu['name']}{vr}{C.RESET}"
    else:
        gpu_txt = f"{C.YELLOW}None (CPU mode){C.RESET}"
    table(
        ["Component", "Status"],
        [
            ["GPU", gpu_txt],
            ["RAM", f"{ram:.1f} GB" + (f"  {C.GREEN}OK{C.RESET}" if ram >= 8 else f"  {C.YELLOW}low{C.RESET}")],
            ["Disk", f"{disk:.1f} GB free" + (f"  {C.GREEN}OK{C.RESET}" if disk >= 10 else f"  {C.RED}low!{C.RESET}")],
            ["OS", f"{PLAT} ({ARCH})"],
        ],
        col_styles=[C.BOLD, C.RESET],
        title="Hardware",
    )

    tool_rows = []
    for t in ["yt-dlp", "ffmpeg", "deno"]:
        p = find_tool(t)
        if p:
            tool_rows.append([t, f"{C.GREEN}installed{C.RESET}", p])
        elif t == "deno" and cfg.get("youtube_auth_method") == "cookies":
            tool_rows.append([t, f"{C.DIM}skipped{C.RESET}", "using cookies"])
        else:
            tool_rows.append([t, f"{C.RED}MISSING{C.RESET}", "run Tools menu to install"])
    table(["Tool", "Status", "Path"], tool_rows, col_styles=[C.CYAN, C.RESET, C.DIM], title="Tools")

    pkg_rows = []
    for pkg, display in [
        ("faster_whisper", "faster-whisper"),
        ("flask", "Flask"),
        ("waitress", "Waitress"),
        ("llama_cpp", "llama-cpp-python"),
    ]:
        try:
            __import__(pkg)
            pkg_rows.append([display, f"{C.GREEN}✓{C.RESET}"])
        except ImportError:
            pkg_rows.append([display, f"{C.RED}✗  not installed{C.RESET}"])
    table(["Package", "Status"], pkg_rows, col_styles=[C.CYAN, C.RESET], title="Python Packages")

    bk = cfg.get("translation_backend", "llamacpp")
    bi = backend_info(bk)
    ws = check_server(cfg["whisper_host"], cfg["whisper_port"], "/health")
    ts = check_server(cfg["translation_host"], cfg["translation_port"], bi["hp"])
    if not ts["up"]:
        ts = check_server(cfg["translation_host"], cfg["translation_port"], HEALTH_PATH_OPENAI)
    ws_s = f"{C.GREEN}● running{C.RESET}" if ws["up"] else f"{C.DIM}○ stopped{C.RESET}"
    ts_s = f"{C.GREEN}● running{C.RESET}" if ts["up"] else f"{C.DIM}○ stopped{C.RESET}"
    table(
        ["Server", "Address", "Status"],
        [
            ["Whisper", f"{cfg['whisper_host']}:{cfg['whisper_port']}", ws_s],
            [bi["name"], f"{cfg['translation_host']}:{cfg['translation_port']}", ts_s],
        ],
        col_styles=[C.CYAN, C.DIM, C.RESET],
        title="Servers",
    )

    gf = find_gguf_models()

    # Models
    section("Models")
    wm = cfg.get("whisper_model", "large-v3")
    wm_path = Path(wm)
    if wm_path.is_dir():
        info(f"Whisper:      {C.BOLD}{wm_path.name}{C.RESET}  {C.DIM}(custom: {wm}){C.RESET}")
    else:
        info(f"Whisper:      {C.BOLD}{wm}{C.RESET}")
        hf_cache = Path.home() / ".cache" / "huggingface" / "hub"
        cached = _is_whisper_model_cached(wm)
        if cached:
            info(f"              {C.GREEN}downloaded{C.RESET}  {C.DIM}{hf_cache}{C.RESET}")
        else:
            info(f"              {C.YELLOW}not downloaded{C.RESET}  (will download on first use)")
            info(f"              {C.DIM}Cache: {hf_cache}{C.RESET}")

    gp = cfg.get("gguf_model_path", "")
    if gp and Path(gp).exists():
        info(f"Translation:  {C.BOLD}{Path(gp).name}{C.RESET}  {C.DIM}({gp}){C.RESET}")
    elif gf:
        info(f"Translation:  {len(gf)} GGUF file(s) in models/translation/")
        for f in gf:
            bullet(f"{f.name} ({f.stat().st_size / GiB:.2f} GB)")
    else:
        info(f"Translation:  {C.DIM}no GGUF models found{C.RESET}")

    section("Ports")
    for label, key in [("Whisper", "whisper"), ("Translation", "translation")]:
        host = cfg.get(f"{key}_host", "127.0.0.1")
        port = cfg.get(f"{key}_port", DEFAULT_TRANSLATION_PORT)
        free = is_port_free(port, host)
        if free:
            info(f"{label:12s} {host}:{port}  {C.GREEN}available{C.RESET}")
        else:
            pid, name = get_port_process(port)
            who = f"{name} (PID {pid})" if pid else "unknown process"
            warn(f"{label:12s} {host}:{port}  {C.RED}in use{C.RESET} by {who}")
    pause()


def detect_fonts() -> None:
    """Detect CJK and common fonts available on this system."""
    header("Font Detection")
    info("Scanning for subtitle-compatible fonts (Chinese, Japanese, Korean, Arabic)...")
    print()

    font_dirs = []
    if IS_WIN:
        winfonts = Path(os.environ.get("WINDIR", "C:\\Windows")) / "Fonts"
        font_dirs = [winfonts]
    elif IS_MAC:
        font_dirs = [Path("/Library/Fonts"), Path.home() / "Library/Fonts", Path("/System/Library/Fonts")]
    else:
        font_dirs = [Path("/usr/share/fonts"), Path.home() / ".local/share/fonts", Path("/usr/local/share/fonts")]

    extensions = {".ttf", ".otf", ".ttc", ".woff", ".woff2"}
    found_files = []
    for d in font_dirs:
        if d.exists():
            for f in d.rglob("*"):
                if f.suffix.lower() in extensions:
                    found_files.append(f)

    info(f"Scanned {len(font_dirs)} font directories, found {len(found_files)} font files")
    print()

    cjk_targets = {
        "Japanese": [
            "NotoSansJP",
            "NotoSerifJP",
            "Noto Sans JP",
            "MPLUS",
            "M PLUS",
            "Yu Gothic",
            "Meiryo",
            "MS Gothic",
            "HiraginoSans",
            "Hiragino",
            "Kosugi",
            "ZenMaru",
        ],
        "Chinese": [
            "NotoSansSC",
            "NotoSansTC",
            "Noto Sans SC",
            "Noto Sans TC",
            "SimHei",
            "Microsoft YaHei",
            "PingFang",
            "STHeiti",
            "Source Han Sans",
        ],
        "Korean": ["NotoSansKR", "Noto Sans KR", "Malgun", "NanumGothic", "AppleSD"],
        "Arabic": ["NotoNaskhArabic", "Noto Naskh Arabic", "Amiri", "Scheherazade"],
    }

    for lang, patterns in cjk_targets.items():
        matches = []
        for f in found_files:
            fname = f.stem
            for pat in patterns:
                if pat.lower().replace(" ", "") in fname.lower().replace(" ", ""):
                    matches.append(f.name)
                    break
        if matches:
            success(f"{lang}: {len(matches)} font(s) found")
            for m in sorted(set(matches))[:5]:
                bullet(m)
            if len(set(matches)) > 5:
                info(f"  ... and {len(set(matches)) - 5} more")
        else:
            warn(f"{lang}: No dedicated fonts found")
            info(f"  Install: Noto Sans {lang[:2].upper()} from Google Fonts")
    print()

    generic = ["Arial", "Georgia", "Verdana", "Consolas", "Times New Roman", "Courier New"]
    found_generic = []
    for f in found_files:
        for g in generic:
            if g.lower().replace(" ", "") in f.stem.lower().replace(" ", ""):
                found_generic.append(g)
    found_generic = sorted(set(found_generic))
    info(f"Generic fonts: {', '.join(found_generic) if found_generic else 'none detected'}")

    ext_fonts = EXT_DIR / "fonts"
    if ext_fonts.exists():
        bundled = [f.name for f in ext_fonts.iterdir() if f.suffix.lower() in extensions]
        if bundled:
            section("Bundled Fonts (extension/fonts/)")
            for f in bundled:
                bullet(f)
        else:
            info(f"Extension fonts folder: empty (add .ttf/.otf files to {ext_fonts})")
    else:
        warn(f"Extension fonts folder missing: {ext_fonts}")
        info("Create it and add .ttf/.otf files for custom subtitle fonts")

    print()
    info("How to use fonts in Yume:")
    info("  1. Yume automatically detects your system fonts")
    info("  2. Pick a font in the extension popup (Settings > Appearance)")
    info("")
    info("To add a custom font that isn't installed system-wide:")
    info("  1. Copy the .ttf or .otf file into the extension/fonts/ folder")
    info("  2. Open extension/js/bundled-fonts.js and find the BUNDLED_FONTS list")
    info("  3. Add your font: { file: 'MyFont.ttf', name: 'My Font' }")
    info("  4. Reload the extension in chrome://extensions")
    info("  The font will appear with '(bundled)' next to its name in the dropdown")
    pause()
