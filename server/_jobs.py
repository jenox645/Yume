"""Per-video subtitle jobs: download -> transcribe regions -> translate/romanize.

The whole pipeline runs here. The extension creates a job (POST /jobs), then
polls it (GET /jobs/<id>?since=<rev>&t=<playhead>) and renders what comes back.
It used to orchestrate everything itself from a content script and an MV3
service worker — chunk scheduling, overlap de-duplication, three retry layers,
caches that vanished whenever Chrome suspended the worker.

Threads:
  * one download thread per job (I/O bound: yt-dlp / ffmpeg)
  * one transcription worker (the GPU is the bottleneck; the model is not thread-safe)
  * one LLM worker (local LLM servers effectively run one request at a time)
Both workers serve the job the user polled most recently first, starting at its
playhead, so seeking re-prioritises work immediately.

Revisions: every change to a segment bumps the job's `rev` and stamps the
segment with it; a poll with since=N returns only segments changed after N.
"""

import collections
import threading
import time
import uuid

import _audio
import _regions
import _romanize
import _separate
import _state
from _filter import (
    _width,
    clean_raw_segments,
    is_credits_line,
    is_fragment,
    is_hallucination,
    is_repeat,
    is_tail,
    split_long,
    window_hallucinations,
)
from _transcribe import detect_language, transcribe_audio
from _translate import BATCH_SIZE, TranslationError, _unshout, src_name

SR = _regions.SAMPLE_RATE
ACTIVE_S = 30  # a job polled within this window is being watched
IDLE_DROP_S = 1800  # jobs idle this long are dropped (cache keeps the work)
MAX_JOBS = 4  # audio is held in memory (~4 MB/min); bound how many tracks
HORIZON_REGIONS = 10  # don't transcribe further than ~4-5 min ahead of the playhead
PREVIEW_GRACE_S = 60  # region 0 waits this long for Whisper to finish the stream preview
PREVIEW_FIRST_S = 10  # vocal separation waits this long for a stream preview in flight
MAX_TRANSLATE_TRIES = 3
NEAR_S = 20  # lines starting this soon after the playhead are translated first...
NEAR_BATCH = 3  # ...in batches this small
READING_LANGS = {"ja", "zh", "ko"}  # romanization sent with each line to translate
LANG_SURE = 0.85  # Whisper this sure a region is another language...
LANG_MISSES = 2  # ...in this many regions: the user picked the wrong language
LANG_CHECKS = 4  # regions checked at most (a song's English chorus is one region)


MAX_SECONDS = 24 * 3600


def seconds(value):
    """A duration or playhead sent by the client: 0.0 unless a number in [0, MAX_SECONDS].

    Live streams report Infinity, and a bogus duration would plan endless
    regions. NaN fails both comparisons."""
    try:
        s = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return s if 0 <= s <= MAX_SECONDS else 0.0


def job_model(duration):
    """The Whisper model id transcripts are cached under: separated vocals
    give other transcripts than the mix, so they get their own."""
    return _state.model_name + (_separate.MODEL_SUFFIX if _separate.wanted(duration) else "")


def _hidden(text):
    return is_hallucination(text) or is_credits_line(text)


def _clean_region(raw, region):
    """A region's raw Whisper segments (the cache keeps them raw) → (segments,
    loop indexes, filler indexes), every line within the region and lines too
    long to read in one go cut into pieces.

    Lines starting after the region belong to the next one (the stream preview
    hears past region 0's cut), and Whisper stamps lines up to the end of its
    30 s window, past the audio it was given: "다음 영상에서 만나요" ("see you in
    the next video") ran to 304 s in a 283 s video."""
    raw = [s for s in raw if s["start"] < region[1] - 0.1]
    filler = window_hallucinations(raw, region)
    segs, loops = clean_raw_segments(raw)
    out, out_loops, out_filler = [], set(), set()
    for i, s in enumerate(segs):
        # The line its pieces were cut from: compared whole at a region cut
        whole = {"start": s["start"], "end": min(s["end"], region[1]), "text": s["text"]}
        s = {**s, "end": whole["end"], "whole": {**whole, "past_cut": s["end"] > region[1]}}
        if i in loops:
            out_loops.add(len(out))
        if i in filler:
            out_filler.add(len(out))
        # Loops and filler stay whole; the blacklist judges each piece, as refilter() does
        out.extend([s] if i in loops or i in filler else split_long(s))
    return out, out_loops, out_filler


def _last_line(lines):
    """The pieces of the last line among a region's visible lines (sorted by start)."""
    return [s for s in lines if s["whole"] == lines[-1]["whole"]] if lines else []


def _first_line(lines):
    """The pieces of the first line among a region's visible lines (sorted by start)."""
    return [s for s in lines if s["whole"] == lines[0]["whole"]] if lines else []


def _spread(pieces, start, end):
    """Time the pieces of one line over [start, end] by their share of its width."""
    upto, total, t = 0, sum(_width(p["text"]) for p in pieces) or 1, start
    for p in pieces:
        upto += _width(p["text"])
        p["start"], p["end"] = t, (round(start + (end - start) * upto / total, 2) if p is not pieces[-1] else end)
        t = p["end"]


def join_across_cut(last, first):
    """A region's last line and the next region's first line, each as its
    pieces (split_long): the pieces to hide. Compared whole, as Whisper wrote
    them: a long line heard by both regions is cut into pieces timed
    differently, and its last and first pieces never matched.

    A line both regions heard is kept once, over both regions' share of it:
    the copy cut into more pieces (the region that held more of it), or the
    left one when it also holds what came before. A short fragment of the next
    line is hidden."""
    a, b = last[0]["whole"], first[0]["whole"]
    if is_fragment(a, b):
        return last
    same = is_repeat(a, b)
    if not (same or is_tail(a, b)):
        return []
    keep, drop = (first, last) if same and len(first) > len(last) else (last, first)
    _spread(keep, a["start"], b["end"])
    return drop


class Job:
    def __init__(self, key, params):
        self.id = uuid.uuid4().hex
        self.key = key
        self.video_key = key[0]
        self.language = params.get("language") or None  # None = Whisper auto-detect
        self.lang_key = self.language or "auto"
        self.target = params.get("target") or None  # None = no translation
        self.romanize = bool(params.get("romanize"))
        self.url = params.get("url") or ""
        self.stream_url = params.get("stream_url") or ""
        self.title = (params.get("title") or "")[:200]
        self.duration = seconds(params.get("duration"))
        self.model = job_model(self.duration)
        self.isolate = self.model.endswith(_separate.MODEL_SUFFIX)  # separate the vocals first

        self.status = "starting"  # starting|downloading|separating|transcribing|translating|done|error
        self.error = ""
        self.audio = None
        self.mode = "full"  # "full": slice local audio; "stream": download each region
        self.regions = []  # [(start, end)]
        self.region_state = []  # pending|running|done|failed
        self.preview_started = 0.0  # stream preview of region 0 in flight since
        self.preview_hearing = 0.0  # ...and in Whisper since
        self.preview_segments = None

        self.segments = {}  # id -> segment dict
        self.next_sid = 1
        self.rev = 0
        self.playhead = seconds(params.get("playhead"))  # YouTube resumes where you left off
        self.last_poll = time.time()
        self.events = collections.deque(maxlen=60)
        self.notice = ""  # shown in the overlay: the video sounds like another language
        self.lang_checks = 0 if self.language else LANG_CHECKS  # nothing to check on auto-detect
        self.lang_misses = collections.Counter()
        self.context = []  # recent (source, translation) pairs for the LLM
        self.llm_backoff_until = 0.0
        self.lock = threading.RLock()

    # ── helpers (call with self.lock held) ──────────────────────────────────

    def event(self, msg, level="info"):
        self.events.append({"t": time.time(), "level": level, "msg": msg})
        print(f"[Yume] [{self.video_key[:24]}] {msg}")

    def _touch(self, seg):
        self.rev += 1
        seg["rev"] = self.rev

    def _needs_llm_roma(self):
        # romaji None = no deterministic romanizer produced it (Arabic, or JA/ZH
        # without pykakasi/pypinyin): only worth an LLM call if the user wants it.
        return self.romanize

    def add_region(self, idx, raw):
        """Add a region's raw Whisper segments; mark the region done."""
        region = self.regions[idx]
        dropped = 0
        cache = _state.store
        raw, loops, filler = _clean_region(raw, region)
        texts = [s["text"] for s in raw]
        cached_tr = {}
        if self.target and cache is not None and texts:
            cached_tr = cache.get_translations(self.lang_key, self.target, _model_id(), texts)
            cached_tr = {k: _unshout(v, k) for k, v in cached_tr.items()}  # cached before that fix
            if cached_tr:
                with _state.stats_lock:
                    _state.server_stats["translation_cache_hits"] += len(cached_tr)
        for i, s in enumerate(raw):
            hidden = i in loops or i in filler or _hidden(s["text"])
            dropped += hidden
            roma = _romanize.romanize(self.language, s["text"]) if self.language else ""
            if roma is None and cache is not None:
                roma = cache.get_romanization(self.lang_key, s["text"])  # LLM result from an earlier run
            seg = {
                "id": self.next_sid,
                "start": s["start"],
                "end": s["end"],
                "text": s["text"],
                "confidence": s.get("confidence", 0),
                "translation": cached_tr.get(s["text"]) if self.target else "",
                "romaji": roma,  # None = still to be produced by the LLM
                "hidden": hidden,
                "region": region,
                "whole": s["whole"],
                "orphan": False,
                "loop": i in loops,
                "filler": i in filler,  # region-level, so refilter() keeps it hidden
                "tries": 0,
            }
            self.next_sid += 1
            self.segments[seg["id"]] = seg
            self._touch(seg)
        if 0 <= idx < len(self.region_state):
            self.region_state[idx] = "done"
            dropped += self._hide_boundary_fragments(idx)
        if dropped:
            with _state.stats_lock:
                _state.server_stats["hallucinations_filtered"] += dropped
        self._update_status()

    def check_language(self, heard, prob):
        """Whisper forced into the wrong language writes garbage: an English
        news video set to Arabic came out as English lines, "ترجمة نانسي قنقر"
        and loops. Tell the user once two regions clearly sound like another
        language; stop at the first region that clearly matches."""
        if self.lang_checks >= LANG_CHECKS or heard is None or prob < LANG_SURE:
            return
        self.lang_checks += 1
        if heard == self.language:
            self.lang_checks = LANG_CHECKS
            return
        self.lang_misses[heard] += 1
        if self.lang_misses[heard] >= LANG_MISSES:
            self.lang_checks = LANG_CHECKS
            self.notice = (
                f"This video sounds like {src_name(heard)}, but Yume is set to {src_name(self.language)}. "
                f"If the subtitles look wrong, change the source language in the Yume popup."
            )
            self.event(self.notice, "warn")

    def _hide_boundary_fragments(self, idx):
        """Regions are transcribed apart, so a word spoken across a cut can end
        one region as a fragment of the next region's first line ("どのような法や
        制度を変えるのか" before "どのような法や制度を変えたくて…"), or as the whole
        line twice ("وظننت أنا أني بذلك حست غنى" at 19.5-20.4 and 20.4-24.8 s).
        Checked once both neighbours are done; returns how many lines it hid."""
        hidden = 0
        for left, right in ((idx - 1, idx), (idx, idx + 1)):
            if (
                left < 0
                or right >= len(self.regions)
                or not self.region_state[left] == self.region_state[right] == "done"
            ):
                continue
            last, first = _last_line(self._shown(left)), _first_line(self._shown(right))
            drop = join_across_cut(last, first) if last and first else []
            for s in drop:
                s["hidden"] = s["filler"] = True  # filler: refilter() keeps it hidden
            for s in (*last, *first) if drop else ():
                self._touch(s)  # hidden, or stretched over both
            hidden += len(drop)
        return hidden

    def _shown(self, idx):
        """Region idx's visible lines, by start."""
        region = self.regions[idx]
        lines = (s for s in self.segments.values() if s["region"] == region and not s["hidden"])
        return sorted(lines, key=lambda s: s["start"])

    def _update_status(self):
        if self.status in ("error", "downloading", "separating", "starting") or not self.regions:
            return
        if any(st in ("pending", "running") for st in self.region_state):
            self.status = "transcribing"
        elif self.pending_llm():
            self.status = "translating"
        else:
            if self.status != "done":
                failed = sum(st == "failed" for st in self.region_state)
                self.event(f"Finished ({failed} region(s) failed)" if failed else "Finished", "ok")
            self.status = "done"
            self.audio = None  # every region is cached; free the memory

    def pending_llm(self):
        return [
            s
            for s in self.segments.values()
            if not s["hidden"]
            and ((self.target and s["translation"] is None) or (s["romaji"] is None and self._needs_llm_roma()))
        ]

    def llm_batch(self):
        """The next LLM work for this job: ("translate" | "romanize", lines), or (None, [])."""
        # Lines at/after the playhead first, then the rest in order
        pending = sorted(self.pending_llm(), key=lambda s: (s["end"] < self.playhead, s["start"]))
        need_tr = [s for s in pending if self.target and s["translation"] is None]
        if not need_tr:
            need_roma = [s for s in pending if s["romaji"] is None]
            return ("romanize", need_roma[:BATCH_SIZE]) if need_roma else (None, [])
        # Lines due within NEAR_S, and the very first lines of a job, go in a
        # small batch so they are ready in seconds; the rest goes in full
        # batches (10 lines take ~25 s on a CPU-only llama.cpp)
        near = [s for s in need_tr if s["start"] < self.playhead + NEAR_S and s["end"] >= self.playhead]
        first = not any(s["translation"] for s in self.segments.values())
        if near or first:
            return "translate", (near or need_tr)[:NEAR_BATCH]
        return "translate", need_tr[:BATCH_SIZE]

    def set_regions(self, regions, cached):
        """Install the region plan. Regions already done in this job (a stream
        preview shown before the plan existed) are kept; regions present in
        `cached` are loaded; segments from regions the plan no longer has are
        hidden so the client drops them."""
        done_before = {r for r, st in zip(self.regions, self.region_state) if st == "done"}
        self.regions = [tuple(r) for r in regions]
        self.region_state = ["pending"] * len(self.regions)
        for seg in self.segments.values():
            if seg["region"] not in self.regions and not seg["orphan"]:
                seg["orphan"] = True
                seg["hidden"] = True
                self._touch(seg)
        for i, r in enumerate(self.regions):
            if r in done_before:
                self.region_state[i] = "done"
            elif r in cached:
                self.add_region(i, cached[r])
        self.status = "transcribing"
        self._update_status()

    def show_preview(self, raw):
        """Show the stream preview before the region plan exists: install a
        provisional single region [0, 30) so its lines are visible right away."""
        first = (0.0, _regions.FIRST_REGION_S)
        self.regions = [first]
        self.region_state = ["pending"]
        self.add_region(0, raw)

    def snapshot(self, since=0, events=False):
        """What a poll returns: the job state and the segments changed after
        `since` (a first poll skips hidden ones: the client has nothing to drop)."""
        with self.lock:
            segs = [_public(s) for s in self.segments.values() if s["rev"] > since and not (since == 0 and s["hidden"])]
            out = {
                "id": self.id,
                "status": self.status,
                "error": self.error,
                "duration": self.duration,
                "rev": self.rev,
                "notice": self.notice,
                "language": self.language,
                "target": self.target,
                "progress": self._progress(),
                "segments": segs,
            }
            if events:
                out["events"] = list(self.events)
            return out

    def _progress(self):
        visible = [s for s in self.segments.values() if not s["hidden"]]
        ended = [st in ("done", "failed") for st in self.region_state]
        return {
            "regions_total": len(self.regions),
            "regions_done": sum(ended),
            "regions_failed": self.region_state.count("failed"),
            # Seconds of the track already transcribed — lets the client tell
            # "no line here" apart from "not transcribed yet".
            "covered": [list(r) for r, e in zip(self.regions, ended) if e],
            "lines": len(visible),
            "translated": sum(s["translation"] is not None for s in visible) if self.target else len(visible),
        }


_PUBLIC_KEYS = ("id", "start", "end", "text", "confidence", "hidden")


def _public(seg):
    """A segment as clients see it ('' for a translation or romaji still to come)."""
    return {
        **{k: seg[k] for k in _PUBLIC_KEYS},
        "translation": seg["translation"] or "",
        "romaji": seg["romaji"] or "",
    }


def _model_id():
    tr = _state.jobs.translator if _state.jobs else None
    return tr.model_id() if tr else "none"


class JobManager:
    def __init__(self, translator):
        self.translator = translator
        self.jobs = {}
        self.by_key = {}
        self.lock = threading.Lock()
        self.wake = threading.Condition()
        self._stop = False
        threading.Thread(target=self._transcribe_loop, name="transcribe-worker", daemon=True).start()
        threading.Thread(target=self._llm_loop, name="llm-worker", daemon=True).start()

    # ── public API (called from Flask routes) ──────────────────────────────

    def create(self, params):
        video_key = str(params.get("video_id") or params.get("url") or "")[:200]
        if not video_key:
            raise ValueError("video_id or url is required")
        model = job_model(seconds(params.get("duration")))
        key = (video_key, params.get("language") or "auto", params.get("target") or "", model)
        with self.lock:
            existing = self.jobs.get(self.by_key.get(key, ""))
            if existing is not None and existing.status == "done" and "failed" in existing.region_state:
                # Pressing Enable again retries regions that failed (a network
                # hiccup, the GPU busy elsewhere): start over from the cache.
                existing.status = "error"
            if existing is not None and existing.status != "error":
                with existing.lock:
                    existing.romanize = existing.romanize or bool(params.get("romanize"))
                    existing.last_poll = time.time()
                self._notify()
                return existing
            if existing is not None:
                self._remove_locked(existing.id)  # failed job: retry from scratch
            self._evict_locked()
            job = Job(key, params)
            self.jobs[job.id] = job
            self.by_key[key] = job.id
        threading.Thread(target=self._prepare, args=(job,), name=f"prepare-{job.id[:6]}", daemon=True).start()
        return job

    def get(self, job_id):
        with self.lock:
            return self.jobs.get(job_id)

    def poll(self, job, playhead=None):
        with job.lock:
            job.last_poll = time.time()
            if playhead is not None:
                job.playhead = seconds(playhead)
        self._notify()

    def set_romanize(self, job, on):
        with job.lock:
            job.romanize = bool(on)
            job._update_status()
        self._notify()

    def refilter(self):
        """Re-apply hallucination filter + blacklist to every job (blacklist edited)."""
        with self.lock:
            jobs = list(self.jobs.values())
        for job in jobs:
            with job.lock:
                for seg in job.segments.values():
                    if seg["orphan"]:
                        continue
                    hidden = seg["loop"] or seg["filler"] or _hidden(seg["text"])
                    if hidden != seg["hidden"]:
                        seg["hidden"] = hidden
                        job._touch(seg)
                job._update_status()
        self._notify()

    def drop_all(self, reason):
        """Forget every job (model switch: new jobs pick up the new model).
        Pollers get 404 and recreate their job, which reloads from the cache."""
        with self.lock:
            for job in self.jobs.values():
                with job.lock:
                    job.event(reason, "warn")
            self.jobs.clear()
            self.by_key.clear()

    def stats(self):
        with self.lock:
            return {
                "jobs": len(self.jobs),
                "active": sum(time.time() - j.last_poll < ACTIVE_S for j in self.jobs.values()),
            }

    # ── internals ───────────────────────────────────────────────────────────

    def _notify(self):
        with self.wake:
            self.wake.notify_all()

    def _wait(self, seconds):
        with self.wake:
            self.wake.wait(seconds)

    def _evict_locked(self):
        now = time.time()
        for jid, job in list(self.jobs.items()):
            if now - job.last_poll > IDLE_DROP_S:
                self._remove_locked(jid)
        while len(self.jobs) >= MAX_JOBS:
            oldest = min(self.jobs.values(), key=lambda j: j.last_poll)
            self._remove_locked(oldest.id)

    def _remove_locked(self, jid):
        job = self.jobs.pop(jid, None)
        if job is not None and self.by_key.get(job.key) == jid:
            del self.by_key[job.key]

    def _alive(self, job):
        with self.lock:
            return self.jobs.get(job.id) is job

    # ── download / planning ─────────────────────────────────────────────────

    def _prepare(self, job):
        """Get the job's regions: from the cache, a full download, or the stream region by region."""
        store = _state.store
        try:
            store.put_video(job.video_key, job.url, job.title, None, None)
            video = store.get_video(job.video_key) or {}
            cached = store.get_transcripts(job.video_key, job.lang_key, job.model)
            if not self._from_cache(job, video, cached):
                self._download(job, video, cached)
        except Exception as e:  # never let a job thread die silently
            self._fail(job, f"{type(e).__name__}: {e}")
        self._notify()

    @staticmethod
    def _fail(job, message):
        with job.lock:
            job.status = "error"
            job.error = message
            job.event(message, "error")

    def _from_cache(self, job, video, cached):
        """Everything cached for this language + model: no download at all."""
        planned = video.get("regions")
        if not planned or not all(tuple(r) in cached for r in planned):
            return False
        with job.lock:
            job.duration = video.get("duration") or job.duration
            job.event(f"Loaded {len(planned)} regions from cache — no download needed", "ok")
            job.set_regions(planned, cached)
        return True

    def _download(self, job, video, cached):
        with job.lock:
            job.status = "downloading"
            job.event("Downloading audio...")
        # The preview is for a video watched from the start, not one resumed at 40:00
        preview = job.playhead < _regions.FIRST_REGION_S
        if preview and not job.stream_url and not any(r[0] == 0.0 for r in cached):
            with job.lock:
                job.preview_started = time.time()
            threading.Thread(target=self._preview, args=(job,), name=f"preview-{job.id[:6]}", daemon=True).start()

        if job.stream_url:
            path, err = _audio.download_direct(job.stream_url)
        else:
            path, err = _audio.download_full_audio(job.url, stereo=job.isolate)
        if not self._alive(job):
            _audio.remove_temp(path)
        elif path:
            self._use_audio(job, path, video, cached)
        elif job.duration > 0 and not job.stream_url and _audio.get_stream_url(job.url):
            # Full download failed but the stream itself is reachable:
            # transcribe each region straight from the stream (slower).
            regions = _regions.fixed_regions(job.duration)
            with job.lock:
                job.mode = "stream"
                job.event(f"Full download failed ({err}) — streaming each region instead (slower)", "warn")
                job.set_regions(regions, cached)
                self._adopt_preview(job)
        else:
            self._fail(job, err or "Audio download failed")

    def _use_audio(self, job, path, video, cached):
        """Load the download (vocals only when isolating), plan its regions, start the job."""
        try:
            audio = _audio.load_audio(path)
            if job.isolate:
                vocals = self._isolate(job, path)
                if vocals is not None:
                    audio = vocals
                else:  # transcripts of the mix are cached as the mix's
                    job.model = _state.model_name
                    cached = _state.store.get_transcripts(job.video_key, job.lang_key, job.model)
        finally:
            _audio.remove_temp(path)
        with _state.stats_lock:
            _state.server_stats["downloads_completed"] += 1
        duration = len(audio) / SR
        planned = video.get("regions")
        # The same video again: keep its regions, so cached transcripts still line up
        if planned and abs((video.get("duration") or 0) - duration) < 1.0:
            regions = planned
        else:
            regions = _regions.plan_regions(audio)
        _state.store.put_video(job.video_key, job.url, job.title, duration, [list(r) for r in regions])
        with job.lock:
            job.audio = audio
            job.duration = duration
            job.event(f"Audio ready: {duration:.0f}s, {len(regions)} regions", "ok")
            job.set_regions(regions, cached)
            self._adopt_preview(job)

    def _isolate(self, job, path):
        """The song's vocals (16 kHz mono), or None to transcribe the mix."""
        self._wait_for_preview(job)
        if _audio.stream_category(job.url) in _separate.SPOKEN_CATEGORIES:
            with job.lock:
                job.isolate = False
            return None
        with job.lock:
            job.status = "separating"
            job.event("Separating the vocals from the music...")
        self._notify()
        t0 = time.time()
        try:
            stereo = _separate.read_stereo(path)
            if stereo is None:
                raise RuntimeError("the download is not 44.1 kHz stereo")
            vocals = _separate.isolate(stereo)
        except Exception as e:  # a separation problem must never cost the subtitles
            with job.lock:
                job.isolate = False
                job.event(f"Vocal isolation failed ({type(e).__name__}: {e}) — transcribing the mix", "warn")
            return None
        with job.lock:
            job.event(f"Vocals separated in {time.time() - t0:.0f}s", "ok")
        return vocals

    def _wait_for_preview(self, job):
        """Whisper and Demucs slow each other down on one GPU: the preview's 30 s
        took 31.6 s instead of 1-4 s, for a viewer with nothing on screen yet."""
        deadline = time.time() + PREVIEW_FIRST_S
        while job.preview_started and time.time() < deadline and self._alive(job):
            time.sleep(0.1)

    def _preview(self, job):
        """Transcribe the first 30 s straight from the stream while the full
        download runs: first subtitles in ~max(download, whisper) instead of
        download + whisper."""
        path = None
        try:
            path = _audio.download_audio_segment(job.url, 0, _regions.FIRST_REGION_S)
            with job.lock:
                # After a 403 the fetch can outlast the full download: the worker took region 0
                hear = path and self._alive(job) and (not job.region_state or job.region_state[0] == "pending")
                if hear:
                    job.preview_hearing = time.time()
            if hear:
                raw = transcribe_audio(_audio.load_audio(path), job.language, 0.0, is_first_region=True)
                with job.lock:
                    if job.status == "error":  # the download failed meanwhile (a live stream)
                        return
                    job.preview_segments = raw
                    if job.regions and job.status not in ("downloading", "separating"):
                        self._adopt_preview(job)
                    elif not job.regions:
                        # preview_segments kept: cached and adopted under the plan,
                        # whose region 0 may end before 30 s
                        job.show_preview(raw)
                        job.event(
                            f"First 30 s ready from the stream ({len(raw)} lines) — full download continues", "ok"
                        )
        except Exception as e:
            print(f"[Yume] Stream preview failed: {e}")
        finally:
            _audio.remove_temp(path)
            with job.lock:
                job.preview_started = job.preview_hearing = 0.0
            self._notify()

    def _adopt_preview(self, job):
        """Use the preview as region 0 if it covers the plan's region 0 (which
        ends at a quiet point at most FIRST_REGION_S in)."""
        raw = job.preview_segments
        if raw is None or not job.regions:
            return
        job.preview_segments = None
        first = job.regions[0]
        if first[0] != 0.0 or first[1] > _regions.FIRST_REGION_S:
            return
        if job.region_state[0] == "pending":
            job.add_region(0, raw)
            # Before the plan existed the preview had nothing to show; it does now
            job.event(f"Region 1/{len(job.regions)}: {len(raw)} lines (stream preview)")
        elif job.region_state[0] != "done" or first != (0.0, _regions.FIRST_REGION_S):
            return  # the worker is transcribing region 0 itself
        # else: shown before the plan, which kept that region
        _state.store.put_transcript(job.video_key, job.lang_key, job.model, *first, raw)

    # ── transcription worker ────────────────────────────────────────────────

    def _pick_region(self):
        """(job, idx) to transcribe next, or (None, None)."""
        now = time.time()
        with self.lock:
            jobs = sorted(self.jobs.values(), key=lambda j: j.last_poll, reverse=True)
        for job in jobs:
            with job.lock:
                if job.status != "transcribing" or now - job.last_poll > ACTIVE_S:
                    continue
                if job.mode == "full" and job.audio is None:
                    continue
                p = _regions.region_index_at(job.regions, job.playhead)
                order = list(range(p, min(len(job.regions), p + HORIZON_REGIONS + 1))) + list(range(p - 1, -1, -1))
                for idx in order:
                    if job.region_state[idx] != "pending":
                        continue
                    if idx == 0 and job.preview_hearing and now - job.preview_hearing < PREVIEW_GRACE_S:
                        continue  # the stream preview is producing this region
                    job.region_state[idx] = "running"
                    return job, idx
        return None, None

    def _transcribe_loop(self):
        while not self._stop:
            if _state.model is None:
                self._wait(1.0)
                continue
            job, idx = self._pick_region()
            if job is None:
                self._wait(1.0)
                continue
            start, end = job.regions[idx]
            path = None
            try:
                if job.mode == "full":
                    audio = job.audio[int(start * SR) : int(end * SR)]
                else:
                    path = _audio.download_audio_segment(job.url, start, end - start)
                    if not path:
                        raise RuntimeError("stream segment download failed")
                    audio = _audio.load_audio(path)
                raw = transcribe_audio(audio, job.language, start, is_first_region=(idx == 0))
                if job.lang_checks < LANG_CHECKS:
                    heard, prob = detect_language(audio)
                    with job.lock:
                        job.check_language(heard, prob)
                _state.store.put_transcript(job.video_key, job.lang_key, job.model, start, end, raw)
                with job.lock:
                    job.event(f"Region {idx + 1}/{len(job.regions)} [{start:.0f}-{end:.0f}s]: {len(raw)} lines")
                    job.add_region(idx, raw)  # may log "Finished" — keep it after the region line
            except Exception as e:
                with _state.stats_lock:
                    _state.server_stats["errors"] += 1
                with job.lock:
                    job.region_state[idx] = "failed"
                    job.event(f"Region {idx + 1} failed: {e}", "error")
                    job._update_status()
            finally:
                _audio.remove_temp(path)
            self._notify()

    # ── LLM worker ──────────────────────────────────────────────────────────

    def _pick_llm_batch(self):
        now = time.time()
        with self.lock:
            jobs = sorted(self.jobs.values(), key=lambda j: j.last_poll, reverse=True)
        for job in jobs:
            with job.lock:
                if now - job.last_poll > ACTIVE_S or now < job.llm_backoff_until:
                    continue
                kind, batch = job.llm_batch()
                if batch:
                    return job, kind, batch
        return None, None, None

    def _llm_loop(self):
        while not self._stop:
            job, kind, batch = self._pick_llm_batch()
            if job is None:
                self._wait(1.0)
                continue
            try:
                results = self._llm_call(job, kind, batch)
            except Exception as e:  # anything else would end this thread: every job stuck "translating"
                self._llm_failed(job, kind, batch, e)
            else:
                self._llm_done(job, kind, batch, results)
            self._notify()

    def _llm_call(self, job, kind, batch):
        texts = [s["text"] for s in batch]
        if kind != "translate":
            return self.translator.romanize_batch(texts, job.language)
        with job.lock:
            context, title = list(job.context), job.title
            # Names written in kanji / hanzi / hangul: the reading tells the model how they sound
            readings = [s["romaji"] or "" for s in batch] if job.language in READING_LANGS else None
        return self.translator.translate_batch(texts, job.language, job.target, title, context, readings)

    @staticmethod
    def _llm_failed(job, kind, batch, e):
        unreachable = isinstance(e, TranslationError) and "unreachable" in str(e)
        with job.lock:
            # Server down: wait for it, don't burn the lines' retries
            job.llm_backoff_until = time.time() + (30 if unreachable else 10)
            what = "Translation server error" if isinstance(e, TranslationError) else "Translation failed"
            job.event(f"{what}: {e}", "error")
            for s in batch if not unreachable else ():
                s["tries"] += 1
                if s["tries"] >= MAX_TRANSLATE_TRIES:
                    # Give up on this line: show the original only
                    s["translation" if kind == "translate" else "romaji"] = ""
                    job._touch(s)
            job._update_status()

    def _llm_done(self, job, kind, batch, results):
        store = _state.store
        with job.lock:
            for s, r in zip(batch, results):
                if kind == "translate":
                    s["translation"] = r
                    if r:
                        store.put_translation(job.lang_key, job.target, self.translator.model_id(), s["text"], r)
                        job.context = (job.context + [(s["text"], r)])[-3:]
                else:
                    s["romaji"] = r
                    if r:
                        store.put_romanization(job.lang_key, s["text"], r)
                job._touch(s)
            if kind == "translate":
                with _state.stats_lock:
                    _state.server_stats["lines_translated"] += len(batch)
            job._update_status()


# ── Export ────────────────────────────────────────────────────────────────────


def _ts(sec, sep):
    ms = max(0, round(sec * 1000))
    return f"{ms // 3600000:02d}:{ms % 3600000 // 60000:02d}:{ms % 60000 // 1000:02d}{sep}{ms % 1000:03d}"


def export_subtitles(segments, fmt="srt"):
    """segments: dicts with start, end, text, translation, romaji, hidden."""
    cues = sorted((s for s in segments if not s.get("hidden")), key=lambda s: s["start"])
    lines = ["WEBVTT", ""] if fmt == "vtt" else []
    sep = "." if fmt == "vtt" else ","
    for i, s in enumerate(cues, 1):
        parts = [s["text"]]
        if s.get("translation") and s["translation"] != s["text"]:
            parts.append(s["translation"])
        if s.get("romaji"):
            parts.append(s["romaji"])
        if fmt != "vtt":
            lines.append(str(i))
        lines.append(f"{_ts(s['start'], sep)} --> {_ts(s['end'], sep)}")
        lines.extend(parts)
        lines.append("")
    return "\n".join(lines), len(cues)


def _library_lines(store, video_key, language, model):
    """A cached video's lines, cleaned as a live job cleans them: (lines, hidden indexes)."""
    cached = store.get_transcripts(video_key, language, model)
    # The video's current plan: a region of an older plan would duplicate lines
    planned = [tuple(r) for r in (store.get_video(video_key) or {}).get("regions") or []]
    raw, hide, last = [], set(), []
    for region in [r for r in planned or sorted(cached) if r in cached]:
        segs, loops, filler = _clean_region(cached[region], region)
        shown = [s for i, s in enumerate(segs) if i not in loops | filler and not _hidden(s["text"])]
        hide |= {len(raw) + i for i in loops | filler}
        raw.extend(segs)
        drop = _cut_drop(last, shown)  # as a live job does across each cut
        hide |= {i for i, s in enumerate(raw) if id(s) in drop}
        last = [s for s in shown if id(s) not in drop]
    return raw, hide


def _cut_drop(last, shown):
    """ids of the lines to hide at the cut between a region's shown lines and the next region's."""
    if not (last and shown):
        return set()
    return {id(s) for s in join_across_cut(_last_line(last), _first_line(shown))}


def library_export(video_key, language, model, target, fmt="srt"):
    """Build subtitles for a cached video without a live job."""
    store = _state.store
    raw, hide = _library_lines(store, video_key, language, model)
    # Any translation model: the library outlives model switches
    translations = store.get_translations(language, target, None, [s["text"] for s in raw]) if target else {}
    lang = None if language == "auto" else language
    segments = []
    for i, s in enumerate(raw):
        roma = _romanize.romanize(lang, s["text"]) if lang else ""
        if roma is None:
            roma = store.get_romanization(language, s["text"]) or ""
        hidden = i in hide or _hidden(s["text"])
        segments.append({**s, "translation": translations.get(s["text"], ""), "romaji": roma, "hidden": hidden})
    return export_subtitles(segments, fmt)
