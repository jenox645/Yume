# Architecture

> Yume v0.1.0 · Pocket Yume CLI · ~13,600 lines across 21 source files

## Overview

Yume is a browser extension + local server system. The extension captures video URLs, the servers process audio, and the extension renders subtitles.

```text
┌─ Browser Extension ──────────────────────────────────┐
│  popup.js           Settings UI, diagnostics         │
│  background.js      Service worker, server proxy     │
│  content.js         Lifecycle, event wiring          │
│  audio-capture.js   Pipeline: chunk, transcribe,     │
│                     translate in parallel             │
│  subtitle-window.js Overlay: drag, resize, RTL       │
└──────────────────────────────────────────────────────┘
        │ fetch() to localhost
        ▼
┌─ Whisper Server (port 5001) ─────────────────────────┐
│  faster-whisper (CTranslate2)                        │
│  yt-dlp audio download · ffmpeg slicing              │
│  Hallucination filter (JA/ZH/KO/RU/AR patterns)     │
│  API token auth · Host header validation             │
└──────────────────────────────────────────────────────┘
┌─ Translation LLM (port 5000) ────────────────────────┐
│  llama.cpp / Ollama / LM Studio / any OpenAI API     │
│  GGUF models · /v1/chat/completions                  │
└──────────────────────────────────────────────────────┘
        │
        ▼
┌─ CLI (pocket_yume.py + yume/ package) ───────────────┐
│  pocket_yume.py: thin entry point, arg dispatch      │
│  yume/: launch, setup, menus, health, benchmark,     │
│         network, ports, installers, hardware, ui     │
│  config.py: load/save/validate/export/import         │
└──────────────────────────────────────────────────────┘
```

## Data Flow: URL → Subtitles

1. User clicks **Enable** in popup
2. `content.js` starts `audio-capture.js` pipeline
3. Pipeline sends video URL to Whisper server via `background.js`
4. Whisper server downloads audio chunk (yt-dlp), transcribes (faster-whisper)
5. Segments returned to extension, hallucination-filtered client-side
6. Extension sends segments to translation LLM (batch, 120s timeout)
7. Romanization: wanakana (JA, local), pypinyin (ZH, server), or LLM (KO/RU/AR) — pykakasi dictionary is pre-loaded in a background thread at startup
8. `subtitle-window.js` renders overlay with RTL support for Arabic

**Key optimization:** Pipeline transcribes chunk N+1 while translating chunk N. 30s audio windows with 5s overlap prevent missed words at boundaries.

## Security Model

| Layer | Mechanism |
|-------|-----------|
| Token | Random per session via `secrets.token_urlsafe(32)`. Required on all endpoints except `/health`. |
| Discovery | `/health` returns token only to `chrome-extension://` or `moz-extension://` origins. Web pages get none. |
| Host | Rejects requests where `Host` header is not `127.0.0.1` or `localhost`. |
| URL | All URLs validated before passing to subprocess. No argument injection. |
| Subprocess | No `shell=True`, no `os.system()`, no `curl \| sh`. All process launches use explicit argv lists. |
| XSS | All dynamic values in popup innerHTML are escaped via `_escapeHtml()`. Subtitle overlay uses `textContent`. |
| Dependencies | All Python packages pinned to exact versions (`==`) in `requirements.txt` and inline installs. |

## File Responsibilities

### CLI — Entry Point

| File | Lines | Role |
|------|-------|------|
| `pocket_yume.py` | 414 | Thin entry point: arg parsing, module wiring, top-level menu dispatch |
| `config.py` | 163 | Config: load, save, validate, export, import. All port constants defined here. |

### CLI — `yume/` Package

| File | Lines | Role |
|------|-------|------|
| `yume/__init__.py` | 14 | Package marker |
| `yume/launch.py` | 656 | Server lifecycle: start llama.cpp / Ollama / Whisper, runtime menu |
| `yume/menus.py` | 1,636 | Interactive menus: main menu, settings, model selection, blacklist, stats |
| `yume/setup.py` | 483 | Setup wizard, first-run detection, uninstall |
| `yume/installers.py` | 560 | Tool download/install: yt-dlp, ffmpeg, Deno, llama.cpp, pip packages |
| `yume/health.py` | 442 | Health check, status display, font detection |
| `yume/benchmark.py` | 396 | Whisper speed benchmark across models |
| `yume/hardware.py` | 205 | GPU/CPU detection (NVIDIA, AMD, Apple Silicon) |
| `yume/network.py` | 310 | HTTP helpers (`server_get`, `server_post`), translation server check |
| `yume/ports.py` | 215 | Port availability checks, ports status display |
| `yume/ui.py` | 340 | ANSI colour helpers, `header()`, `panel()`, `pause()`, `error()` |
| `yume/utils.py` | 120 | `find_tool()`, update checker, shared path constants |

### Whisper Server

| File | Lines | Role |
|------|-------|------|
| `server/faster_whisper_server.py` | 2,646 | Flask server: Whisper STT, hallucination filter, audio download, romanization, cache |

### Browser Extension

| File | Lines | Role |
|------|-------|------|
| `extension/js/audio-capture.js` | 1,640 | Pipeline engine: chunking, parallel transcribe+translate+romanize, subtitle timing |
| `extension/popup.js` | 1,224 | Extension popup: settings, diagnostics, stats, model switching |
| `extension/js/background.js` | 1,078 | Service worker: server proxy, translation cache (LRU-500 + TTL), token management |
| `extension/js/content.js` | 388 | Content script: lifecycle, URL change detection, subtitle event dispatch |
| `extension/js/subtitle-window.js` | 415 | Overlay: DOM creation, drag/resize, dynamic font injection, RTL, alignment |
| `extension/js/debug-system.js` | 225 | Debug overlay: frame counter, pipeline state, network timing |

## Key Design Decisions

**CLI package layout** — `pocket_yume.py` is the single user-facing entry point (`python pocket_yume.py`). All logic lives in the `yume/` package: `launch`, `setup`, `menus`, `health`, `benchmark`, `hardware`, `network`, `ports`, `installers`, `ui`, `utils`. Shared state (GPU info, version, download URLs) is injected via `set_*()` setters called from `_init_modules()` in `pocket_yume.py` — this avoids circular imports while keeping modules testable in isolation. `config.py` is a separate extract handling load/save/validate.

**No build tools** — The extension is plain JS loaded directly by the manifest. No React, no webpack, no TypeScript. Zero build step. Users edit files and reload.

**Overlap chunking** — 30s Whisper windows with 5s overlap (`stepSize = chunkDuration - 5`). Deduplication by timestamp prevents duplicate subtitles at boundaries.

**Generation counter** — `this.generation` increments on stop, language change, or video switch. All in-flight async operations check `gen === this.generation` before writing results. Stale promises abort silently.

**Hallucination filter** — Whisper hallucinates on silence and music (e.g., "ご視聴ありがとうございました" on instrumental intros). Server-side pattern list (JA/ZH/KO/RU/AR) + client-side repeat/concatenation detection + user blacklist.

**Background pykakasi initialization** — The pykakasi dictionary (used for Japanese romanization) is pre-loaded in a background thread at server startup, before the Whisper model begins loading. This avoids a ~2-3s latency spike on the first romanization request. The singleton accessor `_get_kakasi()` is guarded by `threading.Lock()` for thread safety.
