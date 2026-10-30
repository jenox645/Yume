"""Server settings: the config file (read at start, re-read when it changes),
the switched Whisper model written back to it, and the user blacklist.
"""

import json
import os
import threading
from pathlib import Path

import _state

MAX_PORT = 65535
_blacklist_lock = threading.Lock()

LIVE_KEYS = {
    "translation_host": "translation_host",
    "translation_port": "translation_port",
    "translation_backend": "translation_backend",
    "translation_model": "translation_model",
    "translation_prompt": "translation_prompt",
    "romanization_prompt": "romanization_prompt",
    "vocal_isolation": "vocal_isolation",  # not translation, but re-read live the same way
}


def _port_or(value, fallback):
    """A usable port from a hand-edited config value, or the fallback."""
    try:
        port = int(value)
    except (TypeError, ValueError):
        port = 0
    if 1 <= port <= MAX_PORT:
        return port
    print(f"[Yume] Config: port {value!r} is not valid, using {fallback}")
    return fallback


def load_live_config(cfg):
    """Copy the translation keys of a config dict into _state."""
    for key, attr in LIVE_KEYS.items():
        if key in cfg:
            value = cfg[key] if cfg[key] is not None else ""
            if key == "translation_port":
                value = _port_or(value, _state.translation_port)
            setattr(_state, attr, value)


def _read_config(path):
    """The config file as a dict. A broken one is the same as an empty one: the
    CLI has already said so and carried on with defaults, so the server must too."""
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except (OSError, ValueError) as e:
        print(f"[Yume] Config file ignored, using defaults: {e}")
        return {}
    if isinstance(cfg, dict):
        return cfg
    print("[Yume] Config file ignored, using defaults: it is not a JSON object")
    return {}


def reload_config():
    """Re-read the config file when it changed (one stat() per LLM request), so
    translation settings edited in the CLI apply to the running server."""
    path = _state.config_file
    if not path:
        return
    try:
        mtime = os.path.getmtime(path)
        if mtime == _state.config_mtime:
            return
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
        _state.config_mtime = mtime
    except (OSError, ValueError):
        return  # missing or mid-write: keep the current settings
    if not isinstance(cfg, dict):
        return
    before = translation_settings(reload=False)
    load_live_config(cfg)
    if translation_settings(reload=False) != before:
        print("[Yume] Translation settings reloaded from the config file")


def translation_settings(reload=True):
    """Live translation config for the Translator (read on every request)."""
    if reload:
        reload_config()
    return {
        "host": _state.translation_host,
        "port": _state.translation_port,
        "backend": _state.translation_backend,
        "model": _state.translation_model,
        "prompt": _state.translation_prompt,
        "roma_prompt": _state.romanization_prompt,
    }


def apply_config(args):
    """Apply CLI args, then overlay config file values into _state."""
    _state.model_name = args.model
    _state.device = args.device
    _state.compute_type = args.compute_type

    if args.config and os.path.exists(args.config):
        cfg = _read_config(args.config)
        _state.model_name = cfg.get("whisper_model", _state.model_name)
        _state.model_display_name = cfg.get("whisper_model_name", "")
        # NOTE: whisper_device / whisper_compute_type are intentionally NOT loaded
        # from config here.  The CLI resolves "auto" → "cuda"/"cpu" before launching
        # the server and passes the resolved value via --device / --compute-type.
        args.port = _port_or(cfg.get("whisper_port", args.port), args.port)
        _state.youtube_auth_method = cfg.get("youtube_auth_method", _state.youtube_auth_method)
        _state.cookies_browser = cfg.get("cookies_browser", _state.cookies_browser)
        load_live_config(cfg)
        _state.config_file = args.config
        _state.config_mtime = os.path.getmtime(args.config)

    # Persisted user blacklist — reported hallucinations must survive restarts
    cfg_dir = Path(args.config).parent if args.config else Path(__file__).resolve().parent.parent / "config"
    _state.blacklist_file = str(cfg_dir / "blacklist.json")
    try:
        with open(_state.blacklist_file, encoding="utf-8") as f:
            items = json.load(f)
        if isinstance(items, list):
            _state.user_blacklist = [str(i).strip() for i in items if str(i).strip()]
            if _state.user_blacklist:
                print(f"[Yume] Loaded {len(_state.user_blacklist)} blacklist items from {_state.blacklist_file}")
    except FileNotFoundError:
        pass
    except Exception as e:
        print(f"[Yume] Blacklist load failed: {e}")

    # Resolve 'auto' device using CTranslate2's own detection. (This used to test
    # `"cuda" in get_supported_compute_types("cuda")` — a set of compute types like
    # {"float16", "int8"}, which never contains "cuda" — so "auto" always meant CPU.)
    if _state.device == "auto":
        try:
            import ctranslate2

            _state.device = "cuda" if ctranslate2.get_cuda_device_count() > 0 else "cpu"
        except Exception:
            _state.device = "cpu"

    if _state.compute_type == "auto":
        _state.compute_type = "float16" if _state.device == "cuda" else "int8"

    # --low-vram forces int8 regardless of GPU — reduces VRAM by ~30-40%
    if getattr(args, "low_vram", False):
        _state.compute_type = "int8"
        print("  [low-vram]        compute_type overridden to int8")


def persist_whisper_model(model):
    """Save a switched model to the config file, so the next start (launcher or
    one-click) loads it instead of reverting to the old one."""
    path = _state.config_file
    if not path:
        return
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
        cfg["whisper_model"] = model
        cfg["whisper_model_name"] = ""
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
        os.replace(tmp, path)
        _state.config_mtime = os.path.getmtime(path)  # our own write: nothing to reload
    except (OSError, ValueError) as e:
        print(f"[Yume] Could not save the model choice to the config: {e}")


def change_blacklist(change):
    """Save change(current list), one change at a time: two lines reported at
    the same moment must both stay. Returns the clean list."""
    with _blacklist_lock:
        return save_blacklist(change(_state.user_blacklist))


def save_blacklist(items):
    """Replace, persist and re-apply the user blacklist — the single source of
    truth for both the CLI and the extension popup. Returns the clean list."""
    seen = set()
    clean = []
    for item in items:
        s = str(item).strip()
        if s and s.lower() not in seen:
            seen.add(s.lower())
            clean.append(s)
    _state.user_blacklist = clean
    try:
        if _state.blacklist_file:
            tmp = f"{_state.blacklist_file}.tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(clean, f, ensure_ascii=False, indent=2)
            os.replace(tmp, _state.blacklist_file)
    except OSError as e:
        print(f"[Yume] Blacklist save failed: {e}")
    if _state.jobs:
        _state.jobs.refilter()
    print(f"[Yume] User blacklist: {len(clean)} items")
    return clean
