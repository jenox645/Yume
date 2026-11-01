"""The SQLite cache."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "server"))


def test_store_roundtrip(store):
    store.put_video("v", "https://x", "Title", None, None)
    store.put_video("v", "https://x", "", 60.0, [[0, 30], [30, 60]])
    v = store.get_video("v")
    assert v["title"] == "Title"  # an empty title does not overwrite
    assert v["regions"] == [(0, 30), (30, 60)]
    store.put_video("v", "https://x", "", None, None)  # NULLs keep the plan
    assert store.get_video("v")["regions"] == [(0, 30), (30, 60)]

    store.put_transcript("v", "ja", "m", 0.0, 30.0, [{"start": 1, "end": 2, "text": "あ"}])
    assert store.get_transcripts("v", "ja", "m") == {(0.0, 30.0): [{"start": 1, "end": 2, "text": "あ"}]}
    assert store.get_transcripts("v", "zh", "m") == {}  # language is part of the key

    store.put_translation("ja", "English", "t1", "あ", "ah")
    assert store.get_translations("ja", "English", "t1", ["あ", "い"]) == {"あ": "ah"}
    assert store.get_translations("ja", "English", None, ["あ"]) == {"あ": "ah"}
    assert store.get_translations("ja", "French", "t1", ["あ"]) == {}

    lib = store.library()
    assert lib[0]["video_key"] == "v" and lib[0]["regions_done"] == 1 and lib[0]["regions_total"] == 2
    store.delete_video("v")
    assert store.library() == [] and store.get_transcripts("v", "ja", "m") == {}
