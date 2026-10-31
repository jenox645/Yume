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
# digits: 3月 "3 tsuki" (sangatsu), 25日 "25ka", 1人 "1nin" (hitori), 第3話 "hanashi" (wa),
# 3つ "3tsu" (mittsu); and counters whose sound changes with the number, which
# MeCab reads one way whatever the number: 3本 "3 pon" (sanbon), 10分 "10 fun"
# (juppun), 1杯 "1 bai" (ippai), 3匹 "3 hiki" (sanbiki), 3軒 "3 ken" (sangen),
# 1歩 "1 ho" (ippo), 3足 "3 ashi" (sanzoku: pairs of shoes). 3分の1 is a third
# (sanbun); 1足す1 is "1 tasu 1", 1軒家 a house (ikkenya) and 1歩く not a counter.
_JA_COUNTER = re.compile(
    r"(\d+)(日間|日|月|人|話|つ|本|分間|分(?!の)|杯|匹|軒(?!家)|歩(?![かきくけこいっ])|足(?![さしすせそらりるれろっ]))"
)
# counter: (as read after most numbers, after 1/6/8/10 (doubled consonant), after 3)
_JA_SOUND = {"本": ("hon", "pon", "bon"), "杯": ("hai", "pai", "bai"), "匹": ("hiki", "piki", "biki"),
             "分": ("fun", "pun", "pun"), "分間": ("funkan", "punkan", "punkan"), "軒": ("ken", "ken", "gen"),
             "歩": ("ho", "po", "po"), "足": ("soku", "soku", "zoku")}  # fmt: skip
_JA_TSU = {1: "hitotsu", 2: "futatsu", 3: "mittsu", 4: "yottsu", 5: "itsutsu", 6: "muttsu", 7: "nanatsu",
           8: "yattsu", 9: "kokonotsu", 10: "too"}  # fmt: skip
_JA_DAYS = {1: "tsuitachi", 2: "futsuka", 3: "mikka", 4: "yokka", 5: "itsuka", 6: "muika", 7: "nanoka",
            8: "youka", 9: "kokonoka", 10: "tooka", 14: "juuyokka", 20: "hatsuka", 24: "nijuuyokka"}  # fmt: skip
_JA_PLACEHOLDER = "ZQ{}ZQ"  # an ASCII word: cutlet and pykakasi pass it through
# Names UniDic reads wrong, only where the full name settles it: 菅 alone is
# Suga (菅義偉) or Kan (菅直人), and 菅首相 was both, so it is left as UniDic reads it
_JA_NAMES = {
    "菅義偉": "Suga Yoshihide",  # was "Kan Yoshihide"
    "菅官房長官": "Suga kanbou choukan",  # Suga's post 2012-2020
    "菅田将暉": "Suda Masaki",  # was "Sugata Susumu Akira"
}
_JA_NAME_RE = re.compile("|".join(map(re.escape, _JA_NAMES)))


def _sound_counter(n, counter):
    """3本 "3 bon": the counter as it sounds after n, or None past 99 (hyappon,
    sanbyakuhon: the hundreds have rules of their own)."""
    if n > 99:
        return None
    plain, doubled, after_3 = _JA_SOUND[counter]
    last = n % 10
    if last in (0, 1, 6, 8):  # ippon, roppon, happon, juppon; ippun, juppun
        return f"{n} {doubled}"
    if last == 3 or (last == 4 and counter.startswith("分")):  # sanbon, sanpun, yonpun
        return f"{n} {after_3}"
    return f"{n} {plain}"


def _counter_reading(m, after_month):
    """The reading of a number + counter, or None to leave it to MeCab."""
    n, counter = int(m.group(1)), m.group(2)
    if counter in _JA_SOUND:
        return _sound_counter(n, counter)
    if counter in ("月", "話"):
        return f"{m.group(1)} {'gatsu' if counter == '月' else 'wa'}"
    if counter == "つ":  # past 10 the native counter is not used: "11 tsu" stays as it was
        return _JA_TSU.get(n, f"{m.group(1)} tsu")
    if counter == "人":
        return {1: "hitori", 2: "futari"}.get(n, f"{m.group(1)} nin")
    return _day_reading(m.group(1), counter, after_month)


def _day_reading(number, counter, after_month):
    """日 or 日間 after a number."""
    n = int(number)
    day = _JA_DAYS.get(n)
    if n == 1 and not after_month:  # 1日: "one day", not "the 1st"
        day = "ichinichi"
    if counter == "日間":
        return f"{day}kan" if day and n != 1 else f"{number} nichikan"
    return day or f"{number} nichi"


def _hold_counter(m, hold):
    reading = _counter_reading(m, m.string[: m.start()].endswith("月"))
    return m.group(0) if reading is None else hold(reading)


def romanize_japanese(text):
    """Japanese text (kanji/kana) to romaji: cutlet, else pykakasi. ~1 ms."""
    readings = []

    def hold(reading):
        readings.append(reading)
        return _JA_PLACEHOLDER.format(len(readings) - 1)

    # ・ separates words (ジョン・レノン); cutlet wrote it as "/": "Jon/Renon"
    held = _JA_NAME_RE.sub(lambda m: hold(_JA_NAMES[m.group(0)]), text.replace("・", " "))
    held = _JA_COUNTER.sub(lambda m: _hold_counter(m, hold), held)
    roma = _romanize_japanese_words(held)
    if roma is None:
        return None
    if readings:
        for i, reading in enumerate(readings):
            roma = roma.replace(_JA_PLACEHOLDER.format(i), f" {reading} ")
        roma = re.sub(r" +([,.!?、。])", r"\1", " ".join(roma.split()))
        roma = roma[:1].upper() + roma[1:] if held.startswith("ZQ") else roma
    return _split_digits(roma, text)


_DIGIT_GLUE = re.compile(r"[A-Za-z]*\d+[A-Za-z]*")


def _split_digits(roma, text):
    """MeCab glues a number to the word beside it: "Dai2 kai" (第2回), "20sai"
    (20歳), "Dai1i" (第1位). Latin words of the line itself stay whole: MP3, 4K."""
    own = {w.lower() for w in re.findall(r"[A-Za-z0-9]+", text)}

    def split(m):
        word = m.group(0)
        return word if word.lower() in own else re.sub(r"(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])", " ", word)

    return _DIGIT_GLUE.sub(split, roma)


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
        parts = _kakasi_pieces(kakasi.convert(text))
        # a space in the line is a piece of its own: collapse the runs it leaves
        return " ".join(_GREETINGS.get(p, p) for p in " ".join(parts).split())
    except Exception as e:
        print(f"[Yume] pykakasi error: {e}")
        return None


def _starts_with_consonant(r):
    return r[:1].isalpha() and r[:1] not in "aeiou"


def _kakasi_pieces(items):
    """pykakasi's pieces as romaji, particles read as said, っ doubling the next consonant."""
    parts = []
    sokuon = False  # the previous piece ended in a small っ
    prev = ""  # previous piece's original text
    for item in items:
        orig = item.get("orig", "")
        r = _particles(orig, item.get("hepburn", "") or item.get("passport", "") or orig, prev)
        prev = orig or prev
        if sokuon and _starts_with_consonant(r):
            # っ doubles the next consonant (ch → tch): 走っ + て = hashitte, one word
            parts[-1] += ("t" if r.startswith("ch") else r[0]) + r
        else:
            parts.append(r)
        # pykakasi writes a piece-final っ as "tsu" ("hashitsu" for 走っ)
        sokuon = item.get("hira", "").endswith("っ") and r.endswith("tsu")
        if sokuon:
            parts[-1] = parts[-1][:-3]
    return parts


def romanize_chinese(text):
    """Convert Chinese text to pinyin using pypinyin. ~1 ms."""
    try:
        from pypinyin import pinyin, Style

        _load_zh_phrases()
        result = pinyin(text, style=Style.TONE)
        return " ".join(p[0] for p in result).strip()
    except Exception:  # ImportError: pypinyin is not installed
        return None


# Phrases pypinyin reads wrong, from a Jay Chou song in the live run: 弹着琴
# "dàn" (tán, to play), 好好读书 "hǎo hào", 亮著 "liàng zhù" (zhe: the -ing
# particle, written 著 in traditional text), 很长 "zhǎng" (cháng, long)
_ZH_PHRASES = {
    "弹着": "tán zhe", "彈著": "tán zhe", "弹琴": "tán qín", "彈琴": "tán qín", "弹吉他": "tán jí tā",
    "彈吉他": "tán jí tā", "长得": "zhǎng de", "長得": "zhǎng de",
    # 好好 + verb: pypinyin's 好读书 ("loves reading", hào) won over 好好
    **{f"好好{v}": f"hǎo hǎo {p}" for v, p in (("读书", "dú shū"), ("讀書", "dú shū"), ("学习", "xué xí"),
       ("學習", "xué xí"), ("休息", "xiū xi"), ("过", "guò"), ("過", "guò"), ("活着", "huó zhe"),
       ("活著", "huó zhe"), ("的", "de"), ("爱", "ài"), ("愛", "ài"))},
    # ...and after 不/没 the segmenter took 不好, leaving 好读书 again
    "不好好": "bù hǎo hǎo", "没好好": "méi hǎo hǎo", "沒好好": "méi hǎo hǎo",
    **{f"{a}{b}": f"{pa} cháng" for a, pa in (("很", "hěn"), ("太", "tài"), ("好", "hǎo"), ("多", "duō"))
       for b in "长長"},
    # 重重地 "heavily" (pypinyin: "chóng chóng dì", layers upon layers of earth)
    "重重地": "zhòng zhòng de", "静悄悄地": "jìng qiāo qiāo de", "靜悄悄地": "jìng qiāo qiāo de",
    "数着": "shǔ zhe", "数一数": "shǔ yī shǔ", "數一數": "shǔ yī shǔ",  # counting, not 数 "number" (shù)
    # 还/還 "give back" (huán), not "still" (hái); 一行行 "line after line" (háng)
    "还给": "huán gěi", "還給": "huán gěi", "一行行": "yī háng háng", "一行一行": "yī háng yī háng",
    # 樂 "music" (yuè) in traditional text: 音樂 was "yīn lè"
    **{w: p for w, p in (("音樂", "yīn yuè"), ("樂隊", "yuè duì"), ("樂器", "yuè qì"), ("樂團", "yuè tuán"),
       ("樂曲", "yuè qǔ"), ("樂章", "yuè zhāng"))},
    "睡不着觉": "shuì bù zháo jiào", "睡不著覺": "shuì bù zháo jiào", "长相思": "cháng xiāng sī",
    "長相思": "cháng xiāng sī", "切开": "qiē kāi", "切開": "qiē kāi",
    # 舍不得 "can't bear to part" (de), not 不得 "must not" (dé)
    **{f"{a}不得": f"{p} bù de" for a, p in (("舍", "shě"), ("捨", "shě"), ("怪", "guài"), ("恨", "hèn"),
       ("巴", "bā"), ("记", "jì"), ("記", "jì"), ("顾", "gù"), ("顧", "gù"), ("由", "yóu"))},
}  # fmt: skip
# Verb + 得 + how: 唱得很好 "chàng de" (pypinyin: dé, "to obtain")
_ZH_DE_VERBS = {
    "唱": "chàng", "跑": "pǎo", "变": "biàn", "變": "biàn", "走": "zǒu", "说": "shuō", "說": "shuō",
    "过": "guò", "過": "guò", "哭": "kū", "笑": "xiào", "活": "huó", "爱": "ài", "愛": "ài", "飞": "fēi",
    "飛": "fēi", "睡": "shuì", "做": "zuò", "写": "xiě", "寫": "xiě", "听": "tīng", "聽": "tīng", "看": "kàn",
    "想": "xiǎng", "吃": "chī", "玩": "wán", "跳": "tiào", "讲": "jiǎng", "講": "jiǎng", "醉": "zuì",
    "疼": "téng", "痛": "tòng", "等": "děng", "来": "lái", "來": "lái",
}  # fmt: skip
# Verb + 不着/得着: can('t) manage to (zháo): 睡不着 "can't fall asleep", was "shuì bù zhe"
_ZH_ZHAO_VERBS = {"睡": "shuì", "找": "zhǎo", "摸": "mō", "猜": "cāi", "够": "gòu", "夠": "gòu", "用": "yòng",
                  "犯": "fàn", "见": "jiàn", "見": "jiàn", "碰": "pèng"}  # fmt: skip
# AA地 is the adverb "-ly" (de), which pypinyin read as 地 "earth" (dì): 慢慢地
# "màn màn dì". Not 大大/小小/高高: 小小地方 is "a small place" (dì fāng).
_ZH_AA_DE = {
    "慢": "màn", "轻": "qīng", "輕": "qīng", "静": "jìng", "靜": "jìng", "默": "mò", "深": "shēn", "远": "yuǎn",
    "遠": "yuǎn", "悄": "qiāo", "渐": "jiàn", "漸": "jiàn", "紧": "jǐn", "緊": "jǐn", "偷": "tōu", "好": "hǎo",
    "狠": "hěn", "久": "jiǔ", "暗": "àn", "缓": "huǎn", "緩": "huǎn", "淡": "dàn", "傻": "shǎ", "呆": "dāi",
    "早": "zǎo", "乖": "guāi", "稳": "wěn", "穩": "wěn", "牢": "láo", "匆": "cōng", "悠": "yōu", "痴": "chī",
    "癡": "chī", "甜": "tián", "细": "xì", "細": "xì", "白": "bái", "暖": "nuǎn", "柔": "róu", "冷": "lěng",
}  # fmt: skip
# A number before 只 counts animals (两只老虎 liǎng zhī), not 只 "only" (zhǐ)
_ZH_NUMBERS = {"两": "liǎng", "兩": "liǎng", "三": "sān", "四": "sì", "五": "wǔ", "六": "liù", "七": "qī",
               "八": "bā", "九": "jiǔ", "十": "shí", "几": "jǐ", "幾": "jǐ"}  # fmt: skip
# 重 before these verbs is "again" (chóng: 重来, 重回), not "heavy" (zhòng)
_ZH_CHONG = {"来": "lái", "來": "lái", "回": "huí", "拾": "shí", "遇": "yù", "见": "jiàn", "見": "jiàn",
             "燃": "rán", "写": "xiě", "寫": "xiě", "游": "yóu", "遊": "yóu", "走": "zǒu", "做": "zuò",
             "归": "guī", "歸": "guī"}  # fmt: skip
# Verbs 著 follows as "-ing" (看著 kàn zhe), where pypinyin reads zhù
_ZH_ZHE_VERBS = {
    "看": "kàn", "想": "xiǎng", "等": "děng", "聽": "tīng", "唱": "chàng", "走": "zǒu", "笑": "xiào",
    "哭": "kū", "坐": "zuò", "站": "zhàn", "躺": "tǎng", "睡": "shuì", "拿": "ná", "帶": "dài",
    "抱": "bào", "牽": "qiān", "望": "wàng", "亮": "liàng", "活": "huó", "忙": "máng", "記": "jì",
    "陪": "péi", "守": "shǒu", "吃": "chī", "喝": "hē", "寫": "xiě", "念": "niàn", "讀": "dú",
    "找": "zhǎo", "追": "zhuī", "愛": "ài", "等待": "děng dài", "看見": "kàn jiàn", "數": "shǔ",
}  # fmt: skip
_zh_loaded = False


def _load_zh_phrases():
    global _zh_loaded
    if _zh_loaded:
        return
    _zh_loaded = True
    try:
        from pypinyin import load_phrases_dict
    except ImportError:  # a pypinyin without it still romanizes, only less well
        return
    load_phrases_dict({k: [[s] for s in v.split()] for k, v in _zh_phrases().items()})


def _zh_phrases():
    """Every phrase pypinyin gets wrong: {phrase: its pinyin}. Later entries win."""
    return {
        **{f"{c}{c}地": f"{p} {p} de" for c, p in _ZH_AA_DE.items()},
        **{f"{n}只": f"{p} zhī" for n, p in _ZH_NUMBERS.items()},
        **{f"重{v}": f"chóng {p}" for v, p in _ZH_CHONG.items()},
        **{f"{v}得": f"{p} de" for v, p in _ZH_DE_VERBS.items()},
        **{
            f"{v}{m}{z}": f"{p} {mp} zháo"
            for v, p in _ZH_ZHAO_VERBS.items()
            for m, mp in (("不", "bù"), ("得", "de"))
            for z in "着著"
        },
        **_ZH_PHRASES,
        **{f"{v}著": f"{p} zhe" for v, p in _ZH_ZHE_VERBS.items()},
    }


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
_VOWEL_I, _VOWEL_YEO = 20, 6
_KO_BALB = [7, 0, 11]  # 밟: its ㄼ is said ㅂ before a consonant (밟다 bapda, 밟는 bamneun; 넓다 is neolda)
_KO_IP = [11, 20, 26]  # 잎 after a final takes an ㄴ: 꽃잎 kkonnip, 깻잎 kkaennip, 솔잎 sollip
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


def _ko_aspirate(f, v):
    """ㄱㄷㅂㅈ + ㅎ → aspirated: 축하 chuka, 못해 motae; None when f does not change."""
    if f in (7, 25) and v in (_VOWEL_I, _VOWEL_YEO):  # ㄷ/ㅌ + 히 palatalized: 굳히다 guchida, 닫혀 dachyeo
        return 0, _CH
    for finals, new in (({1, 2, 24}, _K), ({7, 19, 20, 25}, _T), ({17, 26}, _P), ({22, 23}, _CH)):
        if f in finals:
            return 0, new
    # ㄺ: 밝히다 balkida; ㄼ: 넓히다 neolpida; ㄵ: 앉히다 anchida
    return {9: (8, _K), 11: (8, _P), 5: (4, _CH)}.get(f)


def _ko_nasalize(f, i):
    """Finals before ㄴ ㅁ ㄹ: 작년 jangnyeon, 신라 silla, 독립 dongnip; None when nothing changes."""
    if f in (8, 13, 15) and i == _N:  # ㄹ + ㄴ → ll: 설날 seollal
        return 8, _R
    if i in (_N, _M):
        for finals, new in ((_KO_K, 21), (_KO_T, 4), (_KO_P, 16)):  # 작년 jangnyeon, 있는 inneun, 합니다 hamnida
            if f in finals:
                return new, i
    return _ko_before_r(f) if i == _R else None


def _ko_before_r(f):
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
        return _ko_aspirate(f, v) or (f, i)
    if f == 9 and i == _G:  # ㄺ before ㄱ is ㄹ: 읽고 ilgo, 맑게 malge
        return 8, i
    if f in _KO_H_FINAL:
        return _ko_h_final(f, i)
    return _ko_nasalize(f, i) or (f, i)


def _ko_h_final(f, i):
    """A final ㅎ (ㄶ ㅀ) before a consonant: 좋다 jota, 좋습니다 josseumnida, 놓는 nonneun."""
    if i in _KO_ASPIRATE:
        return _KO_H_FINAL[f], _KO_ASPIRATE[i]
    if i == _S:
        return _KO_H_FINAL[f], _SS
    if i == _N:
        f = 4 if f in (27, 6) else 8  # ㅀ falls through to ㄹ + ㄴ
    return _ko_nasalize(f, i) or (f, i)


def _ko_word_exceptions(items):
    """Syllables whose sound the general rules get wrong (in place): 밟 and 잎."""
    for a, b in zip(items, [*items[1:], " "]):
        if a == _KO_BALB and not (isinstance(b, list) and b[0] == _O):
            a[2] = 17
        elif b == _KO_IP and isinstance(a, list) and a[2]:
            b[0] = _N


def romanize_korean(text):
    # Syllables as [initial, vowel, final]; other characters as themselves
    items = []
    for ch in text:
        code = ord(ch) - 0xAC00
        items.append([code // 588, (code % 588) // 28, code % 28] if 0 <= code <= 0xD7A3 - 0xAC00 else ch)
    _ko_word_exceptions(items)
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
    for ch, nxt in zip(text, [*text[1:], ""]):
        low = ch.lower()
        if low in _RU:
            # BGN/PCGN: е is "ye" word-initially and after a vowel or ъ/ь, else "e"
            r = "ye" if low == "е" and (not prev.isalpha() or prev.lower() in _RU_VOWELS) else _RU[low]
            out.append(_ru_case(r, ch, prev, nxt))
        else:
            out.append(ch)
        prev = ch
    return "".join(out)


def _ru_case(r, ch, prev, nxt):
    """Ю is "Yu" in "Юля" but "YU" in "ЮЛЯ" (a word in capitals)."""
    if not ch.isupper():
        return r
    return r.upper() if prev.isupper() or nxt.isupper() else r.capitalize()


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
