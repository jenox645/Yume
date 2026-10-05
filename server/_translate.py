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

import json
import re
import threading
import urllib.error
import urllib.request

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

# Kana, CJK ideographs (+ext A, compatibility), half-width katakana
_CJK_RE = re.compile("[぀-ヿ㐀-鿿豈-﫿ｦ-ﾟ]")
_CJK_RUN_RE = re.compile("[぀-ヿ㐀-鿿豈-﫿ｦ-ﾟ]+")

_MARKER_ONLY_RE = re.compile(r"^(?:\[?\d+[\].)]+\s*)+$")
_MARKER_PREFIX_RE = re.compile(r"^(?:\s*\[?\d+[\].)]+\s*)+")
_NUMBERED_RE = re.compile(r"^\[?(\d+)[\].)]\s*(.+)$")

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
    c = re.sub(r"^[\"'「」『』]+|[\"'「」『』]+$", "", c)
    return c.strip()


def _unshout(translation, source):
    """Sentence-case an ALL-CAPS translation of a line that was not itself
    written in capitals (models copy the casing of all-caps context lines)."""
    letters = [c for c in translation if c.isalpha() and c.isascii()]
    if len(letters) < 4 or translation != translation.upper():
        return translation
    src_letters = [c for c in source if c.isalpha() and c.isascii()]
    if src_letters and "".join(src_letters).isupper() and len(src_letters) == len([c for c in source if c.isalpha()]):
        return translation  # a Latin all-caps source line: keep its style
    low = translation.lower()
    return low[:1].upper() + low[1:]


def parse_numbered(raw, n):
    """Parse '[1] text' / '1. text' lines; positional fallback when unnumbered."""
    lines = [ln.strip() for ln in (raw or "").splitlines()]
    lines = [ln for ln in lines if ln and not _MARKER_ONLY_RE.match(ln)]
    out = [None] * n
    hits = 0
    for ln in lines:
        m = _NUMBERED_RE.match(ln)
        if m:
            idx = int(m.group(1)) - 1
            val = clean_translation(m.group(2))
            if 0 <= idx < n and val:
                out[idx] = val
                hits += 1
    if hits == 0:
        for i, ln in enumerate(lines[:n]):
            out[i] = clean_translation(ln) or None
    return out


def parse_json_list(raw, key, n):
    """Parse {"<key>": [...]} — tolerant of code fences and surrounding text."""
    text = (raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in response")
    obj = json.loads(text[start : end + 1])
    items = obj.get(key)
    if not isinstance(items, list):
        raise ValueError(f"missing '{key}' list")
    out = [clean_translation(str(x)) if x is not None else None for x in items[:n]]
    out += [None] * (n - len(out))
    return [x or None for x in out]


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
        schema = {
            "type": "object",
            "properties": {key: {"type": "array", "items": {"type": "string"}, "minItems": n, "maxItems": n}},
            "required": [key],
            "additionalProperties": False,
        }
        formats = {
            "json_schema": {"type": "json_schema", "json_schema": {"name": key, "strict": True, "schema": schema}},
            "json_object": {"type": "json_object", "schema": schema},
        }
        order = ["json_schema", "json_object", "plain"]
        with self._lock:
            known = self._mode.get(base)
        if known:
            order = order[order.index(known) :]
        last_err = None
        for mode in order:
            try:
                if mode == "plain":
                    numbered_hint = (
                        f"\nReply with exactly {n} lines, each starting with its number in brackets: [1] ..."
                    )
                    raw = self._chat(
                        [{"role": "system", "content": system + numbered_hint}, {"role": "user", "content": user}],
                        max_tokens,
                    )
                    result = parse_numbered(raw, n)
                else:
                    raw = self._chat(
                        [{"role": "system", "content": system}, {"role": "user", "content": user}],
                        max_tokens,
                        response_format=formats[mode],
                    )
                    result = parse_json_list(raw, key, n)
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
        raise last_err or TranslationError("translation failed")

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

    def translate_batch(self, texts, src_lang, tgt, title="", context=None):
        """Translate texts; returns a list of strings ('' where it failed)."""
        n = len(texts)
        if n == 0:
            return []
        # The system prompt stays identical for a whole video, so llama.cpp reuses
        # its cached prefix (an uncached prompt costs ~9 s on a CPU-only build);
        # everything that changes per batch (context, line count) goes last
        system = self._system_prompt(src_lang, tgt, title)
        system += '\nReturn JSON {"translations": [...]} with one string per numbered input line, in order.'
        lines = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(texts))
        user = f"Translate these {n} lines:\n{lines}"
        if context:
            ctx = " / ".join(f"{a} -> {b}" for a, b in context[-3:])[:400]
            user = f"(Previous lines, for context only — do not translate them: {ctx})\n\n{user}"
        max_tokens = min(3000, 120 + sum(len(t) for t in texts) * 4 + 40 * n)
        out = self._structured(system, user, "translations", n, max_tokens)

        result = []
        for text, tr in zip(texts, out):
            # One single-line retry for anything missing or leaking CJK into a
            # non-CJK target (a 7B model writes "smile in拉斯媒体" for katakana names).
            if not tr or (tgt not in CJK_LANGS and contains_cjk(tr)):
                try:
                    retry = self._single(text, src_lang, tgt)
                    if retry and not (tgt not in CJK_LANGS and contains_cjk(retry)):
                        tr = retry
                    elif tr or retry:
                        tr = strip_cjk(tr or retry)
                except TranslationError:
                    tr = strip_cjk(tr) if tr and tgt not in CJK_LANGS else tr
            result.append(_unshout(tr, text) if tr else "")
        return result

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
            system = (
                f"You transliterate {name} text into Latin letters. Do NOT translate. "
                f"Write how each line is pronounced, nothing else."
            )
        system += f'\nReturn JSON {{"romanizations": [...]}} with exactly {n} strings, one per numbered input line.'
        user = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(texts))
        out = self._structured(system, user, "romanizations", n, min(3000, 120 + sum(len(t) for t in texts) * 4))
        return [x or "" for x in out]
