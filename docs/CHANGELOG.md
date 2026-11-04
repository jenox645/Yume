# Changelog

### Unreleased

- **True karaoke:** the Karaoke style now lights each word while it is sung, character by character in Japanese and Chinese, instead of sweeping the whole line at an even pace. The lines are heard as before, then their final text is aligned to the audio (`server/_align.py`, about 0.3 s of GPU per region) and the word times are cached with the video. Lines without them (whisper.cpp on AMD, videos cached before) keep the sweep. Arabic now lights up from the right, and a seek inside a line re-times it.
- **Setup in the new style:** five plain steps (This PC, What's missing, YouTube, Installing, Models) as aligned rows of what each part is for, by its plain name and its real one (faster-whisper, llama.cpp, GGUF), and whether it is ready, one install question instead of the list twice, and an ending that says Ready only when nothing is missing. It no longer says AMD cards can't run Whisper.
- **Setup recommends both models:** after the hardware scan it names the model that hears and the one that translates, picked for the PC (Hy-MT2-7B from 8 GB of VRAM, Hy-MT2-1.8B below and on the CPU), with the download size. One key takes them; "Choose myself" lists every Whisper model and opens the translator browser. The Whisper model now downloads during setup with a progress bar instead of silently at the first start. `python pocket_yume.py recommend` names both.
- **A first run makes its own Python environment:** `START_YUME` creates `yume-env` instead of installing into the system Python. An activated environment, or Yume already installed in the system Python, stays as it is.
- **Uninstall takes only what is Yume's:** it used to delete every folder in the HuggingFace cache with "faster-whisper" in its name, even another program's. Now the models Yume can download, the Demucs model, the yt-dlp and Deno caches and PyTorch are each asked about, and stay by default. It also removes the AMD Whisper models, and it ends by saying what is left to do by hand.
- **Whisper on AMD GPUs:** CTranslate2 has no AMD backend, so AMD cards now hear through whisper.cpp on Vulkan, on Windows and Linux. Setup installs it (`yume/whisper_cpp.py`), from a build Yume makes itself from one pinned upstream commit, with the quantised model (about 2 GB of VRAM). On an RTX 3060 it matched faster-whisper on the gold set: songs 14.9% on both, talks 12.2% against 11.1%, same speed. It has not run on a real AMD card yet. If Vulkan can't start, Whisper falls back to the CPU with a notice. NVIDIA stays on faster-whisper. Vocal isolation stays NVIDIA only. AMD cards on Linux are now found through the kernel, so ROCm is not needed.
- **Whisper on AMD without whisper.cpp:** Yume sent AMD cards on Linux to CUDA and saved a GPU precision that the CPU refuses too, so Whisper did not start. It now runs on the CPU with `int8`, a saved GPU precision is ignored and running setup again repairs it.
- **You are told when Whisper is on the CPU for a reason:** after a CUDA fallback, or with a medium or large model on the CPU, the launcher and the popup say so and what to do (`notice` in `/health`). `recommend` no longer treats a 16 GB PC that reports 15.8 GB as 8 GB. A server started by hand with a GPU precision on the CPU uses `int8`, and setup repairs that choice on NVIDIA too.
- **CUDA that cannot run no longer stops Whisper:** with no device or a broken driver the server falls back to the CPU and says why. The startup warm-up now really decodes: it never ran inference, so missing cuBLAS only showed up on the first video instead of moving Whisper to the CPU. The settings menu shows the precision that really runs, and picking the CPU with a GPU precision saved no longer stops Whisper either.

### v0.2.0

- **New look:** the popup is one window with tabs (Watch, Look, Models, More) that never scrolls past the browser's limit, in a light and a dark theme, with a brushed 夢 that draws itself while Yume wakes up. Six subtitle styles and three ways for a line to appear. The terminal is plain aligned rows, with Auto start in its main menu and progress bars while Yume starts.
- **Songs keep their lines:** Whisper turned the silence before a singer into one invented line and lost what came after. It now hears from the voice on: on our test songs, 44% fewer words lost. Song transcripts saved before this are heard again.
- **A second listen:** when everything heard in a section is rejected, or you blacklist what filled it, Yume hears that section again a different way instead of leaving a blank.
- **An empty subtitle window no longer covers the video,** and the popup says when Yume was updated while running.
- **New translator:** Hy-MT2-7B is recommended, one line at a time, with half the serious errors of Qwen2.5-7B in a blind reading. Hy-MT2-1.8B suits small cards.
- **Quieter GPU:** Yume keeps 40 to 80 seconds ready ahead of you and rests in between, and says so instead of showing a count that looks frozen. In one test the GPU was busy 34 s of 150 s, not 88 s.
- **Faster start on a video you resume:** the first line at 10:00 takes about 12 s. The preview is an ordinary download through a pipe, and yt-dlp keeps itself current.
- **Tested on what people wrote:** `tests/gold` scores Whisper and translators against human captions and lyrics. Several popular ideas failed it and were dropped; see `tests/gold/RESULTS.md`.
- **Live audits (October 2026):** real news, interviews and songs in Japanese, Korean, Chinese, Russian and Arabic were run through the server and every line read. Fixed: cut-off copies of the next line and lines written twice are shown once, also across a region cut and in History exports; a line too long to read (a 384-character rap verse on screen for 21 s) is cut into timed pieces at sentence, clause or word ends; loops of long phrases are caught, while sung stutters ("大大大大大嫌い") stay; "谢谢观看" is hidden; Japanese, Chinese and Korean lines are sent to the translator with their reading, so names come out right (21/21 instead of 1/7 in an A/B), and a reading copied back as the "translation" is rejected; one translation pasted onto several lines and English lines padded with the next line's words are retried; the LLM worker survives any error (every job used to stay at "translating"); a notice above the subtitles says when the video sounds like another language than the one picked; romaji for dates and counters (3月 "3 gatsu", 7月1日 "7 gatsu tsuitachi", 3つ mittsu), 菅義偉 Suga, ・ as a space; pinyin heteronyms (弹着 tán, 好好 hǎo hǎo, 亮著 zhe, 很长 cháng).
- **Extension:** the page sends a clean title (no "(2) … - YouTube") for the translation prompt, History and file names, also after YouTube's in-page navigation; "Clear blacklist" asks for a second click; exports are named after the video; downloads work in Firefox (the link was revoked too early); a tab opened before Yume was installed says to reload the page.
- **Server and CLI:** History progress no longer counts regions of an older plan (13 of 12, 108%); `/translation/test` answers JSON on any error; `status`, `health` and `fonts` no longer wait for Enter when run as commands; Windows font detection finds Microsoft YaHei (`msyh.ttc`); AMD VRAM on Linux was read as the memory in use.
- **Code health:** one server-process core for the launcher and the one-click supervisor (`yume/servers.py`), the server entry split into `_api`, `_settings`, `_model` and `_startup`, one constants module and a command table for the CLI; the most tangled functions (complexity up to 80) were split (none is above cyclomatic complexity 17 now), each checked against the old code on the same inputs, and CI fails on anything above grade C (20). A browser test (`npm run e2e`) drives the real extension against the real server code. 749 Python, 24 JavaScript and 14 browser tests.
- **Vocal isolation:** with PyTorch (CUDA) + demucs installed (Tools → Vocal Isolation, or the setup wizard on NVIDIA machines), each song's vocals are separated from the music before transcription (~8 s per 4-minute song). Measured against lyrics: 14.0% → 11.7% and 8.1% → 7.9% of characters wrong. Videos over 10 minutes and machines without CUDA transcribe the mix as before.
- **Multilingual live tests (Korean, Chinese, Russian, Arabic music videos, a 75-minute talk, a live stream, Bilibili):** lines Whisper invents for a stretch it hears no words in (one line stamped across its whole 30 s window — "字幕志愿者 李宗盛", "Субтитры сделал DimaTorzok", "한글자막 by …") are hidden in any language; a phrase said twice (きらきら, もっともっと) is no longer hidden as spam; the first section ends at a quiet point instead of splitting the first sung line at 30 s; lines no longer run past the end of the video; Korean romanization follows pronunciation (좋은 joeun, 감사합니다 gamsahamnida); live streams get a clear message instead of an ffmpeg error; a video resumed at 40:00 starts there without transcribing its first 30 s; Shorts, embeds and youtu.be links share the cache with watch?v= links.
- **Japanese romaji by word:** romanization now comes from a real morphological analysis (cutlet on MeCab + UniDic) instead of character-by-character readings: 囁いて is "sasayaite" (was "shou ite"), 掠れた "kasureta" (was "ryaku reta"), and words are no longer split mid-verb ("somaru", not "soma ru"). pykakasi remains the fallback.
- **`large-v3-turbo` is the default and recommended Whisper model** (measured: as accurate as `large-v3` on songs, twice as fast). Qwen3-ASR was evaluated and not adopted (less accurate on our songs, 4–10× slower).
- **CLI:** no crash when the output is redirected (`health > log.txt`); aligned tables. **Firefox:** declares that it collects no data (`web-ext lint` clean).
- **Fresh installs:** audio decoding failed with PyAV 19 (which a new install gets); Yume now converts audio with ffmpeg and pins PyAV 17. numpy is pinned per Python version (2.5.3 needs Python 3.12; 3.10 and 3.11 get the newest they support), and CI checks that the requirements install on Python 3.10–3.14. Dependencies updated (ESLint 10, GitHub Actions); Dependabot now sends one grouped PR per month.
- **Translation on the GPU without compiling anything:** Yume now installs llama.cpp's official prebuilt `llama-server` (CUDA build matched to your driver, Vulkan for AMD/Intel, Metal on Apple Silicon) instead of llama-cpp-python, whose GPU wheels lag behind new Python versions and usually ended up CPU-only. On an RTX 3060 a batch of 10 lines went from ~26 s to ~5 s; a whole song is translated in seconds. Existing llama-cpp-python setups keep working.
- **Live-test fixes (two real songs):** yt-dlp keeps itself up to date (a 7-month-old one got HTTP 403 on every video); unreadable browser cookies (Chrome/Brave/Edge on Windows) are tried once instead of six times, and the first-30-s stream preview retries without them; Whisper loops ("I don't want to lose you" ×5, a phrase repeated 150× in one line) are hidden/cut (decided on the raw line: a line that is only a loop, like a 30-second "Azumoto-Azumoto-…" over an instrumental intro, is hidden); the first lines and the lines near the playhead are translated in small batches so they are ready in seconds, and the translator prompt keeps a stable prefix for llama.cpp's cache; the progress badge says what it counts ("Listening 3/11", "Translating 11/32"); ALL-CAPS translations are sentence-cased; romaji doubles consonants after っ (hashitte, not "hashitsu te"); switching videos no longer flashes the previous video's subtitles; the timing offset now goes the direction the popup says (+ = later); blacklisting from the popup hides the line at once; missing server packages are reported by the health check and the launcher.
- **Audit fixes:** credits filter no longer hides real lines containing words like "video", "mix", "piano" or "作曲"; the launcher only kills a Python process on its ports when it is really a Yume server; a missing cuBLAS/cuDNN is detected at startup (CPU fallback) instead of every section failing; switching the Whisper model from the popup is remembered; pressing Enable again retries sections that failed; long downloads no longer retry 5× after a timeout; Japanese/other non-ASCII yt-dlp output no longer breaks downloads on Windows; the PO-token helper no longer stalls after a while; a corrupt cache database is set aside instead of stopping the server; `python pocket_yume.py settings` (suggested in many hints) now exists.
- **One-click start:** press Enable in the extension and Yume starts by itself in the background (native messaging helper, registered by the setup wizard or `python pocket_yume.py autostart on`); it stops after 30 idle minutes. New `serve` / `stop` commands, popup Start/Stop buttons. The extension now has a fixed ID (manifest `key`).
- **The pipeline now runs on the server.** The extension creates a job and polls it; the server downloads, transcribes, translates and romanizes. Removed ~3,800 lines of extension code (chunk scheduling, overlap de-duplication, three translation retry layers, client-side caches).
- **No more 5 s overlap:** audio is cut into ~25 s sections at quiet points, so there are no duplicated boundary lines and no de-duplication heuristics.
- **Structured translation:** batches use a JSON schema with exactly N lines (llama.cpp, Ollama 0.5+, LM Studio); falls back to numbered lines for older backends. Ollama/LM Studio now receive the model name (Ollama translation never worked before).
- **Durable cache:** transcripts and translations live in `config/yume_cache.db` — they survive restarts and are shared by every browser. The History panel lists them.
- **One blacklist:** the server holds it; the popup and the CLI edit the same list, and edits apply to subtitles already on screen.
- **Fixes:** long Russian/Korean/Arabic lines were dropped as "hallucinations"; switching language replayed the old language's subtitles; a failed model load showed "loading" forever; prompts told the model "never output Japanese" when translating *to* Japanese; English-only distil models were recommended; the launcher could kill unrelated programs on its ports; the subtitle CSS leaked into every website; interrupted model downloads left corrupt files; multi-GPU NVIDIA systems were detected as CPU-only; `wmic` (removed from Windows 11) was used for detection.
- **One manifest** for Chrome, Edge, Brave and Firefox.

### v0.1.0

- Subtitles now work in fullscreen (window reparents into the fullscreen container); text shadow keeps subtitles readable in glass mode over bright video.
- Pipeline overlap: Whisper transcribes chunk N+1 while the LLM translates chunk N (~25-40% faster end-to-end).
- History panel: fully processed videos are kept for 30 days and restore instantly on reopen; per-entry SRT export. WebVTT export added alongside SRT.
- Popup QoL: shows the actual toggle shortcut (Alt+Y by default); explains when a page can't run Yume (chrome:// pages, Web Store) or has no video; actionable hint when servers are offline; font list follows the selected source language.
- Enabling subtitles now works when the video appeared after page load (SPA navigation) instead of silently showing nothing.
- First-run onboarding: setup wizard ends with an extension install guide; START_YUME.bat hardened (Run-as-administrator, missing Python).
- Server fixes: temp-dir leak in per-chunk fallback, serialized model switching, bounded audio cache, hallucination filter no longer drops real lyrics containing "like/share/comment/follow".

### v0.0.9

- CLI: arrow key navigation (`ask_arrow`) with simultaneous number-key jump support; full-screen clear on every menu transition; Unicode `─` separators.
- Server: lazy Whisper model loading in background thread (server responds immediately, `/health` returns `loading` until ready); model-loading thread runs at reduced priority to prevent PC stutter; transcription returns 503 while loading.
- Log rotation: pre-open 5 MB rotation (3 backups) for both server logs; removed broken RotatingFileHandler approach.
- Health check: all checks run in parallel via `ThreadPoolExecutor`; system status hardware cache (30 s TTL).
- Setup wizard: step indicators (`Step N/M`), retry/skip on install failure, no raw Python tracebacks.
- Bug fixes: large-v3-turbo VRAM corrected to ~6 GB; `-q` flag consistency in installer; empty badge link removed.
- Tests: 127 tests passing. SonarCloud: zip-slip guard, timing-safe token compare, URL validation.

### v0.0.8

- Fixed "Ready" before subtitles exist, URL blocking `&`, first 30s skipped, hallucination filter dropping real lyrics, chunk badge wrong count, pipeline stopping 1-2 chunks early.
- Improved speech detection after silence (`no_speech_threshold` 0.3). Faster server startup (background thread). Parallel translate+romanize (`Promise.all`).
- Security: removed `shell=True`/`os.system()`/`curl|sh`, XSS fixes, pinned deps. CI: 7 jobs, ESLint, Dependabot, pre-commit hooks. Full Ruff cleanup (470 violations).

### v0.0.7

- Reset version scheme to proper semver (was inflated to 5.x for a pre-alpha project).
- Fixed Whisper forced to CPU when config said `auto`; CLI-resolved GPU now takes priority.
- Fixed temp directory leak on failed downloads, partial downloads leaving corrupt files.
- Fixed XSS in popup diagnostics via unescaped `entry.details`.
- Fixed translation cache serving stale results after model switch (added 30-min TTL).
- Fixed `_slice_audio` crash if source audio was deleted mid-operation.
- Fixed `UnicodeDecodeError` on non-UTF-8 model metadata.
- Added `SECURITY.md`, PR template, AST-based import completeness tests.
- Added setup wizard installation summary with per-component pass/fail.
- Prewarm now detects CUDA library failures and falls back to CPU automatically.

### v0.0.6

- Fixed update check freezing the menu when offline (now runs in a background thread).
- Fixed disk space check missing before large downloads.
- Fixed invalid JSON config crashing silently (now warns and falls back to defaults).
- Fixed corner radius wrongly tied to the Glass Effect toggle.
- Fixed translation server status showing incorrect state when busy (socket fallback).
- Settings now show the resolved device (e.g. `cuda (auto)` instead of just `auto`).

### v0.0.5

- Fixed server startup crash (`import logging` missing).
- Fixed `_get_audio_duration` removed by accident; restored to fix `/prepare` failures.
- Fixed stream URL cache never evicted; max size now enforced.
- Fixed excessive werkzeug logging in non-verbose mode.
- Server stats now track cache misses; ffmpeg availability checked on startup.

### v0.0.4

- Fixed YouTube auth — proper Deno PO token support via bgutil server (port 4416).
- Fixed stale API token after server restart — extension now auto-recovers on 403.
- Fixed auto-detect GPU choosing CPU even with NVIDIA present (uses CTranslate2 detection).
- Default YouTube auth switched from `deno` to `cookies`.

### v0.0.3

- Added translation prompt editor and romanization prompt editor to settings.
- Added config export/import with timestamped backups.

### v0.0.2

- Fixed translation silently empty — batch parser dropped translations without `[N]` markers.
- Fixed server caching empty transcription results forever.
- Fixed session storage restoring stale chunks on page reload.
- Added integration tests, dead config detection test, GitHub CI, issue templates.
- Added `--version` CLI flag; diagnostics now show `[cached]` tag.

### v0.0.1

- Initial public release.
- Music-optimized Whisper (VAD off, pause threshold 0.25s), RTL Arabic support, batch translation, parallel pipeline, Glass effect subtitle window.
- Cross-platform CLI with setup wizard, GPU auto-detection, tool installers.
- Per-session API token, DNS rebinding protection, URL sanitization.
