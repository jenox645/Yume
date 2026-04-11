<div align="center">

![Yume Banner](https://github.com/user-attachments/assets/8e3c1813-b1f6-45aa-9f5e-c720842eb477)

# Yume

**Real-time AI subtitles for any video — fully local, no cloud APIs.**

Transcription · Translation · Romanization

![Version](https://img.shields.io/badge/version-0.0.9-blue)
[![Python](https://img.shields.io/badge/python-3.10+-green)]()
[![Chrome](https://img.shields.io/badge/chrome-MV3-yellow)]()
[![Firefox](https://img.shields.io/badge/firefox-MV3-orange)]()
[![Stars](https://img.shields.io/github/stars/jenox645/Yume?style=flat-square)](https://github.com/jenox645/Yume/stargazers)
[![Last Commit](https://img.shields.io/github/last-commit/jenox645/Yume?style=flat-square)](https://github.com/jenox645/Yume/commits/main)
[![License](https://img.shields.io/badge/license-MIT-lightgrey)](LICENSE)

</div>

---

https://github.com/user-attachments/assets/48ee3790-7635-4321-8246-308689e53210

Yume captures audio from any video in your browser, transcribes it with [faster-whisper](https://github.com/SYSTRAN/faster-whisper), translates it with a local LLM, and overlays subtitles. Everything runs on your machine — no API keys, no subscriptions, no data leaves your computer.

**Tested sites:** YouTube, NicoNico, Bilibili, Twitch, Crunchyroll. Other sites may work via [yt-dlp](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md), but are not guaranteed — authentication, bot protection, and DRM vary widely.

**Source languages:** Japanese · Chinese · Korean · Russian · Arabic

> **Note:** This is an early-stage personal project. Expect rough edges. Contributions and bug reports are welcome.

---

| ![Image1](https://github.com/user-attachments/assets/01b99864-e46c-4406-8a3e-b64a28d45541) | ![Image2](https://github.com/user-attachments/assets/d38c6e95-137d-4d6b-b190-18d6d638bd7b) |
|:---:|:---:|
| ![Image3](https://github.com/user-attachments/assets/a8a594fe-eb61-4928-b6d0-ace13da21584) | ![Image4](https://github.com/user-attachments/assets/31cfe65e-4877-44b7-a059-c6d10013ee25) |

---

## Quick Start

**1. Launch Yume**

| Platform | Command |
|----------|---------|
| Windows  | Double-click `START_YUME.bat` |
| Linux    | `./START_YUME.sh` |
| macOS    | Double-click `START_YUME.command` |

The setup wizard runs on first launch — detects your hardware, installs dependencies (yt-dlp, FFmpeg, faster-whisper, translation model), and configures everything.

**2. Install the Extension**

Chrome, Brave, Edge:
1. Open `chrome://extensions`
2. Enable **Developer Mode** (top-right toggle)
3. Click **Load unpacked** → select the `extension/` folder
4. Pin the Yume icon in the toolbar

Firefox: handled by the setup wizard. <!-- TODO: integrate Firefox setup into wizard -->

**3. Watch**

1. Go to any video with speech
2. Click the Yume icon → **Enable**
3. Wait for the audio to download, then subtitles appear automatically
4. Press **Alt+Y** to toggle without opening the popup

> **Heads up:** Yume downloads the video's audio before transcribing. For a 4-minute video on a decent connection, expect ~10–20 seconds before the first subtitle appears. Longer videos take proportionally longer.

---

## How It Works

```mermaid
graph TD

    %% MAIN FLOW
    A([Extension Start]) --> B["Get video URL + settings<br/>background.js"]
    B --> C["Capture audio stream<br/>audio-capture.js"]
    C --> D["Whisper Server (5001)<br/>Transcribe audio"]
    D --> E{"Text received?"}

    %% DECISION AS NODES
    E --> Y([Yes]) --> F["Translate via LLM (5000)<br/>Translation server"]
    E --> N([No]) --> G["Mark empty"]

    F --> H["Send subtitles to overlay<br/>subtitle-window.js"]
    G --> H

    H --> M{"More audio<br/>chunks?"}

    M --> Y2([Yes]) --> C
    M --> N2([No]) --> J([Done])

    %% PARALLEL PATH AS NODE
    C -.-> P([Parallel]) -.-> K["Transcribe next chunk"]
    K -.-> H

    %% COLORS
    style A fill:#f59e0b,stroke:#d97706,color:#fff
    style B fill:#60a5fa,stroke:#2563eb,color:#fff
    style C fill:#3b82f6,stroke:#1d4ed8,color:#fff
    style D fill:#7c3aed,stroke:#5b21b6,color:#fff
    style F fill:#16a34a,stroke:#15803d,color:#fff
    style G fill:#94a3b8,stroke:#475569,color:#fff
    style H fill:#4ade80,stroke:#16a34a,color:#fff
    style J fill:#22c55e,stroke:#16a34a,color:#fff

    %% Decision Nodes (Yes, No, Parallel)
    style Y fill:#0284c7,stroke:#0369a1,color:#fff
    style N fill:#475569,stroke:#334155,color:#fff
    style Y2 fill:#0284c7,stroke:#0369a1,color:#fff
    style N2 fill:#475569,stroke:#334155,color:#fff
    style P fill:#0ea5e9,stroke:#0369a1,color:#fff
```

To reduce perceived latency, Yume transcribes chunk N+1 while translating chunk N — Whisper and the LLM run in parallel rather than sequentially.

---

## Benchmark

Real-world numbers on an **RTX 3060 12 GB VRAM** (a mid-range card):

| Step | Time | Details |
|------|------|---------|
| Whisper model load | ~15 s | `large-v3` (~3 GB download, ~10 GB VRAM) — one-time on launch |
| Translation model load | ~12 s | ~10 GB GGUF file — one-time on launch |
| Chunk (dialogue-heavy) | ~25 s | 30 s of audio with dense speech: transcribe + translate + romanize |

After both models are loaded, a typical chunk with moderate dialogue processes in under 25 seconds. Chunks with silence or sparse speech are faster. The parallel pipeline means chunk N+1 is already being transcribed while chunk N is being translated, so perceived delay is lower than the raw per-chunk time.

Smaller models are significantly faster — a 3B translation model and `small` Whisper cut per-chunk time roughly in half, at the cost of some accuracy. Use `python pocket_yume.py benchmark` to measure your own hardware, and `python pocket_yume.py recommend` to get a model suggestion based on your GPU.

---

## Translation Models

| Model | Size | Speed | Quality | Best for |
|-------|------|-------|---------|----------|
| Qwen2.5-3B-Q6 | ~2.5 GB | Fast | Good | Low VRAM, faster subtitles |
| Mistral-7B-Q4 | ~4.5 GB | Medium | Very good | General use |
| Qwen2.5-7B-Q4 | ~4.5 GB | Medium | Very good | CJK languages |
| Shisa-v2-Nemo-12B-Q6 | ~10 GB | Slow | Excellent | Best translation quality |
| Qwen2.5-14B-Q4 | ~9 GB | Slow | Excellent | Premium CJK quality |

Note: large models (12B+) take 10–20 seconds per chunk on consumer GPUs. Use a 3B or 7B model if subtitle delay is a concern.

Download via CLI: **Tools → Download Translation Model**

### Translation Backends

| Backend | Setup | Notes |
|---------|-------|-------|
| **llama.cpp** (default) | Auto-installed by wizard | Runs GGUF models directly |
| **Ollama** | [ollama.com](https://ollama.com) | One-click install, model management |
| **LM Studio** | [lmstudio.ai](https://lmstudio.ai) | GUI with model browser |
| **Custom** | Your endpoint | Any OpenAI-compatible API |

---

## System Requirements

| | Minimum | Recommended |
|---|---------|-------------|
| **RAM** | 8 GB | 16+ GB |
| **GPU** | None (CPU works) | NVIDIA 4+ GB VRAM |
| **Disk** | 5 GB | 10+ GB |
| **Python** | 3.10 | 3.11+ |

| GPU | Support | Notes |
|-----|---------|-------|
| NVIDIA (CUDA) | Full | Best performance. Auto-detected via CTranslate2. |
| AMD (ROCm) | Linux only | RDNA2+ recommended. |
| CPU | Always | Slower. Use `small` or `base` Whisper model. |

---

## CLI Reference

```bash
python pocket_yume.py                # Interactive menu
python pocket_yume.py launch         # Start servers + runtime menu
python pocket_yume.py status         # Hardware, tools, packages, ports
python pocket_yume.py health         # Full end-to-end diagnostics
python pocket_yume.py benchmark      # Compare Whisper model speeds
python pocket_yume.py recommend      # Suggest best model for your GPU
python pocket_yume.py fonts          # Detect installed subtitle fonts
python pocket_yume.py setup          # Re-run setup wizard
python pocket_yume.py help           # All commands
```

---

## YouTube Authentication

YouTube blocks automated downloads to prevent bots. Yume supports two methods:

| Method | How it works | Requirements | Best for |
|--------|-------------|--------------|----------|
| **Browser Cookies** (default) | Borrows your YouTube login from Chrome/Firefox/Edge | Be logged into YouTube in your browser | Most users |
| **Deno** | Runs a local server that solves YouTube's bot challenge | Internet connection | Users without a YouTube account |

**Browser Cookies** is the default. Yume reads your YouTube session cookie (read-only, never modified) and passes it to yt-dlp for authenticated downloads.

**Deno mode** downloads [Deno](https://deno.land) (~35 MB), runs a local [bgutil](https://github.com/Brainicism/bgutil-ytdlp-pot-provider) server on port 4416, and uses it to generate proof-of-origin tokens. Requires an internet connection. Switch anytime via **Settings > YouTube Auth**.

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| "YouTube requires sign-in" | Settings > YouTube Auth > Browser Cookies > pick your browser |
| Port already in use | `python pocket_yume.py ports` |
| Whisper too slow | `python pocket_yume.py recommend` |
| Extension can't connect | Check both dots are green in the popup |
| "Server not reachable" on start | Normal — server loads the model first, extension retries automatically |
| `cublas64_12.dll not found` | Install CUDA Toolkit from [nvidia.com](https://developer.nvidia.com/cuda-downloads) or run `pip install nvidia-cublas-cu12`. Yume auto-falls back to CPU. |

### Known Limitations

- **Startup wait time** — Yume downloads audio before transcribing, so there's a delay before the first subtitle appears. Duration depends on connection speed and video length.
- **Whisper misses soft vocals** — Whisper's voice activity detection isn't tuned for singing. Quiet vocals over instrumentation (especially in the first 10–15 seconds) may be missed.
- **Large models are slow** — A 12B model takes 10–20 seconds per chunk on consumer GPUs. Use a smaller model if latency matters.
- **Non-YouTube site support is best-effort** — yt-dlp handles extraction, but bot protection, authentication, and DRM vary by site. Only the sites listed above are regularly tested.
- **`word_timestamps` and `pause_threshold` config keys have no effect** — Values are hardcoded server-side for music optimization. This is tracked as known technical debt.

---

## Security

- **Per-session API token** — random 32-byte token required for all endpoints
- **DNS rebinding protection** — Host header validation blocks non-localhost requests
- **URL sanitization** — all URLs validated before subprocess calls
- **Extension-only token** — web pages cannot obtain the API token
- **Cookie access** — read-only, never modified

---

<details>
<summary><strong>Changelog</strong></summary>

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

</details>

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and guidelines.

## License

MIT
