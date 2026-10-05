# Developer Guide

Read [ARCHITECTURE.md](ARCHITECTURE.md) first — it explains the pipeline.

## Setup

```bash
python pocket_yume.py                     # first run: setup wizard
python pocket_yume.py health              # verify everything, incl. a test translation
python pocket_yume.py --verbose launch    # debug logging (or LOG_LEVEL=DEBUG)
npm install                               # ESLint for the extension
```

Developing without a GPU works: set `"whisper_device": "cpu"` and
`"whisper_model": "tiny"` in `config/yume_config.json`. For translation, Ollama
needs no compiling: `ollama pull qwen2.5:3b`, then set the backend to Ollama and
the model name to `qwen2.5:3b` in Settings → Translation settings.

Run the server alone (what `launch` does):

```bash
python server/faster_whisper_server.py --model tiny --device cpu --compute-type int8 --config config/yume_config.json
```

## Code organization

| Path | Role |
|------|------|
| `pocket_yume.py` | CLI entry point: arg parsing, `_init_modules()` wiring, main menu |
| `config.py` | `DEFAULT_CONFIG`, load/save/validate/export/import, port constants |
| `yume/launch.py` | Start llama.cpp / Ollama / Whisper, runtime menu, shutdown; shared command builders (`whisper_command`, `llamacpp_command`, `build_server_env`) |
| `yume/llama_server.py` | llama.cpp's prebuilt `llama-server`: picks the build for the GPU/driver from GitHub releases, verified download, command line (preferred over llama-cpp-python) |
| `yume/service.py` | Headless `serve` supervisor (auto-stop), `stop`, detached spawning |
| `yume/native_host.py` | Native messaging host + browser registration (`autostart on/off`) |
| `yume/setup.py` | Setup wizard, extension install guide, uninstall |
| `yume/menus/` | `server.py` CLI commands (stats, blacklist, model) + blacklist / Whisper model menus; `hf_browser.py` GGUF browser; `tools.py` Tools & Fonts (installers, backend, engine, YouTube auth, one-click start); `settings.py` Settings menu. `__init__` re-exports the entry points |
| `yume/health.py` | Health check (incl. end-to-end translation), status, fonts |
| `yume/installers.py` | yt-dlp, FFmpeg, Deno + PO-token plugin, llama-cpp-python, Ollama |
| `yume/hardware.py` | GPU/CPU/RAM detection, **`WHISPER_MODELS` (the one model table)**, recommendation |
| `yume/network.py` | Downloads (`.part` + rename), server health checks, authenticated server calls |
| `yume/ports.py` | Port checks; kills only Yume's own processes without asking |
| `server/faster_whisper_server.py` | Flask routes, auth/CORS, config, model loading |
| `server/_jobs.py` | Job manager, transcription + LLM workers, SRT/VTT export |
| `server/_regions.py` | Quiet-point region planning (pure numpy) |
| `server/_transcribe.py` | Whisper call with the music-tuned parameters |
| `server/_translate.py` | LLM client: structured output, fallbacks, retries |
| `server/_romanize.py` | Deterministic romanization; `None` = needs the LLM |
| `server/_store.py` | SQLite cache |
| `server/_filter.py` | Hallucination / credits patterns, user blacklist |
| `server/_audio.py` | yt-dlp/ffmpeg download strategies, stream URLs, WAV loading |
| `extension/js/session.js` | Job creation, polling, cue lookup |
| `extension/js/content.js` | Page lifecycle, SPA navigation, popup messages |
| `extension/js/subtitle-window.js` | Shadow-DOM overlay |
| `extension/js/background.js` | Authenticated proxy to the server (path allowlist); start/stop via the native host |
| `extension/js/bundled-fonts.js` | Fonts shipped in `extension/fonts/` (popup + overlay) |
| `extension/popup.js` | Settings, server status, blacklist, library, stats |

Modules in `yume/` must not import `pocket_yume.py`; shared values (backend
table, version, download URLs) are injected with `set_*()` from `_init_modules()`.

## Server API

All endpoints except `/health` need the `X-API-Token` header.

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/health` | `loading` / `ready` / `error` (+ token for extension origins) |
| POST | `/jobs` | Create or rejoin a job: `url`, `video_id`, `language`, `target`, `romanize`, `title`, `duration`, `stream_url` |
| GET | `/jobs/<id>?since=&t=&events=1` | Poll: segments changed since revision `since`; `t` = playhead |
| POST | `/jobs/<id>/options` | `{"romanize": bool}` |
| GET | `/jobs/<id>/export?format=srt\|vtt` | Subtitle file content |
| GET | `/library` · `/library/export` · POST `/library/delete` | Cached videos |
| GET | `/blacklist` · POST `/blacklist/add` `/blacklist/remove` `/blacklist/update` | User blacklist |
| GET | `/translation/health` · `/translation/models` · POST `/translation/test` | LLM backend |
| POST | `/model/switch` | Hot-swap the Whisper model (drops jobs; clients recreate them) |
| GET | `/stats` · `/config` · POST `/cache/clear` | Dashboard, config, wipe cache |

A segment in a poll: `{id, start, end, text, translation, romaji, confidence, hidden}`.
`hidden: true` means "remove it" (hallucination or blacklisted).

## Conventions

- **Subprocesses:** CLI code uses `_run()` (UTF-8 in and out, also for Python
  children); never `shell=True` or `os.system()`. The tests scan every module.
- **HTML:** escape everything with `_escapeHtml()` (it escapes quotes — values
  also go into attributes); subtitle text uses `textContent`.
- **Server URL:** the extension resolves it only in `background.js`
  (`_whisperUrl()`); content scripts call `{type: 'API'}` and never hardcode it.
- **Dependencies:** exact pins (`==`) in `server/requirements.txt` and
  `LLAMA_SERVER_DEPS` (`yume/launch.py`). Exceptions, on purpose: `yt-dlp`
  (YouTube breaks old versions within weeks) and `llama-cpp-python` (prebuilt
  CUDA wheels exist only for some versions).
- **Config keys:** every `DEFAULT_CONFIG` key must be *read* by app code
  (`cfg["key"]` / `cfg.get("key")`, not just printed) — `TestDeadConfigDetection`
  fails otherwise.
- **Device/precision:** the CLI resolves `auto` and passes `--device` /
  `--compute-type`; the server does not re-read them from the config.
- **Whisper models:** add or change models only in `yume/hardware.WHISPER_MODELS`,
  `valid_models` in `switch_model()` and the popup's `<select>`. Multilingual
  models only (`*.en` and `distil-*` cannot transcribe Yume's languages).
- **Temp files:** downloads live in `yume_*` temp dirs; `_audio.remove_temp()`
  deletes them, and server startup removes leftovers older than an hour.
- **Line endings:** `.sh` / `.command` must stay LF (`.gitattributes`).

## Versioning

The version lives in three places, checked by the tests and CI:
`VERSION` in `pocket_yume.py`, `SERVER_VERSION` in
`server/faster_whisper_server.py`, `"version"` in `extension/manifest.json`
(the popup reads it at runtime) — plus the README badge.

## Testing

```bash
pytest tests/ -v          # Python: config, server modules, pipeline, CLI modules, integration
npm test                  # JS: session merge/render, video ids, proxy allowlist
npx eslint extension/     # JS lint
ruff check .              # Python lint
```

- `tests/test_pipeline.py` runs real jobs with a fake Whisper model and a fake
  LLM: regions, cache reuse without download, blacklist re-filtering, error
  paths, translator format fallback.
- `tests/test_integration.py` guards cross-file invariants: dead config keys,
  yt-dlp auth arguments, version sources, manifest contents, no hardcoded
  server URLs, subprocess hygiene, pinned dependencies, every
  `pocket_yume.py <command>` named in hints/docs exists, and every server path
  the extension calls is allowlisted in `background.js` and has a route.
- `tests/test_service.py` covers the native messaging protocol and commands,
  registration (fake registry / temp folders), the supervisor's idle stop and
  error states, and — on Windows — that `serve` survives the browser killing
  the host's job object.
- `tests/js/` evaluates the plain extension scripts in a Node VM with
  minimal browser stubs.

The extension's fixed ID comes from the `key` in `extension/manifest.json`;
`tests/test_service.py` checks it matches `CHROMIUM_EXTENSION_IDS` in
`yume/native_host.py`. Publishing to a store assigns a different ID — add it there.

## Custom Whisper models

`whisper_model` may be a CTranslate2 model directory (`model.bin`,
`config.json`, `tokenizer.json`, `vocabulary.txt`); `whisper_model_name` gives
it a display name. Convert checkpoints with:

```bash
ct2-transformers-converter --model user/model-name --output_dir /path/to/ct2-model --quantization float16
```

Standard models are cached in `~/.cache/huggingface/hub/`; GGUF translation
models in `models/translation/`.

## Adding a source language

1. `<option>` in `popup.html`, `ROMA_LABELS` / `CJK_FONTS` in `popup.js`.
2. A deterministic romanizer in `_romanize._DETERMINISTIC`, or add the language
   to `_LLM_SCRIPTS` if it is non-Latin; Latin-script languages need nothing.
3. Hallucination phrases for the language in `_filter.HALLUCINATION_PATTERNS`.
4. `LANG_NAMES` in `_translate.py` for prompts.
5. RTL scripts: `subtitle-window.js` `updateSubtitle()`.
