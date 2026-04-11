"""Hallucination and credits-line filter for Whisper transcription results.

All detection is stateless except _is_hallucination which reads the mutable
user_blacklist from _state.
"""

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
    "\u4f5c\u8a5e",
    "\u4f5c\u66f2",
    "\u7de8\u66f2",
    "\u6b4c\uff1a",
    "feat.",
    "\u8a5e\u66f2",
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
    "\u5b57\u5e55\u5236\u4f5c",
    "\u5b57\u5e55\u7ec4",
    "\u8c22\u8c22\u5927\u5bb6\u7684\u652f\u6301",
    "\u8bb0\u5f97\u70b9\u8d5e",
    "\u5173\u6ce8\u6211",
    "\u4e00\u952e\u4e09\u8fde",
    # Russian common hallucinations
    "\u041f\u043e\u0434\u043f\u0438\u0441\u044b\u0432\u0430\u0439\u0442\u0435\u0441\u044c \u043d\u0430 \u043a\u0430\u043d\u0430\u043b",
    "\u0421\u043f\u0430\u0441\u0438\u0431\u043e \u0437\u0430 \u043f\u0440\u043e\u0441\u043c\u043e\u0442\u0440",
    "\u0421\u0442\u0430\u0432\u044c\u0442\u0435 \u043b\u0430\u0439\u043a",
    "\u041d\u0430\u0436\u0438\u043c\u0430\u0439\u0442\u0435 \u043a\u043e\u043b\u043e\u043a\u043e\u043b\u044c\u0447\u0438\u043a",
    # Arabic common hallucinations
    "\u0627\u0634\u062a\u0631\u0643 \u0641\u064a \u0627\u0644\u0642\u0646\u0627\u0629",
    "\u0634\u0643\u0631\u0627 \u0644\u0644\u0645\u0634\u0627\u0647\u062f\u0629",
    "\u0644\u0627 \u062a\u0646\u0633\u0649 \u0627\u0644\u0627\u0639\u062c\u0627\u0628",  # bare alef form
    "\u0644\u0627 \u062a\u0646\u0633\u0649 \u0627\u0644\u0625\u0639\u062c\u0627\u0628",  # hamza-below form (Whisper standard)
    # Universal social media
    "Like",
    "Share",
    "Comment",
    "Follow",
]

# ── Credits-line patterns ─────────────────────────────────────────────────────
CREDITS_PATTERNS = [
    "\u4f5c\u8a5e\u30fb\u4f5c\u66f2",
    "\u4f5c\u66f2\u30fb\u7de8\u66f2",
    "\u4f5c\u8a5e",
    "\u4f5c\u66f2",
    "\u7de8\u66f2",
    "vocals",
    "vocal",
    "guitar",
    "bass",
    "drums",
    "piano",
    "illustration",
    "illust",
    "animation",
    "video",
    "mix",
    "mastering",
]

SINGLE_WORD_BLOCKLIST = ["music", "mm", "hmm"]  # vocal sounds (la/na/da/oh/ah are real lyrics)


# ── Detection logic ───────────────────────────────────────────────────────────

def is_hallucination(text):
    """Return True if text looks like a Whisper hallucination."""
    t = text.strip()
    if not t:
        return True
    # Normalize Unicode (NFC) to catch alternate representations of JA/ZH/AR text
    t = unicodedata.normalize("NFC", t)
    t_lower = t.lower()

    for pat in HALLUCINATION_PATTERNS:
        if pat.lower() in t_lower:
            return True

    # User-reported blacklist (sent from extension popup)
    for bl_item in _state.user_blacklist:
        if bl_item and bl_item.lower() in t_lower:
            print(f"[Yume] User blacklist match: {bl_item!r} in {t!r}")
            return True

    # Repeated word spam: "30k 30k 30k 30k" (6+ words, <=2 unique)
    words = t.split()
    if len(words) >= 6:
        unique = {w.lower().strip(".,!?") for w in words}
        if len(unique) <= 2:
            return True

    # Concatenated repetition: "musicmusic", "aaaaa", "la la la la"
    clean = t_lower.replace(" ", "")
    if len(clean) >= 4:
        for sub_len in range(2, min(9, len(clean) // 2 + 1)):
            sub = clean[:sub_len]
            if sub * (len(clean) // len(sub)) == clean[: len(sub) * (len(clean) // len(sub))]:
                repeats = len(clean) // len(sub)
                if repeats >= 2 and len(sub) * repeats >= len(clean) * 0.95:
                    print(f"[Yume] Hallucination: repeated '{sub}' x{repeats} in '{t}'")
                    return True

    if t_lower in SINGLE_WORD_BLOCKLIST:
        return True

    return False


def is_credits_line(text):
    """Return True if text looks like a credits/attribution line."""
    t = text.strip()
    t_lower = t.lower()
    for pat in CREDITS_PATTERNS:
        if pat.lower() in t_lower:
            return True
    if "\u30fb" in t and len(t) < 60:  # Japanese interpunct in short line = credits
        return True
    return False
