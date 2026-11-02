"""Region planning: exclusive sections cut at quiet points."""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

import _regions  # noqa: E402
from pipeline_fakes import SR, _tone_with_gaps  # noqa: E402


def test_regions_cover_track_without_overlap():
    regions = _regions.plan_regions(_tone_with_gaps(200))
    assert regions[0][0] == 0.0
    assert _regions.FIRST_MIN_S <= regions[0][1] <= _regions.FIRST_REGION_S  # the stream preview covers it
    assert regions[-1][1] == pytest.approx(200, abs=0.01)
    for (s1, e1), (s2, _e2) in zip(regions, regions[1:]):
        assert e1 == s2  # contiguous, exclusive
    for s, e in regions[:-1]:
        assert e - s <= _regions.MAX_REGION_S + 1e-6


def test_regions_cut_in_silence():
    regions = _regions.plan_regions(_tone_with_gaps(200, gap_every=10.0))
    for _s, cut in regions[:-1]:  # region 0 too: a cut at a fixed 30 s split the first sung line
        # every gap starts at a multiple of 10 and lasts 1 s
        assert (cut % 10.0) <= 1.0 + _regions.HOP_S, f"cut at {cut}s is not in a silent gap"


def test_short_track_is_one_region():
    assert _regions.plan_regions(np.zeros(20 * SR, dtype=np.float32)) == [(0.0, 20.0)]


def test_tiny_tail_merged():
    regions = _regions.plan_regions(_tone_with_gaps(30 + 26 + 3))
    assert regions[-1][1] - regions[-1][0] >= _regions.MIN_TAIL_S


def test_whisper_starts_where_the_voice_does():
    audio = np.zeros(SR * 20, dtype=np.float32)
    audio[SR * 18 :] = 0.1
    assert _regions.voice_start(audio) == pytest.approx(17.7)


def test_a_silent_region_is_not_heard_at_all():
    assert _regions.voice_start(np.zeros(SR * 20, dtype=np.float32)) is None


def test_a_region_starting_with_a_voice_is_heard_whole():
    assert _regions.voice_start(np.full(SR * 5, 0.1, dtype=np.float32)) == 0.0
    assert _regions.voice_start(np.zeros(10, dtype=np.float32)) == 0.0  # too short to judge


def test_reverb_before_a_voice_is_not_the_voice():
    stem = np.zeros(SR * 20, dtype=np.float32)
    stem[SR * 5 : SR * 10] = 0.02  # bleed: Whisper fills this with an invented line
    stem[SR * 15 :] = 0.14
    level = _regions.voice_level(stem)
    assert _regions.voice_start(stem, level=level) == pytest.approx(14.7)
    assert _regions.voice_start(stem[: SR * 12], level=level) is None


def test_region_index_at():
    regions = [(0.0, 30.0), (30.0, 55.0), (55.0, 80.0)]
    assert _regions.region_index_at(regions, 0) == 0
    assert _regions.region_index_at(regions, 30) == 1
    assert _regions.region_index_at(regions, 79.9) == 2
    assert _regions.region_index_at(regions, 500) == 2
