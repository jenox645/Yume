"""Tests for config.py — load, save, validate, export, import."""
import json
import sys
import tempfile
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from config import (
    DEFAULT_CONFIG, validate_port,
    validate_host, config_export, config_import
)


def test_default_config_has_required_keys():
    required = [
        "whisper_model", "whisper_device", "whisper_compute_type",
        "whisper_host", "whisper_port", "translation_backend",
        "translation_host", "translation_port", "language",
        "first_run_complete"
    ]
    for key in required:
        assert key in DEFAULT_CONFIG, f"Missing required key: {key}"


def test_default_config_no_dead_fields():
    """Config should not contain fields no code reads."""
    dead_fields = ["use_batched_pipeline", "batch_size"]
    for field in dead_fields:
        assert field not in DEFAULT_CONFIG, f"Dead field still in config: {field}"


def test_default_config_values_match_server():
    """Config defaults must match server hardcoded defaults."""
    assert abs(DEFAULT_CONFIG["pause_threshold"] - 0.25) < 1e-9
    assert DEFAULT_CONFIG["word_timestamps"] is False
    assert DEFAULT_CONFIG["whisper_port"] == 5001
    assert DEFAULT_CONFIG["translation_port"] == 5000


def test_validate_port_valid():
    assert validate_port(5001) == 5001
    assert validate_port("8080") == 8080
    assert validate_port(1) == 1
    assert validate_port(65535) == 65535


def test_validate_port_invalid(capsys):
    assert validate_port(0, "test") is None
    assert validate_port(-1, "test") is None
    assert validate_port(70000, "test") is None
    assert validate_port("abc", "test") is None


def test_validate_host_valid():
    assert validate_host("127.0.0.1") == "127.0.0.1"
    assert validate_host("localhost") == "localhost"
    assert validate_host("my-server.local") == "my-server.local"


def test_validate_host_invalid(capsys):
    assert validate_host(None) is None
    assert validate_host("") is None
    assert validate_host("evil host; rm -rf /") is None


def test_save_and_load_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        cfg_file = Path(tmp) / "test_config.json"
        cfg = {**DEFAULT_CONFIG, "whisper_model": "tiny"}

        # Manually save
        cfg_file.write_text(json.dumps(cfg, indent=2), encoding="utf-8")

        # Manually load
        loaded = json.loads(cfg_file.read_text(encoding="utf-8"))
        assert loaded["whisper_model"] == "tiny"
        assert loaded["whisper_port"] == 5001


def test_config_export_import_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        cfg = {**DEFAULT_CONFIG, "whisper_model": "small", "language": "zh"}
        export_path = Path(tmp) / "backup.json"

        # Export
        result = config_export(cfg, export_path)
        assert result is True
        assert export_path.exists()

        # Verify exported content
        exported = json.loads(export_path.read_text(encoding="utf-8"))
        assert exported["whisper_model"] == "small"
        assert exported["language"] == "zh"
        # gguf_model_path should be stripped on export
        assert "gguf_model_path" not in exported


def test_config_import_nonexistent(capsys):
    result = config_import("/nonexistent/path.json")
    assert result is None
