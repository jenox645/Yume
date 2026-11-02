"""Gold set: Whisper settings scored against what people wrote.

    python tests/gold/gold.py find           videos with captions written by people, songs with timed lyrics
    python tests/gold/gold.py fetch          their audio and references, and the vocals of the songs (GPU)
    python tests/gold/gold.py run base ...   transcribes them as a job does (GPU, loud); resumes where it stopped
    python tests/gold/gold.py score base ... the tables, no GPU: clean_region runs again on the raw lines

Everything fetched stays in tests/gold/data/, outside git: the references are
other people's text. manifest.json only lists what was picked.
"""

import json
import re
import statistics
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.parse
import urllib.request
from pathlib import Path

HERE = Path(__file__).parent
ROOT = HERE.parents[1]
DATA = HERE / "data"
MANIFEST = HERE / "manifest.json"
YTDLP = str(ROOT / "tools" / "yt-dlp.exe" if (ROOT / "tools" / "yt-dlp.exe").exists() else ROOT / "tools" / "yt-dlp")
sys.path.insert(0, str(ROOT / "server"))

SR = 16000
SPEECH_START_S, SPEECH_S = 20, 150
SONG_S = 300
PER_LANGUAGE = 6
SONGS_PER_LANGUAGE = {"ja": 6, "ko": 5, "zh": 4, "ru": 3, "ar": 2}
BAD_REFERENCE = 0.6  # error on the base run: the text is not what is said (a summary, another language)
TOO_CLEAN = 0.015  # ...or suspiciously close: probably a machine's captions

# Words for a search; YouTube's "has subtitles" filter does the rest
QUERIES = {
    "ja": ["インタビュー 対談", "講演 日本語", "ニュース 解説", "ドキュメンタリー", "vlog 日本語字幕"],
    "zh": ["TEDxTaipei", "TEDx 中文 演讲", "访谈 中文字幕", "演讲 中文", "纪录片 中文", "新闻 解说", "訪談 繁體中文"],
    "ko": [
        "TEDxSeoul 강연",
        "세바시 강연",
        "EBS 다큐프라임",
        "대학 강의 한국어",
        "인터뷰 한글자막",
        "강연 한국어",
        "다큐멘터리",
        "뉴스 해설",
        "브이로그 자막",
    ],
    "ru": [
        "TEDx на русском",
        "TEDx выступление по-русски",
        "интервью русские субтитры",
        "лекция на русском",
        "документальный фильм",
        "новости разбор",
        "выступление",
    ],
    "ar": [
        "TEDx بالعربية",
        "TEDx عربي محاضرة",
        "مقابلة ترجمة عربية",
        "محاضرة بالعربية",
        "وثائقي",
        "نشرة أخبار",
        "خطاب",
    ],
}
HAS_SUBTITLES = "EgIoAQ%3D%3D"
_NOT_THE_RECORD = re.compile(r"live|cover|remix|karaoke|instrumental|reaction|lyric video|커버|歌ってみた|翻唱", re.I)

VARIANTS = {  # model, beam, what a song's Whisper hears
    "base": ("large-v3-turbo", 5, "vocals"),
    "beam1": ("large-v3-turbo", 1, "vocals"),
    "beam2": ("large-v3-turbo", 2, "vocals"),
    "v3": ("large-v3", 5, "vocals"),
    "v3beam1": ("large-v3", 1, "vocals"),
    "mix": ("large-v3-turbo", 5, "mix"),
    "trim": ("large-v3-turbo", 5, "trim"),
    "v3mix": ("large-v3", 5, "mix"),
    "vk": ("large-v3-turbo", 5, "vocals"),
    "vktrim": ("large-v3-turbo", 5, "trim"),
}


# ── text → what is compared ────────────────────────────────────────────────


_NOISE = re.compile(r"\[[^\]]*\]|\([^)]*\)|（[^）]*）|【[^】]*】|<[^>]*>|^\s*(>>|-|–|—)+", re.M)


def _letters(text):
    kept = (c if unicodedata.category(c)[0] in "LN" else " " for c in text if unicodedata.category(c) != "Mn")
    return "".join(kept)


def _kana(text):
    import fugashi  # the words around a kanji decide its reading: pykakasi reads 名 as めい in 君の名は
    import jaconv

    tagger = fugashi.Tagger()
    return jaconv.kata2hira("".join((w.feature.kana or w.surface) for w in tagger(text)))


def tokens(text, lang):
    """The units errors are counted in: readings where one sound has many spellings
    (kanji/kana, simplified/traditional), so only what was heard is judged."""
    text = _letters(unicodedata.normalize("NFKC", _NOISE.sub(" ", text)).lower())
    if lang == "ja":
        return [c for c in _kana(text) if not c.isspace()]
    if lang == "zh":
        from pypinyin import lazy_pinyin

        return [p for p in lazy_pinyin(text) for p in p.split()]
    if lang == "ko":
        return [c for c in text if not c.isspace()]
    if lang == "ar":
        text = re.sub("[آأإ]", "ا", text).replace("ى", "ي").replace("ة", "ه")
    return text.replace("ё", "е").split()


def errors(ref, hyp):
    """Substitutions, deletions, insertions and insertion runs of 10+ (invented passages)."""
    from rapidfuzz.distance import Levenshtein

    sub = dele = ins = runs = 0
    for op in Levenshtein.opcodes(ref, hyp):
        if op.tag == "replace":
            sub += op.src_end - op.src_start
        elif op.tag == "delete":
            dele += op.src_end - op.src_start
        elif op.tag == "insert":
            ins += op.dest_end - op.dest_start
            runs += op.dest_end - op.dest_start >= 10
    return {"n": len(ref), "sub": sub, "del": dele, "ins": ins, "runs": runs}


def ghost_lines(lines, cues, pad=1.5):
    """Shown lines over nothing anyone said."""
    return sum(not any(c[0] - pad < e and s < c[1] + pad for c in cues) for s, e, _ in lines)


# ── references ─────────────────────────────────────────────────────────────


def _clock(text):
    parts = [float(p) for p in text.replace(",", ".").split(":")]
    return sum(p * 60**i for i, p in enumerate(reversed(parts)))


def parse_vtt(text):
    cues = []
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n")):
        m = re.search(r"([\d:.,]+)\s+-->\s+([\d:.,]+)[^\n]*\n(.+)", block, re.S)
        if m:
            line = " ".join(m.group(3).split("\n")).strip()
            if line and (not cues or cues[-1][2] != line):
                cues.append((_clock(m.group(1)), _clock(m.group(2)), line))
    return cues


def parse_lrc(text, duration):
    stamped = []
    for m in re.finditer(r"^\[(\d+:\d+(?:\.\d+)?)\]([^\n]*)", text, re.M):
        stamped.append((_clock(m.group(1)), m.group(2).strip()))
    ends = [t for t, _ in stamped[1:]] + [duration]
    return [(t, min(end, t + 12), line) for (t, line), end in zip(stamped, ends, strict=True) if line]


def window(cues, start, length):
    """The cues inside a clip, on the clip's clock."""
    return [(a - start, b - start, t) for a, b, t in cues if start <= (a + b) / 2 < start + length]


# ── find ───────────────────────────────────────────────────────────────────


def _ytdlp(*args, timeout=120):
    done = subprocess.run([YTDLP, *args], capture_output=True, text=True, encoding="utf-8", timeout=timeout)
    return done.stdout


def _caption_track(info, lang):
    return next((k for k in (info.get("subtitles") or {}) if k == lang or k.startswith(lang + "-")), None)


def _speech_candidates(lang, have):
    seen, picked = set(), {c.get("channel") for c in have}
    for query in QUERIES[lang]:
        url = f"https://www.youtube.com/results?search_query={urllib.parse.quote(query)}&sp={HAS_SUBTITLES}"
        for vid in _ytdlp("--flat-playlist", "--print", "id", "--playlist-end", "12", url).split():
            if vid in seen or any(c["id"] == vid for c in have):
                continue
            seen.add(vid)
            out = _ytdlp("-j", "--skip-download", "--no-warnings", "--sleep-requests", "1", "--", vid)
            info = json.loads(out) if out.strip() else {}
            track = _caption_track(info, lang)
            spoken = info.get("language")
            fits = 180 <= (info.get("duration") or 0) <= 1500 and (not spoken or spoken.split("-")[0] == lang)
            if track and fits and info.get("channel") not in picked:
                picked.add(info.get("channel"))
                have.append(
                    {
                        "id": vid,
                        "lang": lang,
                        "kind": "speech",
                        "title": info.get("title"),
                        "channel": info.get("channel"),
                        "track": track,
                    }
                )
                print(f"  {lang} {vid} [{track}] {info.get('title')}")
                if len(have) >= PER_LANGUAGE:
                    return


def _lrclib(artist, title):
    query = urllib.parse.urlencode({"artist_name": artist, "track_name": title})
    request = urllib.request.Request(f"https://lrclib.net/api/search?{query}", headers={"User-Agent": "Yume gold set"})
    return _json_with_retries(request)


def _json_with_retries(request, tries=3):
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(request, timeout=20) as reply:
                return json.load(reply)
        except OSError:  # a public service, down for a minute now and then
            if attempt == tries - 1:
                raise
            time.sleep(5 * (attempt + 1))
    return None


def _song(artist, title, lang):
    try:
        results = _lrclib(artist, title)
    except OSError as e:  # one song it cannot answer: the list has spares
        print(f"  no lyrics service for {artist} - {title}: {e}")
        return None
    timed = [r for r in results if r.get("syncedLyrics") and r.get("duration")]
    for lyrics in timed[:3]:
        out = _ytdlp("--flat-playlist", "-J", "--no-warnings", f"ytsearch5:{artist} {title} official")
        for video in json.loads(out).get("entries") or []:
            near = abs((video.get("duration") or 0) - lyrics["duration"]) <= 4
            if near and not _NOT_THE_RECORD.search(video.get("title") or ""):
                return {
                    "id": video["id"],
                    "lang": lang,
                    "kind": "song",
                    "title": f"{artist} - {title}",
                    "lrclib": lyrics["id"],
                }
    return None


def _save(picked):
    MANIFEST.write_text(json.dumps(picked, ensure_ascii=False, indent=1), encoding="utf-8")


def find():
    picked = json.loads(MANIFEST.read_text(encoding="utf-8")) if MANIFEST.exists() else []
    songs = json.loads((HERE / "songs.json").read_text(encoding="utf-8"))
    for lang in QUERIES:
        speech = [c for c in picked if c["lang"] == lang and c["kind"] == "speech"]
        if len(speech) < PER_LANGUAGE:
            _speech_candidates(lang, speech)
            picked = [c for c in picked if c["lang"] != lang or c["kind"] != "speech"] + speech
            _save(picked)
        found = [c for c in picked if c["lang"] == lang and c["kind"] == "song"]
        for artist, title in songs[lang]:
            if len(found) >= SONGS_PER_LANGUAGE[lang]:
                break
            if not any(c["title"] == f"{artist} - {title}" for c in found) and (song := _song(artist, title, lang)):
                found.append(song)
                print(f"  {lang} {song['id']} {song['title']}")
                picked = [c for c in picked if c["lang"] != lang or c["kind"] != "song"] + found
                _save(picked)


# ── fetch ──────────────────────────────────────────────────────────────────


def _reference(clip):
    path = DATA / f"{clip['id']}.cues.json"
    if path.exists():
        return
    if clip["kind"] == "song":
        artist, title = clip["title"].split(" - ", 1)
        time.sleep(8)  # asking one by one by id answers 503 here; the search carries the lyrics too
        lyrics = next(r for r in _lrclib(artist, title) if r["id"] == clip["lrclib"])
        cues = window(parse_lrc(lyrics["syncedLyrics"], lyrics["duration"]), 0, SONG_S)
    else:
        out = DATA / clip["id"]
        _ytdlp(
            "--skip-download",
            "--write-subs",
            "--sub-langs",
            clip["track"],
            "--sub-format",
            "vtt",
            "-o",
            str(out),
            "--",
            clip["id"],
        )
        vtt = next(DATA.glob(f"{clip['id']}*.vtt"), None)
        if vtt is None:
            return
        cues = window(parse_vtt(vtt.read_text(encoding="utf-8")), SPEECH_START_S, SPEECH_S)
        vtt.unlink()
    path.write_text(json.dumps(cues, ensure_ascii=False), encoding="utf-8")


def _audio(clip):
    wav = DATA / f"{clip['id']}.wav"
    if wav.exists():
        return
    source = DATA / f"{clip['id']}.source"
    _ytdlp("-f", "bestaudio/best", "--no-playlist", "-o", str(source), "--", clip["id"], timeout=600)
    cut = ["-t", str(SONG_S)] if clip["kind"] == "song" else ["-ss", str(SPEECH_START_S), "-t", str(SPEECH_S)]
    command = [
        "ffmpeg",
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(source),
        *cut,
        "-ac",
        "2",
        "-ar",
        "44100",
        "-sample_fmt",
        "s16",
        str(wav),
    ]
    if source.exists():
        subprocess.run(command, check=True, timeout=300)
        source.unlink()


def fetch():
    import numpy as np

    import _separate

    DATA.mkdir(exist_ok=True)
    for clip in json.loads(MANIFEST.read_text(encoding="utf-8")):
        try:
            _reference(clip)
        except (OSError, StopIteration) as e:
            print(f"  {clip['lang']} {clip['id']} no reference: {e!r}")
            continue
        _audio(clip)
        vocals = DATA / f"{clip['id']}.vocals.npy"
        wav = DATA / f"{clip['id']}.wav"
        if clip["kind"] == "song" and wav.exists() and not vocals.exists():
            np.save(vocals, _separate.isolate(_separate.read_stereo(str(wav))))
        print(f"  {clip['lang']} {clip['id']} {'ok' if wav.exists() else 'NO AUDIO'}")


# ── run ────────────────────────────────────────────────────────────────────


def _mix(clip):
    import _audio as audio

    return audio.load_audio(str(DATA / f"{clip['id']}.wav"))


def _heard(clip, what):
    """What Whisper hears. A speech clip has no stem: only its mix."""
    import numpy as np

    if clip["kind"] == "speech" or what == "mix":
        return _mix(clip)
    return np.load(DATA / f"{clip['id']}.vocals.npy")


class _GpuLog:
    """nvidia-smi once a second, while Whisper works."""

    def __init__(self):
        self.rows, self._stop = [], threading.Event()
        self._thread = threading.Thread(target=self._read, daemon=True)

    def _read(self):
        query = "--query-gpu=power.draw,temperature.gpu,fan.speed,memory.used,utilization.gpu"
        while not self._stop.wait(1):
            out = subprocess.run(["nvidia-smi", query, "--format=csv,noheader,nounits"], capture_output=True, text=True)
            try:
                self.rows.append([float(x) for x in out.stdout.split(",")])
            except ValueError:  # a card that reports [N/A]
                pass

    def __enter__(self):
        self._thread.start()
        return self

    def __exit__(self, *exc):
        self._stop.set()
        self._thread.join()

    def summary(self):
        if not self.rows:
            return {}
        power, temp, fan, memory, _busy = zip(*self.rows, strict=True)
        return {
            "avg_w": round(statistics.mean(power)),
            "peak_c": max(temp),
            "peak_fan": max(fan),
            "peak_mb": max(memory),
        }


def _transcribe_clip(clip, what):
    import _regions
    from _transcribe import transcribe_audio

    audio = _heard(clip, what)
    regions = _regions.plan_regions(audio)
    raws = []
    t0 = time.time()
    for i, (start, end) in enumerate(regions):
        piece = audio[int(start * SR) : int(end * SR)]
        skip = _regions.voice_start(piece, level=_regions.voice_level(audio)) if what == "trim" else 0.0
        if skip is None:
            raws.append([])
            continue
        raws.append(transcribe_audio(piece[int(skip * SR) :], clip["lang"], start + skip, is_first_region=i == 0))
    return {"regions": regions, "raw": raws, "seconds": round(time.time() - t0, 1), "audio_s": len(audio) / SR}


def run(names):
    import _state
    import _transcribe
    from faster_whisper import WhisperModel

    clips = [c for c in json.loads(MANIFEST.read_text(encoding="utf-8")) if (DATA / f"{c['id']}.wav").exists()]
    for name in names:
        model_name, beam, what = VARIANTS[name]
        out = DATA / "runs" / name
        out.mkdir(parents=True, exist_ok=True)
        todo = [c for c in clips if not (out / f"{c['id']}.json").exists()]
        # Beam and model changes reach speech too; a change in what is heard reaches songs only
        if what != "vocals":
            todo = [c for c in todo if c["kind"] == "song"]
        if not todo:
            continue
        if name.startswith("vk"):  # whisper.cpp on Vulkan, the engine for AMD and Intel GPUs
            from _whispercpp import WhisperCpp

            _state.model_name, _state.device, _state.compute_type = model_name, "vulkan", "default"
            _state.model = WhisperCpp(model_name)
        else:
            _state.model_name, _state.device, _state.compute_type = model_name, "cuda", "float16"
            _state.model = WhisperModel(model_name, device="cuda", compute_type="float16")
        _transcribe.BEAM_SIZE = beam
        print(f"[gold] {name}: {len(todo)} clips")
        with _GpuLog() as gpu:
            for clip in todo:
                result = _transcribe_clip(clip, what)
                (out / f"{clip['id']}.json").write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
        (out / "_gpu.json").write_text(json.dumps(gpu.summary()), encoding="utf-8")
        print(f"[gold] {name}: {gpu.summary()}")
        if hasattr(_state.model, "close"):
            _state.model.close()
        del _state.model
        _state.model = None


# ── score ──────────────────────────────────────────────────────────────────


def shown_lines(result, lang, keep=None):
    """The lines a viewer would see, from the raw lines of a run."""
    import _job

    job = _job.Job(("gold", lang, "", "m"), {"language": lang})
    job.language = None  # no romaji here
    job.regions = [tuple(r) for r in result["regions"]]
    job.region_state = ["pending"] * len(job.regions)
    for i, raw in enumerate(result["raw"]):
        job.add_region(i, [s for s in raw if keep is None or keep(s)])
    return sorted((s["start"], s["end"], s["text"]) for s in job.segments.values() if not s["hidden"])


def score_clip(clip, result, cues, keep=None):
    lang = clip["lang"]
    lines = shown_lines(result, lang, keep)
    counts = errors(tokens(" ".join(c[2] for c in cues), lang), tokens(" ".join(t for *_, t in lines), lang))
    return {**counts, "ghosts": ghost_lines(lines, cues), "seconds": result["seconds"], "audio_s": result["audio_s"]}


def _rate(rows):
    n = sum(r["n"] for r in rows) or 1
    return 100 * sum(r["sub"] + r["del"] + r["ins"] for r in rows) / n


def _clip_scores(clips, name):
    scores = {}
    for clip in clips:
        run_file = DATA / "runs" / name / f"{clip['id']}.json"
        cue_file = DATA / f"{clip['id']}.cues.json"
        if run_file.exists() and cue_file.exists():
            cues = json.loads(cue_file.read_text(encoding="utf-8"))
            scores[clip["id"]] = score_clip(clip, json.loads(run_file.read_text(encoding="utf-8")), cues)
    return scores


def _usable(clips, base):
    """Clips whose reference can be trusted, judged on the base run only."""
    good = []
    for clip in clips:
        row = base.get(clip["id"])
        rate = _rate([row]) / 100 if row and row["n"] >= 60 else None
        if rate is None or rate > BAD_REFERENCE or rate < TOO_CLEAN:
            print(f"  dropped {clip['lang']} {clip['kind']} {clip['id']}: base error {rate}")
        else:
            good.append(clip)
    return good


def _row(rows, base_rows):
    better = sum(_rate([r]) < _rate([b]) - 0.1 for r, b in zip(rows, base_rows, strict=True))
    worse = sum(_rate([r]) > _rate([b]) + 0.1 for r, b in zip(rows, base_rows, strict=True))
    speed = 100 * sum(r["seconds"] for r in rows) / (sum(r["audio_s"] for r in rows) or 1)
    runs = sum(r["runs"] for r in rows)
    ghosts = sum(r["ghosts"] for r in rows)
    return f"{_rate(rows):6.1f}  +{better:<2} -{worse:<2} runs {runs:<4} ghosts {ghosts:<4} {speed:5.1f}% of real time"


def score(names):
    clips = json.loads(MANIFEST.read_text(encoding="utf-8"))
    base = _clip_scores(clips, "base")
    clips = _usable(clips, base)
    for kind in ("speech", "song"):
        for lang in [*QUERIES, None]:
            group = [c for c in clips if c["kind"] == kind and lang in (None, c["lang"])]
            if not group:
                continue
            print(f"\n{kind} {lang or 'ALL'} ({len(group)} clips, {sum(base[c['id']]['n'] for c in group)} units)")
            for name in names:
                scores = _clip_scores(group, name)
                done = [c for c in group if c["id"] in scores]
                if done:
                    print(f"  {name:9}", _row([scores[c["id"]] for c in done], [base[c["id"]] for c in done]))
    for name in names:
        gpu = DATA / "runs" / name / "_gpu.json"
        if gpu.exists():
            print(name, gpu.read_text())


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
    command, names = sys.argv[1], sys.argv[2:]
    if command == "find":
        find()
    elif command == "fetch":
        fetch()
    elif command == "run":
        run(names)
    else:
        score(names or list(VARIANTS))
