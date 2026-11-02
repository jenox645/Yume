"""Gold set for translators: models scored on Yume's own lines against subtitles written by people.

    python tests/gold/mt.py refs             human English and French subtitles of the clips
    python tests/gold/mt.py run qwen hy7 ... translates what Whisper heard, batch by batch as a job does (GPU)
    python tests/gold/mt.py score
    python tests/gold/mt.py sample           the lines for a blind reading

The input is the base run of gold.py: the lines a viewer gets, mistakes included. The models come
from models/translation/ and run on Yume's own llama.cpp (tools/llama.cpp/).
"""

import collections
import contextlib
import io
import json
import random
import re
import statistics
import subprocess
import sys
import time
import urllib.request

import gold
from gold import DATA, MANIFEST, ROOT, _GpuLog, _ytdlp

import _romanize  # noqa: E402 (gold puts server/ on the path)
import _translate  # noqa: E402

PORT = 5055
TARGETS = {"English": ("en", "en-US", "en-GB"), "French": ("fr", "fr-FR")}
READING_LANGS = {"ja", "zh", "ko"}
QWEN = ROOT / "models" / "translation" / "qwen2.5-7b-instruct-q3_k_m.gguf"
MODELS = ROOT / "models" / "translation"
HY_SAMPLING = {"temperature": 0.7, "top_p": 0.6, "top_k": 20, "repeat_penalty": 1.05}
PROFILES = {  # name: (GGUF, how lines reach it)
    "qwen": (QWEN, "yume"),
    "hy7": (MODELS / "Hy-MT2-7B-Q4_K_M.gguf", "yume"),
    "hy7native": (MODELS / "Hy-MT2-7B-Q4_K_M.gguf", "native"),
    "hy18native": (MODELS / "Hy-MT2-1.8B-Q4_K_M.gguf", "native"),
    "hy7line": (MODELS / "Hy-MT2-7B-Q4_K_M.gguf", "line"),
    "hy18line": (MODELS / "Hy-MT2-1.8B-Q4_K_M.gguf", "line"),
    "hy7prod": (MODELS / "Hy-MT2-7B-Q4_K_M.gguf", "yume"),  # Translator's own route for this model name
    "hy18prod": (MODELS / "Hy-MT2-1.8B-Q4_K_M.gguf", "yume"),
}
DELIMITER = "<|>"
UNTRUSTED = {"XXJoJ8lugRY", "1Vijv1yhrY0", "rLbFE5qO95k", "f8wKGwwz0fM", "if3sdCLhS1A", "GpQ222I1ULc"}  # see RESULTS.md


def _clips():
    keep = []
    for clip in json.loads(MANIFEST.read_text(encoding="utf-8")):
        if clip["id"] not in UNTRUSTED and (DATA / "runs" / "base" / f"{clip['id']}.json").exists():
            keep.append(clip)
    return keep


def _ref_path(clip, target):
    return DATA / f"{clip['id']}.{TARGETS[target][0]}.json"


def refs():
    for clip in _clips():
        if clip["kind"] != "speech":
            continue
        for target, codes in TARGETS.items():
            path = _ref_path(clip, target)
            if path.exists():
                continue
            out = DATA / f"mt-{clip['id']}"
            _ytdlp("--skip-download", "--write-subs", "--sub-langs", ",".join(codes), "--sub-format", "vtt", "-o", str(out), "--", clip["id"])  # fmt: skip
            vtts = sorted(DATA.glob(f"mt-{clip['id']}*.vtt"))
            if vtts:
                cues = gold.parse_vtt(vtts[0].read_text(encoding="utf-8"))
                cues = gold.window(cues, gold.SPEECH_START_S, gold.SPEECH_S)
                path.write_text(json.dumps(cues, ensure_ascii=False), encoding="utf-8")
                print(f"  {clip['lang']} {clip['id']} {target}: {len(cues)} cues")
            for v in vtts:
                v.unlink()


# ── running a model ────────────────────────────────────────────────────────


class _Server:
    def __init__(self, gguf):
        exe = ROOT / "tools" / "llama.cpp" / "llama-server.exe"  # what users have
        self.log = open(DATA / "llama.log", "a", encoding="utf-8")  # noqa: SIM115
        command = [str(exe), "--model", str(gguf), "--host", "127.0.0.1", "--port", str(PORT)]
        command += ["--ctx-size", "4096", "--parallel", "1", "--no-webui", "--n-gpu-layers", "999", "--jinja"]
        self.process = subprocess.Popen(command, stdout=self.log, stderr=subprocess.STDOUT)

    def __enter__(self):
        deadline = time.time() + 180
        while time.time() < deadline:
            with contextlib.suppress(OSError):
                with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/health", timeout=2):
                    return self
            if self.process.poll() is not None:
                raise RuntimeError(f"llama-server stopped: {self.process.returncode}")
            time.sleep(1)
        raise RuntimeError("llama-server did not start")

    def __exit__(self, *exc):
        self.process.terminate()
        self.process.wait(timeout=30)
        self.log.close()


def _post(messages, max_tokens, **sampling):
    body = {"messages": messages, "max_tokens": max_tokens, "stream": False, **sampling}
    request = urllib.request.Request(
        f"http://127.0.0.1:{PORT}/v1/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=180) as reply:
        return json.load(reply)["choices"][0]["message"]["content"] or ""


def _native_one(text, tgt):
    prompt = (
        f"Translate the following text into {tgt}. Note that you should only output the "
        f"translated result without any additional explanation:\n\n{text}"
    )
    return _translate.clean_translation(_post([{"role": "user", "content": prompt}], 200, **HY_SAMPLING))


def native_batch(texts, tgt, title, context):
    """Hy-MT2's own templates: one user message, no system prompt, its delimiter rule."""
    background = "\n".join(filter(None, [title and f"Video title: {title}", *(a for a, _ in context[-3:])]))
    prompt = (f"[Background Information]\n{background}\n\n" if background else "") + (
        f"Please accurately translate the following text into {tgt}.\nYou must retain the exact same number of "
        f"delimiters in the translation. Strictly do not omit, escape, or translate these symbols, and pay close "
        f"attention to their placement.\n\n{DELIMITER.join(texts)}"
    )
    raw = _post([{"role": "user", "content": prompt}], 60 + int(sum(map(len, texts)) * 3), **HY_SAMPLING)
    pieces = [_translate.clean_translation(p) for p in raw.split(DELIMITER)]
    if len(pieces) == len(texts):
        return pieces, 0
    return [_native_one(t, tgt) for t in texts], 1  # a wrong count is detectable: one by one


def line_batch(texts, start, count, tgt, title):
    """One line at a time, its neighbours as background: a line cannot slide into the next one's slot."""
    out = []
    for i in range(start, min(start + count, len(texts))):
        near = [title and f"Video title: {title}", *texts[max(0, i - 2) : i], *texts[i + 1 : i + 3]]
        background = "\n".join(filter(None, near))
        prompt = (
            f"[Background Information]\n{background}\n\nPlease translate the following text into {tgt}, taking "
            f"the provided background information into consideration. Translate only this text:\n\n"
            f"[Source Text]\n{texts[i]}"
        )
        raw = _post([{"role": "user", "content": prompt}], 40 + len(texts[i]) * 3, **HY_SAMPLING)
        out.append(_translate.clean_translation(raw))
    return out


def translate_clip(clip, target, how, translator):
    lang = clip["lang"]
    with contextlib.redirect_stdout(io.StringIO()):
        result = json.loads((DATA / "runs" / "base" / f"{clip['id']}.json").read_text(encoding="utf-8"))
        lines = gold.shown_lines(result, lang)
    texts = [t for *_, t in lines]
    out, context, fallbacks, seconds = [], [], 0, []
    for i in range(0, len(texts), _translate.BATCH_SIZE):
        batch = texts[i : i + _translate.BATCH_SIZE]
        t0 = time.time()
        if how == "line":
            done = line_batch(texts, i, _translate.BATCH_SIZE, target, clip["title"])
        elif how == "native":
            done, fell = native_batch(batch, target, clip["title"], context)
            fallbacks += fell
        else:
            readings = [_romanize.romanize(lang, t) or "" for t in batch] if lang in READING_LANGS else None
            done = translator.translate_batch(batch, lang, target, clip["title"], context, readings)
        seconds.append(time.time() - t0)
        out += done
        context = (context + [(a, b) for a, b in zip(batch, done, strict=True) if b])[-3:]
    return {"lines": lines, "out": out, "seconds": seconds, "fallbacks": fallbacks}


def run(names):
    clips = [c for c in _clips() if c["kind"] == "song" or any(_ref_path(c, t).exists() for t in TARGETS)]
    for name in names:
        gguf, how = PROFILES[name]
        out_dir = DATA / "mt" / name
        out_dir.mkdir(parents=True, exist_ok=True)
        # Named as in the config, unless the profile tests the JSON route on this model
        model = "" if name == "hy7" else gguf.name
        settings = {"host": "127.0.0.1", "port": PORT, "backend": "llamacpp", "model": model, "prompt": ""}
        with _Server(gguf), _GpuLog() as gpu:
            translator = _translate.Translator(lambda: settings)
            for clip in clips:
                for target in TARGETS:
                    wanted = clip["kind"] == "song" or _ref_path(clip, target).exists()
                    path = out_dir / f"{clip['id']}.{target}.json"
                    if wanted and not path.exists():
                        result = translate_clip(clip, target, how, translator)
                        path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
                        print(
                            f"  {name} {clip['lang']} {clip['id']} {target} {sum(result['seconds']):.0f} s", flush=True
                        )
        (out_dir / "_gpu.json").write_text(json.dumps(gpu.summary()), encoding="utf-8")
        print(f"[mt] {name}: {gpu.summary()}", flush=True)


# ── scoring ────────────────────────────────────────────────────────────────


def _ngrams(text, n):
    return collections.Counter(text[i : i + n] for i in range(len(text) - n + 1))


def chrf(hyp, ref, beta=2, max_n=6):
    """chrF on characters without spaces: survives a different cut into lines."""
    hyp, ref = (re.sub(r"\s+", "", x.lower()) for x in (hyp, ref))
    precisions, recalls = [], []
    for n in range(1, max_n + 1):
        h, r = _ngrams(hyp, n), _ngrams(ref, n)
        if h and r:
            shared = sum((h & r).values())
            precisions.append(shared / sum(h.values()))
            recalls.append(shared / sum(r.values()))
    if not precisions:
        return 0.0
    p, r = statistics.mean(precisions), statistics.mean(recalls)
    return 100 * (1 + beta**2) * p * r / (beta**2 * p + r) if p + r else 0.0


_NOT_NAMES = set("I I'm I've I'll English French Japanese Korean Chinese Russian Arabic Japan Korea China".split())


def names_in(text):
    """Capitalised words inside a sentence: what a reference spells as a name."""
    found = re.findall(r"(?<=[a-zà-ÿ,;:] )([A-Z][\w'-]{2,})", text)
    return {w for w in found if w not in _NOT_NAMES}


def line_faults(result, target):
    """Fractions of lines that came back empty, in the source's script, or repeating the line before."""
    source = [t for *_, t in result["lines"]]
    out = result["out"]
    empty = sum(not t for t in out)
    leak = sum(bool(t) and _translate.contains_cjk(t) for t in out)
    repeat = sum(bool(a) and a == b for a, b in zip(out, out[1:], strict=False))
    long = sum(len(t) > 3 * max(len(s), 15) for s, t in zip(source, out, strict=True))
    return {"lines": len(out), "empty": empty, "leak": leak, "repeat": repeat, "long": long}


def _report(name, clips, target):
    rows, faults, names_hit, names_all, times = [], collections.Counter(), 0, 0, []
    for clip in clips:
        path = DATA / "mt" / name / f"{clip['id']}.{target}.json"
        if not path.exists():
            continue
        result = json.loads(path.read_text(encoding="utf-8"))
        faults.update(line_faults(result, target))
        times += result["seconds"]
        ref_path = _ref_path(clip, target)
        if ref_path.exists():
            ref = " ".join(c[2] for c in json.loads(ref_path.read_text(encoding="utf-8")))
            hyp = " ".join(result["out"])
            rows.append(chrf(hyp, ref))
            want = names_in(ref)
            names_all += len(want)
            names_hit += sum(n.lower() in hyp.lower() for n in want)
    median = statistics.median(rows) if rows else 0
    time_s = statistics.median(times) if times else 0
    return (
        f"chrF {median:5.1f} ({len(rows)})  names {names_hit}/{names_all}  {time_s:4.1f} s/batch  "
        f"lines {faults['lines']} empty {faults['empty']} leak {faults['leak']} repeat {faults['repeat']} long {faults['long']}"
    )


def score():
    clips = _clips()
    usable = [c for c in clips if c["kind"] == "speech"]
    names = [n for n in PROFILES if (DATA / "mt" / n).exists()]
    for target in TARGETS:
        for lang in [*gold.QUERIES, None]:
            group = [c for c in usable if lang in (None, c["lang"])]
            if not any(_ref_path(c, target).exists() for c in group):
                continue
            print(f"\n{target} talks, {lang or 'ALL'}")
            for name in names:
                print(f"  {name:11}", _report(name, group, target))
        print(f"\n{target} songs (no reference)")
        for name in names:
            print(f"  {name:11}", _report(name, [c for c in clips if c["kind"] == "song"], target))
    for name in names:
        print(
            name, (DATA / "mt" / name / "_gpu.json").read_text() if (DATA / "mt" / name / "_gpu.json").exists() else ""
        )


def sample(names, per_clip=6, seed=7):
    """Talks only, lines at random, models under letters; the key goes to its own file."""
    rng = random.Random(seed)
    letters = dict(zip(names, "ABCDEFG", strict=False))
    rows = []
    for clip in _clips():
        for target in TARGETS:
            found = {n: DATA / "mt" / n / f"{clip['id']}.{target}.json" for n in names}
            if clip["kind"] != "speech" or not all(p.exists() for p in found.values()):
                continue
            results = {n: json.loads(p.read_text(encoding="utf-8")) for n, p in found.items()}
            count = len(next(iter(results.values()))["lines"])
            for i in rng.sample(range(count), min(per_clip, count)):
                options = [(letters[n], results[n]["out"][i]) for n in names]
                rng.shuffle(options)
                source = next(iter(results.values()))["lines"][i][2]
                rows.append({"lang": clip["lang"], "target": target, "source": source, "options": options})
    rng.shuffle(rows)
    (DATA / "mt" / "sample.json").write_text(json.dumps(rows, ensure_ascii=False, indent=0), encoding="utf-8")
    (DATA / "mt" / "sample_key.json").write_text(json.dumps(letters), encoding="utf-8")
    print(len(rows), "lines")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    command, args = sys.argv[1], sys.argv[2:]
    if command == "refs":
        refs()
    elif command == "run":
        run(args)
    elif command == "sample":
        sample(args)
    else:
        score()
