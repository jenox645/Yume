#!/usr/bin/env python3
"""
Pocket Yume CLI -- cross-platform installer & launcher for Yume AI subtitles.
Supports: Windows, Linux, macOS

This file only checks the Python version: the CLI itself is yume/cli.py.
"""

import sys

# Python version check — give a clear message instead of a cryptic TypeError
if sys.version_info < (3, 10):
    print("\n  Yume requires Python 3.10 or newer.")
    print(f"  You are running Python {sys.version.split()[0]}")
    print("  Download the latest Python from: https://www.python.org/downloads/")
    print("  On Windows, make sure to check 'Add Python to PATH' during installation.\n")
    sys.exit(1)

from yume.cli import main

if __name__ == "__main__":
    main()
