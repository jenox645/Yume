"""Deterministic romanization for Japanese, Chinese, and Korean.

Replaces the LLM for these languages — results arrive in <5 ms instead of 1-10 s.
Libraries are lazily loaded so startup is fast even when antivirus scans are slow.

Fallback behaviour (when a library is not installed):
  ja  — pykakasi not available → returns None  (caller falls back to LLM)
  zh  — pypinyin not available → returns None
  ko  — romanization lib not available → returns None
"""

import threading


# ── pykakasi (Japanese kanji → romaji) ───────────────────────────────────────
_kakasi = None  # pykakasi.kakasi instance, or None
_kakasi_checked = False  # True after the first load attempt
_kakasi_lock = threading.Lock()


def get_kakasi():
    """Lazy-load pykakasi (Japanese kanji→romaji converter). Thread-safe."""
    global _kakasi, _kakasi_checked
    with _kakasi_lock:
        if not _kakasi_checked:
            _kakasi_checked = True
            try:
                import pykakasi

                print(f"[Yume] pykakasi found at {pykakasi.__file__}")
                _kakasi = pykakasi.kakasi()
                print("[Yume] pykakasi loaded — deterministic Japanese romanization enabled")
            except ImportError:
                _kakasi = None
                print("[Yume] pykakasi not installed — Japanese romanization falls back to LLM")
            except Exception as e:
                _kakasi = None
                print(f"[Yume] pykakasi failed: {type(e).__name__}: {e}")
                print("[Yume] Japanese romanization falls back to LLM")
    return _kakasi


def romanize_japanese(text):
    """Convert Japanese text (kanji/kana) to romaji using pykakasi. ~1 ms."""
    kakasi = get_kakasi()
    if not kakasi:
        return None
    try:
        result = kakasi.convert(text)
        parts = []
        for item in result:
            r = item.get("hepburn", "") or item.get("passport", "") or item.get("orig", "")
            parts.append(r)
        return " ".join(parts).strip()
    except Exception as e:
        print(f"[Yume] pykakasi error: {e}")
        return None


def romanize_chinese(text):
    """Convert Chinese text to pinyin using pypinyin. ~1 ms."""
    try:
        from pypinyin import pinyin, Style

        result = pinyin(text, style=Style.TONE)
        return " ".join(p[0] for p in result).strip()
    except ImportError:
        return None
    except Exception:
        return None


def romanize_korean(text):
    """Convert Korean text to Revised Romanization.

    The 'romanization' package does not exist on PyPI.  Korean romanization
    falls back to the LLM automatically (this function returns None).
    """
    try:
        from romanization import romanize as kr_romanize  # type: ignore[import-not-found]

        return kr_romanize(text)
    except ImportError:
        return None
    except Exception as e:
        print(f"[Yume] Korean romanization error: {e}")
        return None
