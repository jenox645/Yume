"""One video's subtitle job: its regions, lines and revisions, and what the workers need next.

Revisions: every change to a segment bumps the job's `rev` and stamps the
segment with it; a poll with since=N returns only segments changed after N.

Call the methods with `lock` held, except snapshot(), which takes it.
"""

import collections
import threading
import time
import uuid

import _regions
import _romanize
import _separate
import _state
from _lines import clean_region, first_line, is_unwanted, join_across_cut, last_line
from _translate import BATCH_SIZE, _unshout, src_name

HORIZON_REGIONS = 10  # don't transcribe further than ~4-5 min ahead of the playhead
PREVIEW_GRACE_S = 60  # region 0 waits this long for Whisper to finish the stream preview
NEAR_S = 20  # lines starting this soon after the playhead are translated first...
NEAR_BATCH = 3  # ...in batches this small
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

    def _cached_translations(self, texts):
        cache = _state.store
        if not (self.target and cache is not None and texts):
            return {}
        found = cache.get_translations(self.lang_key, self.target, _model_id(), texts)
        if found:
            with _state.stats_lock:
                _state.server_stats["translation_cache_hits"] += len(found)
        return {k: _unshout(v, k) for k, v in found.items()}  # cached before that fix

    def _romaji(self, text):
        """The line's romaji; None = the LLM must produce it, if the user wants it
        (Arabic, or JA/ZH without pykakasi/pypinyin)."""
        roma = _romanize.romanize(self.language, text) if self.language else ""
        if roma is None and _state.store is not None:
            roma = _state.store.get_romanization(self.lang_key, text)  # LLM result from an earlier run
        return roma

    def add_region(self, idx, raw):
        """Add a region's raw Whisper segments; mark the region done."""
        region = self.regions[idx]
        dropped = 0
        raw, loops, filler = clean_region(raw, region)
        cached_tr = self._cached_translations([s["text"] for s in raw])
        for i, s in enumerate(raw):
            hidden = i in loops or i in filler or is_unwanted(s["text"])
            dropped += hidden
            seg = {
                "id": self.next_sid,
                "start": s["start"],
                "end": s["end"],
                "text": s["text"],
                "confidence": s.get("confidence", 0),
                "translation": cached_tr.get(s["text"]) if self.target else "",
                "romaji": self._romaji(s["text"]),
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
        language; stop at the first region that clearly matches, which also
        takes the hint back: a song that opens with an English chorus."""
        if self.lang_checks >= LANG_CHECKS or heard is None or prob < LANG_SURE:
            return
        self.lang_checks += 1
        if heard == self.language:
            self.lang_checks = LANG_CHECKS
            self.notice = ""
            return
        self.lang_misses[heard] += 1
        if self.lang_misses[heard] >= LANG_MISSES and not self.notice:
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
            last, first = last_line(self._shown(left)), first_line(self._shown(right))
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
            if not s["hidden"] and (s["translation"] is None or (s["romaji"] is None and self.romanize))
        ]

    def llm_batch(self):
        """The next LLM work for this job: ("translate" | "romanize", lines), or (None, [])."""
        # Lines at/after the playhead first, then the rest in order
        pending = sorted(self.pending_llm(), key=lambda s: (s["end"] < self.playhead, s["start"]))
        need_tr = [s for s in pending if s["translation"] is None]
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

    def next_region(self, now):
        """The region to transcribe next, marked running, or None."""
        p = _regions.region_index_at(self.regions, self.playhead)
        order = list(range(p, min(len(self.regions), p + HORIZON_REGIONS + 1))) + list(range(p - 1, -1, -1))
        for idx in order:
            if self.region_state[idx] != "pending":
                continue
            if idx == 0 and self.preview_hearing and now - self.preview_hearing < PREVIEW_GRACE_S:
                continue  # the stream preview is producing this region
            self.region_state[idx] = "running"
            return idx
        return None

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
