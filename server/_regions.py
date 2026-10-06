"""Split a track into transcription regions at quiet points.

Whisper is run once per region. Regions are exclusive (no overlap), so nothing
has to be de-duplicated afterwards; boundaries are placed at the quietest moment
near each target cut so a cut rarely lands inside a word. The old design used
fixed 30 s windows with a 5 s overlap and two layers of heuristics to remove the
duplicated lines that overlap produced.

Pure functions over a numpy array — no model, no I/O — so they are unit-testable.
"""

import numpy as np

SAMPLE_RATE = 16000

# Region 0 ends at the quietest point in [FIRST_MIN_S, FIRST_REGION_S]. It is the
# one transcribed from a stream preview of the first FIRST_REGION_S seconds while
# the full download is still running; the preview covers it, and the lines it
# heard after the cut are dropped (region 1 transcribes them whole). A fixed cut
# at 30 s split the first sung line in two.
FIRST_REGION_S = 30.0
FIRST_MIN_S = 20.0

TARGET_S = 26.0  # aim for a cut every ~26 s ...
SEARCH_BEFORE_S = 6.0  # ... at the quietest point in [target - 6, target + 4]
SEARCH_AFTER_S = 4.0
MAX_REGION_S = 30.0  # one Whisper window: a longer region costs a second window
MIN_TAIL_S = 8.0  # a shorter last region is merged into the previous one
HOP_S = 0.05  # energy resolution
SMOOTH_S = 0.3  # energy smoothing window


def _energy(audio, sr):
    hop = max(1, int(sr * HOP_S))
    n = len(audio) // hop
    if n == 0:
        return np.zeros(0, dtype=np.float32)
    frames = audio[: n * hop].reshape(n, hop).astype(np.float32)
    rms = np.sqrt(np.mean(frames * frames, axis=1))
    k = max(1, int(SMOOTH_S / HOP_S))
    return np.convolve(rms, np.ones(k, dtype=np.float32) / k, mode="same")


def plan_regions(audio, sr=SAMPLE_RATE):
    """Return [(start_s, end_s), ...] covering the whole track."""
    duration = len(audio) / sr
    if duration <= 0:
        return []
    if duration <= FIRST_REGION_S + MIN_TAIL_S:
        return [(0.0, round(duration, 2))]

    energy = _energy(audio, sr)

    def quietest(lo, hi, fallback):
        i0, i1 = int(lo / HOP_S), int(hi / HOP_S)
        window = energy[i0:i1]
        return round((i0 + int(np.argmin(window))) * HOP_S if len(window) else fallback, 2)

    start = quietest(FIRST_MIN_S, FIRST_REGION_S, FIRST_REGION_S)
    regions = [(0.0, start)]
    while duration - start > MAX_REGION_S:
        cut = quietest(
            start + TARGET_S - SEARCH_BEFORE_S,
            min(start + TARGET_S + SEARCH_AFTER_S, start + MAX_REGION_S),
            start + TARGET_S,
        )
        regions.append((start, cut))
        start = cut
    end = round(duration, 2)
    if end - start < MIN_TAIL_S and len(regions) > 1:
        prev_start, _ = regions.pop()
        regions.append((prev_start, end))
    else:
        regions.append((start, end))
    return regions


def fixed_regions(duration):
    """Regions for streaming mode (no local audio to measure): plain 30 s cuts."""
    out = []
    start = 0.0
    while start < duration:
        end = min(duration, start + FIRST_REGION_S)
        out.append((round(start, 2), round(end, 2)))
        start = end
    return out


def region_index_at(regions, t):
    """Index of the region containing time t (clamped to the last region)."""
    for i, (s, e) in enumerate(regions):
        if s <= t < e:
            return i
    return len(regions) - 1 if regions and t >= regions[-1][1] else 0
