# Developer Guide

> Yume v5.7.0 · Pocket Yume CLI

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

### Settings migration

When settings keys are renamed (e.g., `showJapanese` → `showOriginal` in v4.6.0), the migration runs at `loadSettings()` in popup.js:

```javascript
if ('showJapanese' in raw && !('showOriginal' in raw)) {
  raw.showOriginal = raw.showJapanese;
  delete raw.showJapanese;
  chrome.storage.local.set({ settings: raw });
}
```


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

## Common Pitfalls

| Pitfall | Why | Prevention |
|---------|-----|------------|
| `subprocess.run()` on Windows | cp1252 encoding crashes on Japanese output | Always use `_run()` wrapper |
| `except: pass` | Swallows SystemExit, KeyboardInterrupt, MemoryError | Use specific types + `_log.debug()` |
| `chrome.storage.session` in popup | Data lost when popup closes | Use `chrome.storage.local` for persistent settings |
| Translation `\|\| seg.text` fallback | Shows source text as translation (misleading) | Leave `english` empty if LLM fails |
| LLM timeout 30s default | 12B models on consumer GPUs need 15-60s | Pass `120000` explicitly to `_fetchWithTimeout` |
| `no_speech_threshold=0.45` | Drops sung vocals over instruments | Set to 0.6+ for music content |
| Helper functions swallowing `env=` | CUDA/ROCm build silently falls back to CPU | Always pass `env=` through any wrapper that calls `_run()` or `subprocess.run()` |
| Config key exists but is never used | Feature looks active but is a no-op (deno bug) | Run `pytest tests/test_integration.py` — dead config detection catches this |
| `tempfile.mkdtemp()` without cleanup | Failed downloads leak ~50MB dirs in system temp | Always `shutil.rmtree(tmp_dir)` in error/finally paths |
| Caching empty results | Stale empty cache served forever on re-runs | Never cache results with 0 segments — they may be transient failures |
| `innerHTML` with user content | XSS if Whisper returns `<script>` in hallucinated text | Always use `_escapeHtml()` for any text from Whisper/LLM responses |
| Translation cache with no TTL | Model switch serves old model's translations | Set a TTL (30 min) so cache expires after model changes |
| Batch parser drops unnumbered LLM output | Translation looks empty when LLM responds without `[N]` markers | Use positional fallback when no markers found, check content not just array length |
| Session saves untranslated placeholders | Page reload restores `english: ''` segments | Only save fully translated chunks to session storage |
| `cublas64_12.dll` not found | CUDA Toolkit incomplete — model loads but inference crashes | Detect in prewarm, auto-fall back to CPU, show install instructions |
| Unbounded caches | Memory grows forever on long-running servers | Always enforce `_CACHE_MAX` with eviction on insert |
| Server config overlay overwrites CLI args | CLI resolves `auto` → `cuda`, but server reads config and gets `auto` back | Don't load `whisper_device`/`whisper_compute_type` from config in server — CLI already resolves them |
| Missing stdlib import | Adding `logging.basicConfig()` without `import logging` crashes server on startup | Run `pytest tests/test_integration.py` — `TestImportCompleteness` catches this via AST |
| Translation server shows red while busy | LLM takes 10-20s per request, health endpoint times out | Use socket connect fallback + require 3 consecutive failures before showing disconnected |
| Non-UTF-8 bytes in model metadata | llama.cpp log callback hits `UnicodeDecodeError` on tokenizer metadata | Set `PYTHONUTF8=1` in the translation subprocess environment |

## Adding a New Feature

1. Add function in the relevant section of `pocket_yume.py`
2. Wire it to a menu (`main_menu`, `settings_menu`, `tools_menu`, `_runtime_menu`)
3. Optionally add a CLI subcommand in `main()`
4. Verify: `python -m py_compile pocket_yume.py`
5. Test with `--verbose` to see debug output
6. Run `pytest tests/ -v` to verify nothing broke

## Testing

```bash
pytest tests/ -v                           # all tests (54 as of v5.7.0)
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

| Step | Failure | Symptom | Handling |
|------|---------|---------|----------|
| `install_python_deps()` | Missing C++ build tools on Windows | `Microsoft Visual C++ is required` | Detects missing MSVC, shows download URL |
| `install_llamacpp_python()` | CUDA prebuilt wheel unavailable for Python version | Silent fallback to CPU | Shows elapsed timer, falls back through: CUDA prebuilt → CUDA source → CPU prebuilt → CPU source |
| `install_llamacpp_python()` | ROCm not installed on AMD Linux | `cannot find -lhipblas` | Shows ROCm install commands per distro |
| `install_llamacpp_python()` | No ninja/cmake on Linux/macOS | Build from source fails | `_install_build_tools()` auto-installs via package manager |
| `install_ffmpeg()` | Download URL changed | 404 error | Shows "URL may have changed, try updating Yume" |
| `install_ytdlp()` | GitHub rate-limited | Download fails | Caught by `download_file()` error handler |
| `browse_hf()` | HuggingFace rate-limited | Slow or failed download | User sees progress bar, can retry |
| Server launch | `cublas64_12.dll` missing | Model loads but transcription crashes | Prewarm detects and auto-falls back to CPU |
| Server launch | Port already in use | Server won't bind | `ensure_port_free()` offers to kill or reassign |
| Server launch | Python 3.13+ incompatible wheels | `pip install` fails for binary packages | Not yet handled — document minimum Python |

### Python version compatibility

`faster-whisper` and `llama-cpp-python` ship pre-built wheels for specific Python versions. If the user has a version without wheels (e.g., Python 3.14), pip falls back to building from source, which requires C++ build tools. The wizard handles this on Linux/macOS (`_install_build_tools()`) but on Windows, the user must install Visual Studio Build Tools manually.

Tested Python versions: 3.10, 3.11, 3.12, 3.13. Python 3.14 works but requires build tools on all platforms.
