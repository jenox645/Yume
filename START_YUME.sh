#!/usr/bin/env bash
# Pocket Yume -- Linux Launcher
# AI Subtitles for Japanese Videos

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

echo "============================================================"
echo "  POCKET YUME -- Launcher"
echo "  AI Subtitles for Japanese Videos"
echo "============================================================"
echo ""

# Check Python 3
if command -v python3 &>/dev/null; then
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

echo "Python: $($PY --version)"
echo ""

# Check script
if [ ! -f "pocket_yume.py" ]; then
    echo "ERROR: pocket_yume.py not found!"
    echo "Run this from the PocketYume folder."
    exit 1
fi

# Launch
$PY pocket_yume.py "$@"
