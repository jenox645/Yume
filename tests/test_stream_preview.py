"""The vocal isolation limit, from a live run on real YouTube videos."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

import _separate  # noqa: E402
import _state  # noqa: E402


def test_talks_of_10_to_15_minutes_are_not_separated(monkeypatch):
    monkeypatch.setattr(_state, "vocal_isolation", True)
    monkeypatch.setattr(_separate, "available", lambda: (True, ""))
    assert _separate.wanted(9 * 60) and not _separate.wanted(11 * 60)
