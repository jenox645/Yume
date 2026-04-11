"""Health check, system status, and font detection."""

from __future__ import annotations

import os
import platform
import re
import sys
from pathlib import Path

from yume.hardware import detect_gpu, detect_ram_gb, disk_free_gb, recommend_whisper_model
from yume.network import check_server, check_translation_server
from yume.ports import get_port_process, is_port_free
from yume.ui import C, bullet, header, info, panel, pause, section, success, table, warn
from yume.utils import BASE_DIR, GiB, find_gguf_models, find_tool

_PLAT = platform.system()
_IS_WIN = _PLAT == "Windows"
_IS_MAC = _PLAT == "Darwin"
_ARCH = platform.machine().lower()

SERVER_DIR = BASE_DIR / "server"
EXT_DIR = BASE_DIR / "extension"

# Populated by pocket_yume at startup via set_backend_info()
_BI: dict = {}


def set_backend_info(bi: dict) -> None:
    """Inject the BACKEND_INFO dict from pocket_yume at startup."""
    global _BI
    _BI = bi


def health_check(cfg: dict) -> None:
    """Comprehensive health check — tests every component end-to-end."""
    from config import DEFAULT_TRANSLATION_PORT, DEFAULT_WHISPER_PORT, load_config
    from yume.utils import _run

    header("Health Check")
    results = []

    # 1. System resources
    gpu = detect_gpu()
    ram = detect_ram_gb()
    disk = disk_free_gb()
    results.append(("GPU detection", True, gpu["name"] or "CPU mode"))
    results.append(("RAM ≥ 4 GB", ram >= 4, f"{ram:.1f} GB"))
    results.append(("Disk ≥ 5 GB", disk >= 5, f"{disk:.1f} GB free"))

    # 2. Tools
    for t in ["yt-dlp", "ffmpeg", "ffprobe"]:
        p = find_tool(t)
        if p:
            r = _run([p, "--version" if t == "yt-dlp" else "-version"], timeout=5)
            if r.returncode == 0:
                results.append((f"{t} installed", True, p))
            else:
                results.append((f"{t} installed", False, f"found at {p} but fails to run"))
        else:
            results.append((f"{t} installed", False, "not found"))
    dn = find_tool("deno")
    if cfg.get("youtube_auth_method") != "cookies":
        results.append(("deno installed", dn is not None, dn or "not found (needed for YouTube)"))

    # 3. Python packages
    pip_r = _run([sys.executable, "-m", "pip", "--version"], timeout=10)
    results.append(
        (
            "pip available",
            pip_r.returncode == 0,
            pip_r.stdout.split()[1] if pip_r.returncode == 0 else "not found — install python3-pip",
        )
    )
    for pkg, name in [
        ("faster_whisper", "faster-whisper"),
        ("flask", "Flask"),
        ("flask_cors", "Flask-CORS"),
        ("llama_cpp", "llama-cpp-python"),
    ]:
        try:
            __import__(pkg)
            results.append((f"{name}", True, "installed"))
        except ImportError:
            results.append((f"{name}", False, "not installed"))

    # Romanization libraries (optional)
    roma_libs = [
        ("pykakasi", "Japanese romaji (kanji→reading)"),
        ("pypinyin", "Chinese pinyin"),
    ]
    roma_installed = 0
    for pkg, desc in roma_libs:
        try:
            __import__(pkg)
            results.append((f"{pkg}", True, f"installed — {desc}"))
            roma_installed += 1
        except ImportError:
            results.append((f"{pkg}", True, f"not installed (optional) — {desc}"))
    if roma_installed == 0:
        info(f"\n  {C.DIM}Tip: Install romanization libraries for instant romaji/pinyin:{C.RESET}")
        info(f"  {C.CYAN}pip install pykakasi pypinyin{C.RESET}")
        info(f"  {C.DIM}Without them, Yume uses the LLM for romanization (slower).{C.RESET}")
        info(f"  {C.DIM}Japanese kana→romaji works without libraries (WanaKana, bundled).{C.RESET}")
        info(f"  {C.DIM}Only kanji readings need pykakasi.{C.RESET}\n")

    # 4. Config validity
    c = load_config()
    results.append(("Config loadable", bool(c), "OK" if c else "corrupted"))
    wp = c.get("whisper_port", DEFAULT_WHISPER_PORT)
    tp = c.get("translation_port", DEFAULT_TRANSLATION_PORT)
    results.append(("Ports different", wp != tp, f"whisper:{wp} translation:{tp}"))

    # 5. Port availability
    results.append((f"Whisper port {wp} free", is_port_free(wp), "available" if is_port_free(wp) else "in use"))
    results.append((f"Translation port {tp} free", is_port_free(tp), "available" if is_port_free(tp) else "in use"))

    # 6. Live server connectivity
    if not is_port_free(wp):
        ws = check_server(c.get("whisper_host", "127.0.0.1"), wp, "/health")
        if ws["up"]:
            status = ws.get("data", {}).get("status", "unknown")
            results.append(("Whisper server responding", True, f"port {wp} — status: {status}"))
        else:
            results.append(
                (
                    "Whisper server responding",
                    False,
                    f"port {wp} in use but /health failed — restart with: python pocket_yume.py launch",
                )
            )
    if not is_port_free(tp):
        bi = _BI.get(c.get("translation_backend", "llamacpp"), _BI.get("custom", {"hp": "/health"}))
        ts = check_translation_server(c.get("translation_host", "127.0.0.1"), tp, bi)
        if ts["up"]:
            results.append(("Translation server responding", True, f"port {tp} — OK"))
        else:
            results.append(
                (
                    "Translation server responding",
                    False,
                    f"port {tp} in use but not responding — check if your LLM backend is running",
                )
            )

    # 7. Server files
    ss = SERVER_DIR / "faster_whisper_server.py"
    if not ss.exists():
        ss = BASE_DIR / "faster_whisper_server.py"
    results.append(("Whisper server script", ss.exists(), str(ss) if ss.exists() else "MISSING"))

    # 8. GGUF model (if llamacpp backend)
    if cfg.get("translation_backend") == "llamacpp":
        gf = find_gguf_models()
        results.append(("GGUF model present", len(gf) > 0, gf[0].name if gf else "none in models/translation/"))

    # Display results
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

    if ok == total:
        print()
        success("All checks passed! Yume is ready to launch.")
    else:
        print()
        fails = [name for name, p, _ in results if not p]
        warn(f"{total - ok} issue(s) found: {', '.join(fails)}")
        print()
        fail_details = {name: detail for name, passed, detail in results if not passed}
        from config import CONFIG_FILE

        for fname in fails:
            if "yt-dlp" in fname or "ffmpeg" in fname or "ffprobe" in fname:
                tool = fname.split(" ")[0]
                info(f"{C.BOLD}{fname}{C.RESET} -> Run: {C.CYAN}python pocket_yume.py setup{C.RESET}")
                info(
                    f"  Or download manually: {C.CYAN}https://github.com/yt-dlp/yt-dlp/releases{C.RESET}"
                    if "yt-dlp" in tool
                    else f"  Or download manually: {C.CYAN}https://ffmpeg.org/download.html{C.RESET}"
                )
            elif "faster-whisper" in fname or "Flask" in fname or "llama-cpp" in fname:
                info(f"{C.BOLD}{fname}{C.RESET} -> Run: {C.CYAN}python pocket_yume.py setup{C.RESET}")
            elif "port" in fname.lower() and "free" in fname.lower():
                port_num = re.search(r"\d+", fname)
                pn = port_num.group() if port_num else "?"
                info(f"{C.BOLD}{fname}{C.RESET} -> Port {pn} in use. Another Yume instance may be running.")
                info(f"  Run {C.CYAN}python pocket_yume.py launch{C.RESET} to auto-detect a free port.")
            elif "Whisper server responding" in fname:
                info(f"{C.BOLD}{fname}{C.RESET} -> Restart: {C.CYAN}python pocket_yume.py launch{C.RESET}")
                info(f"  Or check logs: {C.CYAN}logs/whisper_server.log{C.RESET}")
            elif "Translation server responding" in fname:
                info(f"{C.BOLD}{fname}{C.RESET} -> Make sure your LLM backend is running:")
                bk = c.get("translation_backend", "llamacpp")
                if bk == "llamacpp":
                    info(f"  Restart everything: {C.CYAN}python pocket_yume.py launch{C.RESET}")
                elif bk == "ollama":
                    info(f"  Start Ollama: {C.CYAN}ollama serve{C.RESET}")
                else:
                    info(f"  Start your {bk} server, then re-run health check.")
            elif "Whisper server script" in fname:
                info(
                    f"{C.BOLD}{fname}{C.RESET} -> Re-extract Yume or run: {C.CYAN}python pocket_yume.py setup{C.RESET}"
                )
            elif "Config" in fname:
                info(f"{C.BOLD}{fname}{C.RESET} -> Delete {C.CYAN}{CONFIG_FILE}{C.RESET} and re-run setup.")
            elif "RAM" in fname:
                info(f"{C.BOLD}{fname}{C.RESET} -> Close other applications to free up memory.")
                info(f"  Or use a smaller Whisper model: {C.CYAN}python pocket_yume.py settings{C.RESET}")
            elif "Disk" in fname:
                info(f"{C.BOLD}{fname}{C.RESET} -> Free up disk space. Yume needs ~5-10 GB for models and tools.")
        print()
        _ = fail_details  # referenced above for potential future use

    # Quick perf estimate
    gpu = detect_gpu()
    rec_model, rec_reason = recommend_whisper_model(gpu)
    print()
    panel(
        f"Recommended model: {C.BOLD}{rec_model}{C.RESET}\n{C.DIM}{rec_reason}{C.RESET}",
        title="Performance Estimate",
        style=C.CYAN,
    )
    pause()


def show_status(cfg: dict) -> None:
    """Display system status overview."""
    from config import DEFAULT_TRANSLATION_PORT
    from yume.network import HEALTH_PATH_OPENAI
    from yume.benchmark import _is_whisper_model_cached

    header("System Status")
    gpu = detect_gpu()
    if gpu["has_nvidia"]:
        gpu_txt = f"{C.GREEN}{gpu['name']} ({gpu['vram_mb']} MB){C.RESET}"
    elif gpu["has_amd"]:
        vr = f" ({gpu['vram_mb']} MB)" if gpu["vram_mb"] else ""
        gpu_txt = f"{C.CYAN}{gpu['name']}{vr}{C.RESET}"
    else:
        gpu_txt = f"{C.YELLOW}None (CPU mode){C.RESET}"
    ram = detect_ram_gb()
    disk = disk_free_gb()
    table(
        ["Component", "Status"],
        [
            ["GPU", gpu_txt],
            ["RAM", f"{ram:.1f} GB" + (f"  {C.GREEN}OK{C.RESET}" if ram >= 8 else f"  {C.YELLOW}low{C.RESET}")],
            ["Disk", f"{disk:.1f} GB free" + (f"  {C.GREEN}OK{C.RESET}" if disk >= 10 else f"  {C.RED}low!{C.RESET}")],
            ["OS", f"{_PLAT} ({_ARCH})"],
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
        ("flask_cors", "Flask-CORS"),
        ("llama_cpp", "llama-cpp-python"),
    ]:
        try:
            __import__(pkg)
            pkg_rows.append([display, f"{C.GREEN}✓{C.RESET}"])
        except ImportError:
            pkg_rows.append([display, f"{C.RED}✗  not installed{C.RESET}"])
    table(["Package", "Status"], pkg_rows, col_styles=[C.CYAN, C.RESET], title="Python Packages")

    bk = cfg.get("translation_backend", "llamacpp")
    bi = _BI.get(bk, _BI.get("custom", {"hp": HEALTH_PATH_OPENAI}))
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
    if _IS_WIN:
        winfonts = Path(os.environ.get("WINDIR", "C:\\Windows")) / "Fonts"
        font_dirs = [winfonts]
    elif _IS_MAC:
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
            "NotoSansJP", "NotoSerifJP", "Noto Sans JP", "MPLUS", "M PLUS",
            "Yu Gothic", "Meiryo", "MS Gothic", "HiraginoSans", "Hiragino",
            "Kosugi", "ZenMaru",
        ],
        "Chinese": [
            "NotoSansSC", "NotoSansTC", "Noto Sans SC", "Noto Sans TC",
            "SimHei", "Microsoft YaHei", "PingFang", "STHeiti", "Source Han Sans",
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
    info("  2. Open extension/popup.js, find the BUNDLED_FONTS list near the top")
    info("  3. Add your font: { file: 'MyFont.ttf', name: 'My Font' }")
    info("  4. Reload the extension in chrome://extensions")
    info("  The font will appear with '(bundled)' next to its name in the dropdown")
    pause()
