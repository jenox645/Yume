"""Clean a region's raw Whisper lines, and join regions at a cut.

Used for live jobs (_job.py) and for exports of cached videos (_export.py), so
both show the same lines.
"""

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


def is_unwanted(text):
    return is_hallucination(text) or is_credits_line(text)


def clean_region(raw, region):
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


def last_line(lines):
    """The pieces of the last line among a region's visible lines (sorted by start)."""
    return [s for s in lines if s["whole"] == lines[-1]["whole"]] if lines else []


def first_line(lines):
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


def cut_drop(last, shown):
    """ids of the lines to hide at the cut between a region's shown lines and the next region's."""
    if not (last and shown):
        return set()
    return {id(s) for s in join_across_cut(last_line(last), first_line(shown))}
