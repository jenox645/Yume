"""Interactive menus — tools, settings, server CLI commands, HuggingFace browser."""

from __future__ import annotations

import json
import logging
import os
import sys
import urllib.request
from pathlib import Path

from yume.hardware import detect_gpu, recommend_whisper_model
from yume.network import (
    check_ollama_models,
    check_translation_server,
    hf_download,
    hf_list_gguf,
)
from yume.network import server_get as _server_get, server_post as _server_post
from yume.ports import find_free_port, get_port_process, is_port_free
from yume.ui import (
    C,
    ask_choice,
    ask_input,
    ask_yn,
    bullet,
    error,
    header,
    info,
    pause,
    section,
    success,
    table,
    warn,
)
from yume.utils import BASE_DIR, EXT_DIR, GGUF_DIR, TOOLS_DIR, GiB, KiB, find_gguf_models, find_tool

_log = logging.getLogger("pocket_yume")

# Populated by pocket_yume at startup via set_backend_info()
_BI: dict = {}


def set_backend_info(bi: dict) -> None:
    """Inject the BACKEND_INFO dict from pocket_yume at startup."""
    global _BI
    _BI = bi


# ── CLI server interaction ─────────────────────────────────────────────────────


def cli_server_stats(cfg: dict) -> None:
    """Print server stats."""
    h, p = cfg["whisper_host"], cfg["whisper_port"]
    data = _server_get(h, p, "/stats")
    if not data:
        error(f"Whisper server not reachable at {h}:{p}")
        return

    header("Server Statistics")
    gpu = data.get("gpu")
    if gpu:
        pct = round(gpu["vram_used_mb"] / gpu["vram_total_mb"] * 100) if gpu["vram_total_mb"] else 0
        bar_w = 30
        filled = round(bar_w * pct / 100)
        bar = f"[{'#' * filled}{'-' * (bar_w - filled)}] {pct}%"
        success(f"GPU: {gpu['gpu_name']}")
        info(f"  VRAM: {gpu['vram_used_mb']}/{gpu['vram_total_mb']} MB  {bar}")
        info(f"  Util: {gpu['gpu_util_pct']}%  |  Temp: {gpu['gpu_temp_c']}C")
    else:
        info("GPU: N/A (CPU mode or nvidia-smi unavailable)")
    print()
    section("Whisper Engine")
    info(
        f"Model: {C.BOLD}{data.get('model', '?')}{C.RESET}  "
        f"({data.get('device', '?')}/{data.get('compute_type', '?')})"
    )
    info(f"Uptime: {data.get('uptime_human', '?')}")
    section("Session")
    info(f"Chunks transcribed:      {data.get('chunks_transcribed', 0)}")
    info(f"Segments produced:       {data.get('segments_produced', 0)}")
    info(f"Hallucinations blocked:  {data.get('hallucinations_filtered', 0)}")
    info(f"Audio processed:         {data.get('total_audio_seconds', 0):.0f}s")
    info(f"Cache hits:              {data.get('cache_hits', 0)}")
    info(f"Avg time/chunk:          {data.get('avg_whisper_time', 0)}s")
    info(
        f"Last chunk:              {data.get('last_chunk_whisper_time', 0)}s "
        f"({data.get('last_chunk_segments', 0)} segs)"
    )
    info(f"Subtitle cache:          {data.get('subtitle_cache_size', 0)} chunks")
    info(f"Blacklist size:          {data.get('blacklist_size', 0)} items")


def cli_blacklist(cfg: dict, args: list) -> None:
    """CLI blacklist management."""
    h, p = cfg["whisper_host"], cfg["whisper_port"]
    if len(args) < 1:
        _menu_blacklist(cfg)
        return
    subcmd = args[0].lower()
    if subcmd == "list":
        data = _server_get(h, p, "/blacklist")
        if not data:
            error(f"Server not reachable at {h}:{p}")
            return
        bl = data.get("blacklist", [])
        if not bl:
            info("Blacklist is empty")
            return
        info(f"Server blacklist ({len(bl)} items):")
        for item in bl:
            bullet(item)
    elif subcmd == "add" and len(args) > 1:
        text = " ".join(args[1:])
        data = _server_get(h, p, "/blacklist")
        if not data:
            error("Server not reachable")
            return
        current = data.get("blacklist", [])
        if text in current:
            warn(f"Already blocked: {text}")
            return
        current.append(text)
        result = _server_post(h, p, "/blacklist/update", {"blacklist": current})
        if result and result.get("success"):
            success(f"Added: {text}")
        else:
            error(f"Failed: {result}")
    elif subcmd in ("remove", "rm") and len(args) > 1:
        text = " ".join(args[1:])
        data = _server_get(h, p, "/blacklist")
        if not data:
            error("Server not reachable")
            return
        current = data.get("blacklist", [])
        if text not in current:
            warn(f"Not in blacklist: {text}")
            return
        current.remove(text)
        result = _server_post(h, p, "/blacklist/update", {"blacklist": current})
        if result and result.get("success"):
            success(f"Removed: {text}")
        else:
            error(f"Failed: {result}")
    elif subcmd == "clear":
        result = _server_post(h, p, "/blacklist/update", {"blacklist": []})
        if result and result.get("success"):
            success("Blacklist cleared")
        else:
            error(f"Failed: {result}")
    else:
        print("  Usage: pocket_yume.py blacklist [list|add <text>|remove <text>|clear]")


def cli_model(cfg: dict, args: list) -> None:
    """CLI model management."""
    from config import save_config

    h, p = cfg["whisper_host"], cfg["whisper_port"]
    if len(args) < 1:
        data = _server_get(h, p, "/stats")
        if not data:
            error(f"Server not reachable at {h}:{p}")
            info(f"Config model: {cfg.get('whisper_model', '?')}")
            return
        info(f"Active model: {C.BOLD}{data.get('model', '?')}{C.RESET}")
        info(f"Device: {data.get('device', '?')}  |  Compute: {data.get('compute_type', '?')}")
        if data.get("gpu"):
            g = data["gpu"]
            info(f"GPU: {g.get('gpu_name', '?')} ({g.get('vram_used_mb', '?')}/{g.get('vram_total_mb', '?')} MB)")
        return
    subcmd = args[0].lower()
    if subcmd == "switch" and len(args) > 1:
        new_model = args[1]
        info(f"Switching to {new_model}...")
        result = _server_post(h, p, "/model/switch", {"model": new_model}, timeout=120)
        if not result:
            error(f"Server not reachable at {h}:{p}")
            return
        if result.get("error"):
            error(result["error"])
            if result.get("valid"):
                info(f"Valid: {', '.join(result['valid'])}")
        elif result.get("status") == "already_loaded":
            info(f"Already using {new_model}")
        else:
            success(f"Switched to {result.get('model', new_model)}")
            cfg["whisper_model"] = result.get("model", new_model)
            save_config(cfg)
    elif subcmd == "list":
        models_info = [
            ("tiny", "~1 GB"), ("base", "~1 GB"), ("small", "~2 GB"), ("medium", "~5 GB"),
            ("large-v2", "~10 GB"), ("large-v3", "~10 GB"), ("large-v3-turbo", "~6 GB"),
            ("distil-large-v2", "~4 GB"), ("distil-large-v3", "~4 GB"),
        ]
        cur = cfg.get("whisper_model", "?")
        for name, vram in models_info:
            cur_marker = f" {C.GOLD}<- current{C.RESET}" if name == cur else ""
            info(f"  {name:22s} {vram}{cur_marker}")
    else:
        print("  Usage: pocket_yume.py model [switch <name>|list]")


# ── Blacklist / whisper model menus ───────────────────────────────────────────


def _menu_blacklist(cfg: dict) -> None:
    """Blacklist management menu."""
    while True:
        header("Subtitle Filter (Blacklist)")
        info("Whisper sometimes generates fake text that wasn't actually spoken —")
        info("things like 'Thank you for watching' or 'Subscribe'. These are called")
        info("'hallucinations'. Yume blocks known patterns automatically, but you can")
        info("add your own phrases to block here.")
        print()
        h, p = cfg["whisper_host"], cfg["whisper_port"]
        data = _server_get(h, p, "/blacklist")
        if not data:
            warn(f"Server not reachable at {h}:{p}")
            info("Start server first")
            pause()
            return
        bl = data.get("blacklist", [])
        info(f"Server blacklist: {len(bl)} items")
        if bl:
            for item in bl[:15]:
                bullet(item)
            if len(bl) > 15:
                info(f"  ... and {len(bl) - 15} more")
        ch = ask_choice(
            "Options:",
            [
                ("Add entry", "Block a phrase from subtitles"),
                ("Remove entry", "Unblock a phrase"),
                ("Clear all", "Remove all entries"),
                ("Back", None),
            ],
            default=3,
        )
        if ch == -1 or ch == 3:
            return
        elif ch == 0:
            text = ask_input("Phrase to block", "")
            if text:
                current = bl[:]
                if text in current:
                    warn("Already blocked")
                    pause()
                    continue
                current.append(text)
                r = _server_post(h, p, "/blacklist/update", {"blacklist": current})
                if r and r.get("success"):
                    success(f"Added: {text}")
                else:
                    error("Failed")
            pause()
        elif ch == 1:
            if not bl:
                info("Empty")
                pause()
                continue
            opts = [(item, None) for item in bl[:20]] + [("Back", None)]
            rc = ask_choice("Remove which?", opts, default=len(opts) - 1)
            if 0 <= rc < len(bl):
                removed = bl[rc]
                current = bl[:]
                current.pop(rc)
                r = _server_post(h, p, "/blacklist/update", {"blacklist": current})
                if r and r.get("success"):
                    success(f"Removed: {removed}")
            pause()
        elif ch == 2:
            if ask_yn("Clear ALL?", False):
                r = _server_post(h, p, "/blacklist/update", {"blacklist": []})
                if r and r.get("success"):
                    success("Cleared")
            pause()


def _menu_whisper_model(cfg: dict) -> None:
    """Interactive whisper model hot-swap."""
    from config import save_config
    from yume.benchmark import _is_whisper_model_cached

    header("Whisper Model")
    h, p = cfg["whisper_host"], cfg["whisper_port"]
    data = _server_get(h, p, "/stats")
    cur = data.get("model", cfg.get("whisper_model", "?")) if data else cfg.get("whisper_model", "large-v3")
    is_custom = os.path.sep in cur or "/" in cur
    friendly_name = cfg.get("whisper_model_name", "")
    if data:
        display = friendly_name or (Path(cur).name if is_custom else cur)
        info(f"Active: {C.BOLD}{display}{C.RESET}  ({data.get('device', '?')})")
        if is_custom:
            info(f"{C.DIM}Path: {cur}{C.RESET}")
        if data.get("gpu"):
            g = data["gpu"]
            info(f"GPU: {g['gpu_name']} ({g['vram_used_mb']}/{g['vram_total_mb']} MB)")
    else:
        display = friendly_name or (Path(cur).name if is_custom else cur)
        warn(f"Server not running. Config: {display}")
    models = [
        "tiny", "base", "small", "medium", "large-v2", "large-v3",
        "large-v3-turbo", "distil-large-v2", "distil-large-v3",
    ]
    vram = {
        "tiny": "~1GB", "base": "~1GB", "small": "~2GB", "medium": "~5GB",
        "large-v2": "~10GB", "large-v3": "~10GB", "large-v3-turbo": "~4GB",
        "distil-large-v2": "~4GB", "distil-large-v3": "~4GB",
    }
    opts = []
    for m in models:
        cached = _is_whisper_model_cached(m)
        tag = f" {C.GREEN}[downloaded]{C.RESET}" if cached else f" {C.DIM}[not downloaded]{C.RESET}"
        if m == cur:
            opts.append((f"{m} ({vram.get(m, '?')}){tag}", "active"))
        else:
            opts.append((f"{m} ({vram.get(m, '?')}){tag}", None))
    custom_tag = f" {C.GREEN}[active]{C.RESET}" if is_custom else ""
    opts.append((f"Custom model (local path){custom_tag}", "active" if is_custom else None))
    opts.append(("Back", None))
    di = models.index(cur) if cur in models else (len(models) if is_custom else len(models) + 1)
    ch = ask_choice("Switch to:", opts, default=di)
    if ch == -1 or ch == len(models) + 1:
        return

    if ch == len(models):
        print()
        info(f"{C.DIM}Paste the path to a CTranslate2 Whisper model directory.{C.RESET}")
        info(f"{C.DIM}The folder must contain: model.bin, config.json, tokenizer.json, vocabulary.txt{C.RESET}")
        info(f"{C.DIM}See README > Whisper Models > Using a custom or fine-tuned model for details.{C.RESET}")
        print()
        custom_path = input(f"  {C.CYAN}>{C.RESET} Path: ").strip().strip('"').strip("'")
        if not custom_path:
            info("Cancelled.")
            pause()
            return
        custom_path = str(Path(custom_path).resolve())
        if not Path(custom_path).is_dir():
            error(f"Directory not found: {custom_path}")
            pause()
            return
        required = ["model.bin", "config.json"]
        missing = [f for f in required if not (Path(custom_path) / f).exists()]
        if missing:
            error(f"Not a valid CTranslate2 model — missing: {', '.join(missing)}")
            info(f"{C.DIM}Convert your model first:{C.RESET}")
            info(f"{C.DIM}  ct2-openai-whisper-converter --model <checkpoint> --output_dir <output>{C.RESET}")
            info(
                f"{C.DIM}  ct2-transformers-converter --model <hf-model> --output_dir <output> --quantization float16{C.RESET}"
            )
            pause()
            return
        new_model = custom_path
        dir_name = Path(custom_path).name
        print()
        info("Give this model a friendly name (shown in menus and popup).")
        friendly = input(f"  {C.CYAN}>{C.RESET} Name [{dir_name}]: ").strip()
        if not friendly:
            friendly = dir_name
        cfg["whisper_model_name"] = friendly
    else:
        new_model = models[ch]
        cfg["whisper_model_name"] = ""

    if new_model == cur:
        info("Already active")
        pause()
        return
    if not data:
        cfg["whisper_model"] = new_model
        save_config(cfg)
        display = Path(new_model).name if os.path.sep in new_model or "/" in new_model else new_model
        success(f"Config set to {display} (applies on next launch)")
        pause()
        return
    info(f"Switching to {Path(new_model).name if os.path.sep in new_model or '/' in new_model else new_model}...")
    result = _server_post(h, p, "/model/switch", {"model": new_model}, timeout=120)
    if result and not result.get("error"):
        success(f"Switched to {result.get('model', new_model)}")
        cfg["whisper_model"] = result.get("model", new_model)
        save_config(cfg)
    else:
        error(f"Failed: {result.get('error', 'unknown') if result else 'server unreachable'}")
    pause()


# ── HuggingFace browser ────────────────────────────────────────────────────────


def browse_hf(cfg: dict) -> None:
    """Browse and download GGUF models from HuggingFace."""
    from config import save_config

    while True:
        header("Download GGUF Model from HuggingFace")
        gpu = detect_gpu()
        if gpu["has_nvidia"]:
            info(f"Your GPU: {gpu['name']} ({gpu['vram_mb']} MB VRAM)\n")
            info("Quantization guide:")
            bullet("Q4_K_M  ~4 bits -- best quality/speed balance")
            bullet("Q5_K_M  ~5 bits -- slightly better quality")
            bullet("Q8_0    ~8 bits -- near-original, 2x size")
            bullet("Q3_K_S  ~3 bits -- smallest, lower quality")
            print()
            bullet(f"7B Q4_K_M ~ 4.4 GB -> fits {C.GREEN}6 GB{C.RESET} VRAM")
            bullet(f"7B Q8_0 ~ 7.7 GB -> needs {C.YELLOW}8 GB{C.RESET} VRAM")
            bullet(f"14B Q4_K_M ~ 8.7 GB -> needs {C.YELLOW}10 GB{C.RESET} VRAM")

        print()
        info("Recommended repos for Japanese -> English:")
        print()
        bullet(f"{C.CYAN}Qwen/Qwen2.5-7B-Instruct-GGUF{C.RESET}       -- best JP->EN, 7B")
        bullet(f"{C.CYAN}Qwen/Qwen2.5-14B-Instruct-GGUF{C.RESET}      -- more accurate, 14B")
        bullet(f"{C.CYAN}Qwen/Qwen2.5-3B-Instruct-GGUF{C.RESET}       -- lighter, 3B")
        bullet(f"{C.CYAN}bartowski/gemma-2-9b-it-GGUF{C.RESET}         -- Google Gemma 2, 9B")
        print()
        repo = ask_input("HuggingFace repo (owner/model-name)", "")
        if not repo:
            return

        info(f"Fetching files from {repo}...")
        files = hf_list_gguf(repo)
        if not files:
            warn("No .gguf files found. Use a GGUF repo (usually has '-GGUF' suffix).")
            pause()
            continue

        print(f"\n  {C.BOLD}Files in {repo}:{C.RESET}\n")
        opts = []
        for f in files:
            sg = f["bytes"] / GiB
            fit = ""
            if gpu["has_nvidia"]:
                vg = gpu["vram_mb"] / KiB
                if sg * 1.15 < vg:
                    fit = f" {C.GREEN}+ fits GPU{C.RESET}"
                elif sg < vg:
                    fit = f" {C.YELLOW}~ tight{C.RESET}"
                else:
                    fit = f" {C.RED}x too large{C.RESET}"
            opts.append((f"{f['name']}  ({f['size']}){fit}", None))
        opts.append(("Back", None))

        ch = ask_choice("Select file:", opts, default=len(opts) - 1)
        if ch == -1 or ch == len(files):
            continue

        sel = files[ch]
        print()
        info(f"File: {sel['name']}")
        info(f"Size: {sel['size']}")
        info(f"Dest: {GGUF_DIR / sel['name']}")
        print()

        if ask_yn(f"Download {sel['name']}?"):
            if hf_download(repo, sel["name"]):
                cfg["gguf_model_path"] = str(GGUF_DIR / sel["name"])
                cfg["translation_model"] = sel["name"]
                save_config(cfg)
                success(f"Saved to {GGUF_DIR / sel['name']}")
                if cfg["translation_backend"] != "llamacpp":
                    if ask_yn("Switch backend to llama.cpp to use this model?"):
                        bi = _BI.get("llamacpp", {})
                        cfg["translation_backend"] = "llamacpp"
                        cfg["translation_host"] = bi.get("dh", "127.0.0.1")
                        cfg["translation_port"] = bi.get("dp", 5000)
                        save_config(cfg)
        pause()
        return


# ── Tools menu ─────────────────────────────────────────────────────────────────


def tools_menu(cfg: dict) -> None:
    """Tools management menu."""
    from yume.benchmark import benchmark_whisper
    from yume.health import detect_fonts

    while True:
        header("Tools Management")
        yt = find_tool("yt-dlp")
        ff = find_tool("ffmpeg")
        dn = find_tool("deno")
        _cur_browser = _detect_extension_browser()
        ch = ask_choice(
            "Select a tool:",
            [
                (f"yt-dlp          {'OK' if yt else 'MISSING'}", "Downloads audio from YouTube and 1000+ video sites"),
                (f"FFmpeg          {'OK' if ff else 'MISSING'}", "Converts audio between formats (required)"),
                (f"Deno            {'OK' if dn else '--'}", "Helps bypass YouTube bot detection (optional)"),
                ("Translation Backend", "Choose how Yume translates: llama.cpp / Ollama / LM Studio / Custom"),
                ("Download Translation Model", "Browse and download GGUF models (the files your translator uses)"),
                ("Python Dependencies", "Install required Python packages + romanization libraries"),
                ("Test Translation", "Send a test sentence to check if translation is working"),
                ("Benchmark Whisper", "Measure how fast each speech recognition model runs on your hardware"),
                ("Detect Fonts", "Find subtitle-compatible fonts installed on your system"),
                (f"Browser Extension  [{_cur_browser}]", "Switch between Chrome and Firefox extension format"),
                ("Back", None),
            ],
            default=10,
        )
        if ch == -1 or ch == 10:
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
            browse_hf(cfg)
        elif ch == 5:
            _menu_pydeps()
        elif ch == 6:
            _test_translation(cfg)
        elif ch == 7:
            benchmark_whisper(cfg)
        elif ch == 8:
            detect_fonts()
        elif ch == 9:
            _menu_browser_extension()


def _detect_extension_browser() -> str:
    """Detect whether the current manifest.json is Chrome or Firefox format."""
    manifest = EXT_DIR / "manifest.json"
    if not manifest.exists():
        return "unknown"
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
        if "browser_specific_settings" in data:
            return "Firefox"
        if data.get("background", {}).get("service_worker"):
            return "Chrome"
        if data.get("background", {}).get("scripts"):
            return "Firefox"
        return "Chrome"
    except Exception:
        return "unknown"


def _menu_browser_extension() -> None:
    """Switch extension manifest between Chrome and Firefox format."""
    import shutil as _shutil

    header("Browser Extension Format")

    manifest_chrome = EXT_DIR / "manifest.json"
    manifest_firefox = EXT_DIR / "manifest_firefox.json"
    current = _detect_extension_browser()

    info(f"Current format: {C.BOLD}{current}{C.RESET}")
    print()
    info("Yume ships with two manifest files:")
    info(f"  {C.CYAN}manifest.json{C.RESET}         — Chrome / Brave / Edge (MV3 service worker)")
    info(f"  {C.CYAN}manifest_firefox.json{C.RESET}  — Firefox (MV3 background scripts)")
    print()
    info(f"{C.DIM}Switching replaces manifest.json with the right format for your browser.{C.RESET}")
    info(f"{C.DIM}A backup of the current manifest is saved as manifest_backup.json.{C.RESET}")
    print()

    if not manifest_firefox.exists():
        error("manifest_firefox.json not found in extension folder!")
        info("Re-extract Yume to get both manifest files.")
        pause()
        return

    ch = ask_choice(
        "Switch to:",
        [
            ("Chrome / Brave / Edge", "MV3 with service_worker (default)"),
            ("Firefox", "MV3 with background scripts + gecko settings"),
            ("Back", None),
        ],
        default=0 if current == "Firefox" else 1,
    )

    if ch == -1 or ch == 2:
        return

    if ch == 0 and current == "Chrome":
        info("Already using Chrome format.")
        pause()
        return
    if ch == 1 and current == "Firefox":
        info("Already using Firefox format.")
        pause()
        return

    backup = EXT_DIR / "manifest_backup.json"
    try:
        _shutil.copy2(manifest_chrome, backup)
        info(f"Backup saved: {backup.name}")
    except Exception as e:
        warn(f"Backup failed: {e}")

    if ch == 1:
        try:
            if current == "Chrome":
                chrome_backup = EXT_DIR / "manifest_chrome.json"
                if not chrome_backup.exists():
                    _shutil.copy2(manifest_chrome, chrome_backup)
            _shutil.copy2(manifest_firefox, manifest_chrome)
            success("Switched to Firefox format!")
            info("Reload the extension in about:debugging to apply changes.")
        except Exception as e:
            error(f"Switch failed: {e}")
    elif ch == 0:
        chrome_backup = EXT_DIR / "manifest_chrome.json"
        if chrome_backup.exists():
            try:
                _shutil.copy2(chrome_backup, manifest_chrome)
                success("Switched to Chrome format!")
                info("Reload the extension in chrome://extensions to apply changes.")
            except Exception as e:
                error(f"Switch failed: {e}")
        else:
            try:
                data = json.loads(manifest_chrome.read_text(encoding="utf-8"))
                data.pop("browser_specific_settings", None)
                bg = data.get("background", {})
                if "scripts" in bg:
                    scripts = bg.pop("scripts")
                    bg["service_worker"] = scripts[0] if scripts else "js/background.js"
                manifest_chrome.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
                success("Switched to Chrome format!")
                info("Reload the extension in chrome://extensions to apply changes.")
            except Exception as e:
                error(f"Switch failed: {e}")

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
        ch = ask_choice(
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
        ch = ask_choice(
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
            bc = ask_choice(
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
        ch = ask_choice("Options:", [("Install / Update", "Download latest static build"), ("Back", None)], default=1)
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
        ch = ask_choice(
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
                    [sys.executable, "-m", "pip", "install", "-q", "--no-warn-script-location",
                     "bgutil-ytdlp-pot-provider"],
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
        bi = _BI.get(cur, _BI.get("custom", {}))
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

        ch = ask_choice(
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
            print(f"\n  {C.BOLD}{bi.get('name', cur)}{C.RESET}\n  {bi.get('desc', '')}\n\n  {C.WHITE}Installation:{C.RESET}")
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
    keys = list(_BI.keys())
    opts = [(_BI[k]["name"], _BI[k]["desc"]) for k in keys] + [("Back", None)]
    cur = cfg.get("translation_backend", "llamacpp")
    di = keys.index(cur) if cur in keys else 0
    ch = ask_choice("Choose:", opts, default=di)
    if ch == -1 or ch == len(keys):
        return
    k = keys[ch]
    bi = _BI[k]
    cfg["translation_backend"] = k
    cfg["translation_host"] = bi["dh"]
    cfg["translation_port"] = bi["dp"]
    save_config(cfg)
    success(f"Backend: {bi['name']}  ({bi['dh']}:{bi['dp']})")
    print(f"\n  {C.WHITE}Installation:{C.RESET}")
    for line in bi["inst"].split("\n"):
        print(f"  {line}")
    pause()


def _change_addr(cfg: dict, prefix: str) -> None:
    from config import save_config, validate_host, validate_port

    ch = cfg.get(f"{prefix}_host", "127.0.0.1")
    cp = cfg.get(f"{prefix}_port", 5000)
    print(f"\n  Current: {C.CYAN}{ch}:{cp}{C.RESET}\n")
    raw_host = ask_input("Host", ch)
    host = validate_host(raw_host)
    if host is None:
        warn(f"Keeping current host: {ch}")
        host = ch
    raw_port = ask_input("Port", str(cp))
    port = validate_port(raw_port, f"{prefix.title()} port")
    if port is None:
        warn(f"Keeping current port: {cp}")
        port = cp
    else:
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
    pause()


def _manage_model(cfg: dict) -> None:
    import subprocess

    from config import DEFAULT_OLLAMA_PORT, save_config

    while True:
        header("Manage Translation Model")
        bk = cfg.get("translation_backend", "llamacpp")
        mdl = cfg.get("translation_model", "")
        gp = cfg.get("gguf_model_path", "")
        bi = _BI.get(bk, {})
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

        ch = ask_choice(
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
            fc = ask_choice("Select:", fo, default=len(fo) - 1)
            if 0 <= fc < len(gf):
                cfg["gguf_model_path"] = str(gf[fc])
                cfg["translation_model"] = gf[fc].stem
                save_config(cfg)
                success(f"Selected: {gf[fc].name}")
                if cfg["translation_backend"] != "llamacpp":
                    if ask_yn("Switch to llama.cpp backend?"):
                        bi2 = _BI.get("llamacpp", {})
                        cfg["translation_backend"] = "llamacpp"
                        cfg["translation_host"] = bi2.get("dh", "127.0.0.1")
                        cfg["translation_port"] = bi2.get("dp", 5000)
                        save_config(cfg)
            pause()


def _menu_pydeps() -> None:
    from yume.installers import _check_pip, install_llamacpp_python, install_python_deps
    from yume.utils import _run

    header("Python Dependencies")
    section("Core (required)")
    deps: dict[str, bool] = {}
    for p in ["faster_whisper", "flask", "flask_cors", "llama_cpp", "uvicorn", "fastapi"]:
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
    roma_deps: dict[str, bool] = {"pykakasi": False, "pypinyin": False, "romanization": False}
    roma_desc = {
        "pykakasi": "Japanese kanji → romaji (instant, no LLM needed)",
        "pypinyin": "Chinese hanzi → pinyin (instant, no LLM needed)",
        "romanization": "Korean hangul → romanization (instant, no LLM needed)",
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
        info(f"{C.DIM}Without these, Japanese kana still gets romaji via WanaKana (bundled),{C.RESET}")
        info(f"{C.DIM}but kanji readings, Chinese pinyin, and Korean need the LLM (slower).{C.RESET}")
        info(f"{C.DIM}Korean and Russian also have built-in client-side romanization.{C.RESET}")
        if ask_yn("Install romanization libraries? (recommended)"):
            _check_pip()
            r = _run(
                [sys.executable, "-m", "pip", "install",
                 "pykakasi==2.3.0", "pypinyin==0.55.0", "romanization==2.0.0",
                 "-q", "--no-warn-script-location"],
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
    bi = _BI.get(bk, _BI.get("custom", {"ap": "/v1/chat/completions", "hp": HEALTH_PATH_OPENAI}))

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
        if bk == "ollama":
            body["model"] = m
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            f"http://{h}:{p}{bi.get('ap', '/v1/chat/completions')}",
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


# ── Settings menu ──────────────────────────────────────────────────────────────


def settings_menu(cfg: dict) -> None:
    """Full settings menu."""
    from config import DEFAULT_CONFIG, save_config, config_export, config_import

    while True:
        header("Settings")
        bi = _BI.get(cfg.get("translation_backend", "llamacpp"), _BI.get("custom", {}))
        ym = cfg["youtube_auth_method"]
        if ym == "cookies":
            ym += f" ({cfg.get('cookies_browser', 'chrome')})"

        dev_raw = cfg["whisper_device"]
        comp_raw = cfg["whisper_compute_type"]
        gpu = detect_gpu()
        if dev_raw == "auto":
            from yume.hardware import IS_WIN as _IS_WIN

            if gpu["has_nvidia"]:
                dev_display = f"{C.GREEN}cuda{C.RESET} (auto)"
            elif gpu.get("has_amd") and not _IS_WIN:
                dev_display = f"{C.RED}cuda{C.RESET} (auto, ROCm)"
            else:
                dev_display = "cpu (auto)"
        else:
            dev_display = dev_raw
        if comp_raw == "auto":
            resolved_comp = (
                "float16"
                if "cuda" in dev_display and gpu.get("vram_mb", 0) >= 8000
                else ("int8_float16" if "cuda" in dev_display else "int8")
            )
            comp_display = f"{resolved_comp} (auto)"
        else:
            comp_display = comp_raw

        table(
            ["Setting", "Value"],
            [
                [f"{C.GOLD}Whisper Model{C.RESET}", cfg["whisper_model"]],
                [f"{C.GOLD}Device / Precision{C.RESET}", f"{dev_display} / {comp_display}"],
                [f"{C.GOLD}Whisper Address{C.RESET}", f"{cfg['whisper_host']}:{cfg['whisper_port']}"],
                ["", ""],
                [f"{C.MAGENTA}Translation{C.RESET}", bi.get("name", "?")],
                [f"{C.MAGENTA}TL Address{C.RESET}", f"{cfg['translation_host']}:{cfg['translation_port']}"],
                [f"{C.MAGENTA}TL Model{C.RESET}", cfg.get("translation_model", "—")],
                ["", ""],
                [f"{C.CYAN}Chunk Duration{C.RESET}",
                 f"{cfg['chunk_duration']}s (audio processed in this many seconds at a time)"],
                [f"{C.DIM}Word Timestamps{C.RESET}",
                 f"{C.DIM}{'Yes' if cfg['word_timestamps'] else 'No'} "
                 f"(no effect — server overrides for music optimization){C.RESET}"],
                [f"{C.DIM}Pause Threshold{C.RESET}",
                 f"{C.DIM}{cfg['pause_threshold']}s "
                 f"(no effect — requires word_timestamps which is forced off){C.RESET}"],
                ["", ""],
                [f"{C.YELLOW}YouTube Auth{C.RESET}", ym],
            ],
            col_styles=[C.RESET, C.CYAN],
            title="Current Settings",
        )

        ch = ask_choice(
            "Change:",
            [
                ("Whisper settings", "Speech recognition model, device, precision"),
                ("Translation settings", "Translation backend, address, model"),
                ("Server addresses", "Host/port for Whisper and Translation servers"),
                ("Subtitle tuning", "Chunk size, pause detection, word splitting"),
                ("Translation prompt", "Customize how the AI translates (tone, style, rules)"),
                ("Romanization prompt", "Customize how the AI romanizes non-Latin text"),
                ("YouTube auth", "How Yume accesses YouTube (Deno bot bypass or browser cookies)"),
                ("Export config", "Save settings to a backup file"),
                ("Import config", "Load settings from a backup file"),
                ("Reset to defaults", None),
                ("Back", None),
            ],
            default=10,
        )
        if ch == -1 or ch == 10:
            return
        elif ch == 0:
            _set_whisper(cfg)
        elif ch == 1:
            _menu_backend(cfg)
        elif ch == 2:
            _set_addrs(cfg)
        elif ch == 3:
            _set_subs(cfg)
        elif ch == 4:
            _menu_translation_prompt(cfg)
        elif ch == 5:
            _menu_romanization_prompt(cfg)
        elif ch == 6:
            _menu_yt_auth(cfg)
        elif ch == 7:
            config_export(cfg)
            pause()
        elif ch == 8:
            backups = sorted(BASE_DIR.glob("yume_config_backup_*.json"), reverse=True)
            if backups:
                info("Found backup files:")
                for i, b in enumerate(backups[:5]):
                    bullet(f"{i + 1}. {b.name}")
                choice = ask_input("File number or path", "1")
                try:
                    idx = int(choice) - 1
                    if 0 <= idx < len(backups):
                        imported = config_import(backups[idx])
                        if imported:
                            cfg.update(imported)
                except ValueError:
                    imported = config_import(choice)
                    if imported:
                        cfg.update(imported)
            else:
                path = ask_input("Path to config file", "")
                if path:
                    imported = config_import(path)
                    if imported:
                        cfg.update(imported)
            pause()
        elif ch == 9:
            if ask_yn("Reset ALL settings?", False):
                n = dict(DEFAULT_CONFIG)
                n["first_run_complete"] = True
                save_config(n)
                cfg.update(n)
                success("Reset!")
            pause()


def _set_whisper(cfg: dict) -> None:
    from config import save_config

    header("Whisper Settings")
    gpu = detect_gpu()
    rec_model, rec_reason = recommend_whisper_model(gpu)
    if gpu["has_nvidia"]:
        info(f"GPU: {gpu['name']} ({gpu['vram_mb']} MB VRAM)")
        info("  VRAM = Video RAM, the memory on your graphics card used by AI models")
    info(f"Current model: {C.BOLD}{cfg['whisper_model']}{C.RESET}")
    info(f"Recommendation: {rec_model} ({rec_reason})")

    ch = ask_choice(
        "What to change:",
        [
            ("Whisper model", f"Currently: {cfg['whisper_model']} — the AI that converts speech to text"),
            ("Device (CPU/GPU)", f"Currently: {cfg['whisper_device']} — where the AI runs"),
            ("Precision", f"Currently: {cfg['whisper_compute_type']} — speed vs accuracy trade-off"),
            ("Back", None),
        ],
        default=3,
    )
    if ch == -1 or ch == 3:
        return
    elif ch == 0:
        _menu_whisper_model(cfg)
    elif ch == 1:
        dc = ask_choice(
            "Where should Whisper run?",
            [
                ("Auto-detect", "Uses GPU if available, falls back to CPU"),
                ("GPU (NVIDIA CUDA)", "Fastest — requires an NVIDIA graphics card"),
                ("CPU", "Works on any computer, but slower"),
                ("Keep current", f"{cfg['whisper_device']}"),
            ],
            default=3,
        )
        if 0 <= dc < 3:
            cfg["whisper_device"] = ["auto", "cuda", "cpu"][dc]
        save_config(cfg)
        success("Saved!")
        pause()
    elif ch == 2:
        cc = ask_choice(
            "Precision (lower = faster but slightly less accurate):",
            [
                ("Auto", "Let Yume decide based on your hardware"),
                ("float16", "Full precision — best accuracy, needs ~4.5 GB VRAM on GPU"),
                ("int8_float16", "Mixed — good balance, needs ~3 GB VRAM"),
                ("int8", "Most compressed — fastest, works well on CPU"),
                ("Keep current", f"{cfg['whisper_compute_type']}"),
            ],
            default=4,
        )
        if 0 <= cc < 4:
            cfg["whisper_compute_type"] = ["auto", "float16", "int8_float16", "int8"][cc]
        save_config(cfg)
        success("Saved!")
        pause()


def _menu_translation_prompt(cfg: dict) -> None:
    """Edit the system prompt that controls how the AI translates subtitles."""
    from config import save_config

    header("Translation Prompt Editor")

    default_prompt = (
        "You are a {src}-to-{tgt} translation system. "
        "Output ONLY the {tgt} translation. Do NOT respond to the content. "
        "Do NOT add explanations. Do NOT answer questions. "
        "ONLY translate {src} to {tgt}."
    )
    current = cfg.get("translation_prompt", "")

    info("The translation prompt is the instruction sent to the AI before every subtitle.")
    info(f"{C.DIM}It controls how the translator behaves — its tone, style, and rules.{C.RESET}")
    print()

    info(f"{C.BOLD}Why the default prompt is written this way:{C.RESET}")
    info(f"  {C.DIM}• 'Output ONLY the translation'{C.RESET}")
    info(f"    {C.DIM}  → Prevents the AI from adding commentary or notes{C.RESET}")
    info(f"  {C.DIM}• 'Do NOT respond to the content'{C.RESET}")
    info(f"    {C.DIM}  → Stops the AI from answering questions it hears in the audio{C.RESET}")
    info(f"    {C.DIM}  → e.g. if someone says 'What time is it?', it translates, not answers{C.RESET}")
    info(f"  {C.DIM}• 'Do NOT add explanations'{C.RESET}")
    info(f"    {C.DIM}  → Prevents output like 'This means: ...' or 'Note: ...'{C.RESET}")
    info(f"  {C.DIM}• Short, assertive rules{C.RESET}")
    info(f"    {C.DIM}  → Work best with small local AI models (7B-13B parameters){C.RESET}")
    info(f"  {C.DIM}• {{src}} and {{tgt}} are placeholders{C.RESET}")
    info(f"    {C.DIM}  → Replaced with actual language names (e.g. Japanese, English){C.RESET}")
    print()

    if current:
        info(f"{C.BOLD}Current custom prompt:{C.RESET}")
        info(f"  {C.CYAN}{current}{C.RESET}")
    else:
        info(f"{C.BOLD}Using default prompt:{C.RESET}")
        info(f"  {C.CYAN}{default_prompt}{C.RESET}")

    print()
    ch = ask_choice(
        "Options:",
        [
            ("Edit prompt", "Write your own translation instruction"),
            ("Reset to default", "Restore the built-in prompt"),
            ("View example prompts", "See templates for different styles"),
            ("Back", None),
        ],
        default=3,
    )

    if ch == -1 or ch == 3:
        return
    elif ch == 0:
        info("Use {src} for source language and {tgt} for target language.")
        info(f"{C.DIM}Example: 'Translate {{src}} to {{tgt}}. Keep it casual.'{C.RESET}")
        new_prompt = ask_input("New prompt", current or default_prompt)
        if new_prompt:
            cfg["translation_prompt"] = new_prompt
            save_config(cfg)
            success("Prompt saved! Will be used for all future translations.")
        pause()
    elif ch == 1:
        cfg.pop("translation_prompt", None)
        save_config(cfg)
        success("Reset to default prompt.")
        pause()
    elif ch == 2:
        section("Example Prompts")
        examples = [
            (
                "Casual / informal",
                "Translate {src} to casual {tgt}. Use everyday language, contractions, and slang where appropriate. Output ONLY the translation.",
            ),
            (
                "Formal / literary",
                "Translate {src} to formal {tgt}. Use proper grammar and literary vocabulary. Output ONLY the translation.",
            ),
            (
                "Song lyrics (poetic)",
                "Translate these {src} song lyrics to {tgt}. Preserve poetic rhythm and feeling. Output ONLY the translation.",
            ),
            (
                "Keep honorifics (anime)",
                "Translate {src} to {tgt}. Keep Japanese honorifics (-san, -kun, -chan, -sama, -sensei) untranslated. Output ONLY the translation.",
            ),
            (
                "Technical / precise",
                "Translate {src} to {tgt}. Preserve technical terms and proper nouns exactly. Output ONLY the translation.",
            ),
        ]
        for i, (name, prompt) in enumerate(examples):
            info(f"  {C.BOLD}{i + 1}. {name}{C.RESET}")
            info(f"     {C.DIM}{prompt}{C.RESET}")
            print()
        choice = ask_input("Use which? (number, or press Enter to go back)", "")
        if choice.strip().isdigit():
            idx = int(choice.strip()) - 1
            if 0 <= idx < len(examples):
                cfg["translation_prompt"] = examples[idx][1]
                save_config(cfg)
                success(f"Prompt set to: {examples[idx][0]}")
        pause()


def _menu_romanization_prompt(cfg: dict) -> None:
    """Edit the system prompt that controls how the AI romanizes text."""
    from config import save_config

    header("Romanization Prompt Editor")

    info("This prompt controls how the AI converts non-Latin text to Latin characters.")
    print()
    info(f"{C.BOLD}How romanization works per language:{C.RESET}")
    info(f"  {C.DIM}Japanese  → pykakasi library (instant, ignores this prompt){C.RESET}")
    info(f"  {C.DIM}Chinese   → pypinyin library (instant, ignores this prompt){C.RESET}")
    info(f"  {C.DIM}Korean    → romanization library (instant, ignores this prompt){C.RESET}")
    info(f"  {C.DIM}Russian, Arabic, and all other non-Latin languages{C.RESET}")
    info(f"  {C.DIM}  → Uses your translation LLM with this prompt{C.RESET}")
    info(f"  {C.DIM}If the instant libraries aren't installed, those languages also use the LLM.{C.RESET}")
    print()

    info(f"{C.BOLD}Available placeholders:{C.RESET}")
    info(f"  {C.DIM}{{src}} → source language name (e.g. 'Russian', 'Japanese'){C.RESET}")
    info(f"  {C.DIM}{{sys}} → romanization system name (e.g. 'transliteration', 'romaji'){C.RESET}")
    print()

    defaults = {
        "ru": "You are a Russian to Latin transliteration converter. Output ONLY the Latin transliteration of the Russian input. Do NOT translate. Do NOT add explanations. ONLY transliteration.",
        "ar": "You are an Arabic to Latin transliteration converter. Output ONLY the Latin transliteration of the Arabic input. Do NOT translate. Do NOT add explanations. ONLY transliteration.",
    }

    current = cfg.get("romanization_prompt", "")
    if current:
        info(f"{C.BOLD}Current custom prompt:{C.RESET}")
        info(f"  {C.CYAN}{current}{C.RESET}")
    else:
        info(f"{C.BOLD}Using built-in per-language prompts:{C.RESET}")
        for lang, prompt in defaults.items():
            info(f"  {C.DIM}{lang}: {prompt[:70]}...{C.RESET}")

    print()
    ch = ask_choice(
        "Options:",
        [
            ("Edit prompt", "Write your own romanization instruction"),
            ("Reset to default", "Restore the built-in per-language prompts"),
            ("View example prompts", "See templates for different styles"),
            ("Back", None),
        ],
        default=3,
    )

    if ch == -1 or ch == 3:
        return
    elif ch == 0:
        info("Use {src} for source language and {sys} for the romanization system name.")
        info(f"{C.DIM}Example: 'Convert {{src}} to Latin script using {{sys}}. Output ONLY the result.'{C.RESET}")
        new_prompt = ask_input("New prompt", current or "")
        if new_prompt:
            cfg["romanization_prompt"] = new_prompt
            save_config(cfg)
            success("Romanization prompt saved!")
        pause()
    elif ch == 1:
        cfg.pop("romanization_prompt", None)
        save_config(cfg)
        success("Reset to built-in per-language prompts.")
        pause()
    elif ch == 2:
        section("Example Prompts")
        examples = [
            (
                "Standard transliteration",
                "Convert {src} text to Latin characters using {sys}. Output ONLY the result. No translation. No explanations.",
            ),
            (
                "Phonetic (pronunciation-focused)",
                "Convert {src} to how it sounds in English letters. Prioritize pronunciation over spelling rules. Output ONLY the result.",
            ),
            (
                "Academic (strict system)",
                "Transliterate {src} to Latin using the ISO 9 standard. Be precise. Output ONLY the transliteration.",
            ),
        ]
        for i, (name, prompt) in enumerate(examples):
            info(f"  {C.BOLD}{i + 1}. {name}{C.RESET}")
            info(f"     {C.DIM}{prompt}{C.RESET}")
            print()
        choice = ask_input("Use which? (number, or press Enter to go back)", "")
        if choice.strip().isdigit():
            idx = int(choice.strip()) - 1
            if 0 <= idx < len(examples):
                cfg["romanization_prompt"] = examples[idx][1]
                save_config(cfg)
                success(f"Prompt set to: {examples[idx][0]}")
        pause()


def _set_addrs(cfg: dict) -> None:
    while True:
        header("Server Addresses")
        info(f"Whisper:     {C.CYAN}{cfg['whisper_host']}:{cfg['whisper_port']}{C.RESET}")
        info(f"Translation: {C.CYAN}{cfg['translation_host']}:{cfg['translation_port']}{C.RESET}")
        ch = ask_choice(
            "Change:", [("Whisper address", None), ("Translation address", None), ("Back", None)], default=2
        )
        if ch == -1 or ch == 2:
            return
        elif ch == 0:
            _change_addr(cfg, "whisper")
        elif ch == 1:
            _change_addr(cfg, "translation")


def _set_subs(cfg: dict) -> None:
    from config import save_config

    header("Subtitle Tuning")
    print()
    info("These settings control how Yume processes audio into subtitles.")
    print()

    info(f"{C.BOLD}Word-level timestamps{C.RESET}: Break subtitles at individual words instead of sentences.")
    info(f"  Currently: {'ON' if cfg['word_timestamps'] else 'OFF'}")
    cfg["word_timestamps"] = ask_yn("Enable word-level timestamps? (OFF is better for music)", cfg["word_timestamps"])

    print()
    info(f"{C.BOLD}Pause threshold{C.RESET}: How long a silence (in seconds) before Yume splits")
    info("  a subtitle into two lines. Lower = more lines, higher = longer subtitles.")
    info("  0.25s works well for songs, 0.4s works well for speech.")
    info(f"  Currently: {cfg['pause_threshold']}s")
    pt = ask_input("Pause threshold (0.2-1.0s)", str(cfg["pause_threshold"]))
    try:
        cfg["pause_threshold"] = float(pt)
    except Exception as e:
        _log.debug("[_set_subs] float-parse failed: %s", e)

    print()
    info(f"{C.BOLD}Chunk duration{C.RESET}: Yume processes audio in chunks of this many seconds.")
    info("  Smaller chunks = subtitles appear faster, but may cut words at boundaries.")
    info("  Larger chunks = more context for the AI, but longer wait for first subtitle.")
    info("  30s is the default and works well for most content.")
    info(f"  Currently: {cfg['chunk_duration']}s")
    cd = ask_input("Chunk duration (10-60s)", str(cfg["chunk_duration"]))
    try:
        cfg["chunk_duration"] = int(cd)
    except Exception as e:
        _log.debug("[_set_subs] int-parse failed: %s", e)

    save_config(cfg)
    success("Saved!")
    pause()
