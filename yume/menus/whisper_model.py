"""Whisper model: the `model` command and the hot-swap menu."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from yume.menus.server import _server_down
from yume.network import server_get as _server_get, server_post as _server_post
from yume.ui import C, ask_arrow, error, header, info, pause, success, warn

_CT2_FILES = ["model.bin", "config.json", "tokenizer.json", "vocabulary.txt"]


def _is_path(model: str) -> bool:
    return os.path.sep in model or "/" in model


def _short_name(model: str) -> str:
    """A model's name; a custom model's folder name instead of its path."""
    return Path(model).name if _is_path(model) else model


def _use_model(cfg: dict, model: str, name: str = "") -> None:
    """Point the config at a model. Its friendly name goes with it: the old one described the old model."""
    from config import save_config

    cfg["whisper_model"] = model
    cfg["whisper_model_name"] = name
    save_config(cfg)


def _post_switch(cfg: dict, model: str) -> dict:
    # Long timeout: the first switch to a model downloads it (up to ~3 GB)
    return _server_post(cfg["whisper_host"], cfg["whisper_port"], "/model/switch", {"model": model}, timeout=1800)


# ── Menu ──────────────────────────────────────────────────────────────────────


def _ask_custom_model() -> tuple[str, str] | None:
    """Ask for a CTranslate2 model folder and a friendly name; None when cancelled or invalid."""
    print()
    info(f"{C.DIM}Paste the path to a CTranslate2 Whisper model directory.{C.RESET}")
    info(f"{C.DIM}The folder must contain: {', '.join(_CT2_FILES)}{C.RESET}")
    info(f"{C.DIM}See README > Whisper Models > Using a custom or fine-tuned model for details.{C.RESET}")
    print()
    raw = input(f"  {C.CYAN}>{C.RESET} Path: ").strip().strip('"').strip("'")
    if not raw:
        info("Cancelled.")
        return None
    folder = Path(raw).resolve()
    if not folder.is_dir():
        error(f"Directory not found: {folder}")
        return None
    missing = [f for f in _CT2_FILES if not (folder / f).exists()]
    if missing:
        error(f"Not a valid CTranslate2 model — missing: {', '.join(missing)}")
        info(f"{C.DIM}Convert your model first:{C.RESET}")
        info(f"{C.DIM}  ct2-openai-whisper-converter --model <checkpoint> --output_dir <output>{C.RESET}")
        info(
            f"{C.DIM}  ct2-transformers-converter --model <hf-model> --output_dir <output> --quantization float16{C.RESET}"
        )
        return None
    print()
    info("Give this model a friendly name (shown in menus and popup).")
    return str(folder), input(f"  {C.CYAN}>{C.RESET} Name [{folder.name}]: ").strip() or folder.name


def _download_tag(name: str) -> str:
    from yume.benchmark import _is_whisper_model_cached

    if _is_whisper_model_cached(name):
        return f" {C.GREEN}[downloaded]{C.RESET}"
    return f" {C.DIM}[not downloaded]{C.RESET}"


def _pick_whisper_model(cur: str) -> int:
    """Index into WHISPER_MODELS_INFO, len(models) for a custom model, -1 for Back."""
    from yume.benchmark import WHISPER_MODELS_INFO

    opts = [
        (f"{name} ({vr}){_download_tag(name)}", "active" if name == cur else None)
        for name, _params, vr, _desc in WHISPER_MODELS_INFO
    ]
    n = len(opts)
    custom = _is_path(cur)
    tag = f" {C.GREEN}[active]{C.RESET}" if custom else ""
    opts.append((f"Custom model (local path){tag}", "active" if custom else None))
    opts.append(("Back", None))
    names = [name for name, *_ in WHISPER_MODELS_INFO]
    ch = ask_arrow("Switch to:", opts, default=names.index(cur) if cur in names else (n if custom else n + 1))
    return -1 if ch in (-1, n + 1) else ch


def _show_active(cfg: dict, data: dict | None, cur: str) -> None:
    display = cfg.get("whisper_model_name", "") or _short_name(cur)
    if not data:
        warn(f"Server not running. Config: {display}")
        return
    info(f"Active: {C.BOLD}{display}{C.RESET}  ({data.get('device', '?')})")
    if _is_path(cur):
        info(f"{C.DIM}Path: {cur}{C.RESET}")
    if data.get("gpu"):
        g = data["gpu"]
        info(f"GPU: {g['gpu_name']} ({g['vram_used_mb']}/{g['vram_total_mb']} MB)")


def _apply_model(cfg: dict, new_model: str, name: str, cur: str, online: bool) -> None:
    if new_model == cur:
        info("Already active")
    elif not online:
        _use_model(cfg, new_model, name)
        success(f"Config set to {_short_name(new_model)} (applies on next launch)")
    else:
        info(f"Switching to {_short_name(new_model)}...")
        info(f"{C.DIM}A model's first use downloads it — this can take several minutes.{C.RESET}")
        result = _post_switch(cfg, new_model)
        if result.get("error"):
            error(f"Failed: {result['error']}")
        else:
            success(f"Switched to {result.get('model', new_model)}")
            _use_model(cfg, result.get("model", new_model), name)


def _menu_whisper_model(cfg: dict) -> None:
    """Interactive whisper model hot-swap."""
    from yume.benchmark import WHISPER_MODELS_INFO

    header("Whisper Model")
    data = _server_get(cfg["whisper_host"], cfg["whisper_port"], "/stats")
    cur = data.get("model", cfg.get("whisper_model", "?")) if data else cfg.get("whisper_model", "large-v3-turbo")
    _show_active(cfg, data, cur)

    ch = _pick_whisper_model(cur)
    if ch == -1:
        return
    picked = _ask_custom_model() if ch == len(WHISPER_MODELS_INFO) else (WHISPER_MODELS_INFO[ch][0], "")
    if picked:
        _apply_model(cfg, *picked, cur, bool(data))
    pause()


# ── `model` command ───────────────────────────────────────────────────────────


def cli_model(cfg: dict, args: list) -> None:
    """CLI model management."""
    subcmd = args[0].lower() if args else ""
    if not args:
        _model_status(cfg)
    elif subcmd == "switch" and len(args) > 1:
        _model_switch(cfg, args[1])
    elif subcmd == "list":
        from yume.benchmark import WHISPER_MODELS_INFO

        cur = cfg.get("whisper_model", "?")
        for name, _params, vram, _desc in WHISPER_MODELS_INFO:
            cur_marker = f" {C.GOLD}<- current{C.RESET}" if name == cur else ""
            info(f"  {name:22s} {vram}{cur_marker}")
    else:
        print("  Usage: pocket_yume.py model [switch <name>|list]")
        sys.exit(2)


def _model_status(cfg: dict) -> None:
    h, p = cfg["whisper_host"], cfg["whisper_port"]
    data = _server_get(h, p, "/stats")
    if not data:
        _server_down(h, p)
        info(f"Config model: {cfg.get('whisper_model', '?')}")
        return
    info(f"Active model: {C.BOLD}{data.get('model', '?')}{C.RESET}")
    info(f"Device: {data.get('device', '?')}  |  Compute: {data.get('compute_type', '?')}")
    if data.get("gpu"):
        g = data["gpu"]
        info(f"GPU: {g.get('gpu_name', '?')} ({g.get('vram_used_mb', '?')}/{g.get('vram_total_mb', '?')} MB)")


def _model_switch(cfg: dict, new_model: str) -> None:
    """Switch the running server's model, or set it for the next start when it is down."""
    from yume.hardware import WHISPER_MODELS

    if not _server_get(cfg["whisper_host"], cfg["whisper_port"], "/stats"):
        # Server down: like the menu, remember it for the next start
        if new_model not in {m[0] for m in WHISPER_MODELS}:
            if not Path(new_model).is_dir():
                error(f"Unknown model: {new_model}  (see: python pocket_yume.py model list)")
                return
            new_model = str(Path(new_model).resolve())  # the server may start in another folder
        _use_model(cfg, new_model)
        success(f"Config set to {new_model} (applies on next launch)")
        return
    info(f"Switching to {new_model}... (a model's first use downloads it)")
    result = _post_switch(cfg, new_model)
    if result.get("error"):
        error(result["error"])
        if result.get("valid"):
            info(f"Valid: {', '.join(result['valid'])}")
    elif result.get("status") == "already_loaded":
        info(f"Already using {new_model}")
    else:
        success(f"Switched to {result.get('model', new_model)}")
        _use_model(cfg, result.get("model", new_model))
