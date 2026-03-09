#!/usr/bin/env bash
# Pocket Yume -- macOS Launcher
# Double-click this file in Finder to start Yume
# AI Subtitles for Japanese Videos

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
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
    echo ""
    echo "Install options:"
    echo "  1. brew install python3"
    echo "  2. Download from python.org"
    echo ""
    read -p "Press Enter to close..."
    exit 1
fi

echo "Python: $($PY --version)"
echo ""

if [ ! -f "pocket_yume.py" ]; then
    echo "ERROR: pocket_yume.py not found!"
    echo "Run this from the PocketYume folder."
    read -p "Press Enter to close..."
    exit 1
fi

$PY pocket_yume.py "$@"

echo ""
read -p "Press Enter to close..."
