"""Karaoke word timing (server/_align.py) against a fake alignment: the real one
needs Whisper on a GPU and was checked on gold songs."""

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "server"))
import _align
import _state
from _store import Store


def word(text, start, end, tokens=1):
    return {"word": text, "start": start, "end": end, "tokens": list(range(tokens))}


def test_the_region_words_go_back_to_their_lines_by_token_count():
    alignment = [word(" Hello", 0, 1), word(" world", 1, 2, tokens=2), word(" again", 3, 4)]
    lines = _align._line_words(alignment, [[1, 2, 3], [4]])
    assert [[w["word"] for w in line] for line in lines] == [[" Hello", " world"], [" again"]]


def test_a_line_the_tokens_do_not_fill_gets_nothing():
    assert _align._line_words([word(" Hi", 0, 1)], [[1, 2]]) == [None]


def test_each_word_knows_where_it_sits_in_the_line():
    spans = _align._spans([word(" Звездная", 0.1, 0.9), word(" пыль,", 1.0, 1.4)], "Звездная пыль,", 50, 50, 52)
    assert spans == [[50.1, 50.9, 0, 8], [51.0, 51.4, 9, 14]]


def test_japanese_lights_up_character_by_character():
    spans = _align._spans([word("さ", 0.5, 0.8), word("よ", 0.8, 1.0), word("なら", 1.0, 1.3)], "さよなら", 10, 10, 12)
    assert [s[2:] for s in spans] == [[0, 1], [1, 2], [2, 4]]


def test_words_stay_inside_their_line_and_in_order():
    spans = _align._spans([word(" a", -1.0, 0.2), word(" b", 0.1, 9.0)], "a b", 0, 0, 2)
    assert spans[0][0] == 0 and spans[1][1] == 2  # nothing before the line shows or after it goes
    assert spans[1][0] >= spans[0][1]


def test_a_word_that_took_in_the_silence_before_it_starts_when_it_is_sung():
    # "さ" aligned from the line's start, 9 s before the next character: the intro
    words = [word("さ", 0.0, 9.5), word("よ", 9.5, 9.7), word("な", 9.7, 9.9), word("ら", 9.9, 10.1)]
    first = _align._spans(words, "さよなら", 0, 0, 11)[0]
    assert first[1] - first[0] <= _align.MIN_LONGEST_S


def test_words_that_do_not_spell_the_line_are_dropped():
    assert _align._spans([word(" Hello", 0, 1)], "Goodbye", 0, 0, 1) is None


def test_whisper_cpp_has_no_alignment_so_no_words():
    with patch.object(_state, "model", object()):
        assert _align.word_times([0.0] * 32000, 0, [{"text": "a", "start": 0, "end": 1}], "ja") == {}


def test_word_times_are_kept_with_the_video(tmp_path):
    store = Store(tmp_path / "cache.db")
    store.put_word_times("v", "ja", "m", [(1.234, "さよなら", [[1.3, 1.5, 0, 1]])])
    assert store.get_word_times("v", "ja", "m") == {(1.23, "さよなら"): [[1.3, 1.5, 0, 1]]}
    store.delete_video("v")
    assert store.get_word_times("v", "ja", "m") == {}
