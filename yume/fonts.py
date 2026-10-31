"""Font detection: which subtitle fonts this system and the extension have."""

from __future__ import annotations

import os
from pathlib import Path

from yume.constants import EXT_DIR, IS_MAC, IS_WIN
from yume.ui import bullet, header, info, section, success, warn


_FONT_EXTENSIONS = {".ttf", ".otf", ".ttc", ".woff", ".woff2"}

# script -> (file-name patterns, the free font to suggest). Patterns match the
# FILE name, which on Windows is not the font's name: Microsoft YaHei is
# msyh.ttc, Yu Gothic YuGothR.ttc, MS Gothic msgothic.ttc.
_SCRIPT_FONTS = {
    "Japanese": (
        ["NotoSansJP", "NotoSerifJP", "MPLUS", "YuGoth", "YuMin", "Meiryo", "MSGothic", "MSMincho", "Hiragino",
         "Kosugi", "ZenMaru"],
        "Noto Sans JP",
    ),
    "Chinese": (
        ["NotoSansSC", "NotoSansTC", "SourceHanSans", "msyh", "MicrosoftYaHei", "msjh", "SimHei", "SimSun", "MingLiU",
         "DengXian", "PingFang", "STHeiti"],
        "Noto Sans SC",
    ),
    "Korean": (["NotoSansKR", "Malgun", "NanumGothic", "AppleSD", "Batang", "Gulim"], "Noto Sans KR"),
    "Arabic": (
        ["NotoNaskhArabic", "NotoSansArabic", "Amiri", "Scheherazade", "trado", "arabtype", "majalla"],
        "Noto Naskh Arabic",
    ),
}  # fmt: skip
_GENERIC_FONTS = ["Arial", "Georgia", "Verdana", "Consolas", "Times New Roman", "Courier New"]


def _font_files() -> tuple[int, list[Path]]:
    """(number of font folders, font files in them) for this OS."""
    if IS_WIN:
        dirs = [Path(os.environ.get("WINDIR", "C:\\Windows")) / "Fonts"]
    elif IS_MAC:
        dirs = [Path("/Library/Fonts"), Path.home() / "Library/Fonts", Path("/System/Library/Fonts")]
    else:
        dirs = [Path("/usr/share/fonts"), Path.home() / ".local/share/fonts", Path("/usr/local/share/fonts")]
    files = [f for d in dirs if d.exists() for f in d.rglob("*") if f.suffix.lower() in _FONT_EXTENSIONS]
    return len(dirs), files


def _matching(files: list[Path], patterns: list[str]) -> list[str]:
    """Names of the font files whose name contains one of the patterns (case and spaces ignored)."""
    pats = [p.lower().replace(" ", "") for p in patterns]
    return sorted({f.name for f in files if any(p in f.stem.lower().replace(" ", "") for p in pats)}, key=str.lower)


def _show_bundled_fonts() -> None:
    ext_fonts = EXT_DIR / "fonts"
    if not ext_fonts.exists():
        warn(f"Extension fonts folder missing: {ext_fonts}")
        info("Create it and add .ttf/.otf files for custom subtitle fonts")
        return
    bundled = [f.name for f in ext_fonts.iterdir() if f.suffix.lower() in _FONT_EXTENSIONS]
    if not bundled:
        info(f"Extension fonts folder: empty (add .ttf/.otf files to {ext_fonts})")
        return
    section("Bundled Fonts (extension/fonts/)")
    for f in bundled:
        bullet(f)


_FONT_HELP = [
    "How to use fonts in Yume:",
    "  1. Yume automatically detects your system fonts",
    "  2. Pick a font in the extension popup (Settings > Appearance)",
    "",
    "To add a custom font that isn't installed system-wide:",
    "  1. Copy the .ttf or .otf file into the extension/fonts/ folder",
    "  2. Open extension/js/bundled-fonts.js and find the BUNDLED_FONTS list",
    "  3. Add your font: { file: 'MyFont.ttf', name: 'My Font' }",
    "  4. Reload the extension in chrome://extensions",
    "  The font will appear with '(bundled)' next to its name in the dropdown",
]


def detect_fonts() -> None:
    """Detect CJK and common fonts available on this system."""
    header("Font Detection")
    info("Scanning for subtitle-compatible fonts (Chinese, Japanese, Korean, Arabic)...")
    print()
    n_dirs, files = _font_files()
    info(f"Scanned {n_dirs} font directories, found {len(files)} font files")
    print()

    for script, (patterns, suggestion) in _SCRIPT_FONTS.items():
        matches = _matching(files, patterns)
        if not matches:
            warn(f"{script}: No dedicated fonts found")
            info(f"  Install: {suggestion} from Google Fonts")
            continue
        success(f"{script}: {len(matches)} font(s) found")
        for m in matches[:5]:
            bullet(m)
        if len(matches) > 5:
            info(f"  ... and {len(matches) - 5} more")
    print()

    generic = [g for g in _GENERIC_FONTS if _matching(files, [g])]
    info(f"Generic fonts: {', '.join(sorted(generic)) if generic else 'none detected'}")
    _show_bundled_fonts()
    print()
    for line in _FONT_HELP:
        info(line)
