"""The gold set's scoring: a wrong count here would decide a model change.

CI installs neither fugashi nor rapidfuzz: the gold set runs on a PC with a GPU."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent / "gold"))
import gold


@pytest.mark.parametrize(
    ("lang", "a", "b"),
    [
        ("ja", "君の名は", "きみのなは"),  # kanji or kana: the same sound
        ("zh", "告白氣球", "告白气球"),  # traditional or simplified
        ("ko", "밤 편지!", "밤편지"),  # spacing is not hearing
        ("ru", "Всё хорошо,", "все хорошо"),
        ("ar", "أهلا بِكَ", "اهلا بك"),  # hamza and vowel marks
    ],
)
def test_spelling_is_not_an_error(lang, a, b):
    if lang == "ja":
        pytest.importorskip("fugashi")
    assert gold.tokens(a, lang) == gold.tokens(b, lang)


def test_sound_marks_in_a_reference_are_not_words():
    pytest.importorskip("fugashi")
    assert gold.tokens("[音楽] (拍手) >> はい", "ja") == gold.tokens("はい", "ja")


def test_errors_count_each_kind():
    pytest.importorskip("rapidfuzz")
    assert gold.errors(list("abcd"), list("axd")) == {"n": 4, "sub": 1, "del": 1, "ins": 0, "runs": 0}


def test_a_long_invented_passage_is_one_run():
    pytest.importorskip("rapidfuzz")
    out = gold.errors(list("abcd"), list("ab") + list("x" * 12) + list("cd"))
    assert out["ins"] == 12 and out["runs"] == 1


def test_a_shown_line_over_silence_is_a_ghost():
    cues = [(10.0, 12.0, "x")]
    assert gold.ghost_lines([(11.0, 13.0, "real"), (30.0, 32.0, "invented")], cues) == 1


def test_a_clip_keeps_its_cues_on_its_own_clock():
    cues = [(5.0, 7.0, "before"), (21.0, 23.0, "in"), (171.0, 172.0, "after")]
    assert gold.window(cues, 20, 150) == [(1.0, 3.0, "in")]


def test_captions_join_their_lines_and_drop_repeats():
    vtt = "WEBVTT\n\n00:00:01.000 --> 00:00:03.000 align:start\nhello\nworld\n\n00:00:03.000 --> 00:00:04.000\nhello world\n"
    assert gold.parse_vtt(vtt) == [(1.0, 3.0, "hello world")]


def test_a_lyric_line_ends_where_the_next_starts_or_soon_after():
    lrc = "[00:10.50]one\n[00:12.00]\n[00:15.00]two\n[01:00.00]three"
    assert gold.parse_lrc(lrc, 70) == [(10.5, 12.0, "one"), (15.0, 27.0, "two"), (60.0, 70, "three")]


def test_chrf_rewards_the_same_words_and_ignores_line_cuts():
    import mt

    assert mt.chrf("the cat sat", "the cat sat") == pytest.approx(100)
    assert mt.chrf("the cat sat on it", "the  cat sat  on it") == pytest.approx(100)
    assert mt.chrf("the cat sat", "the cat") > mt.chrf("dogs ran off", "the cat")


def test_a_name_is_capitalised_inside_a_sentence():
    import mt

    assert mt.names_in("The tsar met Boris and then Godunov, and I left.") == {"Boris", "Godunov"}
