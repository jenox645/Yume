"""Romanization of transcribed lines.

Deterministic for Japanese (pykakasi), Chinese (pypinyin), Korean (Revised
Romanization) and Russian (BGN/PCGN) — <5 ms per line instead of an LLM call.
pykakasi/pypinyin are loaded lazily (antivirus scans make the import slow).

romanize() returns None when the LLM must do it: Arabic and other non-Latin
scripts, or JA/ZH when the library is not installed.
"""

import threading
import unicodedata


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


def _is_word_end(ch):
    """Kanji or katakana: a word a particle can follow (お月様は, ドリンクを)."""
    return "一" <= ch <= "鿿" or "ァ" <= ch <= "ヺ" or ch == "々"


def _particles(orig, roma, prev):
    """Particles are pronounced differently from their kana: は → wa, へ → e.
    pykakasi gives them as their own piece after a word (頬 + は), or glued to
    the hiragana that follows (本当 + はもっと)."""
    if not prev:
        return roma
    if orig == "は":
        return "wa"
    if orig == "へ":
        return "e"
    if (
        len(orig) > 1
        and orig[0] == "は"
        and roma.startswith("ha")
        and all("ぁ" <= c <= "ゖ" for c in orig)
        and _is_word_end(prev[-1])
    ):
        return "wa " + roma[2:]
    return roma


_GREETINGS = {"konnichiha": "konnichiwa", "konbanha": "konbanwa"}


def romanize_japanese(text):
    """Convert Japanese text (kanji/kana) to romaji using pykakasi. ~1 ms."""
    kakasi = get_kakasi()
    if not kakasi:
        return None
    try:
        result = kakasi.convert(text)
        parts = []
        sokuon = False  # the previous piece ended in a small っ
        prev = ""  # previous piece's original text
        for item in result:
            r = item.get("hepburn", "") or item.get("passport", "") or item.get("orig", "")
            r = _particles(item.get("orig", ""), r, prev)
            prev = item.get("orig", "") or prev
            if sokuon and parts and r[:1].isalpha() and r[:1] not in "aeiou":
                # っ doubles the next consonant (ch → tch): 走っ + て = hashitte, one word
                parts[-1] += ("t" if r.startswith("ch") else r[0]) + r
            else:
                parts.append(r)
            # pykakasi writes a piece-final っ as "tsu" ("hashitsu" for 走っ)
            sokuon = item.get("hira", "").endswith("っ") and r.endswith("tsu")
            if sokuon:
                parts[-1] = parts[-1][:-3]
        # a space in the line is a piece of its own: collapse the runs it leaves
        return " ".join(_GREETINGS.get(p, p) for p in " ".join(parts).split())
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


# ── Korean (Revised Romanization, per syllable) ──────────────────────────────
# Ported from the extension (audio-capture.js) so all romanization lives here.
_KO_INITIALS = ["g", "kk", "n", "d", "tt", "r", "m", "b", "pp", "s", "ss", "", "j", "jj", "ch", "k", "t", "p", "h"]
_KO_MEDIALS = [
    "a", "ae", "ya", "yae", "eo", "e", "yeo", "ye", "o", "wa", "wae", "oe", "yo", "u", "wo", "we", "wi", "yu", "eu", "ui", "i",
]  # fmt: skip
_KO_FINALS = [
    "", "k", "k", "k", "n", "n", "n", "t", "l", "l", "l", "l", "l", "l", "l", "l",
    "m", "p", "p", "t", "t", "ng", "t", "t", "k", "t", "p", "t",
]  # fmt: skip


def romanize_korean(text):
    out = []
    for ch in text:
        code = ord(ch)
        if 0xAC00 <= code <= 0xD7A3:
            off = code - 0xAC00
            out.append(_KO_INITIALS[off // 588] + _KO_MEDIALS[(off % 588) // 28] + _KO_FINALS[off % 28])
        else:
            out.append(ch)
    return "".join(out)


# ── Russian (BGN/PCGN) ────────────────────────────────────────────────────────
_RU = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo", "ж": "zh", "з": "z", "и": "i",
    "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t",
    "у": "u", "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch", "ъ": "", "ы": "y", "ь": "",
    "э": "e", "ю": "yu", "я": "ya",
}  # fmt: skip
_RU_VOWELS = set("аеёиоуыэюяъь")


def romanize_russian(text):
    out = []
    prev = ""
    for ch in text:
        low = ch.lower()
        if low in _RU:
            # BGN/PCGN: е is "ye" word-initially and after a vowel or ъ/ь, else "e"
            r = "ye" if low == "е" and (not prev.isalpha() or prev.lower() in _RU_VOWELS) else _RU[low]
            out.append(r.capitalize() if ch.isupper() else r)
        else:
            out.append(ch)
        prev = ch
    return "".join(out)


_DETERMINISTIC = {
    "ja": romanize_japanese,
    "zh": romanize_chinese,
    "ko": romanize_korean,
    "ru": romanize_russian,
}


# Non-Latin scripts without a deterministic romanizer: the LLM transliterates them
_LLM_SCRIPTS = {"ar", "fa", "he", "el", "uk", "bg", "sr", "hi", "th", "ka", "hy"}


def _latin_only(text):
    """No letters outside the Latin script ("One more kiss", "Living in a dream")."""
    return all(not ch.isalpha() or "LATIN" in unicodedata.name(ch, "") for ch in text)


def romanize(lang, text):
    """Romanization of one line.

    Returns the romanized text, "" when there is nothing to do (Latin-script
    language, a line already in Latin letters, empty line), or None when the
    LLM must do it: Arabic and other non-Latin scripts, or JA/ZH when
    pykakasi/pypinyin are not installed.
    """
    # An English line in a Japanese song needs no romanization — without this
    # it was shown twice, or cost an LLM call when pykakasi is not installed
    if not text.strip() or _latin_only(text):
        return ""
    fn = _DETERMINISTIC.get(lang)
    if fn is not None:
        return fn(text.strip())  # None if the library is missing
    return None if lang in _LLM_SCRIPTS else ""
