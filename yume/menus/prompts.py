"""Prompt editors: custom instructions for the translation and romanization LLM."""

from __future__ import annotations

from yume.ui import C, ask_arrow, ask_input, header, info, pause, section, success

# Starting point for a custom prompt. The built-in prompt (server/_translate.py)
# also adds script rules, the video title and the previous lines as context.
_TRANSLATION_STARTER = (
    "You translate {src} subtitle lines into {tgt}. Translate each line on its own and keep "
    "the order. Output {tgt} only. Do not explain, do not answer questions in the text."
)
_ROMANIZATION_BUILTIN = {
    "ar": "You transliterate Arabic text into Latin letters. Do NOT translate. Write how each line is pronounced, nothing else.",
}
_TRANSLATION_EXAMPLES = [
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
_ROMANIZATION_EXAMPLES = [
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


def _store(cfg: dict, key: str, value: str) -> None:
    from config import save_config

    cfg[key] = value
    save_config(cfg)


def _show_custom(current: str) -> None:
    info(f"{C.BOLD}Current custom prompt:{C.RESET}")
    info(f"  {C.CYAN}{current}{C.RESET}")


def _edit(cfg: dict, key: str, hints: tuple, starter: str, saved: str) -> None:
    for hint in hints:
        info(hint)
    offered = cfg.get(key, "") or starter
    new_prompt = ask_input("New prompt", offered)
    # Enter returns what was offered: the starter must not replace the built-in prompt
    if new_prompt and new_prompt != offered:
        _store(cfg, key, new_prompt)
        success(saved)
    pause()


def _use_example(cfg: dict, key: str, examples: list) -> None:
    section("Example Prompts")
    for i, (name, prompt) in enumerate(examples):
        info(f"  {C.BOLD}{i + 1}. {name}{C.RESET}")
        info(f"     {C.DIM}{prompt}{C.RESET}")
        print()
    choice = ask_input("Use which? (number, or press Enter to go back)", "")
    if choice.strip().isdigit():
        idx = int(choice.strip()) - 1
        if 0 <= idx < len(examples):
            _store(cfg, key, examples[idx][1])
            success(f"Prompt set to: {examples[idx][0]}")
    pause()


def _prompt_actions(
    cfg: dict, key: str, descs: tuple, hints: tuple, saved: str, examples: list, starter: str = ""
) -> None:
    """The options both editors share. descs: what Edit and Reset do; hints: shown before editing."""
    print()
    ch = ask_arrow(
        "Options:",
        [
            ("Edit prompt", descs[0]),
            ("Reset to default", descs[1]),
            ("View example prompts", "See templates for different styles"),
            ("Back", None),
        ],
        default=3,
    )
    if ch == 0:
        _edit(cfg, key, hints, starter, saved)
    elif ch == 1:
        _store(cfg, key, "")
        success("Reset to the built-in prompt.")
        pause()
    elif ch == 2:
        _use_example(cfg, key, examples)


def _menu_translation_prompt(cfg: dict) -> None:
    """Edit the system prompt that controls how the AI translates subtitles."""
    header("Translation Prompt Editor")

    info("The translation prompt is the instruction sent to the AI before every subtitle.")
    info(f"{C.DIM}It controls how the translator behaves — its tone, style, and rules.{C.RESET}")
    print()

    info(f"{C.BOLD}Why the default prompt is written this way:{C.RESET}")
    info(f"  {C.DIM}• 'Output {{tgt}} only'{C.RESET}")
    info(f"    {C.DIM}  → Prevents the AI from adding commentary or notes{C.RESET}")
    info(f"  {C.DIM}• 'Do not answer questions in the text'{C.RESET}")
    info(f"    {C.DIM}  → Stops the AI from answering questions it hears in the audio{C.RESET}")
    info(f"    {C.DIM}  → e.g. if someone says 'What time is it?', it translates, not answers{C.RESET}")
    info(f"  {C.DIM}• 'Do not explain'{C.RESET}")
    info(f"    {C.DIM}  → Prevents output like 'This means: ...' or 'Note: ...'{C.RESET}")
    info(f"  {C.DIM}• Short, assertive rules{C.RESET}")
    info(f"    {C.DIM}  → Work best with small local AI models (7B-13B parameters){C.RESET}")
    info(f"  {C.DIM}• {{src}} and {{tgt}} are placeholders{C.RESET}")
    info(f"    {C.DIM}  → Replaced with actual language names (e.g. Japanese, English){C.RESET}")
    print()

    current = cfg.get("translation_prompt", "")
    if current:
        _show_custom(current)
    else:
        info(
            f"{C.BOLD}Using the built-in prompt{C.RESET} {C.DIM}(adapts its script rules to each language pair){C.RESET}"
        )
        info(f"  {C.CYAN}{_TRANSLATION_STARTER}{C.RESET}")

    _prompt_actions(
        cfg,
        "translation_prompt",
        ("Write your own translation instruction", "Restore the built-in prompt"),
        (
            "Use {src} for source language and {tgt} for target language.",
            f"{C.DIM}Example: 'Translate {{src}} to {{tgt}}. Keep it casual.'{C.RESET}",
        ),
        "Prompt saved! A running server picks it up automatically.",
        _TRANSLATION_EXAMPLES,
        starter=_TRANSLATION_STARTER,
    )


def _menu_romanization_prompt(cfg: dict) -> None:
    """Edit the system prompt that controls how the AI romanizes text."""
    header("Romanization Prompt Editor")

    info("This prompt controls how the AI converts non-Latin text to Latin characters.")
    print()
    info(f"{C.BOLD}How romanization works per language:{C.RESET}")
    info(f"  {C.DIM}Japanese  → cutlet, or pykakasi without it (instant, ignores this prompt){C.RESET}")
    info(f"  {C.DIM}Chinese   → pypinyin library (instant, ignores this prompt){C.RESET}")
    info(f"  {C.DIM}Korean    → built-in Revised Romanization (instant, ignores this prompt){C.RESET}")
    info(f"  {C.DIM}Russian   → built-in BGN/PCGN transliteration (instant, ignores this prompt){C.RESET}")
    info(f"  {C.DIM}Arabic    → your translation LLM, with this prompt{C.RESET}")
    info(f"  {C.DIM}Without those libraries installed, Japanese/Chinese also use the LLM.{C.RESET}")
    print()

    info(f"{C.BOLD}Available placeholders:{C.RESET}")
    info(f"  {C.DIM}{{src}} → source language name (e.g. 'Russian', 'Japanese'){C.RESET}")
    info(f"  {C.DIM}{{sys}} → the word 'romanization'{C.RESET}")
    print()

    current = cfg.get("romanization_prompt", "")
    if current:
        _show_custom(current)
    else:
        info(f"{C.BOLD}Using built-in per-language prompts:{C.RESET}")
        for lang, prompt in _ROMANIZATION_BUILTIN.items():
            info(f"  {C.DIM}{lang}: {prompt[:70]}...{C.RESET}")

    _prompt_actions(
        cfg,
        "romanization_prompt",
        ("Write your own romanization instruction", "Restore the built-in per-language prompts"),
        (
            "Use {src} for source language and {sys} for the word 'romanization'.",
            f"{C.DIM}Example: 'Convert {{src}} to Latin script using {{sys}}. Output ONLY the result.'{C.RESET}",
        ),
        "Romanization prompt saved! A running server picks it up automatically.",
        _ROMANIZATION_EXAMPLES,
    )
