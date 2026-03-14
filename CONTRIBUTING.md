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
python pocket_yume.py --verbose launch   # debug logging
```

**Extension** (Chrome/Brave/Edge):
- Edit files in `extension/js/`, `extension/css/`, or `extension/popup.*`
- Reload the extension in `chrome://extensions` after changes
- Open the service worker console for background.js logs

## What to Work On

- Check [Issues](https://github.com/jenox645/Yume/issues) for open tasks
- `python pocket_yume.py health` shows what's broken on your setup
- The external review series tracks known issues — see README changelog

**High-impact areas:**
- Whisper hallucination filtering (new patterns for any language)
- Translation prompt quality for non-Japanese languages
- Cross-platform testing (macOS, Linux distros)
- UI/UX improvements to the popup

## Code Style

**Python**: Follow PEP 8. Use `_run()` instead of bare `subprocess.run()`. Log errors at WARNING+ level, not DEBUG.

**JavaScript**: No build tools. Plain JS, no TypeScript. Use `async`/`await` over `.then()` chains. Always `return true` from message handlers that call `sendResponse` asynchronously.

## Pull Request Process

1. Create a feature branch from `main`
2. Keep PRs focused — one feature or fix per PR
3. Test with `python -m py_compile pocket_yume.py` and `node --check extension/js/*.js`
4. Update version strings if changing behavior (search for the current version across all files)
5. Describe what you changed and why in the PR description

## Architecture Quick Reference

| Component | File | Role |
|-----------|------|------|
| CLI | `pocket_yume.py` | Installer, launcher, menus |
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

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
