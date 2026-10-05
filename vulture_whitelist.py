"""Vulture whitelist — functions detected as unused but actually called by frameworks."""
# ruff: noqa: F821

# yume package public API (used by callers or required by ctypes structure layout)
gold_hr       # yume.ui — decorative separator, available for menu code to call
MIN_PORT      # yume.ports — public constant for callers that validate user input
dwLength      # yume.hardware — ctypes MEMORYSTATUSEX struct field, must be present
frame         # server/faster_whisper_server.py — required second arg of signal handler signature

# Flask route handlers (server/faster_whisper_server.py)
_security_checks
_add_cors_headers
health
stats
get_config
switch_model
list_translation_models
create_job
get_job
job_options
job_export
library
library_export
library_delete
get_blacklist
update_blacklist
blacklist_add
blacklist_remove
translation_health
translation_test
clear_cache
