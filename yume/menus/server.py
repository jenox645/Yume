"""CLI commands that talk to the running Yume server, plus the blacklist and Whisper model menus."""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

from yume.network import server_get as _server_get, server_post as _server_post
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

_log = logging.getLogger("pocket_yume")


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
        info(f"  Util: {gpu.get('gpu_util_pct') or '?'}%  |  Temp: {gpu.get('gpu_temp_c') or '?'}C")
    else:
        info("GPU: N/A (CPU mode or nvidia-smi unavailable)")
    print()
    section("Whisper Engine")
    info(
        f"Model: {C.BOLD}{data.get('model', '?')}{C.RESET}  ({data.get('device', '?')}/{data.get('compute_type', '?')})"
    )
    info(f"Uptime: {data.get('uptime_human', '?')}")
    section("Session")
    info(f"Sections transcribed:    {data.get('regions_transcribed', 0)}")
    info(f"Lines produced:          {data.get('segments_produced', 0)}")
    info(f"Hallucinations blocked:  {data.get('hallucinations_filtered', 0)}")
    info(f"Audio processed:         {data.get('total_audio_seconds', 0):.0f}s")
    info(f"Avg Whisper time:        {data.get('avg_whisper_time', 0)}s per section")
    info(
        f"Lines translated:        {data.get('lines_translated', 0)}  (cache hits: {data.get('translation_cache_hits', 0)})"
    )
    info(f"Saved videos:            {data.get('library_size', 0)}")
    info(f"Active jobs:             {data.get('active', 0)} of {data.get('jobs', 0)}")
    info(f"Blacklist size:          {data.get('blacklist_size', 0)} items")


def cli_blacklist(cfg: dict, args: list) -> None:
    """CLI blacklist management."""
    h, p = cfg["whisper_host"], cfg["whisper_port"]
    if len(args) < 1:
        _menu_blacklist(cfg)
        return
    subcmd = args[0].lower()
    # Server down: edit config/blacklist.json, which it loads at startup (like the menu)
    data = _server_get(h, p, "/blacklist")
    current = data.get("blacklist", []) if data else _bl_read_offline()
    where = "" if data else f"  {C.DIM}(server not running — saved list){C.RESET}"

    def _save(items: list) -> bool:
        if not data:
            return _bl_write_offline(items)
        r = _server_post(h, p, "/blacklist/update", {"blacklist": items})
        return bool(r and r.get("success"))

    text = " ".join(args[1:]).strip()
    if subcmd == "list":
        if not current:
            info(f"Blacklist is empty{where}")
            return
        info(f"Blacklist ({len(current)} items):{where}")
        for item in current:
            bullet(item)
    elif subcmd == "add" and text:
        if text.lower() in (b.lower() for b in current):
            warn(f"Already blocked: {text}")
        elif _save([*current, text]):
            success(f"Added: {text}{where}")
        else:
            error("Failed to save")
    elif subcmd in ("remove", "rm") and text:
        keep = [b for b in current if b.lower() != text.lower()]
        if len(keep) == len(current):
            warn(f"Not in blacklist: {text}")
        elif _save(keep):
            success(f"Removed: {text}{where}")
        else:
            error("Failed to save")
    elif subcmd == "clear":
        if _save([]):
            success(f"Blacklist cleared{where}")
        else:
            error("Failed to save")
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
        from yume.hardware import WHISPER_MODELS

        if not _server_get(h, p, "/stats"):
            # Server down: like the menu, remember it for the next start
            if new_model not in {m[0] for m in WHISPER_MODELS} and not Path(new_model).is_dir():
                error(f"Unknown model: {new_model}  (see: python pocket_yume.py model list)")
                return
            cfg["whisper_model"] = new_model
            cfg["whisper_model_name"] = ""
            save_config(cfg)
            success(f"Config set to {new_model} (applies on next launch)")
            return
        info(f"Switching to {new_model}... (a model's first use downloads it)")
        # Long timeout: the first switch to a model downloads it (up to ~3 GB)
        result = _server_post(h, p, "/model/switch", {"model": new_model}, timeout=1800)
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
        from yume.benchmark import WHISPER_MODELS_INFO

        cur = cfg.get("whisper_model", "?")
        for name, _params, vram, _desc in WHISPER_MODELS_INFO:
            cur_marker = f" {C.GOLD}<- current{C.RESET}" if name == cur else ""
            info(f"  {name:22s} {vram}{cur_marker}")
    else:
        print("  Usage: pocket_yume.py model [switch <name>|list]")


# ── Blacklist / whisper model menus ───────────────────────────────────────────


def _bl_file():
    """Path of the persisted blacklist (shared with the whisper server)."""
    from config import CONFIG_DIR

    return CONFIG_DIR / "blacklist.json"


def _bl_read_offline() -> list:
    try:
        items = json.loads(_bl_file().read_text(encoding="utf-8"))
        return [str(i).strip() for i in items if str(i).strip()] if isinstance(items, list) else []
    except FileNotFoundError:
        return []
    except Exception as e:
        _log.debug("[_bl_read_offline] %s", e)
        return []


def _bl_write_offline(items: list) -> bool:
    try:
        _bl_file().parent.mkdir(parents=True, exist_ok=True)
        _bl_file().write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception as e:
        error(f"Could not save: {e}")
        return False


def _menu_blacklist(cfg: dict) -> None:
    """Blacklist management menu. Works offline too — the list lives in
    config/blacklist.json, which the server loads at startup."""
    while True:
        header("Subtitle Filter (Blacklist)")
        info("Whisper sometimes generates fake text that wasn't actually spoken —")
        info("things like 'Thank you for watching' or 'Subscribe'. These are called")
        info("'hallucinations'. Yume blocks known patterns automatically, but you can")
        info("add your own phrases to block here.")
        print()
        h, p = cfg["whisper_host"], cfg["whisper_port"]
        data = _server_get(h, p, "/blacklist")
        offline = not data
        if offline:
            bl = _bl_read_offline()
            warn(f"Server not running — editing the saved list ({_bl_file().name}).")
            info(f"{C.DIM}Changes apply automatically the next time the server starts.{C.RESET}")
            print()
        else:
            bl = data.get("blacklist", [])

        def _bl_save(items: list) -> bool:
            if offline:
                return _bl_write_offline(items)
            r = _server_post(h, p, "/blacklist/update", {"blacklist": items})
            return bool(r and r.get("success"))

        info(f"Blacklist: {len(bl)} items")
        if bl:
            for item in bl[:15]:
                bullet(item)
            if len(bl) > 15:
                info(f"  ... and {len(bl) - 15} more")
        ch = ask_arrow(
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
                if _bl_save(current):
                    success(f"Added: {text}")
                else:
                    error("Failed")
            pause()
        elif ch == 1:
            if not bl:
                info("Empty")
                pause()
                continue
            shown = bl[:20]
            if len(bl) > 20:
                info(f"Showing first 20 of {len(bl)} — remove others with:")
                info(f"{C.DIM}  python pocket_yume.py blacklist remove <text>{C.RESET}")
            opts = [(item, None) for item in shown] + [("Back", None)]
            rc = ask_arrow("Remove which?", opts, default=len(opts) - 1)
            # Guard against len(shown), not len(bl) — with >20 items the "Back"
            # entry sits at index 20, which is a valid bl index.
            if 0 <= rc < len(shown):
                removed = bl[rc]
                current = bl[:]
                current.pop(rc)
                if _bl_save(current):
                    success(f"Removed: {removed}")
            pause()
        elif ch == 2:
            if ask_yn("Clear ALL?", False):
                if _bl_save([]):
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
    from yume.benchmark import WHISPER_MODELS_INFO

    models = [name for name, *_ in WHISPER_MODELS_INFO]
    vram = {
        name: f"{vr}{', English only' if name.startswith('distil') else ''}"
        for name, _params, vr, _desc in WHISPER_MODELS_INFO
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
    ch = ask_arrow("Switch to:", opts, default=di)
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
        required = ["model.bin", "config.json", "tokenizer.json", "vocabulary.txt"]
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
    info(f"{C.DIM}A model's first use downloads it — this can take several minutes.{C.RESET}")
    result = _server_post(h, p, "/model/switch", {"model": new_model}, timeout=1800)
    if result and not result.get("error"):
        success(f"Switched to {result.get('model', new_model)}")
        cfg["whisper_model"] = result.get("model", new_model)
        save_config(cfg)
    else:
        error(f"Failed: {result.get('error', 'unknown') if result else 'server unreachable'}")
    pause()
