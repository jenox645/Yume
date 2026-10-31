"""Settings menu: Whisper, addresses, backups, and the entry to every other setting."""

from __future__ import annotations

from yume.constants import BASE_DIR, IS_WIN, backend_info
from yume.hardware import detect_gpu, recommend_whisper_model
from yume.ui import (
    C,
    ask_arrow,
    ask_input,
    ask_yn,
    bullet,
    header,
    info,
    pause,
    success,
    table,
)

from yume.menus.one_click import menu_one_click
from yume.menus.prompts import _menu_romanization_prompt, _menu_translation_prompt
from yume.menus.translation import _change_addr, _menu_backend
from yume.menus.whisper_model import _menu_whisper_model
from yume.menus.youtube_auth import _menu_yt_auth

# ── Settings menu ──────────────────────────────────────────────────────────────


def _one_click_label(cfg: dict) -> str:
    from yume import native_host

    if not native_host.registered_browsers():
        return "off"
    mins = int(cfg.get("auto_stop_minutes", 30))
    return f"on, auto-stop {f'{mins} min' if mins else 'never'}"


def _device_labels(cfg: dict) -> tuple[str, str]:
    """Whisper device and precision for display, with "auto" resolved for this machine."""
    gpu = detect_gpu()
    device = cfg["whisper_device"]
    if device == "auto":
        if gpu["has_nvidia"]:
            device = f"{C.GREEN}cuda{C.RESET} (auto)"
        elif gpu.get("has_amd") and not IS_WIN:
            device = f"{C.RED}cuda{C.RESET} (auto, ROCm)"
        else:
            device = "cpu (auto)"
    compute = cfg["whisper_compute_type"]
    if compute == "auto":
        if "cuda" not in device:
            compute = "int8 (auto)"
        else:
            compute = f"{'float16' if gpu.get('vram_mb', 0) >= 8000 else 'int8_float16'} (auto)"
    return device, compute


def _show_settings(cfg: dict) -> None:
    device, compute = _device_labels(cfg)
    yt_auth = cfg["youtube_auth_method"]
    if yt_auth == "cookies":
        yt_auth += f" ({cfg.get('cookies_browser', 'chrome')})"
    table(
        ["Setting", "Value"],
        [
            [f"{C.GOLD}Whisper Model{C.RESET}", cfg["whisper_model"]],
            [f"{C.GOLD}Device / Precision{C.RESET}", f"{device} / {compute}"],
            [f"{C.GOLD}Whisper Address{C.RESET}", f"{cfg['whisper_host']}:{cfg['whisper_port']}"],
            ["", ""],
            [
                f"{C.MAGENTA}Translation{C.RESET}",
                backend_info(cfg.get("translation_backend", "llamacpp")).get("name", "?"),
            ],
            [f"{C.MAGENTA}TL Address{C.RESET}", f"{cfg['translation_host']}:{cfg['translation_port']}"],
            [f"{C.MAGENTA}TL Model{C.RESET}", cfg.get("translation_model", "—")],
            ["", ""],
            [f"{C.YELLOW}YouTube Auth{C.RESET}", yt_auth],
            [f"{C.YELLOW}One-click start{C.RESET}", _one_click_label(cfg)],
        ],
        col_styles=[C.RESET, C.CYAN],
        title="Current Settings",
    )


def _export_config(cfg: dict) -> None:
    from config import config_export

    config_export(cfg)
    pause()


def _import_config(cfg: dict) -> None:
    """Import one of the last 5 backups (by number) or any file (by path)."""
    from config import config_import

    backups = sorted(BASE_DIR.glob("yume_config_backup_*.json"), reverse=True)
    if backups:
        info("Found backup files:")
        for i, b in enumerate(backups[:5]):
            bullet(f"{i + 1}. {b.name}")
        choice = ask_input("File number or path", "1")
        if choice.isdigit():
            idx = int(choice) - 1
            source = backups[idx] if 0 <= idx < len(backups) else None
        else:
            source = choice
    else:
        source = ask_input("Path to config file", "") or None
    imported = config_import(source) if source else None
    if imported:
        cfg.update(imported)
    pause()


def _reset_config(cfg: dict) -> None:
    from config import DEFAULT_CONFIG, save_config

    if ask_yn("Reset ALL settings?", False):
        fresh = {**DEFAULT_CONFIG, "first_run_complete": True}
        save_config(fresh)
        cfg.update(fresh)
        success("Reset!")
    pause()


def settings_menu(cfg: dict) -> None:
    """Full settings menu."""
    entries = [
        ("Whisper settings", "Speech recognition model, device, precision", _set_whisper),
        ("Translation settings", "Translation backend, address, model", _menu_backend),
        ("Server addresses", "Host/port for Whisper and Translation servers", _set_addrs),
        ("Translation prompt", "Customize how the AI translates (tone, style, rules)", _menu_translation_prompt),
        ("Romanization prompt", "Customize how the AI romanizes non-Latin text", _menu_romanization_prompt),
        ("YouTube auth", "How Yume accesses YouTube (Deno bot bypass or browser cookies)", _menu_yt_auth),
        ("One-click start", "Let the extension start/stop Yume by itself, auto-stop delay", menu_one_click),
        ("Export config", "Save settings to a backup file", _export_config),
        ("Import config", "Load settings from a backup file", _import_config),
        ("Reset to defaults", None, _reset_config),
    ]
    while True:
        header("Settings")
        _show_settings(cfg)
        ch = ask_arrow(
            "Change:", [(label, desc) for label, desc, _ in entries] + [("Back", None)], default=len(entries)
        )
        if not 0 <= ch < len(entries):
            return
        entries[ch][2](cfg)


def _pick_setting(cfg: dict, key: str, prompt: str, choices: list) -> None:
    """Ask for one of (value, label, description), with "Keep current" last; save a new choice."""
    from config import save_config

    opts = [(label, desc) for _, label, desc in choices] + [("Keep current", cfg[key])]
    ch = ask_arrow(prompt, opts, default=len(choices))
    if 0 <= ch < len(choices):
        cfg[key] = choices[ch][0]
        save_config(cfg)
        success("Saved!")
        pause()


def _set_whisper(cfg: dict) -> None:
    header("Whisper Settings")
    gpu = detect_gpu()
    rec_model, rec_reason = recommend_whisper_model(gpu)
    if gpu["has_nvidia"]:
        info(f"GPU: {gpu['name']} ({gpu['vram_mb']} MB VRAM)")
        info("  VRAM = Video RAM, the memory on your graphics card used by AI models")
    info(f"Current model: {C.BOLD}{cfg['whisper_model']}{C.RESET}")
    info(f"Recommendation: {rec_model} ({rec_reason})")

    ch = ask_arrow(
        "What to change:",
        [
            ("Whisper model", f"Currently: {cfg['whisper_model']} — the AI that converts speech to text"),
            ("Device (CPU/GPU)", f"Currently: {cfg['whisper_device']} — where the AI runs"),
            ("Precision", f"Currently: {cfg['whisper_compute_type']} — speed vs accuracy trade-off"),
            ("Back", None),
        ],
        default=3,
    )
    if ch == 0:
        _menu_whisper_model(cfg)
    elif ch == 1:
        _pick_setting(
            cfg,
            "whisper_device",
            "Where should Whisper run?",
            [
                ("auto", "Auto-detect", "Uses GPU if available, falls back to CPU"),
                ("cuda", "GPU (NVIDIA CUDA)", "Fastest — requires an NVIDIA graphics card"),
                ("cpu", "CPU", "Works on any computer, but slower"),
            ],
        )
    elif ch == 2:
        _pick_setting(
            cfg,
            "whisper_compute_type",
            "Precision (lower = faster but slightly less accurate):",
            [
                ("auto", "Auto", "Let Yume decide based on your hardware"),
                ("float16", "float16", "Full precision — best accuracy, needs ~4.5 GB VRAM on GPU"),
                ("int8_float16", "int8_float16", "Mixed — good balance, needs ~3 GB VRAM"),
                ("int8", "int8", "Most compressed — fastest, works well on CPU"),
            ],
        )


def _set_addrs(cfg: dict) -> None:
    while True:
        header("Server Addresses")
        info(f"Whisper:     {C.CYAN}{cfg['whisper_host']}:{cfg['whisper_port']}{C.RESET}")
        info(f"Translation: {C.CYAN}{cfg['translation_host']}:{cfg['translation_port']}{C.RESET}")
        ch = ask_arrow("Change:", [("Whisper address", None), ("Translation address", None), ("Back", None)], default=2)
        if ch == -1 or ch == 2:
            return
        elif ch == 0:
            _change_addr(cfg, "whisper")
        elif ch == 1:
            _change_addr(cfg, "translation")
