"""In-CLI cookbook — task-oriented recipes for common goals.

The other menus tell users what Yume CAN do; this menu tells them HOW to get
an outcome, end to end, without reading the README. Keep each guide short
(one screen), numbered, and pointing at the exact menu/button names.
"""

from __future__ import annotations

from yume.hardware import detect_gpu, recommend_whisper_model
from yume.ui import C, ask_arrow, bullet, header, info, pause, section
from yume.utils import EXT_DIR, LOGS_DIR


def _steps(*lines: str) -> None:
    """Print numbered recipe steps with consistent indentation."""
    print()
    for i, line in enumerate(lines, 1):
        print(f"    {C.BOLD}{i}.{C.RESET} {line}")
    print()


def guides_menu(cfg: dict) -> None:
    """Cookbook menu: pick a goal, get a recipe."""
    while True:
        header("Help & Guides")
        info("Pick a goal — each guide is a short step-by-step recipe.")
        ch = ask_arrow(
            "What do you want to do?",
            [
                ("Get subtitles on a video", "The full path from zero to subtitles, in 4 steps"),
                ("Install the browser extension", "Load Yume into Chrome, Edge, Brave or Firefox"),
                ("Fix YouTube download problems", "When audio won't download: update, cookies, Deno, stream URL"),
                ("Pick the right models for your PC", "Whisper sizes and translation model quantization"),
                ("Improve translation quality", "Bigger models, prompt styles, hallucination filter"),
                ("Save subtitles to a file", "Export SRT / WebVTT from the extension"),
                ("Fix common problems", "No subtitles, red server dots, port conflicts, logs"),
                ("Back", None),
            ],
            default=7,
        )
        if ch in (-1, 7):
            return
        [
            _guide_quickstart,
            _guide_extension,
            _guide_youtube,
            _guide_models,
            _guide_quality,
            _guide_export,
            _guide_troubleshoot,
        ][ch](cfg)


def _guide_quickstart(cfg: dict) -> None:
    header("Get Subtitles on a Video")
    info("From nothing to live subtitles:")
    _steps(
        f"Start the servers: main menu → {C.BOLD}Launch Yume{C.RESET}  "
        f"{C.DIM}(or: python pocket_yume.py launch){C.RESET}\n"
        f"       {C.DIM}Keep that window open — it IS Yume. First launch downloads the model.{C.RESET}",
        f"Load the browser extension {C.DIM}(once){C.RESET} — see the "
        f"{C.BOLD}Install the browser extension{C.RESET} guide.",
        f"Open any video page, click the {C.BOLD}Yume icon{C.RESET} in the toolbar, press "
        f"{C.BOLD}Enable{C.RESET}  {C.DIM}(or Alt+Y on the page){C.RESET}.",
        "Wait 10-30 seconds — subtitles appear in a draggable window over the video.",
    )
    info(f"{C.DIM}Pick source language and translation language in the extension popup first{C.RESET}")
    info(f"{C.DIM}if the video isn't Japanese → English (the defaults).{C.RESET}")
    print()
    info(f"{C.DIM}Fully processed videos are saved for 30 days — reopening one shows{C.RESET}")
    info(f"{C.DIM}subtitles instantly with no GPU work (popup → History).{C.RESET}")
    pause()


def _guide_extension(cfg: dict) -> None:
    header("Install the Browser Extension")
    # The setup wizard's guide is already the best version of this text —
    # reuse it (function-level import: yume.setup imports from yume.menus).
    from yume.setup import _extension_guide

    _extension_guide()
    pause()


def _guide_youtube(cfg: dict) -> None:
    header("Fix YouTube Download Problems")
    info('Symptom: subtitles never start, popup or window shows "Failed to download audio".')
    info("Try these in order — the first one fixes it most of the time:")
    _steps(
        f"{C.BOLD}Update yt-dlp{C.RESET} — YouTube changes constantly; old versions break.\n"
        f"       {C.DIM}Tools & Fonts → yt-dlp → Install / Update{C.RESET}",
        f"{C.BOLD}Check your login (cookies method){C.RESET} — Yume borrows your browser's YouTube\n"
        f"       login. Be logged into YouTube in the browser set in Settings → YouTube auth.\n"
        f"       {C.DIM}Currently: {cfg.get('youtube_auth_method', 'cookies')}"
        + (
            f" ({cfg.get('cookies_browser', 'chrome')})"
            if cfg.get("youtube_auth_method", "cookies") == "cookies"
            else ""
        )
        + f"{C.RESET}",
        f"{C.BOLD}Switch to Deno{C.RESET} — solves YouTube's bot challenge without any account.\n"
        f"       {C.DIM}Settings → YouTube auth → Deno (downloads ~35 MB once){C.RESET}",
        f"{C.BOLD}Last resort: paste the stream URL directly{C.RESET} — works on any site yt-dlp\n"
        f"       can't handle. In the video tab: DevTools (F12) → Network → filter \"m3u8\" →\n"
        f"       copy the URL → extension popup → Server Settings → Custom Stream URL.",
    )
    pause()


def _guide_models(cfg: dict) -> None:
    header("Pick the Right Models for Your PC")
    gpu = detect_gpu()
    rec_model, rec_reason = recommend_whisper_model(gpu)
    if gpu["has_nvidia"]:
        info(f"Your GPU: {C.GREEN}{gpu['name']}{C.RESET} ({gpu['vram_mb']} MB VRAM)")
    else:
        info(f"Your hardware: {C.YELLOW}CPU mode{C.RESET} — prefer the smaller models below.")
    info(f"Recommended Whisper model: {C.BOLD}{rec_model}{C.RESET}  {C.DIM}({rec_reason}){C.RESET}")

    section("Whisper (speech → text) — accuracy vs. VRAM")
    bullet(f"tiny / base      ~1 GB   {C.DIM}fastest, weakest — okay for clear speech{C.RESET}")
    bullet(f"small            ~2 GB   {C.DIM}decent for podcasts and vlogs{C.RESET}")
    bullet(f"distil-large-v3  ~4 GB   {C.DIM}near large quality, half the size{C.RESET}")
    bullet(f"large-v3-turbo   ~6 GB   {C.DIM}best speed/quality balance on 8 GB GPUs{C.RESET}")
    bullet(f"large-v3         ~10 GB  {C.DIM}most accurate — music, mumbling, noise{C.RESET}")
    info(f"{C.DIM}Switch any time: Settings → Whisper settings, or live from the runtime menu.{C.RESET}")

    section("Translation model (GGUF) — quality vs. VRAM")
    bullet(f"7B  Q4_K_M  ~4.4 GB → fits {C.GREEN}6 GB{C.RESET} VRAM  {C.DIM}(good default: Qwen2.5-7B){C.RESET}")
    bullet(f"7B  Q8_0    ~7.7 GB → needs {C.YELLOW}8 GB{C.RESET} VRAM  {C.DIM}(slightly better, 2x size){C.RESET}")
    bullet(f"14B Q4_K_M  ~8.7 GB → needs {C.YELLOW}10 GB{C.RESET} VRAM  {C.DIM}(noticeably better translations){C.RESET}")
    info(f"{C.DIM}Rule of thumb: pick the biggest model that fits your VRAM with ~1 GB spare,{C.RESET}")
    info(f"{C.DIM}and prefer Q4_K_M quantization. Download: Tools & Fonts → Download Translation Model.{C.RESET}")
    print()
    info(f"{C.DIM}Note: Whisper and the translation model share the GPU — their VRAM adds up.{C.RESET}")
    pause()


def _guide_quality(cfg: dict) -> None:
    header("Improve Translation Quality")
    info("In order of impact:")
    _steps(
        f"{C.BOLD}Set the source language explicitly{C.RESET} in the extension popup —\n"
        f"       auto-detect costs accuracy, especially for songs.",
        f"{C.BOLD}Use a bigger translation model{C.RESET} if your VRAM allows (7B → 14B is a big\n"
        f"       jump). See the {C.BOLD}Pick the right models{C.RESET} guide.",
        f"{C.BOLD}Tune the translation prompt{C.RESET} — Settings → Translation prompt has ready\n"
        f"       templates: keep anime honorifics, poetic song lyrics, formal, casual...",
        f"{C.BOLD}Report hallucinations{C.RESET} — phantom lines like \"Thanks for watching\":\n"
        f"       popup → Hallucination Filter → Report Current Subtitle → Update Server.",
        f"{C.BOLD}Use a larger Whisper model{C.RESET} — bad translations often start as bad\n"
        f"       transcriptions. large-v3 hears music and noisy audio far better than small.",
    )
    pause()


def _guide_export(cfg: dict) -> None:
    header("Save Subtitles to a File")
    info("While (or after) watching a video with subtitles enabled:")
    _steps(
        f"Click the {C.BOLD}Yume icon{C.RESET} → open the {C.BOLD}Diagnostics{C.RESET} section.",
        f"Click {C.BOLD}Export SRT{C.RESET} (players, editors) or {C.BOLD}Export VTT{C.RESET} (web players).",
        "The file downloads with original text + translation + romanization lines.",
    )
    info(f"{C.DIM}Older videos: popup → History lists everything processed in the last 30 days —{C.RESET}")
    info(f"{C.DIM}each entry has its own SRT button, no need to reopen the video.{C.RESET}")
    print()
    info(f"{C.DIM}Tip: export reflects processed chunks — let the ✓ appear in the subtitle{C.RESET}")
    info(f"{C.DIM}window's counter to capture the whole video.{C.RESET}")
    pause()


def _guide_troubleshoot(cfg: dict) -> None:
    header("Fix Common Problems")

    section("No subtitles after clicking Enable")
    bullet("Reload the video page (F5) — the extension loads on page load.")
    bullet(
        f"Check the servers are running: main menu shows {C.BOLD}Servers{C.RESET} status, "
        f"or run {C.CYAN}python pocket_yume.py status{C.RESET}."
    )
    bullet("Make sure you clicked Enable on the tab that has the video (not another window).")

    section("Red dots in the extension popup")
    bullet(f"Servers not started — use {C.BOLD}Launch Yume{C.RESET} and keep the window open.")
    bullet(
        f"Wrong ports — popup → Server Settings must match "
        f"{C.CYAN}{cfg['whisper_host']}:{cfg['whisper_port']}{C.RESET} (Whisper) and "
        f"{C.CYAN}{cfg['translation_host']}:{cfg['translation_port']}{C.RESET} (translation)."
    )

    section("Port already in use at launch")
    bullet(f"See what's using it: {C.CYAN}python pocket_yume.py ports{C.RESET}")
    bullet("Launch offers to free the port or pick another automatically.")

    section("Still stuck? Look at the evidence")
    bullet(f"Full diagnostic: {C.CYAN}python pocket_yume.py health{C.RESET} — tells you exactly what to fix.")
    bullet(f"Server logs: {C.DIM}{LOGS_DIR}{C.RESET} (or runtime menu → View Logs).")
    bullet("Extension log: popup → Diagnostics → Refresh Log / Download Log.")
    bullet(f"Extension files live in: {C.DIM}{EXT_DIR}{C.RESET}")
    pause()
