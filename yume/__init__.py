"""Yume — AI subtitle engine package.

Submodules:
  ui          — Terminal colours, panels, tables, spinner, input helpers
  hardware    — GPU / RAM / disk detection
  network     — Downloads, server health checks, server API calls
  ports       — Port availability, discovery, conflict resolution
  installers  — yt-dlp / FFmpeg / Deno / Ollama / pip-package installers
  llama_python — llama-cpp-python builds per GPU and OS
  po_token    — YouTube PO-token plugin and server
  health      — health_check, detect_fonts
  status      — show_status
  benchmark   — Whisper model benchmark
  menus       — All interactive CLI menus
  launch      — Server lifecycle: start
  resource_check — Pre-launch RAM/VRAM check
  runtime_menu — The menu while the servers run, stop
  setup       — First-run wizard, uninstall
"""
