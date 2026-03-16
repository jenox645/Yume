<div align="center">

<img src="assets/banner.png" alt="Yume Banner" width="800"/>

# Yume

**Real-time AI subtitles for any video — fully local, no cloud APIs.**

Transcription · Translation · Romanization

[![Version](https://img.shields.io/badge/version-5.5.0-blue)]()
[![Python](https://img.shields.io/badge/python-3.10+-green)]()
[![Chrome](https://img.shields.io/badge/chrome-MV3-yellow)]()
[![Firefox](https://img.shields.io/badge/firefox-MV3-orange)]()
[![Stars](https://img.shields.io/github/stars/jenox645/Yume?style=flat-square)](https://github.com/jenox645/Yume/stargazers)
[![Last Commit](https://img.shields.io/github/last-commit/jenox645/Yume?style=flat-square)](https://github.com/jenox645/Yume/commits/main)
[![CI](https://img.shields.io/github/actions/workflow/status/jenox645/Yume/ci.yml?label=CI&style=flat-square)](https://github.com/jenox645/Yume/actions)
[![License](https://img.shields.io/badge/license-MIT-lightgrey)](LICENSE)

</div>

---


Yume captures audio from any video in your browser, transcribes it with [faster-whisper](https://github.com/SYSTRAN/faster-whisper) running on your GPU, translates it with a local LLM, and overlays subtitles in real-time. Everything runs on your machine — no API keys, no subscriptions, no data leaves your computer.

**Supported sites:** YouTube, NicoNico, Bilibili, Twitch, Crunchyroll, and [1000+ more](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md) via yt-dlp

**Source languages:** Japanese · Chinese · Korean · Russian · Arabic

---

## Quick Start

**1. Launch Yume**

| Platform | Command |
|----------|---------|
| Windows  | Double-click `START_YUME.bat` |
| Linux    | `./START_YUME.sh` |
| macOS    | Double-click `START_YUME.command` |

The setup wizard runs on first launch — detects your hardware, installs dependencies, and configures everything.

**2. Install the Extension**

1. Open `chrome://extensions` (Chrome, Brave, Edge)
2. Enable **Developer Mode** (top-right)
3. **Load unpacked** → select the `extension/` folder
4. Pin the Yume icon

> **Firefox:** Rename `manifest_firefox.json` → `manifest.json`, then `about:debugging` → Load Temporary Add-on

**3. Watch**

1. Go to any video → Click Yume icon → **Enable**
2. Subtitles appear automatically
3. Press **Alt+Y** to toggle without opening the popup

---

## Architecture

```mermaid
graph LR
    subgraph ext["Browser Extension"]
        P["popup.js — Settings UI"]
        B["background.js — Server Proxy"]
        A["audio-capture.js — Pipeline"]
        C["content.js — Lifecycle"]
        S["subtitle-window.js — Overlay"]
    end

    subgraph srv["Local Servers"]
        W["Whisper Server :5001\nfaster-whisper + yt-dlp"]
        T["Translation LLM :5000\nllama.cpp / Ollama"]
    end

    B -- "video URL + settings" --> W
    W -- "transcribed text" --> B
    B -- "source text" --> T
    T -- "translated text" --> B
    B -- "subtitles" --> S

    style W fill:#7c3aed,stroke:#5b21b6,color:#fff
    style T fill:#16a34a,stroke:#15803d,color:#fff
    style P fill:#f87171,stroke:#dc2626,color:#fff
    style B fill:#60a5fa,stroke:#2563eb,color:#fff
    style A fill:#60a5fa,stroke:#2563eb,color:#fff
    style C fill:#4ade80,stroke:#16a34a,color:#fff
    style S fill:#4ade80,stroke:#16a34a,color:#fff
```

---

## Real-time Pipeline

```mermaid
graph TD
    A([Start]) --> B["Download full audio via yt-dlp"]
    B --> C["Slice chunk N from audio"]
    C --> D["Whisper: Transcribe"]
    D --> E{"Speech\nfound?"}
    E -->|Yes| F["LLM: Translate"]
    E -->|No| G["Mark empty"]
    F --> H["Cache + display subtitles"]
    G --> H
    H --> I{"More\nchunks?"}
    I -->|Yes| C
    I -->|No| J([Done])
    C -.->|"Parallel"| K["Transcribe chunk N+1"]
    K -.-> H

    style D fill:#7c3aed,stroke:#5b21b6,color:#fff
    style F fill:#16a34a,stroke:#15803d,color:#fff
    style A fill:#f59e0b,stroke:#d97706,color:#fff
    style J fill:#22c55e,stroke:#16a34a,color:#fff
```

Yume hides latency by **transcribing chunk N+1 while translating chunk N** — Whisper and the LLM never wait for each other.

---

## Features

```mermaid
mindmap
  root((Yume))
    Transcription
      faster-whisper on GPU
      9 Whisper models + turbo
      Hallucination filter
      Music-optimized thresholds
    Translation
      llama.cpp / Ollama / LM Studio
      5 language pairs
      Batch translation + caching
      Custom prompts editor
    Romanization
      Romaji — Japanese, instant
      Pinyin — Chinese, instant
      Revised — Korean, instant
      LLM fallback for RU/AR
    Interface
      Draggable subtitle overlay
      Glass blur effect
      Custom fonts per line
      SRT export
    Performance
      Parallel pipeline
      LRU translation cache
      Batch romanization
      Download-once architecture
```

---

## Translation Models

Yume uses a local LLM to translate subtitles. Recommended models:

| Model | Size | Speed | Quality | Best for |
|-------|------|-------|---------|----------|
| Qwen2.5-3B-Q6 | ~2.5 GB | Medium| Good | Low VRAM, fast subtitles |
| Mistral-7B-Q4 | ~4.5 GB | Slow| Very good | Most users |
| Qwen2.5-7B-Q4 | ~4.5 GB | Slow| Very good | CJK languages |
| Shisa-v2-Nemo-12B-Q6 | ~10 GB | | Excellent | Best translation quality |
| Qwen2.5-14B-Q4 | ~9 GB | | Excellent | Premium CJK quality |

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
| AMD (ROCm) | Linux | RDNA2+ recommended. |
| CPU | Always | Slower. Use `small` or `base` model. |

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

YouTube blocks automated downloads to prevent bots. Yume supports two methods to authenticate:

| Method | How it works | Requirements | Best for |
|--------|-------------|--------------|----------|
| **Browser Cookies** (default) | Borrows your YouTube login from Chrome/Firefox/Edge | Be logged into YouTube in your browser | Most users |
| **Deno** | Runs a local server that solves YouTube's bot challenge | Deno installed + internet connection | Users without a YouTube account |

**Browser Cookies** is the default and recommended option. During first-time setup, Yume asks which browser you use. It reads your YouTube session cookie (read-only, never modified) and passes it to yt-dlp for authenticated downloads.

**Deno mode** is for users who don't want to link a YouTube account. Yume downloads [Deno](https://deno.land) (~35 MB), clones the [bgutil-ytdlp-pot-provider](https://github.com/Brainicism/bgutil-ytdlp-pot-provider) server, and runs it locally on port 4416. This server solves YouTube's BotGuard challenge and generates "proof-of-origin" (PO) tokens that prove you're a real browser. It requires an active internet connection to fetch challenges from YouTube.

Change the method anytime: **Settings > YouTube Auth**

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| "YouTube requires sign-in" | Settings > YouTube Auth > Browser Cookies > pick your browser |
| Port already in use | `python pocket_yume.py ports` to find and kill the process |
| Whisper too slow | `python pocket_yume.py recommend` for the best model |
| Extension can't connect | Check both dots are green in the popup |
| No subtitles on music | Thresholds are tuned for music in v5.0+ |
| Translation server red dot | Fixed in v5.4.2 — update to latest |
| Auto-detect picks CPU with NVIDIA | Fixed in v5.5.0 — uses CTranslate2 detection |
| "Server not reachable" on start | Normal — server loads model first, extension retries |
| First 30s show "no speech" | If the song has an instrumental intro, this is normal |

---

## Security

- **Per-session API token** — random 32-byte token, required for all endpoints
- **DNS rebinding protection** — Host header validation blocks non-localhost
- **URL sanitization** — all URLs validated before subprocess calls
- **Extension-only token** — web pages cannot obtain the API token
- **Cookie access** — read-only browser cookie access for YouTube auth (never modified)

---

<details>
<summary><strong>Changelog</strong></summary>

### v5.5.0
- **Fixed**: YouTube auth — implemented proper Deno PO token support via bgutil server (port 4416)
- **Fixed**: YouTube auth was a no-op since v1 — "deno" mode ran with zero authentication
- **Fixed**: Stale API token after server restart — extension now auto-recovers on 403
- **Fixed**: Auto-detect GPU choosing CPU with NVIDIA — uses CTranslate2 detection
- **Fixed**: "Listening (no speech yet)" replaced with helpful messages showing vocal start time
- **Fixed**: "Ready" signal now fires when all chunks complete, not only on current-chunk match
- **Changed**: Default YouTube auth from "deno" to "cookies" (works out of the box)
- **Added**: bgutil PO token HTTP server auto-setup (download, install, start, cleanup)
- **Added**: Cookies fallback for deno mode (tries cookies if PO token fails)
- **Added**: CPU name display with color (Intel blue, AMD red) in main menu
- **Added**: Mermaid diagrams (architecture, pipeline, mindmap) in README
- **Added**: Translation model recommendations table
- **Added**: YouTube Authentication documentation section in README
- **Improved**: Setup wizard explains cookies vs deno clearly during first run
- **Improved**: Deno menu shows full bgutil server status + plugin status

### v5.4.2
- **Fixed**: Translation "not reachable" — tries multiple health endpoints for each backend
- **Fixed**: Translation red indicator — token leak + non-JSON crash + no auto-refresh
- **Fixed**: GPL conflict — `korean_romanizer` to MIT `romanization`
- **Added**: Glass effect on subtitle window, translation prompt editor, CI, 32 tests, ARIA
- **Improved**: All CLI jargon replaced with plain explanations

### v5.3.0
- **Added**: BatchedInferencePipeline, `whisper-large-v3-turbo`, batch romanization

### v5.2.0
- **Fixed**: Settings wiped on update, 503 during loading, double language restart
- **Added**: Korean romanization, project banner

### v5.1.0
- **Improved**: Server starts before model loads, prewarm inference, cache key fix

### v5.0.0
- Initial reviewed release. Music-optimized Whisper, RTL Arabic, batch translation.

</details>

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and guidelines.

## License

MIT
