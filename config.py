"""Pocket Yume configuration — load, save, validate, export, import."""

from __future__ import annotations  # Allows int | str type hint syntax

import json
import re
import time
import logging
from pathlib import Path

_log = logging.getLogger("pocket_yume.config")

# Use logging for diagnostics — no monkey-patching needed

# Paths
BASE_DIR = Path(__file__).parent.resolve()
CONFIG_DIR = BASE_DIR / "config"
CONFIG_FILENAME = "yume_config.json"
CONFIG_FILE = CONFIG_DIR / CONFIG_FILENAME
if not CONFIG_FILE.exists() and (BASE_DIR / CONFIG_FILENAME).exists():
    CONFIG_FILE = BASE_DIR / CONFIG_FILENAME

# Port defaults
DEFAULT_WHISPER_PORT = 5001
DEFAULT_TRANSLATION_PORT = 5000
DEFAULT_OLLAMA_PORT = 11434

# Defaults
DEFAULT_CONFIG = {
    "whisper_model": "large-v3",
    "whisper_model_name": "",
    "whisper_device": "auto",
    "whisper_compute_type": "auto",
    "whisper_host": "127.0.0.1",
    "whisper_port": DEFAULT_WHISPER_PORT,
    "word_timestamps": False,
    "pause_threshold": 0.25,
    "chunk_duration": 30,
    "language": "ja",
    "translation_backend": "llamacpp",
    "translation_host": "127.0.0.1",
    "translation_port": DEFAULT_TRANSLATION_PORT,
    "translation_model": "",
    "gguf_model_path": "",
    "youtube_auth_method": "cookies",
    "cookies_browser": "chrome",
    "romanization_prompt": "",
    "first_run_complete": False,
}

MAX_PORT = 65535


def load_config() -> dict:
    """Load config from disk, merged with defaults."""
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                _log.warning("[load_config] Config file is not a JSON object — using defaults")
                return dict(DEFAULT_CONFIG)
            return {**DEFAULT_CONFIG, **data}
        except json.JSONDecodeError as e:
            # Common cause: user manually edited the file with unescaped backslashes
            # (e.g., C:\Users\... instead of C:\\Users\\... in JSON)
            print(f"\n  !  Config file has invalid JSON: {e}")
            print(f"     File: {CONFIG_FILE}")
            # Try to recover by fixing common backslash issues
            try:
                raw = CONFIG_FILE.read_text(encoding="utf-8")
                # Replace single backslashes that aren't already escaped or part of JSON escapes
                fixed = re.sub(r'(?<!\\)\\(?![\\"/bfnrtu])', r"\\\\", raw)
                data = json.loads(fixed)
                if isinstance(data, dict):
                    print("     Auto-recovered by fixing backslash escapes.")
                    print("     Tip: Use forward slashes (/) or double backslashes (\\\\) in paths.\n")
                    save_config({**DEFAULT_CONFIG, **data})  # save the fixed version
                    return {**DEFAULT_CONFIG, **data}
            except Exception:
                pass
            print("     Using default settings. Your config file was not overwritten.")
            print(f"     Fix the JSON manually or delete {CONFIG_FILE.name} to start fresh.\n")
        except Exception as e:
            _log.debug("[load_config] config-parse failed: %s", e)
    return dict(DEFAULT_CONFIG)


def save_config(cfg: dict) -> None:
    """Write config to disk."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def validate_port(value: int | str, name: str = "Port") -> int | None:
    """Validate port number. Returns int or None."""
    try:
        p = int(value)
        if 1 <= p <= MAX_PORT:
            return p
        print(f"  x  {name} must be 1-{MAX_PORT}, got {p}")
        return None
    except (ValueError, TypeError):
        print(f"  x  {name} must be a number, got {value!r}")
        return None


def validate_host(value: str | None) -> str | None:
    """Validate hostname/IP. Returns string or None."""
    if value is None:
        print("  x  Host cannot be None")
        return None
    value = str(value).strip()
    if not value:
        print("  x  Host cannot be empty")
        return None
    if re.match(r"^[a-zA-Z0-9._-]+$", value) or re.match(r"^\d+\.\d+\.\d+\.\d+$", value):
        return value
    print(f"  x  Invalid host: {value!r}")
    return None


def config_export(cfg: dict, path: Path | str | None = None) -> bool:
    """Export config to a portable JSON file."""
    if path is None:
        path = BASE_DIR / f"yume_config_backup_{time.strftime('%Y%m%d_%H%M%S')}.json"
    else:
        path = Path(path)
    try:
        export = dict(cfg)
        export.pop("gguf_model_path", None)
        path.write_text(json.dumps(export, indent=2), encoding="utf-8")
        print(f"  +  Config exported to {path.name}")
        return True
    except Exception as e:
        print(f"  x  Export failed: {e}")
        return False


def config_import(path: Path | str) -> dict | None:
    """Import config from a JSON file. Merges with defaults."""
    path = Path(path)
    if not path.exists():
        print(f"  x  File not found: {path}")
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            print("  x  Invalid config file (not a JSON object)")
            return None
        merged = dict(DEFAULT_CONFIG)
        merged.update(data)
        save_config(merged)
        print(f"  +  Config imported from {path.name}")
        print("  i  Restart Yume for all changes to take effect")
        return merged
    except json.JSONDecodeError as e:
        print(f"  x  Invalid JSON: {e}")
        return None
    except Exception as e:
        print(f"  x  Import failed: {e}")
        return None
