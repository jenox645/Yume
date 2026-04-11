# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What is Yume

Yume is a browser extension + local server system for real-time AI-powered video subtitles. It captures audio from videos, transcribes with [faster-whisper](https://github.com/SYSTRAN/faster-whisper), translates with a local LLM, and renders subtitles as a DOM overlay. Supported source languages: Japanese, Chinese, Korean, Russian, Arabic.

**Components:**
- **Browser extension** (`extension/`) — Chrome/Firefox MV3, overlays subtitles, no build step required
- **Whisper server** (`server/faster_whisper_server.py`) — Flask on port 5001, transcribes audio via faster-whisper
- **Translation server** — Port 5000, runs llama.cpp / Ollama / custom OpenAI-compatible API
- **CLI** (`pocket_yume.py`) — Thin entry point; all logic lives in the `yume/` package (benchmark, hardware, health, installers, launch, menus, network, ports, setup, ui, utils)

## Code Review Policy

After every fix or refactoring session, run CodeRabbit on all changed files before committing. Fix all critical and high-severity issues. Re-run until no critical issues remain.

## Commands

### Python

```bash
pytest tests/ -v                         # Run all tests
pytest tests/test_integration.py -v      # Integration tests only
pytest tests/test_config.py -v           # Config unit tests
pytest tests/test_server_logic.py -v     # Server logic unit tests

ruff check .                             # Lint
ruff check --fix .                       # Auto-fix lint violations
ruff format pocket_yume.py config.py server/faster_whisper_server.py

bandit -r pocket_yume.py config.py server/faster_whisper_server.py -ll -c pyproject.toml  # Security scan
```

### JavaScript (extension)

```bash
npm install                                              # Install ESLint
npx eslint extension/js/ extension/popup.js             # Lint JS
node --check extension/js/background.js                 # Syntax check a file
```

### Running

```bash
python pocket_yume.py          # Interactive menu
python pocket_yume.py launch   # Start servers + runtime menu
python pocket_yume.py status   # Hardware, tools, packages, ports
python pocket_yume.py health   # End-to-end diagnostics
python pocket_yume.py benchmark  # Compare Whisper model speeds
```

### Pre-commit

```bash
pip install pre-commit && pre-commit install
pre-commit run --all-files
```

## Architecture & Data Flow

**Pipeline (per 30-second audio chunk with 5s overlap):**
1. `content.js` starts `audio-capture.js` pipeline when user enables subtitles
2. `audio-capture.js` segments audio and sends chunks to `background.js`
3. `background.js` calls Whisper server (`/transcribe_url`) → transcribed text
4. In parallel: calls translation LLM → translated text + romanization
5. `subtitle-window.js` renders the DOM overlay with RTL support for Arabic
6. Generation counter (`gen === this.generation`) aborts stale in-flight async ops

**Translation batching:** Text is sent as `[1] line\n[2] line` and parsed back by `[N]` markers. Results are cached LRU-500 with 30-min TTL (prevents stale results after model switches).

**Romanization:** Pre-loaded in a background thread to avoid 2–3s latency. Deterministic libs (`pykakasi` for JA, `pypinyin` for ZH, `romanization` for KO); LLM-based for RU, AR.

## Configuration

User config lives at `config/yume_config.json` (auto-created). Key defaults:
- Whisper port: 5001, Translation port: 5000, Ollama port: 11434
- Always use helpers `_whisperUrl()` / `_translationUrl()` — never hardcode `localhost:5001`

`pyproject.toml` sets Ruff line length to 120 and targets Python 3.10+.

## Critical Conventions

**Security (must follow):**
- Always escape dynamic content with `_escapeHtml()` before `innerHTML`; use `textContent` for subtitle text
- Per-session random 32-byte API token required on all server endpoints except `/health`
- Sanitize URLs before passing to subprocess; validate `Host` header to prevent DNS rebinding

**Subprocess safety:**
- Always use the `_run()` wrapper (forces UTF-8 on Windows); never use bare `subprocess.run()` with `shell=True`

**CLI package layout:**
- `pocket_yume.py` is the user-facing entry point; all logic lives in `yume/`. Shared state (GPU info, version, download URLs) is injected via `set_*()` setters called from `_init_modules()`. Do not import from `pocket_yume.py` inside `yume/` modules — use the injected values instead.

**Config hygiene:**
- Every key in `DEFAULT_CONFIG` must be actively used — integration tests detect dead config keys
- Use `==` (exact versions) in `requirements.txt`, never `>=`

**Version bumping:**
- When bumping the version, update it in 13 places: `pocket_yume.py`, `faster_whisper_server.py`, both manifests (`manifest.json` + `manifest_firefox.json`), `popup.html`, `popup.js`, all `extension/js/*.js` files, `README.md`, `docs/ARCHITECTURE.md`, `docs/DEVELOPER_GUIDE.md`, `test_integration.py`

**Temp file cleanup:**
- Always `shutil.rmtree()` in error/finally paths; startup cleanup catches leftover temp dirs

## Key Files

| File | Purpose |
|------|---------|
| `pocket_yume.py` | Thin entry point: arg parsing, module wiring via `_init_modules()`, top-level menu dispatch |
| `config.py` | Config load/save/validate, `DEFAULT_CONFIG` |
| `yume/launch.py` | Server lifecycle: start llama.cpp / Ollama / Whisper, runtime menu |
| `yume/setup.py` | Setup wizard, first-run detection, uninstall |
| `yume/menus.py` | Interactive menus: main menu, settings, model selection, blacklist, stats |
| `yume/health.py` | Health check, system status display, font detection |
| `yume/benchmark.py` | Whisper speed benchmark across models |
| `yume/network.py` | HTTP helpers (`server_get`, `server_post`), download, server health checks |
| `yume/ports.py` | Port availability checks, conflict resolution, status display |
| `yume/hardware.py` | GPU/CPU detection (NVIDIA, AMD, Apple Silicon) |
| `yume/installers.py` | Tool download/install: yt-dlp, ffmpeg, Deno, llama.cpp, pip packages |
| `yume/ui.py` | ANSI colour helpers, `header()`, `panel()`, `pause()`, `error()` |
| `yume/utils.py` | `_run()`, `find_tool()`, update checker, shared path constants |
| `server/faster_whisper_server.py` | Flask server: `/transcribe`, `/transcribe_url`, `/romanize`, `/model/switch`, `/stats` |
| `extension/js/audio-capture.js` | Pipeline engine, chunking, retry logic |
| `extension/js/background.js` | Service worker, proxy requests, translation/romanization cache |
| `extension/js/content.js` | Page lifecycle, event wiring |
| `extension/js/subtitle-window.js` | DOM overlay renderer, RTL support |
| `extension/popup.js` | Settings UI, diagnostics panel |
