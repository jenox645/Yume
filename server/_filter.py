"""Hallucination and credits-line filter for Whisper transcription results.

All detection is stateless except _is_hallucination which reads the mutable
user_blacklist from _state.
"""

import itertools
import math
import os
import re
import unicodedata

import _state


# ── Built-in hallucination patterns ──────────────────────────────────────────
HALLUCINATION_PATTERNS = [
    "\u3054\u8996\u8074\u3042\u308a\u304c\u3068\u3046\u3054\u3056\u3044\u307e\u3057\u305f",
    "\u3054\u8996\u8074\u3042\u308a\u304c\u3068\u3046",
    "\u304a\u75b2\u308c\u69d8\u3067\u3057\u305f",
    "\u304a\u75b2\u308c\u69d8",
    "\u5b57\u5e55\u306f\u81ea\u52d5\u751f\u6210",
    "\u5b57\u5e55\u5236\u4f5c",
    "\u6b4c\uff1a",
    "feat.",
    "Sound Hodori",
    "\uc0ac\uc6b4\ub4dc \ud638\ub3cc\uc774",
    "\u30db\u30c9\u30ea",
    "Instagram",
    "Twitter",
    "\u30c1\u30e3\u30f3\u30cd\u30eb\u767b\u9332",
    "\u9ad8\u8a55\u4fa1",
    "\u30b5\u30d6\u30b9\u30af\u30e9\u30a4\u30d6",
    "Subscribe",
    "Like and subscribe",
    "Thank you for watching",
    "Thanks for watching",
    "Please subscribe",
    "[Music]",
    "[Applause]",
    "[Laughter]",
    "(Music)",
    # Chinese common hallucinations
    "\u8bf7\u8ba2\u9605",
    "\u611f\u8c22\u89c2\u770b",
    "\u611f\u8c22\u6536\u770b",
    "\u8c22\u8c22\u89c2\u770b",  # "thanks for watching", heard at the end of a Jay Chou song
    "\u8b1d\u8b1d\u89c0\u770b",
    "\u8c22\u8c22\u6536\u770b",
    "\u8b1d\u8b1d\u6536\u770b",
    "\u5b57\u5e55\u7ec4",
    "\u8c22\u8c22\u5927\u5bb6\u7684\u652f\u6301",
    "\u8bb0\u5f97\u70b9\u8d5e",
    "\u5173\u6ce8\u6211",
    "\u4e00\u952e\u4e09\u8fde",
    "字幕志愿者",  # "subtitle volunteer …"
    "字幕志願者",
    "中文字幕",
    "优优独播剧场",  # "YoYo Television Series Exclusive"
    "優優獨播劇場",
    "YoYo Television Series",
    "Amara.org",
    "明镜与点点",
    "请不吝点赞",
    # Korean common hallucinations
    "한글자막",  # "Korean subtitles by …"
    "자막 제공",
    "자막 by",
    "다음 영상에서 만나요",  # "see you in the next video"
    "시청해주셔서 감사합니다",
    "시청해 주셔서 감사합니다",
    "구독과 좋아요",
    "MBC 뉴스",
    # Russian common hallucinations
    "субтитр",  # "Субтитры сделал/создавал/подогнал …" ("subtitles by …")
    "DimaTorzok",
    "Продолжение следует",  # "to be continued"
    "\u041f\u043e\u0434\u043f\u0438\u0441\u044b\u0432\u0430\u0439\u0442\u0435\u0441\u044c \u043d\u0430 \u043a\u0430\u043d\u0430\u043b",
    "\u0421\u043f\u0430\u0441\u0438\u0431\u043e \u0437\u0430 \u043f\u0440\u043e\u0441\u043c\u043e\u0442\u0440",
    "\u0421\u0442\u0430\u0432\u044c\u0442\u0435 \u043b\u0430\u0439\u043a",
    "\u041d\u0430\u0436\u0438\u043c\u0430\u0439\u0442\u0435 \u043a\u043e\u043b\u043e\u043a\u043e\u043b\u044c\u0447\u0438\u043a",
    # Arabic common hallucinations
    "\u0627\u0634\u062a\u0631\u0643 \u0641\u064a \u0627\u0644\u0642\u0646\u0627\u0629",
    "\u0634\u0643\u0631\u0627 \u0644\u0644\u0645\u0634\u0627\u0647\u062f\u0629",
    "\u0644\u0627 \u062a\u0646\u0633\u0649 \u0627\u0644\u0627\u0639\u062c\u0627\u0628",  # bare alef form
    "\u0644\u0627 \u062a\u0646\u0633\u0649 \u0627\u0644\u0625\u0639\u062c\u0627\u0628",  # hamza-below form (Whisper standard)
    "نانسي قنقر",  # "ترجمة نانسي قنقر" ("translated by …")
]

# ── Credits-line patterns ─────────────────────────────────────────────────────
# Roles that open a credits line ("Vocals: X", "作詞・作曲：Y", "Mix & Mastering by Z").
# They used to be plain substrings, which hid real lines: "I watched the video",
# "remix", "piano man", "この曲を作曲した".
CREDITS_PATTERNS = [
    "\u4f5c\u8a5e",  # 作詞 lyrics
    "\u4f5c\u66f2",  # 作曲 composition
    "\u7de8\u66f2",  # 編曲 arrangement
    "\u8a5e\u66f2",  # 詞曲
    "vocals",
    "vocal",
    "vo",
    "guitar",
    "bass",
    "drums",
    "piano",
    "illustration",
    "illust",
    "animation",
    "movie",
    "video",
    "mix",
    "mixing",
    "mastering",
    "lyrics",
    "music",
    "arrangement",
]
_ROLE = "|".join(re.escape(p) for p in sorted(CREDITS_PATTERNS, key=len, reverse=True))
# role, then a separator / "by" / "&" / another role — e.g. "Vocals:", "作詞・作曲",
# "Mix & Mastering", "Illustration by", "piano arrangement"
_CREDITS_RE = re.compile(
    rf"^\s*(?:{_ROLE})s?(?![a-z])\s*(?:[:：/／・&＆\-–—|｜]|by\b|and\b|(?:{_ROLE})(?![a-z]))",
    re.IGNORECASE,
)
# CJK/Korean roles need no separator: "詞曲 李宗盛", "작사 김이나"
_CJK_ROLES = ["作詞", "作曲", "編曲", "詞曲", "作词", "编曲", "词曲", "監製", "监制", "작사", "작곡", "편곡"]
_CJK_CREDITS_RE = re.compile(rf"^\s*(?:{'|'.join(_CJK_ROLES)})(?:\s|[:：/／・、&＆|｜]|$)")
_CREDITS_MAX_LEN = 80

# Exact whole-line matches only.  "like"/"share"/"comment"/"follow" used to be
# substring patterns, which silently dropped real lyrics ("I like you", "follow me").
# As bare one-word lines they are YouTube-outro hallucinations; inside a sentence
# they are legitimate. (la/na/da/oh/ah are real lyrics and stay allowed.)
SINGLE_WORD_BLOCKLIST = ["music", "mm", "hmm", "like", "share", "comment", "follow", "subscribe"]


# ── Detection logic ───────────────────────────────────────────────────────────


# Precomputed lowercase copies — is_hallucination runs per transcribed segment,
# so lowering every pattern on every call is pure waste.
_HALLUCINATION_PATTERNS_LOWER = [p.lower() for p in HALLUCINATION_PATTERNS]


def is_hallucination(text):
    """Return True if text looks like a Whisper hallucination."""
    t = text.strip()
    if not t:
        return True
    # Normalize Unicode (NFC) to catch alternate representations of JA/ZH/AR text
    t = unicodedata.normalize("NFC", t)
    t_lower = t.lower()

    for pat in _HALLUCINATION_PATTERNS_LOWER:
        if pat in t_lower:
            return True

    # User-reported blacklist (sent from extension popup)
    for bl_item in _state.user_blacklist:
        if bl_item and bl_item.lower() in t_lower:
            print(f"[Yume] User blacklist match: {bl_item!r} in {t!r}")
            return True

    # Repeated word spam: "30k 30k 30k 30k" (6+ words, <=2 unique)
    words = t.split()
    if len(words) >= 6 and len({w.lower().strip(".,!?") for w in words}) <= 2:
        return True

    sub = _repeated_unit(t_lower.replace(" ", ""))
    if sub:
        print(f"[Yume] Hallucination: repeated '{sub}' in '{t}'")
        return True

    return t_lower in SINGLE_WORD_BLOCKLIST


def _repeated_unit(clean):
    """The unit a whole line repeats, or None.

    Concatenated repetition: "aaaaaa", "la la la la", 3+ times. Twice is a
    lyric: きらきら, もっともっと, "더 그리워 더 그리워", "I love you I love you".
    The repeating unit may start after a short prefix — "MACACACACA..." is
    "ma" + "ca"*N and never matches anchored at position 0, so try offsets
    up to one unit length (require an extra repeat there to avoid false
    positives on words like "banana")."""
    if len(clean) < 4:
        return None
    for sub_len in range(2, min(9, len(clean) // 2 + 1)):
        for offset in range(0, sub_len + 1):
            body = clean[offset:]
            repeats = len(body) // sub_len
            if repeats < (3 if offset == 0 else 4):
                continue
            sub = body[:sub_len]
            if sub * repeats == body[: sub_len * repeats] and sub_len * repeats >= len(body) * 0.95:
                return sub
    return None


# A unit of 1-40 characters repeated 5+ times in a row: Whisper stuck in a loop
# ("…パープルドリームしてみてみてみてみて…" x150); "la la la la" is caught as a
# whole-line hallucination above.
# The repeated unit: a syllable ("みて") up to a phrase ("عن ماذا يتحدث ", 14 chars, which a
# 12-char limit let through: a 27 s Arabic loop line was shown)
_LOOP_RE = re.compile(r"(.{1,40}?)\1{4,}", re.DOTALL)
# ...over at least this many characters. A sung stutter is short and real:
# "あたし大大大大大大嫌い" and "カイカイカイカイカイはない" in "ダイダイダイダイダイ
# キライ" were cut to "大嫌い" and hidden. Whisper's loops run on: "みて" x150,
# "Azumoto-" x44, a 14-char Arabic phrase x30.
LOOP_MIN_CHARS = 20
LOOP_LINES = 3  # this many identical consecutive lines = a hallucination loop


def _loops(text):
    """The loops in text: repeats of a unit over LOOP_MIN_CHARS+ characters."""
    pos = 0
    while m := _LOOP_RE.search(text, pos):
        if len(m.group(0)) >= LOOP_MIN_CHARS:
            yield m
            pos = m.end()
        else:  # a longer unit may still start further in ("aaaaab aaaaab …")
            pos = m.start() + 1


def collapse_loops(text):
    """Cut a repetition loop down to one occurrence of the repeated unit."""
    out, pos = [], 0
    for m in _loops(text):
        out += [text[pos : m.start()], m.group(1)]
        pos = m.end()
    return ("".join(out) + text[pos:]).strip()


def _without_loops(text):
    """The text with every loop removed, including a trailing partial repeat
    ("Azumoto-Azumoto-Azum" leaves nothing, not "Azum")."""
    out, pos = [], 0
    for m in _loops(text):
        out.append(text[pos : m.start()])
        unit, end = m.group(1), m.end()
        k = 0
        while end + k < len(text) and k < len(unit) and text[end + k] == unit[k]:
            k += 1
        pos = end + k
    out.append(text[pos:])
    return "".join(out)


def clean_raw_segments(segs):
    """Whisper's raw segments → (segments with loops cut, indexes to hide).

    Decided on the RAW text: a line that is nothing but a loop
    ("Azumoto-" x44 over an instrumental intro, stamped 0-30 s) is hidden; a
    real line that degenerates into a loop at the end is kept, cut short.
    Cutting first and filtering after let "Azumoto-Azum" through."""
    out, hide = [], set()
    for i, s in enumerate(segs):
        text = s["text"]
        if next(_loops(text), None):
            # Only what is OUTSIDE the loops counts: words before it ("…チェック
            # してみてみて…") and after it ("ラララララ 君と夢を見ていた")
            if len(re.sub(r"\W", "", _without_loops(text))) < 4:
                hide.add(i)
            text = collapse_loops(text)
        out.append({**s, "text": text})
    hide |= loop_indexes([s["text"] for s in out])
    for i in range(len(out) - 1):
        if i in hide or i + 1 in hide:
            continue
        a, b = out[i], out[i + 1]
        if is_repeat(a, b):  # one line over both
            out[i] = {**a, "end": b["end"]}
            hide.add(i + 1)
        elif is_fragment(a, b):
            hide.add(i)
    return out, hide


FRAGMENT_MAX_S = 1.5  # a line this short...
FRAGMENT_GAP_S = 1.0  # ...just before the next one...
FRAGMENT_SHARED = 0.6  # ...that starts with this much of it is a cut-off copy


def _compact(text):
    return re.sub(r"[\W_]+", "", text.lower())


def is_repeat(seg, nxt):
    """The same line twice in a row (fewer than a loop): Whisper heard it twice,
    or both regions heard a line sung across their cut."""
    return _compact(seg["text"]) == _compact(nxt["text"]) and nxt["start"] - seg["end"] < 2.0


def is_fragment(seg, nxt):
    """Whisper wrote the start of the next line as a line of its own: "Место..."
    before "Место встречи Эрмитаж…", "호주 최고한" before "호주 최고 안보기관인…"
    (a word across a region cut, heard by both regions)."""
    if seg["end"] - seg["start"] > FRAGMENT_MAX_S or nxt["start"] - seg["end"] > FRAGMENT_GAP_S:
        return False
    a, b = _compact(seg["text"]), _compact(nxt["text"])
    if not a or len(a) >= len(b):
        return False
    return len(os.path.commonprefix([a, b])) >= FRAGMENT_SHARED * len(a)


TAIL_MIN = 4  # letters: a shorter end of a line ("사랑해") is sung again too often


def is_tail(seg, nxt):
    """`nxt` is the end of `seg` heard again across a region cut: the left
    region wrote its last line on past the cut ("매장입니다. 제품 안내판에 한글로
    된 로고가 보입니다." stamped 19.5-23.8 s, cut at 20.7) and the right region
    wrote what it heard after the cut ("제품 안내판에 한글로 된 로고가 보입니다.")."""
    a, b = _compact(seg["text"]), _compact(nxt["text"])
    return (
        bool(seg.get("past_cut"))
        and len(a) > len(b) >= TAIL_MIN
        and a.endswith(b)
        and nxt["start"] - seg["end"] < FRAGMENT_GAP_S
    )


SPLIT_WIDTH = 84  # wider than two subtitle lines (CJK characters count double)...
SPLIT_MIN_S = 2.0  # ...is cut into at most one piece per this many seconds (timed by width)
SPLIT_SLACK = 1.25  # a cut at a better place may leave a piece this much wider than even
_SENTENCE = r"[。！？!?…]+[」』)）\"']*\s*|[.]\s+"
_CLAUSE = r"[、，；：]\s*|[,;:]\s+"  # "6,5 миллиардов" is a number, not a clause
# Where a long line may be cut, best first: sentence ends, clauses, words
_CUTS = [re.compile("|".join(p)) for p in ([_SENTENCE], [_SENTENCE, _CLAUSE], [_SENTENCE, _CLAUSE, r"\s+"])]


def _width(text):
    return sum(2 if unicodedata.east_asian_width(c) in "WF" else 1 for c in text)


def _chunks(text, cut):
    """text cut after each match of `cut` (the punctuation stays on the left)."""
    out, pos = [], 0
    for m in cut.finditer(text):
        if pos < m.end() < len(text):
            out.append(text[pos : m.end()])
            pos = m.end()
    return [*out, text[pos:]]


def split_long(seg):
    """A line too long to read in one go → pieces, timed by their share of the
    text. Whisper sometimes writes a whole region as one line: a 384-character
    Russian rap verse on screen for 21 s, a 123-character news sentence for 20 s."""
    text, dur, width = seg["text"], seg["end"] - seg["start"], _width(seg["text"])
    n = min(math.ceil(width / SPLIT_WIDTH), int(dur // SPLIT_MIN_S))
    if n < 2:
        return [seg]
    pieces = None
    for cut in _CUTS:
        chunks = _chunks(text, cut)
        if len(chunks) > 1:
            pieces = _pieces(chunks, n)
            # "Соответственно," then 107 columns: too uneven, cut finer
            if max(_width(p) for p, _ in pieces) <= SPLIT_SLACK * max(SPLIT_WIDTH, width / n):
                break
    if not pieces:
        return [seg]  # no place to cut: never inside a word
    total, out, t = pieces[-1][1], [], seg["start"]
    for piece, upto in pieces:
        end = round(seg["start"] + dur * upto / total, 2) if upto < total else seg["end"]
        out.append({**seg, "start": t, "end": end, "text": piece})
        t = end
    return out


def _pieces(chunks, n):
    """chunks joined into about n pieces of even width: [(piece, width up to its end)]."""
    widths = list(itertools.accumulate(_width(c) for c in chunks))
    # cut after the chunk whose running width is closest to each 1/n of the line
    cuts = {min(range(len(chunks) - 1), key=lambda i: abs(widths[i] - k * widths[-1] / n)) for k in range(1, n)}
    out, prev = [], 0
    for c in [*sorted(cuts), len(chunks) - 1]:
        out.append(("".join(chunks[prev : c + 1]).strip(), widths[c]))
        prev = c + 1
    return out


def loop_indexes(texts):
    """Indexes of lines in runs of LOOP_LINES+ identical consecutive lines
    ("I don't want to lose you" x5 over an instrumental)."""
    out = set()
    run = []
    for i, t in enumerate([*texts, None]):
        key = t.strip().lower() if t is not None else None
        if run and key == texts[run[0]].strip().lower():
            run.append(i)
            continue
        if len(run) >= LOOP_LINES:
            out.update(run)
        run = [i] if t is not None else []
    return out


def is_credits_line(text):
    """Return True if text looks like a credits/attribution line: a short line
    that starts with a role followed by a separator, "by" or another role."""
    t = unicodedata.normalize("NFC", text.strip())
    return len(t) <= _CREDITS_MAX_LEN and bool(_CREDITS_RE.match(t) or _CJK_CREDITS_RE.match(t))


# ── Region-level detection ────────────────────────────────────────────────────

WINDOW_S = 30.0  # Whisper's window
# "Thank you" at the end of a region: what Whisper hears in a fade-out
THANKS_LINES = {
    "thank you", "thanks", "thank you very much", "감사합니다", "고맙습니다", "спасибо",
    "شكرا", "شكرا لكم", "谢谢", "謝謝", "ありがとうございました",
}  # fmt: skip


def _norm(text):
    t = "".join(c for c in unicodedata.normalize("NFC", text) if unicodedata.category(c) != "Mn")  # ً in شكراً
    return " ".join(re.sub(r"[\W_]+", " ", t.lower()).split())


def window_hallucinations(segs, region):
    """Indexes of lines Whisper made up for a region it heard no words in.

    With nothing to transcribe, Whisper fills its whole 30 s window with one
    line, stamped 0 → 30 s even when the region is shorter (the rest of the
    window is padding): "字幕志愿者 李宗盛", "Субтитры сделал DimaTorzok",
    "한글자막 by …" over intros and instrumentals, in every language. A real
    line alone in its region is stamped at most to the region's end (a chorus
    line across a whole 20 s region was one), so only the full window counts.
    Also "Thank you" in a region's last seconds. `segs` are the region's raw
    segments (times not clipped yet)."""
    start, end = region
    out = set()
    if len(segs) == 1:
        s = segs[0]
        if s["start"] - start <= 1.0 and s["end"] - s["start"] >= WINDOW_S - 0.5:
            out.add(0)
    for i, s in enumerate(segs):
        if end - s["end"] <= 3.0 and _norm(s["text"]) in THANKS_LINES:
            out.add(i)
    return out
