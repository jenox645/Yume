"""SRT / WebVTT export, of a live job's lines or of a cached video without a job."""

import _romanize
import _state
from _lines import clean_region, cut_drop, is_unwanted


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


def _cached_regions(store, video_key, cached):
    """The cached regions to show, in order: the video's current plan's, since a
    region of an older plan would duplicate lines."""
    planned = [tuple(r) for r in (store.get_video(video_key) or {}).get("regions") or []]
    return [r for r in planned or sorted(cached) if r in cached]


def _library_lines(store, video_key, language, model):
    """A cached video's lines, cleaned as a live job cleans them: (lines, ids of the hidden ones)."""
    cached = store.get_transcripts(video_key, language, model)
    lines, hide, last = [], set(), []
    for region in _cached_regions(store, video_key, cached):
        segs, loops, filler = clean_region(cached[region], region)
        hide |= {id(segs[i]) for i in loops | filler}
        lines.extend(segs)
        shown = [s for s in segs if id(s) not in hide and not is_unwanted(s["text"])]
        drop = cut_drop(last, shown)  # as a live job does across each cut
        hide |= drop
        last = [s for s in shown if id(s) not in drop]
    return lines, hide


def library_export(video_key, language, model, target, fmt="srt"):
    """Build subtitles for a cached video without a live job."""
    store = _state.store
    raw, hide = _library_lines(store, video_key, language, model)
    # Any translation model: the library outlives model switches
    translations = store.get_translations(language, target, None, [s["text"] for s in raw]) if target else {}
    lang = None if language == "auto" else language
    segments = []
    for s in raw:
        roma = _romanize.romanize(lang, s["text"]) if lang else ""
        if roma is None:
            roma = store.get_romanization(language, s["text"]) or ""
        hidden = id(s) in hide or is_unwanted(s["text"])
        segments.append({**s, "translation": translations.get(s["text"], ""), "romaji": roma, "hidden": hidden})
    return export_subtitles(segments, fmt)
