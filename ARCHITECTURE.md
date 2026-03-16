# Architecture

> Yume v5.6.0 · Pocket Yume CLI · ~12,500 lines across 15 source files

## Overview

Yume is a browser extension + local server system. The extension captures video URLs, the servers process audio, and the extension renders subtitles.

```
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
┌─ CLI (pocket_yume.py) ───────────────────────────────┐
│  Setup wizard · Server lifecycle · Port management   │
│  GPU detection · Tool installer · Benchmark          │
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
7. Romanization: wanakana (JA, local), pypinyin (ZH, server), or LLM (KO/RU/AR)
8. `subtitle-window.js` renders overlay with RTL support for Arabic

**Key optimization:** Pipeline transcribes chunk N+1 while translating chunk N. 30s audio windows with 5s overlap prevent missed words at boundaries.

## Security Model

| Layer | Mechanism |
|-------|-----------|
| Token | Random per session via `secrets.token_urlsafe(32)`. Required on all endpoints except `/health`. |
| Discovery | `/health` returns token only to `chrome-extension://` or `moz-extension://` origins. Web pages get none. |
| Host | Rejects requests where `Host` header is not `127.0.0.1` or `localhost`. |
| URL | All URLs validated before passing to subprocess. No argument injection. |

## File Responsibilities

| File | Lines | Role |
|------|-------|------|
| `pocket_yume.py` | 3,897 | CLI: installer, launcher, port management, benchmarks |
| `config.py` | 131 | Config: load, save, validate, export, import. All port constants defined here. |
| `faster_whisper_server.py` | 2,118 | Flask server: Whisper STT, hallucination filter, audio download, cache |
| `audio-capture.js` | 1,076 | Pipeline engine: chunking, parallel transcribe+translate, subtitle timing |
| `popup.js` | 1,143 | Extension popup: settings, diagnostics, stats, model switching |
| `background.js` | 913 | Service worker: server proxy, translation cache (LRU-500), token management |
| `content.js` | 388 | Content script: lifecycle, URL change detection, subtitle event dispatch |
| `subtitle-window.js` | 360 | Overlay: DOM creation, drag/resize, dynamic font injection, RTL, alignment |

## Key Design Decisions

**Single-file CLI** — `pocket_yume.py` is large (~3,900 lines) by design. It's a self-contained installer that users run with `python pocket_yume.py`. Config management was extracted to `config.py` as the first modular step.

**No build tools** — The extension is plain JS loaded directly by the manifest. No React, no webpack, no TypeScript. Zero build step. Users edit files and reload.

**Overlap chunking** — 30s Whisper windows with 5s overlap (`stepSize = chunkDuration - 5`). Deduplication by timestamp prevents duplicate subtitles at boundaries.

**Generation counter** — `this.generation` increments on stop, language change, or video switch. All in-flight async operations check `gen === this.generation` before writing results. Stale promises abort silently.

**Hallucination filter** — Whisper hallucinates on silence and music (e.g., "ご視聴ありがとうございました" on instrumental intros). Server-side pattern list (JA/ZH/KO/RU/AR) + client-side repeat/concatenation detection + user blacklist.
