"""Vulture whitelist — functions detected as unused but actually called by frameworks."""
# ruff: noqa: F821

# yume package public API (used by callers or required by ctypes structure layout)
gold_hr       # yume.ui — decorative separator, available for menu code to call
MIN_PORT      # yume.ports — public constant for callers that validate user input
dwLength      # yume.hardware — ctypes MEMORYSTATUSEX struct field, must be present

# Flask route handlers (server/faster_whisper_server.py)
_security_checks
_cleanup_request_temps
health
stats
switch_model
get_config
list_translation_models
prepare
prepare_direct
transcribe_url
transcribe
clear_cache
cache_status
romanize
romanize_batch
update_blacklist
get_blacklist
get_hallucination_patterns
