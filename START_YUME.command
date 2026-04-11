#!/usr/bin/env bash
# Pocket Yume -- macOS Launcher
# Double-click this file in Finder to start Yume
# AI Subtitles for Japanese Videos

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR" || { echo "ERROR: Cannot cd to script directory"; exit 1; }

echo "============================================================"
echo "  POCKET YUME -- Launcher"
echo "  AI Subtitles for Japanese Videos"
echo "============================================================"
echo ""

# Check Python 3
if command -v python3 &>/dev/null; then
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

echo "Python: $($PY --version)"
echo ""

if [ ! -f "pocket_yume.py" ]; then
    echo "ERROR: pocket_yume.py not found!"
    echo "Run this from the Yume folder."
    read -rp "Press Enter to close..."
    exit 1
fi

$PY pocket_yume.py "$@"

echo ""
read -rp "Press Enter to close..."
