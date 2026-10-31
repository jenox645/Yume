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

A job's own state lives in _job.py, its lines in _lines.py, exports in _export.py.
"""

import threading
import time

import _audio
import _download
import _regions
import _separate
import _state
import _stream
from _job import LANG_CHECKS, Job, job_model, seconds
from _lines import is_unwanted
from _transcribe import detect_language, transcribe_audio
from _translate import TranslationError

SR = _regions.SAMPLE_RATE
ACTIVE_S = 30  # a job polled within this window is being watched
IDLE_DROP_S = 1800  # jobs idle this long are dropped (cache keeps the work)
MAX_JOBS = 4  # audio is held in memory (~4 MB/min); bound how many tracks
PREVIEW_FIRST_S = 10  # vocal separation waits this long for a stream preview in flight
MAX_TRANSLATE_TRIES = 3
READING_LANGS = {"ja", "zh", "ko"}  # romanization sent with each line to translate


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
            if existing is not None and self._reusable(existing):
                with existing.lock:
                    existing.romanize = existing.romanize or bool(params.get("romanize"))
                    existing.last_poll = time.time()
                self._notify()
                return existing
            if existing is not None:
                self._remove_locked(existing.id)
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
                    hidden = seg["loop"] or seg["filler"] or is_unwanted(seg["text"])
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

    @staticmethod
    def _reusable(job):
        """Not a failed job. Nor a finished one with failed regions: pressing Enable
        again retries them (a network hiccup, the GPU busy elsewhere), from the cache."""
        return job.status != "error" and not (job.status == "done" and "failed" in job.region_state)

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

    def _by_recent_poll(self):
        with self.lock:
            return sorted(self.jobs.values(), key=lambda j: j.last_poll, reverse=True)

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
        self._start_preview(job, cached)
        if job.stream_url:
            path, err = _download.download_direct(job.stream_url)
        else:
            path, err = _download.download_full_audio(job.url, stereo=job.isolate)
        if not self._alive(job):
            _audio.remove_temp(path)
        elif path:
            self._use_audio(job, path, video, cached)
        elif job.duration > 0 and not job.stream_url and _stream.get_stream_url(job.url):
            self._stream_regions(job, err, cached)
        else:
            self._fail(job, err or "Audio download failed")

    def _start_preview(self, job, cached):
        # For a video watched from the start, not one resumed at 40:00
        if job.playhead >= _regions.FIRST_REGION_S or job.stream_url or any(r[0] == 0.0 for r in cached):
            return
        with job.lock:
            job.preview_started = time.time()
        threading.Thread(target=self._preview, args=(job,), name=f"preview-{job.id[:6]}", daemon=True).start()

    def _stream_regions(self, job, err, cached):
        """Full download failed but the stream itself is reachable:
        transcribe each region straight from the stream (slower)."""
        regions = _regions.fixed_regions(job.duration)
        with job.lock:
            job.mode = "stream"
            job.event(f"Full download failed ({err}) — streaming each region instead (slower)", "warn")
            job.set_regions(regions, cached)
            self._adopt_preview(job)

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
        if _stream.stream_category(job.url) in _separate.SPOKEN_CATEGORIES:
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
            path = _stream.download_audio_segment(job.url, 0, _regions.FIRST_REGION_S)
            with job.lock:
                # After a 403 the fetch can outlast the full download: the worker took region 0
                hear = path and self._alive(job) and (not job.region_state or job.region_state[0] == "pending")
                if hear:
                    job.preview_hearing = time.time()
            if hear:
                self._preview_heard(
                    job, transcribe_audio(_audio.load_audio(path), job.language, 0.0, is_first_region=True)
                )
        except Exception as e:
            print(f"[Yume] Stream preview failed: {e}")
        finally:
            _audio.remove_temp(path)
            with job.lock:
                job.preview_started = job.preview_hearing = 0.0
            self._notify()

    def _preview_heard(self, job, raw):
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
                job.event(f"First 30 s ready from the stream ({len(raw)} lines) — full download continues", "ok")

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
        for job in self._by_recent_poll():
            with job.lock:
                if job.status != "transcribing" or now - job.last_poll > ACTIVE_S:
                    continue
                if job.mode == "full" and job.audio is None:
                    continue
                idx = job.next_region(now)
                if idx is not None:
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
            self._transcribe_region(job, idx)
            self._notify()

    def _transcribe_region(self, job, idx):
        start, end = job.regions[idx]
        path = None
        try:
            if job.mode == "full":
                audio = job.audio[int(start * SR) : int(end * SR)]
            else:
                path = _stream.download_audio_segment(job.url, start, end - start)
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

    # ── LLM worker ──────────────────────────────────────────────────────────

    def _pick_llm_batch(self):
        now = time.time()
        for job in self._by_recent_poll():
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
