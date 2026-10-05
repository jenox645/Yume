#!/usr/bin/env bash
# Pocket Yume -- Linux Launcher
# Local AI subtitles for videos

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
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
elif command -v python &>/dev/null; then
    PY=python
else
    echo "ERROR: Python 3 not found!"
    echo "Install: sudo apt install python3 python3-pip"
    echo "     or: sudo dnf install python3 python3-pip"
    echo ""
    exit 1
fi

echo "Python: $("$PY" --version)"
echo ""

# Check script
if [ ! -f "pocket_yume.py" ]; then
    echo "ERROR: pocket_yume.py not found!"
    echo "Run this from the Yume folder."
    exit 1
fi

# Launch
"$PY" pocket_yume.py "$@"
