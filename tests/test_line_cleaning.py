"""What is shown of a region's raw lines: filters, long lines, joins at a cut."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

import _filter  # noqa: E402
import _job  # noqa: E402
import _jobs  # noqa: E402
import _lines  # noqa: E402
from pipeline_fakes import RAP, _seg  # noqa: E402


@pytest.mark.parametrize("bad", [float("inf"), float("nan"), -5, "x", 10**12])
def test_job_duration_and_playhead_are_sane(bad):
    job = _job.Job(("k", "ja", "", "m"), {"duration": bad, "playhead": bad})
    assert job.duration == 0.0 and job.playhead == 0.0
    manager = _jobs.JobManager.__new__(_jobs.JobManager)
    manager._notify = lambda: None
    manager.poll(job, 42.5)
    assert job.playhead == 42.5
    manager.poll(job, bad)  # a poll's ?t= goes through the same check
    assert job.playhead == 0.0


def test_line_that_is_only_a_loop_is_hidden_not_trimmed():

    azumoto = {"start": 0.0, "end": 30.0, "text": "Azumoto-" * 44 + "Azum"}
    real = {"start": 226.0, "end": 255.0, "text": "高い夢を見つけるようなパープをチェックしてみて" + "みて" * 150}
    segs, hide = _filter.clean_raw_segments([azumoto, real])
    assert 0 in hide  # nothing but a loop: an instrumental-intro hallucination
    assert 1 not in hide and segs[1]["text"] == "高い夢を見つけるようなパープをチェックしてみて"
    # a vocalised loop at the START does not hide the lyric that follows it
    segs, hide = _filter.clean_raw_segments([{"start": 0, "end": 5, "text": "ラ" * 30 + " 君と夢を見ていた"}])
    assert not hide and segs[0]["text"] == "ラ 君と夢を見ていた"


def test_cut_off_copies_of_the_next_line_are_hidden():

    segs = [
        _seg(44.9, 45.4, "Место..."),
        _seg(45.4, 50.3, "Место встречи Эрмитаж, где так много символов общего прошлого,"),
        _seg(27.3, 27.8, "どのような法や制度を変えるのか"),
        _seg(27.8, 32.7, "どのような法や制度を変えたくて事件を起こしたのですかと問うと、"),
        _seg(60.0, 63.0, "Hey"),  # long enough to be a real line
        _seg(63.1, 66.0, "Hey Jude, don't make it bad"),
    ]
    _out, hide = _filter.clean_raw_segments(segs)
    assert hide == {0, 2}


def test_the_same_line_twice_becomes_one_line():

    out, hide = _filter.clean_raw_segments(
        [
            _seg(95.3, 98.0, "而它涉及其他技術的措施"),
            _seg(98.0, 101.9, "而它涉及其他技術的措施"),
            _seg(101.9, 104.9, "但也可使用了"),
        ]
    )
    assert hide == {1} and out[0]["end"] == 101.9


@pytest.mark.parametrize(
    ("heard", "notice"),
    [
        ([("en", 0.97), ("en", 0.95)], "sounds like English, but Yume is set to Arabic"),
        ([("ar", 0.99), ("en", 0.97), ("en", 0.97)], ""),  # a region clearly in Arabic settles it
        ([("en", 0.97), ("en", 0.97), ("ar", 0.97)], ""),  # ...even after the hint: an English chorus first
        ([("en", 0.6), ("en", 0.7), ("en", 0.5)], ""),  # not sure enough
        ([("en", 0.97), ("fr", 0.95), ("en", 0.6), ("ja", 0.9)], ""),  # no language twice in 4 checks
    ],
)
def test_the_user_is_told_when_the_video_sounds_like_another_language(heard, notice):
    job = _job.Job(("v", "ar", "", "m"), {"language": "ar"})
    for lang, prob in heard:
        job.check_language(lang, prob)
    assert (notice in job.notice) if notice else job.notice == ""


def test_a_loop_of_a_long_phrase_is_cut():

    line = "هل تتحدث عن ماذا يمكن أن يتحدث " + "عن ماذا يتحدث " * 30  # 14-char unit: was let through
    out, hide = _filter.clean_raw_segments([_seg(50.0, 77.5, line)])
    assert out[0]["text"] == "هل تتحدث عن ماذا يمكن أن يتحدث عن ماذا يتحدث" and not hide


def test_a_sung_stutter_is_a_lyric_not_a_loop():

    lines = ["あたし大大大大大大嫌い", "カイカイカイカイカイはない", "ダイダイダイダイダイキライ"]  # "ダイダイ…キライ"
    out, hide = _filter.clean_raw_segments([_seg(i * 2.0, i * 2.0 + 2.0, t) for i, t in enumerate(lines)])
    assert [s["text"] for s in out] == lines and not hide


def test_thanks_for_watching_in_chinese_is_hidden():
    for line in ("谢谢观看", "謝謝觀看", "谢谢收看"):
        assert _lines.is_unwanted(line), line
    assert not _lines.is_unwanted("谢谢你")  # "thank you" in a lyric


def test_a_line_too_long_to_read_is_cut_into_timed_pieces():

    pieces = _filter.split_long(_seg(101.75, 123.2, RAP))  # one Whisper line: 384 characters for 21 s
    assert len(pieces) == 5
    assert " ".join(p["text"] for p in pieces) == RAP
    assert all(p["text"].endswith((",", ".")) for p in pieces)  # cut after a clause
    assert all(_filter._width(p["text"]) <= _filter.SPLIT_WIDTH for p in pieces)
    assert pieces[0]["start"] == 101.75 and pieces[-1]["end"] == 123.2
    assert all(a["end"] == b["start"] and b["start"] - a["start"] > 3 for a, b in zip(pieces, pieces[1:]))

    news = "На участок Путиловская и Каретная в следующем году направляют около 6,5 миллиардов рублей."
    assert [p["text"] for p in _filter.split_long(_seg(129.8, 135.3, news))] == [
        "На участок Путиловская и Каретная в следующем",
        "году направляют около 6,5 миллиардов рублей.",  # 6,5 is a number, not a clause
    ]
    ja = "明るい話題にしましょう。6個目のテーマ。明るい話題。これはもう永遠のテーマですよ。無人島に持っていくならなり。"
    assert [p["text"] for p in _filter.split_long(_seg(0.0, 10.0, ja))] == [
        "明るい話題にしましょう。6個目のテーマ。明るい話題。",
        "これはもう永遠のテーマですよ。無人島に持っていくならなり。",
    ]


def test_a_short_or_quick_line_is_not_cut():

    assert len(_filter.split_long(_seg(0.0, 9.2, "Пусть отболит за ночь, пока ты там совсем не ждёшь, я войду"))) == 1
    assert len(_filter.split_long(_seg(0.0, 3.0, RAP))) == 1  # 3 s: no piece would be readable
    no_break = "而AI的網絡" * 10  # nowhere to cut but inside a word
    assert len(_filter.split_long(_seg(0.0, 20.0, no_break))) == 1
