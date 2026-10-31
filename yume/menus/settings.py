"""Settings menu: Whisper, translation, prompts, addresses."""

from __future__ import annotations

import logging

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
    section,
    success,
    table,
)

from yume.menus.server import _menu_whisper_model
from yume.menus.one_click import menu_one_click
from yume.menus.translation import _change_addr, _menu_backend
from yume.menus.youtube_auth import _menu_yt_auth

_log = logging.getLogger("pocket_yume")


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
    if ch == -1 or ch == 3:
        return
    elif ch == 0:
        _menu_whisper_model(cfg)
    elif ch == 1:
        dc = ask_arrow(
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
        cc = ask_arrow(
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

    # Starting point for a custom prompt. The built-in prompt (server/_translate.py)
    # also adds script rules, the video title and the previous lines as context.
    default_prompt = (
        "You translate {src} subtitle lines into {tgt}. Translate each line on its own and keep "
        "the order. Output {tgt} only. Do not explain, do not answer questions in the text."
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
        info(
            f"{C.BOLD}Using the built-in prompt{C.RESET} {C.DIM}(adapts its script rules to each language pair){C.RESET}"
        )
        info(f"  {C.CYAN}{default_prompt}{C.RESET}")

    print()
    ch = ask_arrow(
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
            success("Prompt saved! A running server picks it up automatically.")
        pause()
    elif ch == 1:
        cfg["translation_prompt"] = ""
        save_config(cfg)
        success("Reset to the built-in prompt.")
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
    info(f"  {C.DIM}Korean    → built-in Revised Romanization (instant, ignores this prompt){C.RESET}")
    info(f"  {C.DIM}Russian   → built-in BGN/PCGN transliteration (instant, ignores this prompt){C.RESET}")
    info(f"  {C.DIM}Arabic    → your translation LLM, with this prompt{C.RESET}")
    info(f"  {C.DIM}Without pykakasi/pypinyin installed, Japanese/Chinese also use the LLM.{C.RESET}")
    print()

    info(f"{C.BOLD}Available placeholders:{C.RESET}")
    info(f"  {C.DIM}{{src}} → source language name (e.g. 'Russian', 'Japanese'){C.RESET}")
    info(f"  {C.DIM}{{sys}} → romanization system name (e.g. 'transliteration', 'romaji'){C.RESET}")
    print()

    defaults = {
        "ar": "You transliterate Arabic text into Latin letters. Do NOT translate. Write how each line is pronounced, nothing else.",
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
    ch = ask_arrow(
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
            success("Romanization prompt saved! A running server picks it up automatically.")
        pause()
    elif ch == 1:
        cfg["romanization_prompt"] = ""
        save_config(cfg)
        success("Reset to the built-in prompt.")
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
        ch = ask_arrow("Change:", [("Whisper address", None), ("Translation address", None), ("Back", None)], default=2)
        if ch == -1 or ch == 2:
            return
        elif ch == 0:
            _change_addr(cfg, "whisper")
        elif ch == 1:
            _change_addr(cfg, "translation")
