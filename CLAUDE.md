# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What is Yume

Yume is a browser extension + local server system for AI-powered video subtitles. The server downloads a video's audio (yt-dlp/ffmpeg), transcribes it with [faster-whisper](https://github.com/SYSTRAN/faster-whisper), translates it with a local LLM, romanizes it, and caches everything; the extension renders the subtitles as an overlay. Main source languages: Japanese, Chinese, Korean, Russian, Arabic.

**Components:**
- **Browser extension** (`extension/`) — one MV3 manifest for Chrome/Edge/Brave and Firefox 121+, no build step. A thin client: creates a job on the server and polls it.
- **Yume server** (`server/faster_whisper_server.py` + `server/_*.py`) — Flask on port 5001. Runs the whole pipeline (jobs, Whisper, translation, romanization, SQLite cache, blacklist).
- **Translation LLM** — llama.cpp (port 5000) / Ollama / LM Studio / any OpenAI-compatible API. Only the server talks to it.
- **CLI** (`pocket_yume.py`) — thin entry point; all logic lives in the `yume/` package (benchmark, guides, hardware, health, installers, launch, menus, network, ports, setup, ui, utils).

## Code Review Policy

Primary review before committing: run the linters and tests (`ruff check .`, `npx eslint extension/`, `pytest tests/ -v`, `npm test`) and Claude Code's built-in `/code-review` on the diff. Fix all critical and high-severity issues.

CodeRabbit is on the **free tier** (rate-limited, weaker than the trial) — do NOT run it automatically after every session. Reserve it for large or risky changes (security-sensitive code, big refactors, releases), at most one run per session, and only when the built-in review isn't enough.

## Commands

### Python

```bash
pytest tests/ -v                         # Run all tests
pytest tests/test_pipeline.py -v         # Server pipeline (fake Whisper + fake LLM)
pytest tests/test_integration.py -v      # Cross-file invariants
pytest tests/test_server_modules.py -v   # Server module unit tests

ruff check .                             # Lint
ruff check --fix .                       # Auto-fix lint violations
ruff format pocket_yume.py config.py server/   # CI checks formatting of these

bandit -r pocket_yume.py config.py server/ yume/ -ll -c pyproject.toml  # Security scan
```

On Windows `pytest` needs no special flags (the server no longer rebinds stdout at import).

### JavaScript (extension)

```bash
npm install                    # ESLint
npx eslint extension/ tests/js/  # Lint JS
npm test                       # node --test tests/js/ (VM-evaluated extension scripts)
```

### Running

```bash
python pocket_yume.py          # Interactive menu
python pocket_yume.py launch   # Start servers + runtime menu
python pocket_yume.py serve    # Headless servers (what the extension starts), auto-stop when idle
python pocket_yume.py autostart on|off|status  # Register the native messaging host (one-click start)
python pocket_yume.py status   # Hardware, tools, packages, ports
python pocket_yume.py health   # Diagnostics incl. an end-to-end test translation
python pocket_yume.py benchmark  # Compare Whisper model speeds
```

### Pre-commit

```bash
pip install pre-commit && pre-commit install
pre-commit run --all-files
```

## Architecture & Data Flow

See `docs/ARCHITECTURE.md`. In short:
1. `content.js` finds the main video and starts a `SubtitleSession` (`session.js`).
2. The session `POST /jobs` (url, video id, language, target, romanize, duration) via `background.js` (authenticated proxy with a path allowlist), then polls `GET /jobs/<id>?since=<rev>&t=<playhead>`.
3. `_jobs.JobManager`: cache check → full download (+ a stream preview of the first 30 s) → `_regions.plan_regions` (exclusive ~25 s regions cut at quiet points; no overlap) → one transcription worker (playhead region first) → hallucination filter/blacklist (`hidden` flag; raw text kept) → deterministic romanization → one LLM worker (batches of ≤10 lines).
4. Every segment change bumps the job `rev`; polls return only changed segments. 404 on poll → the session recreates its job (reloads from cache).

**Translation:** `_translate.Translator` uses structured output (JSON schema with exactly N strings), falling back to `json_object`, then numbered lines; the working mode is remembered per endpoint. The model name is sent for every backend except llama.cpp. One single-line retry for missing lines / CJK leaks.

**Cache:** `config/yume_cache.db` (SQLite, `_store.py`): videos + region plans, raw transcripts per (video, language, Whisper model, region), translations per (source, target, LLM model, text), LLM romanizations.

**Romanization:** pykakasi (JA) and pypinyin (ZH) on the server, built-in KO/RU tables; `None` from `_romanize.romanize()` means "LLM needed" (Arabic, or JA/ZH without the libraries). The pykakasi dictionary is pre-loaded in a background thread at startup.

## Configuration

User config lives at `config/yume_config.json` (auto-created). Key defaults:
- Whisper port: 5001, Translation port: 5000, Ollama port: 11434
- The Whisper server binds 127.0.0.1 only; only its port is configurable.
- Extension: only `background.js` resolves the server URL (`_whisperUrl()`); content scripts never hardcode `localhost:5001`.

`pyproject.toml` sets Ruff line length to 120 and targets Python 3.10+.

## Critical Conventions

**Security (must follow):**
- Always escape dynamic content with `_escapeHtml()` before `innerHTML` (it escapes quotes); use `textContent` for subtitle text
- Per-session random 32-byte API token required on all server endpoints except `/health`; CORS only for extension origins
- Sanitize URLs before passing to subprocess; validate `Host` header to prevent DNS rebinding
- New server paths must be added to `_ALLOWED_PATH` in `background.js` to be reachable from the extension

**Subprocess safety:**
- CLI code uses the `_run()` wrapper (UTF-8 for output and for Python children); never `shell=True` or `os.system()`
- Never kill a process on a port unless it is Yume's own (`kill_port_process` asks otherwise)

**CLI package layout:**
- `pocket_yume.py` is the user-facing entry point; all logic lives in `yume/`. Shared state (backend table, version, download URLs) is injected via `set_*()` setters called from `_init_modules()`. Do not import from `pocket_yume.py` inside `yume/` modules.
- Whisper model metadata lives only in `yume/hardware.WHISPER_MODELS` (multilingual models only — `distil-*` and `*.en` are English-only).

**Config hygiene:**
- Every key in `DEFAULT_CONFIG` must be read by app code (`cfg["key"]`/`cfg.get("key")`) — `TestDeadConfigDetection` enforces it
- Use `==` (exact versions) in `requirements.txt` and `LLAMA_SERVER_DEPS`; intentional exceptions: `yt-dlp`, `llama-cpp-python`

**Version bumping:**
- Three sources: `VERSION` in `pocket_yume.py`, `SERVER_VERSION` in `server/faster_whisper_server.py`, `"version"` in `extension/manifest.json` (the popup reads it) — plus the README badge. Tests check they match.

**Temp file cleanup:**
- Downloads live in `yume_*` temp dirs; delete with `_audio.remove_temp()` in `finally`; server startup removes leftovers older than an hour

**Line endings:** many tracked files are CRLF; scripted edits must preserve each file's line endings. `.sh`/`.command` must stay LF (`.gitattributes`).

## Key Files

| File | Purpose |
|------|---------|
| `pocket_yume.py` | Thin entry point: arg parsing, module wiring via `_init_modules()`, top-level menu dispatch |
| `config.py` | Config load/save/validate, `DEFAULT_CONFIG` |
| `yume/launch.py` | Server lifecycle: start llama.cpp / Ollama / Whisper, runtime menu, `LLAMA_SERVER_DEPS`; shared builders `whisper_command` / `llamacpp_command` / `build_server_env` (also used by `serve`) |
| `yume/llama_server.py` | Installs/locates llama.cpp's prebuilt `llama-server` (tools/llama.cpp/); `launch.llamacpp_command` prefers it over llama-cpp-python |
| `yume/service.py` | Headless `serve` supervisor: hidden children, `config/service.json` state, idle auto-stop (`auto_stop_minutes`), detached spawn that survives the browser's job object |
| `yume/native_host.py` | Native messaging host `com.pocketyume.yume` (status/start/stop) + registration; allowed extension ID must match the manifest `key` |
| `yume/setup.py` | Setup wizard, extension guide, uninstall |
| `yume/menus/` | Interactive menus, split by area: `server.py` (CLI commands, blacklist, Whisper model), `hf_browser.py`, `tools.py`, `settings.py`; backend info injected into `_shared.py` |
| `yume/guides.py` | Help & Guides menu: in-CLI cookbook of task-oriented recipes |
| `yume/health.py` | Health check (incl. end-to-end translation), system status, font detection |
| `yume/benchmark.py` | Whisper speed benchmark across models |
| `yume/network.py` | HTTP helpers (`server_get`, `server_post`), downloads (`.part` + rename), health checks |
| `yume/ports.py` | Port availability checks, conflict resolution, status display |
| `yume/hardware.py` | GPU/CPU detection (NVIDIA, AMD), `WHISPER_MODELS`, model recommendation |
| `yume/installers.py` | Tool download/install: yt-dlp, ffmpeg, Deno + PO-token plugin, llama.cpp, Ollama |
| `yume/ui.py` | ANSI colour helpers, `header()`, `panel()`, `pause()`, `error()` |
| `yume/utils.py` | `_run()`, `find_tool()`, update checker, shared path constants |
| `server/faster_whisper_server.py` | Flask routes (`/jobs`, `/library`, `/blacklist`, `/translation/*`, `/model/switch`, `/stats`), auth, startup |
| `server/_jobs.py` | Job manager, transcription + LLM workers, export |
| `server/_regions.py` | Quiet-point region planning |
| `server/_translate.py` | LLM client with structured output |
| `server/_store.py` | SQLite cache |
| `server/_audio.py` | yt-dlp/ffmpeg download strategies, WAV loading |
| `extension/js/session.js` | Job creation, polling, cue lookup |
| `extension/js/background.js` | Authenticated server proxy |
| `extension/js/content.js` | Page lifecycle, SPA navigation, popup messages |
| `extension/js/subtitle-window.js` | Shadow-DOM overlay, RTL support |
| `extension/popup.js` | Settings UI, server status, blacklist, library, diagnostics |
