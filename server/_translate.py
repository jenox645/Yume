"""Subtitle translation and LLM romanization through an OpenAI-compatible server.

Runs inside the Whisper server (it used to run in the extension's service
worker). Batches use structured output — a JSON schema with exactly N strings —
so the model cannot drop, merge or renumber lines. The old free-text "[N] line"
format needed marker regexes, a positional fallback and three stacked retry
layers to survive weak models; that format is still the last-resort fallback
for backends that reject response_format.

Every supported backend speaks /v1/chat/completions: llama.cpp (llama-cpp-python
or llama-server), Ollama (>= 0.5 for json_schema), LM Studio, text-generation-webui.
"""

import difflib
import json
import re
import threading
import urllib.error
import urllib.request

from _romanize import _latin_only

LANG_NAMES = {
    "ja": "Japanese",
    "zh": "Chinese",
    "ko": "Korean",
    "en": "English",
    "fr": "French",
    "de": "German",
    "es": "Spanish",
    "pt": "Portuguese",
    "ru": "Russian",
    "it": "Italian",
    "ar": "Arabic",
}
CJK_LANGS = {"Chinese", "Japanese", "Korean"}
# One example line per LLM-romanized language, to anchor the style
_ROMA_EXAMPLES = {
    "ar": "كيفك انت بتذكر آخر مرة → kifak enta btetzakar akher marra",
    "ja": "君の名前を呼んだ → kimi no namae wo yonda",
    "zh": "我想你 → wǒ xiǎng nǐ",
}

# Kana, CJK ideographs (+ext A, compatibility), half-width katakana
_CJK_RE = re.compile("[぀-ヿ㐀-鿿豈-﫿ｦ-ﾟ]")
_CJK_RUN_RE = re.compile(_CJK_RE.pattern + "+")

_MARKER_ONLY_RE = re.compile(r"^(?:\[?\d+[\].)]+\s*)+$")
_MARKER_PREFIX_RE = re.compile(r"^(?:\s*\[?\d+[\].)]+\s*)+")
# "[1] …", "1. …", "1) …", "1: …", "**1.** …" (not "3:30 …", a time)
_NUMBERED_RE = re.compile(r"^\**\[?(\d+)(?:[\].)]|:(?=\s))\**\s*(.+)$")
# The reasoning a thinking model (Qwen3, DeepSeek-R1) writes before its answer
_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
# Quotes a model wraps a translation in: (opening, closing)
_QUOTES = (('"', '"'), ("“", "”"), ("「", "」"), ("『", "』"), ("'", "'"))

BATCH_SIZE = 10
TIMEOUT_S = 120  # consumer GPUs can take >60 s before the first token of a batch


class TranslationError(Exception):
    pass


def src_name(lang):
    return "the original language" if not lang or lang == "auto" else LANG_NAMES.get(lang, lang)


def contains_cjk(s):
    return bool(s) and bool(_CJK_RE.search(s))


def strip_cjk(s):
    return re.sub(r"\s{2,}", " ", _CJK_RUN_RE.sub("", s)).strip()


def forbidden_scripts(src_lang, tgt):
    """Scripts the model must not output. A CJK target gets no CJK ban (kanji and
    hanzi are shared), and the target's own script is never banned."""
    src = None if not src_lang or src_lang == "auto" else LANG_NAMES.get(src_lang)
    tgt_cjk = tgt in CJK_LANGS
    names = [] if tgt_cjk else ["Chinese", "Japanese"]
    if src and src not in names and not (tgt_cjk and src in CJK_LANGS):
        names.insert(0, src)
    return [n for n in names if n != tgt]


def clean_translation(text):
    """Strip echoes weak models add around a translation."""
    c = (text or "").strip()
    if len(c) > 2000:
        return c
    c = _MARKER_PREFIX_RE.sub("", c).strip()
    c = re.sub(r"^.{1,40}\s+translates?\s+to\s+", "", c, flags=re.I)
    c = re.sub(r"^translation:\s*", "", c, flags=re.I)
    c = re.sub(r"^line\s*\d+:\s*", "", c, flags=re.I)
    c = re.sub(r"^(Romanization|Romaji|English)\s*[:：]\s*", "", c, flags=re.I)
    return _unquote(c.strip())


def _unquote(c):
    """Quotes around the whole translation ("Hello" → Hello), or a lone one at
    an end. An apostrophe that belongs to the words stays: 'Cause, dreamin',
    and so does a quote inside the line: 「君の名は」という映画."""
    for o, e in _QUOTES:
        inner = _inside_quotes(c, o, e)
        if inner is not None:
            return _unquote(inner.strip())
    return c


def _inside_quotes(c, o, e):
    """c without the quotes (o, e) around it, or None if they are not around it."""
    marks = sum(c.count(q) for q in {o, e})
    if marks == 2 and c[0] == o and c[-1] == e:
        return c[1:-1]
    if marks == 1 and o != "'" and (c[:1] in (o, e) or c[-1:] in (o, e)):
        return c.strip(o + e)
    return None


def _unshout(translation, source):
    """Sentence-case an ALL-CAPS translation of a line that was not itself
    written in capitals (models copy the casing of all-caps context lines)."""
    letters = [c for c in translation if c.isalpha() and c.isascii()]
    if len(letters) < 4 or translation != translation.upper() or _in_latin_capitals(source):
        return translation
    low = translation.lower()
    return low[:1].upper() + low[1:]


def _in_latin_capitals(text):
    letters = [c for c in text if c.isalpha()]
    return bool(letters) and all(c.isascii() and c.isupper() for c in letters)


# A reading the model copied into its answer (never seen in tests, but cheap)
_READING_RE = re.compile(r"\s*\(reading:[^)]*\)", re.IGNORECASE)


def _is_reading(translation, reading, text=""):
    """The model gave back the line's romanization instead of translating it
    ("Iradachi wo arawa ni shita" for 苛立ちをあらわにした). A line that is mostly
    English already ("所以, jailbreaks talk about topics") rightly looks like its reading."""
    if not translation or not reading:
        return False
    letters = [c for c in text if c.isalpha()]
    if letters and sum(c.isascii() for c in letters) * 2 >= len(letters):
        return False
    a, b = (re.sub(r"[\W_]+", "", x.lower()) for x in (translation, reading))
    return a == b or difflib.SequenceMatcher(None, a, b).ratio() > 0.7


def _has_words(text):
    return any(c.isalnum() for c in text)


def _is_english(tgt):
    return (tgt or "").strip().lower() in ("english", "en")


def _drop_copied(texts, out):
    """'' (= retry alone) for a translation the model gave to different lines.

    A sentence spread over several lines came back as the whole sentence on
    each of them ("These men, who were arrested as part of Strikeforce Pearl…"
    on three lines). The same line repeated, like a chorus, may share one."""
    sources = {}
    for text, tr in zip(texts, out):
        if tr:
            sources.setdefault(tr.strip().lower(), set()).add(re.sub(r"[\W_]+", "", text.lower()))
    return [("" if tr and len(sources[tr.strip().lower()]) > 1 else tr) for tr in out]


_JSON_REPLY_RE = re.compile(r'^\s*(?:```\w*\s*)?[{\[]\s*["{\[]')
_JSON_STRING_RE = re.compile(r'"((?:[^"\\]|\\.)*)"\s*(?=[,\]}]|$)')
_ESCAPE_RE = re.compile(r"\\(.)", re.DOTALL)


def _json_string(s):
    """A JSON string as a model wrote it: an escape JSON does not have ("it\\'s")
    stands for the character itself, and a raw newline is allowed."""
    valid = _ESCAPE_RE.sub(lambda m: m.group(0) if m.group(1) in '"\\/bfnrtu' else m.group(1), s)
    try:
        return json.loads(f'"{valid}"', strict=False)
    except json.JSONDecodeError:  # a \u escape cut short
        return valid


def _json_like_list(raw, key, n):
    """The strings of a reply that looks like JSON but may not parse (no closing
    brace, cut off), or None if it does not look like JSON. The subtitles showed
    `{"translations": ["[1] It targeted...` line by line."""
    if not _JSON_REPLY_RE.match(raw):
        return None
    try:
        return parse_json_list(raw, key, n)
    except (ValueError, json.JSONDecodeError):
        pass
    found = [_json_string(m) for m in _JSON_STRING_RE.findall(raw) if m != key]
    return ([clean_translation(x) or None for x in found[:n]] + [None] * n)[:n]


def parse_numbered(raw, n, key="translations"):
    """Parse '[1] text' / '1. text' lines; positional fallback when unnumbered.
    A model told to number its lines can still answer in JSON."""
    raw = _THINK_RE.sub("", raw or "")
    as_json = _json_like_list(raw, key, n)
    if as_json is not None:
        return as_json
    lines = [ln.strip() for ln in raw.splitlines()]
    lines = [ln for ln in lines if ln and not _MARKER_ONLY_RE.match(ln)]
    return _by_number(lines, n) or _by_position(lines, n)


def _by_number(lines, n):
    """The lines that carry a number in 1..n, in its place; None if none does."""
    out = [None] * n
    for ln in lines:
        m = _NUMBERED_RE.match(ln)
        if m:
            idx = int(m.group(1)) - 1
            val = clean_translation(m.group(2))
            if 0 <= idx < n and val:
                out[idx] = val
    return out if any(out) else None


def _by_position(lines, n):
    out = [clean_translation(ln) or None for ln in lines[:n]]
    return out + [None] * (n - len(out))


def parse_json_list(raw, key, n):
    """Parse {"<key>": [...]} — tolerant of code fences, a thinking model's
    <think> block, text after the JSON, another key, a bare list and
    {"text": …} items (what json_object mode gets from small models)."""
    items = _json_list(_THINK_RE.sub("", raw or ""), key)
    out = [_item_text(x) for x in items[:n]]
    out += [None] * (n - len(out))
    return [x or None for x in out]


def _json_list(text, key):
    """The list in a JSON reply: under `key`, the object's only list, or the reply itself."""
    for opener in "{[":
        start = text.find(opener)
        if start < 0:
            continue
        try:
            obj = json.JSONDecoder().raw_decode(text[start:])[0]
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            lists = [v for v in obj.values() if isinstance(v, list)]
            obj = obj.get(key) if isinstance(obj.get(key), list) else lists[0] if len(lists) == 1 else None
        if isinstance(obj, list):
            return obj
    raise ValueError(f"no '{key}' list in response")


def _item_text(x):
    if isinstance(x, dict):  # {"text": "…"} or {"translation": "…"}
        x = next((v for v in x.values() if isinstance(v, str)), None)
    return clean_translation(str(x)) if x is not None else None


def _response_format(mode, key, n):
    schema = {
        "type": "object",
        "properties": {key: {"type": "array", "items": {"type": "string"}, "minItems": n, "maxItems": n}},
        "required": [key],
        "additionalProperties": False,
    }
    if mode == "json_schema":
        return {"type": "json_schema", "json_schema": {"name": key, "strict": True, "schema": schema}}
    return {"type": "json_object", "schema": schema}


def _batch_message(texts, readings, context):
    """What changes per batch (see _translate_lines): the numbered lines and the context before them."""
    lines = "\n".join(
        f"{i + 1}. {t} (reading: {r})" if r and r != t else f"{i + 1}. {t}"
        for i, (t, r) in enumerate(zip(texts, readings))
    )
    message = f"Translate these {len(texts)} lines:\n{lines}"
    if context:
        ctx = " / ".join(f"{a} -> {b}" for a, b in context[-3:])[:400]
        message = f"(Previous lines, for context only — do not translate them: {ctx})\n\n{message}"
    return message


def _final_line(tr, retry, leaks):
    """The better of a batch's line and its single-line retry. Neither clean of
    CJK: the leak cut out, the rest is still better than no line."""
    if retry and not (leaks and contains_cjk(retry)):
        return retry
    line = tr or retry
    return strip_cjk(line) if line and leaks else line


class Translator:
    """One instance per server; the LLM worker is its only caller."""

    def __init__(self, settings):
        # settings: callable returning dict(host, port, backend, model, prompt, roma_prompt)
        self._settings = settings
        self._mode = {}  # endpoint -> "json_schema" | "json_object" | "plain"
        self._lock = threading.Lock()

    # ── HTTP ──────────────────────────────────────────────────────────────────

    def _cfg(self):
        s = self._settings()
        return s, f"http://{s['host']}:{s['port']}"  # noqa: S5332 — local LLM backend

    def _chat(self, messages, max_tokens, response_format=None, temperature=0.1):
        s, base = self._cfg()
        body = {"messages": messages, "max_tokens": max_tokens, "temperature": temperature, "stream": False}
        # llama-cpp-python serves exactly one model; every other backend REQUIRES
        # the model name (Ollama rejects requests without it).
        if s.get("backend") != "llamacpp" and s.get("model"):
            body["model"] = s["model"]
        if response_format:
            body["response_format"] = response_format
        req = urllib.request.Request(
            f"{base}/v1/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:  # nosec B310 — configured LLM endpoint
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            raise TranslationError(f"HTTP {e.code}: {detail}") from e
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            raise TranslationError(f"translation server unreachable: {e}") from e
        try:
            return data["choices"][0]["message"]["content"] or ""
        except (KeyError, IndexError, TypeError) as e:
            raise TranslationError(f"unexpected response: {str(data)[:200]}") from e

    def health(self):
        s, base = self._cfg()
        path = "/api/tags" if s.get("backend") == "ollama" else "/v1/models"
        try:
            with urllib.request.urlopen(f"{base}{path}", timeout=5) as resp:  # nosec B310
                data = json.loads(resp.read())
            models = [m.get("name") or m.get("id") for m in (data.get("models") or data.get("data") or [])]
            return {"up": True, "backend": s.get("backend"), "address": base, "models": models}
        except Exception as e:
            return {"up": False, "backend": s.get("backend"), "address": base, "error": str(e)}

    def model_id(self):
        """Identity of what produces translations, for cache keys: backend,
        model and — when set — a fingerprint of the custom prompt, so editing
        the prompt does not keep serving lines translated with the old one."""
        s = self._settings()
        ident = f"{s.get('backend')}:{s.get('model') or 'default'}"
        prompt = (s.get("prompt") or "").strip()
        if prompt:
            import hashlib

            ident += ":" + hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:10]
        return ident

    # ── Structured batch with fallback ────────────────────────────────────────

    def _structured(self, system, user, key, n, max_tokens):
        """Return n items (None = missing). Picks the best output format the
        backend accepts and remembers it per endpoint."""
        _, base = self._cfg()
        order = ["json_schema", "json_object", "plain"]
        with self._lock:
            known = self._mode.get(base)
        if known:
            order = order[order.index(known) :]
        last_err = None
        # A reply that does not parse (cut off at max_tokens) is this batch's problem:
        # the backend accepted the format, so nothing lower is remembered for it. The
        # first batch after a start used to leave the endpoint on "plain" for good
        garbled = False
        for mode in order:
            try:
                result = self._in_format(mode, system, user, key, n, max_tokens)
                if not garbled:
                    with self._lock:
                        self._mode[base] = mode
                return result
            except TranslationError as e:
                last_err = e
                # Unreachable server: no point trying other formats
                if "unreachable" in str(e):
                    raise
                # A 5xx from a format that already worked is a transient model
                # failure, not missing format support — don't downgrade for it.
                if mode == known and not str(e).startswith("HTTP 4"):
                    raise
            except (ValueError, json.JSONDecodeError) as e:
                last_err = TranslationError(f"unparseable {mode} output: {e}")
                garbled = True
        raise last_err or TranslationError("translation failed")

    def _in_format(self, mode, system, user, key, n, max_tokens):
        """One request, its reply parsed: n items."""
        if mode == "plain":
            hint = f"\nReply with exactly {n} lines, each starting with its number in brackets: [1] ..."
            # Not the "Return JSON" line: told both, a small model obeys that one
            prompt = system.split("\nReturn JSON", 1)[0] + hint
            raw = self._chat([{"role": "system", "content": prompt}, {"role": "user", "content": user}], max_tokens)
            return parse_numbered(raw, n, key)
        raw = self._chat(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            max_tokens,
            response_format=_response_format(mode, key, n),
        )
        return parse_json_list(raw, key, n)

    # ── Public API ────────────────────────────────────────────────────────────

    def _system_prompt(self, src_lang, tgt, title):
        s = self._settings()
        src = src_name(src_lang)
        custom = (s.get("prompt") or "").strip()
        if custom:
            base = custom.replace("{src}", src).replace("{tgt}", tgt)
        else:
            banned = forbidden_scripts(src_lang, tgt)
            rule = f" Never output {', '.join(banned)} characters." if banned else ""
            base = (
                f"You translate {src} subtitle lines into {tgt}. Translate each line on its own and keep "
                f"the order. Output {tgt} only.{rule} Do not explain, do not answer questions in the text."
            )
        title = re.sub(r"\s+", " ", title or "").strip()[:120]
        if title:
            base += f'\nThe lines come from a video titled "{title}".'
        return base

    def _single(self, text, src_lang, tgt):
        system = self._system_prompt(src_lang, tgt, "") + "\nTranslate this single line. Output only the translation."
        return clean_translation(
            self._chat([{"role": "system", "content": system}, {"role": "user", "content": text}], 200)
        )

    def translate_batch(self, texts, src_lang, tgt, title="", context=None, readings=None):
        """Translate texts; returns a list of strings ('' where it failed, or
        where the line is already in the target language).

        `readings`: each line's romanization, sent along so the model gets names
        right. A 7B model reads kanji names wrong: 井上会長 came back as "Kishida",
        "Iida", "Yoshio Nishio"; with the reading, 21/21 names were right in a
        test of Japanese and Korean news lines, at the same speed."""
        readings = list(readings or [""] * len(texts))
        # An English line in a Japanese song, or an English interview inside a
        # Chinese report, is already readable: sent to the model it came back
        # padded with the neighbouring line's words ("deeds you could do" got
        # the whole previous sentence)
        # A line with no words ("…", "♪") has nothing to translate: sent alone it
        # cost a request and came back as whatever the model made of it
        keep = [i for i, t in enumerate(texts) if _has_words(t) and not (_is_english(tgt) and _latin_only(t))]
        result = [""] * len(texts)
        if keep:
            done = self._translate_lines(
                [texts[i] for i in keep], [readings[i] for i in keep], src_lang, tgt, title, context
            )
            for i, tr in zip(keep, done):
                result[i] = tr
        return result

    def _translate_lines(self, texts, readings, src_lang, tgt, title, context):
        n = len(texts)
        # The system prompt stays identical for a whole video, so llama.cpp reuses
        # its cached prefix (an uncached prompt costs ~9 s on a CPU-only build);
        # everything that changes per batch (context, line count) goes last
        system = self._system_prompt(src_lang, tgt, title)
        system += '\nReturn JSON {"translations": [...]} with one string per numbered input line, in order.'
        max_tokens = min(3000, 120 + sum(len(t) for t in texts) * 4 + 40 * n)
        out = self._structured(system, _batch_message(texts, readings, context), "translations", n, max_tokens)
        out = [_READING_RE.sub("", tr).strip() if tr else tr for tr in out]
        # A copied reading is no translation: retried alone, without the reading,
        # and never fed back as context (it snowballed: batch after batch of romaji)
        out = _drop_copied(texts, ["" if _is_reading(tr, r, t) else tr for tr, r, t in zip(out, readings, texts)])
        return [self._checked(text, tr, reading, src_lang, tgt) for text, tr, reading in zip(texts, out, readings)]

    def _checked(self, text, tr, reading, src_lang, tgt):
        """One single-line retry for anything missing or leaking CJK into a
        non-CJK target (a 7B model writes "smile in拉斯媒体" for katakana names)."""
        leaks = tgt not in CJK_LANGS
        if tr and not (leaks and contains_cjk(tr)):
            return _unshout(tr, text)
        line = _final_line(tr, self._retried(text, reading, src_lang, tgt), leaks)
        return _unshout(line, text) if line else ""

    def _retried(self, text, reading, src_lang, tgt):
        try:
            retry = self._single(text, src_lang, tgt)
        except TranslationError:
            return ""
        return "" if _is_reading(retry, reading, text) else retry

    def romanize_batch(self, texts, lang):
        """LLM transliteration (used for Arabic, and for JA/ZH when the
        deterministic libraries are not installed)."""
        n = len(texts)
        if n == 0:
            return []
        s = self._settings()
        name = LANG_NAMES.get(lang, lang)
        custom = (s.get("roma_prompt") or "").strip()
        if custom:
            system = custom.replace("{src}", name).replace("{sys}", "romanization")
        else:
            # A style to hold to: without one, a small model switched between
            # batches ("kayfak ant" / "Kifka qala 'am b-qoole")
            system = (
                f"You transliterate {name} text into Latin letters. Do NOT translate. "
                f"Write how each line is pronounced, nothing else. Use a simple, readable "
                f"romanization like song-lyric sites: lowercase, one Latin word per original word, "
                f"no hyphens inside words."
            )
            if lang in _ROMA_EXAMPLES:
                system += f" Example: {_ROMA_EXAMPLES[lang]}"
        system += f'\nReturn JSON {{"romanizations": [...]}} with exactly {n} strings, one per numbered input line.'
        user = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(texts))
        out = self._structured(system, user, "romanizations", n, min(3000, 120 + sum(len(t) for t in texts) * 4))
        return [x or "" for x in out]
