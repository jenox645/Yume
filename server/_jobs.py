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
import _state
from _filter import clean_raw_segments, is_credits_line, is_hallucination
from _transcribe import transcribe_audio
from _translate import BATCH_SIZE, TranslationError, _unshout

SR = _regions.SAMPLE_RATE
ACTIVE_S = 30  # a job polled within this window is being watched
IDLE_DROP_S = 1800  # jobs idle this long are dropped (cache keeps the work)
MAX_JOBS = 4  # audio is held in memory (~4 MB/min); bound how many tracks
HORIZON_REGIONS = 10  # don't transcribe further than ~4-5 min ahead of the playhead
PREVIEW_GRACE_S = 60  # region 0 waits this long for an in-flight stream preview
MAX_TRANSLATE_TRIES = 3
NEAR_S = 20  # lines starting this soon after the playhead are translated first...
NEAR_BATCH = 3  # ...in batches this small


def _hidden(text):
    return is_hallucination(text) or is_credits_line(text)


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
        try:
            duration = float(params.get("duration") or 0)
        except (TypeError, ValueError):
            duration = 0.0
        self.duration = duration if 0 <= duration < 7 * 24 * 3600 else 0.0  # NaN/inf fail both tests
        self.model = _state.model_name

        self.status = "starting"  # starting|downloading|transcribing|translating|done|error
        self.error = ""
        self.audio = None
        self.mode = "full"  # "full": slice local audio; "stream": download each region
        self.regions = []  # [(start, end)]
        self.region_state = []  # pending|running|done|failed
        self.preview_started = 0.0  # stream preview of region 0 in flight since
        self.preview_segments = None

        self.segments = {}  # id -> segment dict
        self.next_sid = 1
        self.rev = 0
        self.playhead = 0.0
        self.last_poll = time.time()
        self.events = collections.deque(maxlen=60)
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
        # The cache keeps Whisper's raw output; loops are cleaned here, on the way in
        raw, loops = clean_raw_segments(raw)
        texts = [s["text"] for s in raw]
        cached_tr = {}
        if self.target and cache is not None and texts:
            cached_tr = cache.get_translations(self.lang_key, self.target, _model_id(), texts)
            cached_tr = {k: _unshout(v, k) for k, v in cached_tr.items()}  # cached before that fix
            if cached_tr:
                with _state.stats_lock:
                    _state.server_stats["translation_cache_hits"] += len(cached_tr)
        for i, s in enumerate(raw):
            hidden = i in loops or _hidden(s["text"])
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
                "orphan": False,
                "loop": i in loops,
                "tries": 0,
            }
            self.next_sid += 1
            self.segments[seg["id"]] = seg
            self._touch(seg)
        if 0 <= idx < len(self.region_state):
            self.region_state[idx] = "done"
        if dropped:
            with _state.stats_lock:
                _state.server_stats["hallucinations_filtered"] += dropped
        self._update_status()

    def _update_status(self):
        if self.status in ("error", "downloading", "starting") or not self.regions:
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
        with self.lock:
            segs = []
            for s in self.segments.values():
                if s["rev"] <= since or (since == 0 and s["hidden"]):
                    continue
                segs.append(
                    {
                        "id": s["id"],
                        "start": s["start"],
                        "end": s["end"],
                        "text": s["text"],
                        "translation": s["translation"] or "",
                        "romaji": s["romaji"] or "",
                        "confidence": s["confidence"],
                        "hidden": s["hidden"],
                    }
                )
            visible = [s for s in self.segments.values() if not s["hidden"]]
            done_regions = sum(st in ("done", "failed") for st in self.region_state)
            failed_regions = sum(st == "failed" for st in self.region_state)
            out = {
                "id": self.id,
                "status": self.status,
                "error": self.error,
                "duration": self.duration,
                "rev": self.rev,
                "language": self.language,
                "target": self.target,
                "progress": {
                    "regions_total": len(self.regions),
                    "regions_done": done_regions,
                    "regions_failed": failed_regions,
                    # Seconds of the track already transcribed — lets the client tell
                    # "no line here" apart from "not transcribed yet".
                    "covered": [list(r) for r, st in zip(self.regions, self.region_state) if st in ("done", "failed")],
                    "lines": len(visible),
                    "translated": sum(1 for s in visible if s["translation"] is not None)
                    if self.target
                    else len(visible),
                },
                "segments": segs,
            }
            if events:
                out["events"] = list(self.events)
            return out


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
        key = (video_key, params.get("language") or "auto", params.get("target") or "", _state.model_name)
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
                job.playhead = max(0.0, float(playhead))
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
                    hidden = seg["loop"] or _hidden(seg["text"])
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
        store = _state.store
        try:
            store.put_video(job.video_key, job.url, job.title, None, None)
            video = store.get_video(job.video_key) or {}
            cached = store.get_transcripts(job.video_key, job.lang_key, job.model)
            planned = video.get("regions")

            # Everything cached for this language + model: no download at all.
            if planned and all(tuple(r) in cached for r in planned):
                with job.lock:
                    job.duration = video.get("duration") or job.duration
                    job.event(f"Loaded {len(planned)} regions from cache — no download needed", "ok")
                    job.set_regions(planned, cached)
                self._notify()
                return

            with job.lock:
                job.status = "downloading"
                job.event("Downloading audio...")
            if not job.stream_url and (0.0, _regions.FIRST_REGION_S) not in cached:
                threading.Thread(target=self._preview, args=(job,), daemon=True).start()

            if job.stream_url:
                path, err = _audio.download_direct(job.stream_url)
            else:
                path, err = _audio.download_full_audio(job.url)
            if not self._alive(job):
                _audio.remove_temp(path)
                return
            if path:
                try:
                    audio = _audio.load_audio(path)
                finally:
                    _audio.remove_temp(path)
                with _state.stats_lock:
                    _state.server_stats["downloads_completed"] += 1
                duration = len(audio) / SR
                if planned and abs((video.get("duration") or 0) - duration) < 1.0:
                    regions = planned
                else:
                    regions = _regions.plan_regions(audio)
                store.put_video(job.video_key, job.url, job.title, duration, [list(r) for r in regions])
                with job.lock:
                    job.audio = audio
                    job.duration = duration
                    job.event(f"Audio ready: {duration:.0f}s, {len(regions)} regions", "ok")
                    job.set_regions(regions, cached)
                    self._adopt_preview(job)
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
                with job.lock:
                    job.status = "error"
                    job.error = err or "Audio download failed"
                    job.event(job.error, "error")
        except Exception as e:  # never let a job thread die silently
            with job.lock:
                job.status = "error"
                job.error = f"{type(e).__name__}: {e}"
                job.event(job.error, "error")
        self._notify()

    def _preview(self, job):
        """Transcribe the first 30 s straight from the stream while the full
        download runs: first subtitles in ~max(download, whisper) instead of
        download + whisper."""
        with job.lock:
            job.preview_started = time.time()
        path = None
        try:
            path = _audio.download_audio_segment(job.url, 0, _regions.FIRST_REGION_S)
            if path and self._alive(job):
                raw = transcribe_audio(_audio.load_audio(path), job.language, 0.0, is_first_region=True)
                with job.lock:
                    job.preview_segments = raw
                    if job.regions and job.status != "downloading":
                        self._adopt_preview(job)
                    elif not job.regions:
                        _state.store.put_transcript(
                            job.video_key, job.lang_key, job.model, 0.0, _regions.FIRST_REGION_S, raw
                        )
                        job.preview_segments = None
                        job.show_preview(raw)
                        job.event(
                            f"First 30 s ready from the stream ({len(raw)} lines) — full download continues", "ok"
                        )
        except Exception as e:
            print(f"[Yume] Stream preview failed: {e}")
        finally:
            _audio.remove_temp(path)
            with job.lock:
                job.preview_started = 0.0
            self._notify()

    def _adopt_preview(self, job):
        """Use the preview as region 0 if the plan has a matching region 0."""
        raw = job.preview_segments
        if raw is None or not job.regions:
            return
        job.preview_segments = None
        first = (0.0, _regions.FIRST_REGION_S)
        if job.regions[0] == first and job.region_state[0] == "pending":
            _state.store.put_transcript(job.video_key, job.lang_key, job.model, *first, raw)
            job.add_region(0, raw)
            # Before the plan existed the preview had nothing to show; it does now
            job.event(f"Region 1/{len(job.regions)}: {len(raw)} lines (stream preview)")

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
                    if idx == 0 and job.preview_started and now - job.preview_started < PREVIEW_GRACE_S:
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
                pending = job.pending_llm()
                if not pending:
                    continue
                # Lines at/after the playhead first, then the rest in order
                pending.sort(key=lambda s: (s["end"] < job.playhead, s["start"]))
                need_tr = [s for s in pending if job.target and s["translation"] is None]
                if need_tr:
                    # Lines due within NEAR_S, and the very first lines of a job, go
                    # in a small batch so they are ready in seconds; the rest goes in
                    # full batches (10 lines take ~25 s on a CPU-only llama.cpp)
                    near = [s for s in need_tr if s["start"] < job.playhead + NEAR_S and s["end"] >= job.playhead]
                    first = not any(s["translation"] for s in job.segments.values())
                    if near or first:
                        return job, "translate", (near or need_tr)[:NEAR_BATCH]
                    return job, "translate", need_tr[:BATCH_SIZE]
                need_roma = [s for s in pending if s["romaji"] is None][:BATCH_SIZE]
                if need_roma:
                    return job, "romanize", need_roma
        return None, None, None

    def _llm_loop(self):
        while not self._stop:
            job, kind, batch = self._pick_llm_batch()
            if job is None:
                self._wait(1.0)
                continue
            texts = [s["text"] for s in batch]
            try:
                if kind == "translate":
                    with job.lock:
                        context, title = list(job.context), job.title
                    results = self.translator.translate_batch(texts, job.language, job.target, title, context)
                else:
                    results = self.translator.romanize_batch(texts, job.language)
            except TranslationError as e:
                unreachable = "unreachable" in str(e)
                with job.lock:
                    # Server down: wait for it, don't burn the lines' retries
                    job.llm_backoff_until = time.time() + (30 if unreachable else 10)
                    job.event(f"Translation server error: {e}", "error")
                    for s in batch:
                        if unreachable:
                            continue
                        s["tries"] += 1
                        if s["tries"] >= MAX_TRANSLATE_TRIES:
                            # Give up on this line: show the original only
                            if kind == "translate":
                                s["translation"] = ""
                            else:
                                s["romaji"] = ""
                            job._touch(s)
                    job._update_status()
                self._notify()
                continue
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
            self._notify()


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


def library_export(video_key, language, model, target, fmt="srt"):
    """Build subtitles for a cached video without a live job."""
    store = _state.store
    raw, loops = [], set()
    for _region, segs in sorted(store.get_transcripts(video_key, language, model).items()):
        segs, hide = clean_raw_segments(segs)
        loops |= {len(raw) + i for i in hide}
        raw.extend(segs)
    # Any translation model: the library outlives model switches
    translations = store.get_translations(language, target, None, [s["text"] for s in raw]) if target else {}
    lang = None if language == "auto" else language
    segments = []
    for i, s in enumerate(raw):
        roma = _romanize.romanize(lang, s["text"]) if lang else ""
        if roma is None:
            roma = store.get_romanization(language, s["text"]) or ""
        hidden = i in loops or _hidden(s["text"])
        segments.append({**s, "translation": translations.get(s["text"], ""), "romaji": roma, "hidden": hidden})
    return export_subtitles(segments, fmt)
