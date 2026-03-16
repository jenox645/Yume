"""PocketYume configuration — load, save, validate, export, import."""

import json, os, re, time, logging
from pathlib import Path

_log = logging.getLogger('pocket_yume.config')

# Use logging for diagnostics — no monkey-patching needed

# Paths
BASE_DIR = Path(__file__).parent.resolve()
CONFIG_DIR = BASE_DIR / "config"
CONFIG_FILE = CONFIG_DIR / "yume_config.json"
if not CONFIG_FILE.exists() and (BASE_DIR / "yume_config.json").exists():
    CONFIG_FILE = BASE_DIR / "yume_config.json"

# Port defaults
DEFAULT_WHISPER_PORT = 5001
DEFAULT_TRANSLATION_PORT = 5000
DEFAULT_OLLAMA_PORT = 11434

# Defaults
DEFAULT_CONFIG = {
    "whisper_model": "large-v3",
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
    "first_run_complete": False,
}

MAX_PORT = 65535


def load_config() -> dict:
    """Load config from disk, merged with defaults."""
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                return {**DEFAULT_CONFIG, **json.load(f)}
        except Exception as e:
            _log.debug('[load_config] config-parse failed: %s', e)
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
    if re.match(r'^[a-zA-Z0-9._-]+$', value) or re.match(r'^\d+\.\d+\.\d+\.\d+$', value):
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
