# Developer Guide

> Yume v0.0.8 · Pocket Yume CLI

## Setup

```bash
# First run — wizard installs everything
python pocket_yume.py

# Verify installation
python pocket_yume.py health

# Debug mode (logs all suppressed exceptions to stderr)
python pocket_yume.py --verbose launch

# Or via environment variable
LOG_LEVEL=DEBUG python pocket_yume.py launch
```

## Code Organization

### Python

| Module | Purpose |
|--------|---------|
| `config.py` | `DEFAULT_CONFIG`, `load_config()`, `save_config()`, `validate_port()`, `validate_host()`, `config_export()`, `config_import()`. All port constants (`DEFAULT_WHISPER_PORT`, `DEFAULT_TRANSLATION_PORT`, `DEFAULT_OLLAMA_PORT`) defined here. |
| `pocket_yume.py` | Everything else: CLI menus, GPU detection, tool installation, server lifecycle, benchmarks. Imports from `config.py`. |
| `faster_whisper_server.py` | Flask server. Endpoints: `/health`, `/stats`, `/transcribe`, `/transcribe_url`, `/prepare`, `/prepare_direct`, `/blacklist`, `/romanize`, `/model/switch`. |

### Extension (Chrome MV3)

| File | Loaded by | Purpose |
|------|-----------|---------|
| `background.js` | Manifest (service worker) | Server proxy, translation cache, token management |
| `content.js` | Manifest (content script) | Lifecycle, URL detection, subtitle event wiring |
| `audio-capture.js` | Manifest (content script) | Pipeline: chunk scheduling, parallel transcribe+translate |
| `subtitle-window.js` | Manifest (content script) | DOM overlay, drag/resize, fonts, RTL |
| `debug-system.js` | Manifest (content script) | Must load first. Defines global `DEBUG` object. |
| `popup.js` | popup.html (`<script>`) | Settings UI, diagnostics, stats dashboard |

## Key Patterns

### Subprocess safety (Windows)

All `subprocess.run()` calls go through `_run()` which forces UTF-8 encoding. This prevents `cp1252` crashes on Windows when Japanese text appears in stdout.

```python
# Never use bare subprocess.run() — always use _run()
result = _run(["yt-dlp", "--version"], timeout=10)
```

### Process cleanup on Ctrl+C

`launch_services()` wraps `_launch_inner()` in try/except. If Ctrl+C fires during model loading, `_cleanup()` terminates all Popen processes AND verifies ports are freed via `kill_port_process()`.

### Port management stack

```python
is_port_free(port)              # Socket connect test
find_free_port(start, exclude)  # Scan upward from start
get_port_process(port)          # lsof → ss → fuser fallback chain
kill_port_process(port)         # kill → fuser -k fallback
ensure_port_free(port, cfg)     # Interactive: kill / reassign / manual
```

### Translation pipeline

The extension sends batch translation requests to the LLM with numbered segments:

```
[1] こんにちは
[2] 今日はいい天気ですね
```

The LLM responds with numbered translations. `_parseBatchResponse()` parses by `[N]` markers. If the LLM returns fewer than expected, missing slots stay empty (no positional fallback — misaligned translations are worse than missing ones).

### Translation-only retry

When translation fails mid-pipeline, the system retries translation up to 3 times with exponential backoff — without re-transcribing the audio. This avoids wasting the expensive Whisper pass. After 3 consecutive failures, the pipeline stores source-only segments (with empty `english` fields) so the subtitle display continues with original text rather than blocking the entire pipeline.

### Startup parallelization

The yt-dlp and ffmpeg availability checks at server startup now run in parallel (via `concurrent.futures.ThreadPoolExecutor`) instead of sequentially, reducing startup time by ~1-2s.

### Settings migration

When settings keys are renamed (e.g., `showJapanese` → `showOriginal` in v4.6.0), the migration runs at `loadSettings()` in popup.js:

```javascript
if ('showJapanese' in raw && !('showOriginal' in raw)) {
  raw.showOriginal = raw.showJapanese;
  delete raw.showJapanese;
  chrome.storage.local.set({ settings: raw });
}
```


## Whisper Model Loading

### How the model flows from config to inference

```
yume_config.json: "whisper_model": "large-v3"  (or a local path)
    ↓
pocket_yume.py: reads cfg["whisper_model"], passes --model to server
    ↓
faster_whisper_server.py: argparse --model → model_name → WhisperModel(model_name, ...)
    ↓
faster-whisper: if model_name is a path → load from disk
                if model_name is a name → download from HuggingFace → ~/.cache/huggingface/hub/
```

### Model storage locations

| What | Where |
|------|-------|
| Standard Whisper models (auto-downloaded) | `~/.cache/huggingface/hub/models--Systran--faster-whisper-{name}/` |
| Older faster-whisper cache (legacy) | `~/.cache/faster_whisper/` |
| Custom/fine-tuned models | Wherever you put them — set path in config |
| Translation GGUF models | `models/translation/` inside the Yume project folder |

### Custom model support

`WhisperModel()` from faster-whisper accepts either a model name (e.g., `"large-v3"`) or a local directory path (e.g., `"/home/user/my-finetuned-whisper"`). The directory must contain CTranslate2 format files: `model.bin`, `config.json`, `tokenizer.json`, `vocabulary.txt`.

The config key `whisper_model` is passed directly to `WhisperModel()`, so setting it to a path works:

```json
{
  "whisper_model": "/path/to/my-ct2-model",
  "whisper_model_name": "My Fine-tune"
}
```

The optional `whisper_model_name` key provides a friendly display name for custom models. When set, it appears in the CLI menus, the popup's Active Model display, and the server's `/stats` response. If empty, the directory name is used instead.

The popup model switcher dynamically adds custom models to the dropdown when the server reports a custom path via `/stats`. The CLI's Whisper Model menu (`_menu_whisper_model()`) includes a "Custom model (local path)" option that validates the CTranslate2 directory structure and prompts for a friendly name.

### Converting models to CTranslate2

Fine-tuned Whisper checkpoints (HuggingFace format or OpenAI format) must be converted before use:

```bash
# From OpenAI checkpoint
ct2-openai-whisper-converter --model /path/to/checkpoint --output_dir /path/to/ct2-model

# From HuggingFace model
ct2-transformers-converter --model user/model-name --output_dir /path/to/ct2-model --quantization float16
```

### Adding a custom model to the popup switcher

If you want to add a new model name to the hot-swap dropdown, update `valid_models` in `switch_model()` in `faster_whisper_server.py` and add a matching `<option>` in `popup.html` under the Whisper Model section.

## Font System

The font system has three layers:

1. **System font detection** (`popup.js`): Uses canvas-based measurement to detect which fonts are actually installed. Groups fonts by source language (JA/ZH/KO/AR) in the dropdown.

2. **Bundled fonts** (`extension/fonts/`): Users can place `.ttf`/`.otf` files here and list them in `BUNDLED_FONTS` in `popup.js`. `subtitle-window.js` injects `@font-face` rules at runtime via `chrome.runtime.getURL()`.

3. **CLI detection** (`pocket_yume.py fonts`): Scans platform font directories (`C:\Windows\Fonts`, `/usr/share/fonts`, `~/Library/Fonts`) for known CJK font families.

### Adding a bundled font

```js
// In popup.js
const BUNDLED_FONTS = [
  { file: 'MyFont-Regular.ttf', name: 'My Font' },
];
```

Place the file in `extension/fonts/` and reload the extension.

## Romanization Pipeline

Two strategies, chosen by language:

| Strategy | Languages | Library | Speed | Speculative? |
|----------|-----------|---------|-------|-------------|
| Deterministic | ja (`pykakasi`), zh (`pypinyin`), ko (`romanization`) | Server-side | <100ms | Yes — always computed in parallel with translation |
| LLM-based | ru, ar, and fallback for ja/zh/ko if libs missing | Translation LLM | 5-30s | No — only when romaji enabled |

**Key implementation details:**
- pykakasi pre-loads in a background thread before Whisper model load. Accessor `_get_kakasi()` uses `threading.Lock()`. Probe timeout: 60s.
- Translation and romanization run via `Promise.all()` in `audio-capture.js` — romanization only needs source text, not translation output.
- LLM romanization prompt customizable via `romanization_prompt` config key (`{src}` and `{sys}` placeholders). Flows: CLI config → server → `/health` → extension auto-discovers.

## Developer Conventions

These are enforced rules, not suggestions. The test suite and CI check most of them.

### Subprocess: no shell, no os.system

Never use `shell=True`, `os.system()`, or pipe-to-shell patterns. All process launches must use explicit argv lists.

```python
# WRONG — shell injection risk, breaks on paths with spaces
os.system("cls")
subprocess.run(["chcp", "65001"], shell=True)
subprocess.Popen([str(path)], shell=True)
_run(["bash", "-c", "curl -fsSL https://example.com/install.sh | sh"])

# CORRECT
subprocess.run(["cmd", "/c", "cls"])                          # Windows clear
ctypes.windll.kernel32.SetConsoleOutputCP(65001)              # Windows codepage
subprocess.Popen([str(path)])                                  # launch exe directly
download_file(url, script, "label"); _run(["bash", str(script)])  # download first
sys.stdout.write("\033[2J\033[H"); sys.stdout.flush()          # ANSI clear (Unix)
```

On Windows, use `ctypes.windll.kernel32` for console operations (codepage, VT100) instead of `cmd.exe` wrappers.

### innerHTML: always escape dynamic content

Any value from a server response, user input, or external source must go through `_escapeHtml()` before being inserted into `innerHTML`. Static HTML structure is fine — dynamic values are not.

```javascript
// WRONG — backend name could contain <script> tags
html += `<b>${backend}</b>`;
html += `${m.name}`;

// CORRECT
html += `<b>${_escapeHtml(backend)}</b>`;
html += `${_escapeHtml(m.name)}`;
```

For subtitle content, always use `textContent` (already enforced in `subtitle-window.js`).

### URL defaults: use helpers, not literals

Default server URLs live in `DEFAULT_SETTINGS` (background.js) and `getDefaultSettings()` (popup.js). Never hardcode `'http://localhost:5001'` or `'http://localhost:5000'` inline.

```javascript
// WRONG — scattered fallbacks that drift if ports change
const whisperUrl = settings?.whisperUrl || 'http://localhost:5001';

// CORRECT — single source of truth
const whisperUrl = _whisperUrl(settings);
const translationUrl = _translationUrl(settings);
```

### Dependencies: pin exact versions

All Python packages use `==` (exact version), never `>=` (minimum). This applies to `requirements.txt` and all inline `pip install` calls in `pocket_yume.py`.

```
# WRONG — untested future versions auto-install
faster-whisper>=1.0.0

# CORRECT — deliberately tested version
faster-whisper==1.2.1
```

When upgrading a dependency: update the version in `requirements.txt`, update all inline `pip install` calls in `pocket_yume.py` (search for the package name — there may be 2-3 copies), test, then commit.

### Version bumping

Version must match in **all** of these locations (the test suite checks most of them):

1. `pocket_yume.py` → `VERSION = "X.Y.Z"` + docstring
2. `faster_whisper_server.py` → `"version": "X.Y.Z"` in /health + startup banner + docstring
3. `extension/manifest.json` → `"version": "X.Y.Z"`
4. `extension/manifest_firefox.json` → `"version": "X.Y.Z"`
5. `extension/popup.html` → `vX.Y.Z`
6. `extension/popup.js` → comment header
7. `extension/js/background.js` → comment header + `version:` in onInstalled
8. `extension/js/audio-capture.js` → comment header
9. `extension/js/subtitle-window.js` → comment header
10. `extension/js/content.js` → comment header
11. `README.md` → badge `version-X.Y.Z-blue`
12. `ARCHITECTURE.md` → `vX.Y.Z`
13. `DEVELOPER_GUIDE.md` → `vX.Y.Z`
14. `tests/test_integration.py` → docstring

Use `sed -i 's/5\.8\.0/5.9.0/g'` across all files, then verify with `pytest tests/test_integration.py::TestVersionConsistencyExtended -v`.

### Single-file CLI: don't split pocket_yume.py

`pocket_yume.py` is one file by design (~4100 lines). Users must be able to run `python pocket_yume.py` after downloading a single file. Only `config.py` has been extracted (it's imported at the top). Don't create new modules.

### Config keys: must be used or exempted

Every key in `DEFAULT_CONFIG` (config.py) must be referenced in application code. The dead config detection test (`TestDeadConfigDetection`) catches orphaned keys. If you add a key that's intentionally display-only, add it to `EXEMPT_KEYS` in the test with a comment explaining why.

### Server config: CLI resolves, server trusts

The CLI resolves `"auto"` → `"cuda"` (or `"cpu"`) and passes the result to the server via `--device`. The server must **not** re-read `whisper_device` or `whisper_compute_type` from the config file — this caused the v5.7.0 CPU bug.

### Error handling: specific exceptions, not bare except

```python
# WRONG
except:
    pass

# CORRECT
except (FileNotFoundError, PermissionError) as e:
    _log.debug(f"Non-critical: {e}")
```

`except Exception` is acceptable for top-level fallbacks where you log the error. Never catch `BaseException` or bare `except:`.

### Temp files: always clean up

Every `tempfile.mkdtemp()` must have a matching `shutil.rmtree()` in the error path. Prefer `try/finally` or ensure the function's error return path includes cleanup. The startup function `_cleanup_stale_temps()` catches leaks, but don't rely on it.

## Common Pitfalls (Yume-specific)

| Pitfall | Prevention |
|---------|------------|
| Hardcoded `localhost:5001` | Use `_whisperUrl()` / `_translationUrl()` helpers |
| `subprocess.run()` on Windows | Use `_run()` wrapper (forces UTF-8, avoids cp1252 crashes) |
| Unescaped `innerHTML` | Always `_escapeHtml()` for Whisper/LLM output |
| `chrome.storage.session` in popup | Use `chrome.storage.local` — session data lost on popup close |
| LLM timeout 30s default | 12B models need 15-60s — pass `120000` to `_fetchWithTimeout` |
| `no_speech_threshold=0.45` | Drops sung vocals — use 0.6+ for music |
| Config key exists but unused | `pytest tests/test_integration.py` catches dead config keys |
| `tempfile.mkdtemp()` without cleanup | Always `shutil.rmtree()` in error/finally paths |
| Caching empty results | Never cache 0-segment results — may be transient failures |
| Translation cache with no TTL | Set 30-min TTL so model switches don't serve stale data |
| Server config overlay overwrites CLI args | Don't load `whisper_device` from config in server — CLI resolves it |
| `cublas64_12.dll` not found | Prewarm detects and auto-falls back to CPU |
| Unpinned dependencies (`>=`) | Always use `==` in requirements.txt |

## Adding a New Feature

1. Add function in the relevant section of `pocket_yume.py`
2. Wire it to a menu (`main_menu`, `settings_menu`, `tools_menu`, `_runtime_menu`)
3. Optionally add a CLI subcommand in `main()`
4. Verify: `python -m py_compile pocket_yume.py`
5. Test with `--verbose` to see debug output
6. Run `pytest tests/ -v` to verify nothing broke

## Linting (Ruff)

Yume uses [Ruff](https://docs.astral.sh/ruff/) for Python linting. Configuration lives in `pyproject.toml` at the project root:

```bash
ruff check .                 # check all files
ruff check --fix .           # auto-fix what it can
ruff check pocket_yume.py    # check one file
```

### pyproject.toml rules

- **Selected**: `E` (pycodestyle errors), `F` (pyflakes), `W` (warnings)
- **Ignored globally**: `E501` (line too long — handled separately)
- **Per-file ignores**: `E402` on `pocket_yume.py` and `tests/*` (imports after version/path checks are intentional)

### Suppressed warnings (intentional)

- **`# noqa: F401`** on import availability checks (e.g., `import pykakasi` inside try/except to set a `has_pykakasi` flag). These imports are used for their side effect, not their name.
- **`E402`** on `pocket_yume.py` lines 38-40: `from config import ...` must come AFTER the Python 3.10 version check on line 13.

### Before committing

Run `ruff check .` and ensure zero violations. The CI will reject PRs with lint errors.

## Testing

```bash
pytest tests/ -v                           # all tests
pytest tests/test_integration.py -v        # integration tests
pytest tests/test_config.py -v             # config unit tests
pytest tests/test_server_logic.py -v       # server logic tests
```

### Test categories

**Unit tests** (`test_config.py`, `test_server_logic.py`): Test individual functions — config validation, URL parsing, hallucination patterns. These are fast and need no servers.

**Integration tests** (`test_integration.py`): Verify that config options actually affect behavior. These exist because of the deno incident (v1.0-v5.4.2: `youtube_auth_method="deno"` was a no-op, never caught by code review). The tests include:

- **Dead config detection**: Flags any `DEFAULT_CONFIG` key that exists but is never used in application code. If you add a new config key, these tests will fail unless the key is actually used somewhere.
- **Auth config tracing**: Verifies that `youtube_auth_method` values produce the correct yt-dlp arguments.
- **Health check consistency**: Verifies CLI and popup use the same health endpoints per backend.
- **Version consistency**: All 7+ files that contain version strings must agree.
- **Subprocess hygiene**: No hardcoded `"yt-dlp"` in subprocess calls (must use `_ytdlp_cmd()`).
- **Naming consistency**: No camelCase project name in user-facing docs (must be "Yume" or "Pocket Yume").

### Testing without a GPU

You can develop and test Yume on CPU. Set these in your config:

```json
{
  "whisper_device": "cpu",
  "whisper_model": "tiny"
}
```

Or run the setup wizard — it auto-detects CPU mode. The `tiny` model loads in seconds and transcribes in real-time on most machines. Translation, the extension UI, subtitle rendering, and all CLI features work identically on CPU. Only transcription speed is affected.

For the translation backend, Ollama is the easiest to set up without compiling anything: `ollama pull qwen2.5:3b` gives you a working translator in one command.

### Adding a new config key

If you add a key to `DEFAULT_CONFIG` in `config.py`:

1. Use it somewhere in application code (not just display it in a menu)
2. Run `pytest tests/test_integration.py::TestDeadConfigDetection -v` to verify it's detected
3. If the key is intentionally display-only, add it to `EXEMPT_KEYS` in the test with a comment

## Adding a New Source Language

1. Add `<option>` to `popup.html` source language dropdown
2. Add entry to `ROMA_LABELS` in `popup.js` (if romanization exists)
3. Add hallucination patterns to `HALLUCINATION_PATTERNS` in `faster_whisper_server.py`
4. Add fonts to `CJK_FONTS` in `popup.js` for the new language's script
5. If RTL: add direction handling in `subtitle-window.js` `updateSubtitle()`

## Installation Architecture

The setup wizard (`pocket_yume.py setup_wizard()`) runs on first launch. Here's what it does and where it can fail:

```
START_YUME.bat/sh/command
  └─ python pocket_yume.py
       └─ main() → main_menu() → setup_wizard(cfg)
            ├─ System scan (GPU, RAM, disk)
            ├─ Component check:
            │   ├─ yt-dlp binary (find_tool → PATH + tools/)
            │   ├─ FFmpeg binary (find_tool → PATH + tools/)
            │   ├─ Deno binary (optional)
            │   ├─ faster-whisper (import check)
            │   ├─ llama-cpp-python (import check)
            │   ├─ uvicorn + fastapi (import check)
            │   └─ GGUF model files (models/translation/)
            ├─ Install missing:
            │   ├─ install_ytdlp()      → downloads binary to tools/
            │   ├─ install_ffmpeg()     → downloads + extracts to tools/
            │   ├─ install_python_deps() → pip install -r requirements.txt
            │   ├─ install_llamacpp_python() → pip install with CUDA/ROCm/CPU
            │   └─ browse_hf()         → downloads GGUF to models/translation/
            ├─ YouTube auth config
            └─ save_config(cfg)
```

### Known installation failure points

| Step | Failure | Handling |
|------|---------|----------|
| `install_python_deps()` | Missing C++ build tools (Windows) | Detects missing MSVC, shows download URL |
| `install_llamacpp_python()` | No CUDA/ROCm prebuilt wheel | Falls back: CUDA prebuilt → source → CPU prebuilt → source |
| `install_ffmpeg()` / `install_ytdlp()` | Download URL changed or rate-limited | Error handler shows retry guidance |
| Server launch | `cublas64_12.dll` missing | Prewarm detects, auto-falls back to CPU |
| Server launch | Port in use | `ensure_port_free()` offers to kill or reassign |

### Python version compatibility

Tested: 3.10, 3.11, 3.12, 3.13, 3.14. Versions without pre-built wheels (3.14) require C++ build tools — the wizard auto-installs on Linux/macOS, but Windows users need Visual Studio Build Tools.
