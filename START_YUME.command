#!/usr/bin/env bash
# Pocket Yume -- macOS Launcher
# Double-click this file in Finder to start Yume
# Local AI subtitles for videos

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR" || { echo "ERROR: Cannot cd to script directory"; exit 1; }

echo "============================================================"
echo "  POCKET YUME -- Launcher"
echo "  Local AI subtitles for videos"
echo "============================================================"
echo ""

# Prefer a virtual environment that actually has Yume installed (the setup
# wizard recommends "yume-env"): packages installed there are invisible to the
# system Python. An empty venv is skipped.
PY=""
for v in yume-env venv .venv; do
    if [ -x "$v/bin/python" ] && "$v/bin/python" -c "import importlib.util,sys; sys.exit(importlib.util.find_spec('faster_whisper') is None)" 2>/dev/null; then
        PY="$v/bin/python"
        break
    fi
done

# Otherwise Python 3 from PATH
if [ -n "$PY" ]; then
    :
elif command -v python3 &>/dev/null; then
    PY=python3
elif command -v python &>/dev/null && python -c "import sys; sys.exit(0 if sys.version_info >= (3, 0) else 1)" 2>/dev/null; then
    PY=python
else
    echo "ERROR: Python 3 not found!"
    echo ""
    echo "Install options:"
    echo "  1. brew install python3"
    echo "  2. Download from python.org"
    echo ""
    read -rp "Press Enter to close..."
    exit 1
fi

echo "Python: $("$PY" --version)"
echo ""

if [ ! -f "pocket_yume.py" ]; then
    echo "ERROR: pocket_yume.py not found!"
    echo "Run this from the Yume folder."
    read -rp "Press Enter to close..."
    exit 1
fi

"$PY" pocket_yume.py "$@"

echo ""
read -rp "Press Enter to close..."
