"""Romanization of transcribed lines.

Deterministic for Japanese (cutlet — MeCab + UniDic, vendored in _vendor/ —
or pykakasi without it), Chinese (pypinyin), Korean (Revised Romanization) and
Russian (BGN/PCGN) — <5 ms per line instead of an LLM call. The libraries are
loaded lazily (antivirus scans make the import slow).

romanize() returns None when the LLM must do it: Arabic and other non-Latin
scripts, or JA/ZH when the library is not installed.
"""

import re
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


# ── cutlet (MeCab + UniDic: real word boundaries and readings) ───────────────
# pykakasi reads character by character from a small dictionary: 囁いて came
# out "shou ite", 掠れた "ryaku reta", ひと時 "hito toki", and words were split
# mid-verb ("soma ru", "waka tteitemo"). cutlet romanizes MeCab's morphological
# analysis: "sasayaite", "kasureta", "hitotoki", "somaru", "wakatte ite mo".
_cutlet = None
_cutlet_checked = False
_cutlet_lock = threading.Lock()


def get_cutlet():
    """Lazy-load cutlet (needs fugashi + unidic-lite). Thread-safe; None if missing."""
    global _cutlet, _cutlet_checked
    with _cutlet_lock:
        if not _cutlet_checked:
            _cutlet_checked = True
            try:
                from _vendor.cutlet import Cutlet

                # Lyrics keep katakana as read ("dorinku", not "Drink"), and
                # sing 私 "watashi" (UniDic's reading is the formal "watakushi")
                _cutlet = Cutlet(use_foreign_spelling=False, ensure_ascii=False)
                _cutlet.add_exception("私", "watashi")
                print("[Yume] cutlet loaded — Japanese romanization by word (MeCab + UniDic)")
            except ImportError as e:
                print(f"[Yume] cutlet unavailable ({e.name or e}) — Japanese romanization uses pykakasi")
            except Exception as e:  # a broken dictionary must not stop the server
                print(f"[Yume] cutlet failed: {type(e).__name__}: {e} — using pykakasi")
    return _cutlet


def _greeting(word):
    """こんにちは is said konnichiwa: the は is the particle (Konnichiha → Konnichiwa)."""
    core = word.rstrip(".,!?;:")
    fixed = _GREETINGS.get(core.lower())
    if fixed is None:
        return word
    return (fixed.capitalize() if word[:1].isupper() else fixed) + word[len(core) :]


def preload():
    """Load the Japanese romanizer at startup (antivirus scans make it slow)."""
    if get_cutlet() is None:
        get_kakasi()


# A number followed by a counter whose reading MeCab gets wrong after Arabic
# digits: 3月 "3 tsuki" (sangatsu), 25日 "25ka", 1人 "1nin" (hitori).
_JA_COUNTER = re.compile(r"(\d+)(日間|日|月|人)")
_JA_DAYS = {1: "tsuitachi", 2: "futsuka", 3: "mikka", 4: "yokka", 5: "itsuka", 6: "muika", 7: "nanoka",
            8: "youka", 9: "kokonoka", 10: "tooka", 14: "juuyokka", 20: "hatsuka", 24: "nijuuyokka"}  # fmt: skip
_JA_PLACEHOLDER = "ZQ{}ZQ"  # an ASCII word: cutlet and pykakasi pass it through


def _counter_reading(m, after_month):
    n, counter = int(m.group(1)), m.group(2)
    if counter == "月":
        return f"{m.group(1)} gatsu"
    if counter == "人":
        return {1: "hitori", 2: "futari"}.get(n, f"{m.group(1)} nin")
    day = _JA_DAYS.get(n)
    if n == 1 and not after_month:  # 1日: "one day", not "the 1st"
        day = "ichinichi"
    if counter == "日間":
        return f"{day}kan" if day and n != 1 else f"{m.group(1)} nichikan"
    return day or f"{m.group(1)} nichi"


def romanize_japanese(text):
    """Japanese text (kanji/kana) to romaji: cutlet, else pykakasi. ~1 ms."""
    readings = []

    def hold(m):
        readings.append(_counter_reading(m, after_month=text[: m.start()].endswith("月")))
        return _JA_PLACEHOLDER.format(len(readings) - 1)

    held = _JA_COUNTER.sub(hold, text)
    roma = _romanize_japanese_words(held)
    if roma is None or not readings:
        return roma
    for i, reading in enumerate(readings):
        roma = roma.replace(_JA_PLACEHOLDER.format(i), f" {reading} ")
    roma = re.sub(r" +([,.!?、。])", r"\1", " ".join(roma.split()))
    return roma[:1].upper() + roma[1:] if held.startswith("ZQ") else roma


def _romanize_japanese_words(text):
    katsu = get_cutlet()
    if katsu is not None:
        try:
            return " ".join(_greeting(w) for w in katsu.romaji(text).split())
        except Exception as e:
            print(f"[Yume] cutlet error: {e} — using pykakasi")
    return _romanize_japanese_kakasi(text)


def _romanize_japanese_kakasi(text):
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


# ── Korean (Revised Romanization, as pronounced) ─────────────────────────────
# RR writes the sound changes between syllables of a word: 좋은 joeun (not
# joteun), 감사합니다 gamsahamnida, 말을 mareul, 멀리 meolli, 어떻게 eotteoke.
_KO_INITIALS = ["g", "kk", "n", "d", "tt", "r", "m", "b", "pp", "s", "ss", "", "j", "jj", "ch", "k", "t", "p", "h"]
_KO_MEDIALS = [
    "a", "ae", "ya", "yae", "eo", "e", "yeo", "ye", "o", "wa", "wae", "oe", "yo", "u", "wo", "we", "wi", "yu", "eu", "ui", "i",
]  # fmt: skip
_KO_FINALS = [
    "", "k", "k", "k", "n", "n", "n", "t", "l", "k", "m", "l", "l", "l", "p", "l",
    "m", "p", "p", "t", "t", "ng", "t", "t", "k", "t", "p", "t",
]  # fmt: skip

# Initial (choseong) indexes
_G, _N, _D, _R, _M, _S, _SS, _O, _J, _CH, _K, _T, _P, _H = 0, 2, 3, 5, 6, 9, 10, 11, 12, 14, 15, 16, 17, 18
_VOWEL_I = 20
# Final (jongseong) index → the initial it becomes before a vowel (liaison)
_KO_LIAISON = {1: 0, 2: 1, 4: 2, 7: 3, 8: 5, 16: 6, 17: 7, 19: 9, 20: 10, 22: 12, 23: 14, 24: 15, 25: 16, 26: 17}
# Double finals: (stays, moves)
_KO_SPLIT = {3: (1, 19), 5: (4, 22), 6: (4, 27), 9: (8, 1), 10: (8, 16), 11: (8, 17), 12: (8, 19),
             13: (8, 25), 14: (8, 26), 15: (8, 27), 18: (17, 19)}  # fmt: skip
# Finals sounding k / t / p: nasalized to ng / n / m before ㄴ or ㅁ
_KO_K = {1, 2, 3, 9, 24}
_KO_T = {5, 7, 19, 20, 22, 23, 25, 27}
_KO_P = {14, 17, 18, 26}
_KO_ASPIRATE = {_G: _K, _D: _T, _J: _CH}  # after ㅎ: 좋다 jota, 그렇게 geureoke
_KO_H_FINAL = {27: 0, 6: 4, 15: 8}  # ㅎ, ㄶ, ㅀ → what is left without the ㅎ


def _ko_liaison(f, v):
    """A final before an empty initial (ㅇ) moves into it: 좋은 joeun, 같이 gachi."""
    if f in _KO_H_FINAL:  # the ㅎ is silent: 좋은 joeun, 많이 mani
        stay = _KO_H_FINAL[f]
        return (0, _KO_LIAISON[stay]) if stay else (0, _O)
    if f in _KO_SPLIT:
        stay, move = _KO_SPLIT[f]
        f, i = stay, _KO_LIAISON[move]
    else:
        f, i = 0, _KO_LIAISON[f]
    if v == _VOWEL_I and i in (_D, _T):  # palatalization: 같이 gachi, 굳이 guji
        i = _J if i == _D else _CH
    return f, i


def _ko_aspirate(f):
    """ㄱㄷㅂㅈ + ㅎ → aspirated: 축하 chuka, 못해 motae; None when f does not change."""
    for finals, new in (({1, 2, 24}, _K), ({7, 19, 20, 25}, _T), ({17, 26}, _P), ({22, 23}, _CH)):
        if f in finals:
            return 0, new
    if f == 9:  # ㄺ: 밝히다 balkida
        return 8, _K
    if f == 11:  # ㄼ
        return 8, _P
    return None


def _ko_nasalize(f, i):
    """Finals before ㄴ ㅁ ㄹ: 작년 jangnyeon, 신라 silla, 독립 dongnip; None when nothing changes."""
    if f in (8, 13, 15) and i == _N:  # ㄹ + ㄴ → ll: 설날 seollal
        return 8, _R
    if i in (_N, _M):
        for finals, new in ((_KO_K, 21), (_KO_T, 4), (_KO_P, 16)):  # 작년 jangnyeon, 있는 inneun, 합니다 hamnida
            if f in finals:
                return new, i
    if i == _R:
        if f == 4:
            return 8, _R  # 신라 silla
        if f in (16, 21):
            return f, _N  # 심리 simni, 종로 jongno
        if f in _KO_K:
            return 21, _N  # 독립 dongnip
        if f in _KO_P:
            return 16, _N  # 협력 hyeomnyeok
    return None


def _ko_assimilate(f, i, v):
    """Sound change between a final f and the next syllable's initial i (vowel v)."""
    if i == _O and f not in (0, 21):
        return _ko_liaison(f, v)
    if i == _H:
        return _ko_aspirate(f) or (f, i)
    if f in _KO_H_FINAL:  # a final ㅎ (ㄶ ㅀ): 좋다 jota, 좋습니다 josseumnida, 놓는 nonneun
        if i in _KO_ASPIRATE:
            return _KO_H_FINAL[f], _KO_ASPIRATE[i]
        if i == _S:
            return _KO_H_FINAL[f], _SS
        if i == _N:
            f = 4 if f in (27, 6) else 8  # ㅀ falls through to ㄹ + ㄴ
    return _ko_nasalize(f, i) or (f, i)


def romanize_korean(text):
    # Syllables as [initial, vowel, final]; other characters as themselves
    items = []
    for ch in text:
        code = ord(ch) - 0xAC00
        items.append([code // 588, (code % 588) // 28, code % 28] if 0 <= code <= 0xD7A3 - 0xAC00 else ch)
    # Sound changes within a word (between adjacent syllables)
    for a, b in zip(items, items[1:]):
        if isinstance(a, list) and isinstance(b, list):
            a[2], b[0] = _ko_assimilate(a[2], b[0], b[1])
    out = []
    prev_final = 0
    for it in items:
        if isinstance(it, str):
            out.append(it)
            prev_final = 0
            continue
        i, v, f = it
        # ㄹㄹ is "ll" (멀리 meolli), an initial ㄹ elsewhere "r"
        out.append(("l" if i == _R and prev_final == 8 else _KO_INITIALS[i]) + _KO_MEDIALS[v] + _KO_FINALS[f])
        prev_final = f
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
