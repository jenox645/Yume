<div align="center">

# Pocket Yume

**Real-time AI subtitles for any video — fully local, no cloud APIs.**

Transcription · Translation · Romanization

[![Version](https://img.shields.io/badge/version-5.0.0-blue)]()
[![Python](https://img.shields.io/badge/python-3.10+-green)]()
[![Chrome](https://img.shields.io/badge/chrome-MV3-yellow)]()
[![License](https://img.shields.io/badge/license-MIT-lightgrey)]()

</div>

---

Yume captures audio from any video in your browser, transcribes it with [faster-whisper](https://github.com/SYSTRAN/faster-whisper) running on your GPU, translates it with a local LLM, and overlays subtitles in real-time. Everything runs on your machine. No API keys, no subscriptions, no data leaves your computer.

**Supported sites:** YouTube, NicoNico, Bilibili, Twitch, Crunchyroll, and [1000+ more](https://github.com/yt-dlp/yt-dlp/blob/master/supportedsites.md) via [yt-dlp](https://github.com/yt-dlp/yt-dlp/tree/master). Also works with direct stream URLs (m3u8, mp4).

**Source languages:** Japanese · Chinese · Korean · Russian · Arabic

**Subtitle output:** Original text → Romanization (Romaji / Pinyin / etc.) → Translation

---

## Quick Start

### 1. Launch Yume

| Platform | Command |
|----------|---------|
| Windows  | Double-click `START_YUME.bat` |
| Linux    | `./START_YUME.sh` |
| macOS    | Double-click `START_YUME.command` |

The setup wizard runs on first launch — it detects your hardware, installs dependencies (yt-dlp, FFmpeg, faster-whisper, translation model), and configures everything automatically.

### 2. Install the Extension

1. Open `chrome://extensions` (works in Chrome, Brave, Edge)
2. Enable **Developer Mode** (top-right toggle)
3. Click **Load unpacked** → select the `extension/` folder
4. Pin the Yume icon in the toolbar

**Firefox:**
1. Rename "manifest_firefox.json" by "manifest.json", replacing the already existing "manifest.json".
2. Go to `about:debugging`
3. This Firefox → Load Temporary Add-on → select `extension/manifest_firefox.json`
4. Pin the Yume icon in the toolbar

### 3. Watch

1. Go to any video with speech
2. Click the Yume icon → **Enable**
3. Subtitles appear automatically

---

## How It Works

```
Browser Extension                    Local Servers
┌──────────────┐                    ┌─────────────────┐
│  popup.js    │  settings/control  │ Whisper Server   │
│  content.js  │ ◄───────────────── │ (port 5001)      │
│  audio-      │  transcription     │ faster-whisper   │
│  capture.js  │ ──────────────────►│ yt-dlp + ffmpeg  │
│  subtitle-   │                    └─────────────────┘
│  window.js   │                    ┌─────────────────┐
│              │  translation       │ Translation LLM  │
│              │ ◄─────────────────►│ (port 5000)      │
└──────────────┘                    │ llama.cpp/Ollama │
                                    └─────────────────┘
```

The extension sends the video URL to the Whisper server, which downloads audio chunks via yt-dlp, transcribes them with faster-whisper, and returns timestamped segments. The extension then sends text to the translation LLM in parallel — transcribing chunk N+1 while translating chunk N.

---

## System Requirements

| | Minimum | Recommended |
|---|---------|-------------|
| **RAM** | 8 GB | 16+ GB |
| **GPU** | None (CPU works) | NVIDIA 4+ GB VRAM |
| **Disk** | 5 GB | 10+ GB |
| **Python** | 3.10 | 3.11+ |
| **OS** | Windows 10, Ubuntu 20.04, macOS 12 | Latest |

### GPU Support

| GPU | Status | Notes |
|-----|--------|-------|
| NVIDIA (CUDA) | Full support | Best performance. Auto-detected. |
| AMD (ROCm) | Linux only | RDNA2+ recommended. VRAM-aware compute tiers. |
| CPU | Always works | Slower. Recommended model: `small` or `base`. |

---

## Translation Backends

| Backend | How it works | Setup |
|---------|-------------|-------|
| **llama.cpp** (default) | Runs a GGUF model directly | Auto-installed by setup wizard |
| **Ollama** | Model management via CLI | [ollama.com](https://ollama.com) |
| **LM Studio** | GUI with model browser | [lmstudio.ai](https://lmstudio.ai) |
| **text-generation-webui** | Feature-rich web UI | [github](https://github.com/oobabooga/text-generation-webui) |
| **Custom** | Any OpenAI-compatible API | Point to your endpoint |

---

## CLI Reference

```bash
python pocket_yume.py                # Interactive menu
python pocket_yume.py launch         # Start servers + runtime menu
python pocket_yume.py status         # Hardware, tools, packages, ports
python pocket_yume.py health         # Full end-to-end diagnostics
python pocket_yume.py fonts          # Detect installed CJK fonts
python pocket_yume.py benchmark      # Compare Whisper model speeds
python pocket_yume.py recommend      # Suggest best model for your hardware
python pocket_yume.py ports          # Port availability
python pocket_yume.py setup          # Re-run setup wizard
python pocket_yume.py export [path]  # Backup config
python pocket_yume.py import <path>  # Restore config
python pocket_yume.py blacklist list # Manage hallucination filter
python pocket_yume.py --verbose      # Enable debug logging
python pocket_yume.py help           # All commands
```

---

## Project Structure

```
PocketYume/
├── pocket_yume.py              CLI launcher & installer (93 functions)
├── config.py                   Configuration management (extracted module)
├── yume_doctor.py              Standalone diagnostic tool
│
├── server/
│   ├── faster_whisper_server.py    Whisper STT + hallucination filter
│   └── requirements.txt
│
├── extension/                  Chrome/Brave/Edge extension (load this folder)
│   ├── manifest.json
│   ├── popup.html / popup.js / popup.css
│   ├── js/
│   │   ├── background.js          Service worker (server proxy, translation cache)
│   │   ├── content.js             Content script (lifecycle, event wiring)
│   │   ├── audio-capture.js       Pipeline engine (chunking, parallel transcribe+translate)
│   │   ├── subtitle-window.js     Overlay renderer (drag, resize, RTL)
│   │   ├── debug-system.js        Structured logging
│   │   └── wanakana_min.js        Japanese romanization (offline)
│   ├── css/
│   ├── icons/
│   └── fonts/                  Custom font files (.ttf/.otf)
│
├── config/                     User settings (auto-generated)
├── models/translation/         GGUF model files
├── tools/                      yt-dlp, ffmpeg, deno (auto-downloaded)
├── logs/                       Server logs (auto-rotated)
└── START_YUME.bat/sh/command   Platform launchers
```

---


## Fonts

Yume detects installed system fonts automatically and groups them by language in the popup dropdown. CJK fonts (Japanese, Chinese, Korean, Arabic) are shown first when you select the matching source language.

### Custom fonts

Place `.ttf` or `.otf` files in `extension/fonts/`, add them to the `BUNDLED_FONTS` array in `popup.js`, and reload the extension. They'll appear as "(bundled)" in the font picker.

### Recommended free fonts

| Language | Font | Source |
|----------|------|--------|
| Japanese | Noto Sans JP | [Google Fonts](https://fonts.google.com/noto/specimen/Noto+Sans+JP) |
| Chinese (Simplified) | Noto Sans SC | [Google Fonts](https://fonts.google.com/noto/specimen/Noto+Sans+SC) |
| Chinese (Traditional) | Noto Sans TC | [Google Fonts](https://fonts.google.com/noto/specimen/Noto+Sans+TC) |
| Korean | Noto Sans KR | [Google Fonts](https://fonts.google.com/noto/specimen/Noto+Sans+KR) |
| Arabic | Noto Naskh Arabic | [Google Fonts](https://fonts.google.com/noto/specimen/Noto+Naskh+Arabic) |

Run `python pocket_yume.py fonts` to see which CJK fonts are already installed on your system.

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Port already in use | `python pocket_yume.py ports` to find and kill the process |
| Whisper too slow | `python pocket_yume.py recommend` for the best model for your GPU |
| Extension can't connect | Check both dots are green in the popup. Restart servers if needed. |
| No subtitles on music | Whisper drops quiet vocals. Thresholds are tuned for music in v5.0. |
| Non-YouTube site blocked (403) | Copy the m3u8/mp4 URL from DevTools Network tab → paste as Custom Stream URL |
| Translation shows source text | Fixed in v5.0. If it persists, check your LLM server is running. |
| Arabic text renders wrong | v5.0 adds RTL support. Update to latest version. |

---

## Security

- **API token auth**: Random token per session, required for all server endpoints except `/health`
- **DNS rebinding protection**: Host header validation rejects non-localhost requests
- **URL sanitization**: All URLs validated before passing to subprocess
- **Token discovery**: Extension-only — web pages cannot obtain the API token

---

## License

MIT
