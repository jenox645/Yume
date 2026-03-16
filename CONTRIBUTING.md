# Contributing to Yume

Thanks for your interest in contributing! Yume is a local AI subtitle system — contributions that improve transcription quality, translation speed, or user experience are especially welcome.

## Getting Started

1. Fork the repository and clone it locally
2. Run `python pocket_yume.py setup` to install dependencies
3. Load the extension from `extension/` in Chrome (Developer Mode → Load unpacked)
4. Start the server with `python pocket_yume.py launch`

## Development Setup

**Python** (server + CLI):
```bash
pip install -r server/requirements.txt
pip install pytest                       # for running tests
python pocket_yume.py --verbose launch   # debug logging
```

**Extension** (Chrome/Brave/Edge):
- Edit files in `extension/js/`, `extension/css/`, or `extension/popup.*`
- Reload the extension in `chrome://extensions` after changes
- Open the service worker console for background.js logs

**Testing without a GPU:**
You can develop and test most of Yume without an NVIDIA or AMD GPU:
```bash
# In your config, set:
#   whisper_device: cpu
#   whisper_model: tiny (or base or small)
# CPU mode is slower but fully functional for development.
python pocket_yume.py setup   # wizard auto-detects CPU mode
```
The extension, popup UI, subtitle overlay, and translation pipeline all work identically on CPU. Only transcription speed is affected.

## Running Tests

```bash
pytest tests/ -v                           # all tests
pytest tests/test_integration.py -v        # integration tests only
pytest tests/test_config.py -v             # config unit tests only
```

The integration tests (`test_integration.py`) exist to prevent silent no-op bugs — they verify that config options actually affect behavior. If you add a new config key, add a test that proves it does something.

## What to Work On

Check [Issues](https://github.com/jenox645/Yume/issues) for open tasks. Look for the `good first issue` label.

**Concrete starter tasks:**
- Add a progress indicator for llama-cpp-python download (currently shows elapsed time, could show download size)
- Document wanakana.js version and source URL in a comment at the top of the file
- Add `--version` flag output to `python pocket_yume.py status` (currently only in `--version`)
- Write a test that verifies popup.js and pocket_yume.py use the same default port numbers
- Improve error messages when the server can't bind to a port (show which process is using it)

**High-impact areas:**
- Translation prompt quality for non-Japanese source languages (Chinese, Korean, Russian, Arabic) — compare LLM output quality across different system prompts
- Cross-platform testing: macOS (especially Apple Silicon), Linux distros beyond Fedora/Ubuntu
- Subtitle rendering improvements: better timing sync, karaoke-style word highlighting
- NLLB fast translation mode as an alternative to LLM translation (50-100x faster, lower quality)

## Code Style

**Python**: Follow PEP 8. Use `_run()` instead of bare `subprocess.run()` (forces UTF-8 on Windows). Log errors at WARNING+ level, not DEBUG. Never use `except: pass` — catch specific exceptions.

**JavaScript**: No build tools. Plain JS, no TypeScript. Use `async`/`await` over `.then()` chains. Always `return true` from message handlers that call `sendResponse` asynchronously.

## Pull Request Process

1. Create a feature branch from `main`
2. Keep PRs focused — one feature or fix per PR
3. Run the full test suite: `pytest tests/ -v`
4. Run syntax checks: `python -m py_compile pocket_yume.py && node --check extension/js/*.js`
5. If you changed behavior, update version strings (search for the current version across all files)
6. Describe what you changed and why in the PR description

## Architecture Quick Reference

| Component | File | Role |
|-----------|------|------|
| CLI | `pocket_yume.py` | Installer, launcher, menus (Pocket Yume) |
| Config | `config.py` | Load/save/validate settings |
| Server | `server/faster_whisper_server.py` | Whisper STT + hallucination filter |
| Pipeline | `extension/js/audio-capture.js` | Chunk scheduling, parallel transcribe+translate |
| Background | `extension/js/background.js` | Service worker, server proxy, caches |
| UI | `extension/js/content.js` | Lifecycle, event wiring |
| Overlay | `extension/js/subtitle-window.js` | DOM subtitle rendering |

See `ARCHITECTURE.md` and `DEVELOPER_GUIDE.md` for full details.

## Reporting Bugs

1. Run `python pocket_yume.py health` and include the output
2. Include your OS, GPU, and Whisper model
3. For extension bugs, include the browser console log and service worker log
4. Screenshots of subtitle rendering issues are very helpful

Use the [bug report template](https://github.com/jenox645/Yume/issues/new?template=bug_report.md) when opening an issue.

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
