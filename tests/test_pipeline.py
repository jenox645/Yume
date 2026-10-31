"""Tests for the server-side subtitle pipeline: regions, cache, translator, jobs.

No GPU, model, network or LLM needed: Whisper and the LLM are faked.
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
import threading
import time
import types
import urllib.error
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

import _audio  # noqa: E402
import _download  # noqa: E402
import _export  # noqa: E402
import _job  # noqa: E402
import _jobs  # noqa: E402
import _lines  # noqa: E402
import _regions  # noqa: E402
import _romanize  # noqa: E402
import _separate  # noqa: E402
import _state  # noqa: E402
import _store  # noqa: E402
import _stream  # noqa: E402
import _translate  # noqa: E402
import _ytdlp  # noqa: E402

SR = 16000


# ── regions ───────────────────────────────────────────────────────────────────


def _tone_with_gaps(seconds, gap_every=10.0):
    """Tone with a 1 s silence every `gap_every` seconds."""
    t = np.arange(int(seconds * SR)) / SR
    audio = (0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    for g in np.arange(gap_every, seconds, gap_every):
        audio[int(g * SR) : int((g + 1) * SR)] = 0
    return audio


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


def test_region_index_at():
    regions = [(0.0, 30.0), (30.0, 55.0), (55.0, 80.0)]
    assert _regions.region_index_at(regions, 0) == 0
    assert _regions.region_index_at(regions, 30) == 1
    assert _regions.region_index_at(regions, 79.9) == 2
    assert _regions.region_index_at(regions, 500) == 2


# ── store ─────────────────────────────────────────────────────────────────────


@pytest.fixture
def store(tmp_path):
    st = _store.Store(tmp_path / "c.db")
    yield st
    st.close()


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


# ── translator ────────────────────────────────────────────────────────────────


def _translator(backend="llamacpp", model=""):
    return _translate.Translator(
        lambda: {"host": "127.0.0.1", "port": 1, "backend": backend, "model": model, "prompt": "", "roma_prompt": ""}
    )


def test_forbidden_scripts_never_ban_target():
    assert _translate.forbidden_scripts("ko", "Japanese") == []
    assert _translate.forbidden_scripts("ja", "Chinese") == []
    assert _translate.forbidden_scripts("ja", "English") == ["Chinese", "Japanese"]
    assert _translate.forbidden_scripts("ru", "English") == ["Russian", "Chinese", "Japanese"]
    assert _translate.forbidden_scripts("auto", "English") == ["Chinese", "Japanese"]


def test_parse_numbered_and_json():
    assert _translate.parse_numbered("[1] hello\n[2] world", 2) == ["hello", "world"]
    assert _translate.parse_numbered("[1] hello\n[2]", 2) == ["hello", None]  # bare marker is not a line
    assert _translate.parse_numbered("hello\nworld", 2) == ["hello", "world"]  # positional fallback
    assert _translate.parse_json_list('```json\n{"translations": ["a", "b"]}\n```', "translations", 3) == [
        "a",
        "b",
        None,
    ]
    with pytest.raises(ValueError):
        _translate.parse_json_list("no json here", "translations", 1)


def test_structured_output_used_and_remembered():
    tr = _translator()
    calls = []

    def fake_chat(messages, max_tokens, response_format=None, temperature=0.1):
        calls.append(response_format["type"] if response_format else "plain")
        return json.dumps({"translations": ["Hello", "World"]})

    with patch.object(tr, "_chat", side_effect=fake_chat):
        assert tr.translate_batch(["こんにちは", "世界"], "ja", "English") == ["Hello", "World"]
        tr.translate_batch(["あ", "い"], "ja", "English")
    assert calls == ["json_schema", "json_schema"]


def test_falls_back_when_backend_rejects_response_format():
    tr = _translator()
    calls = []

    def fake_chat(messages, max_tokens, response_format=None, temperature=0.1):
        kind = response_format["type"] if response_format else "plain"
        calls.append(kind)
        if kind != "plain":
            raise _translate.TranslationError("HTTP 400: response_format not supported")
        return "[1] Hello\n[2] World"

    with patch.object(tr, "_chat", side_effect=fake_chat):
        assert tr.translate_batch(["あ", "い"], "ja", "English") == ["Hello", "World"]
        calls.clear()
        tr.translate_batch(["あ", "い"], "ja", "English")
    assert calls == ["plain"]  # remembered: no more failing attempts


def test_missing_line_retried_once_and_cjk_leak_stripped():
    tr = _translator()

    def fake_chat(messages, max_tokens, response_format=None, temperature=0.1):
        if response_format:
            return json.dumps({"translations": ["", "smile in拉斯媒体"]})
        return "拉斯"  # single-line retry still leaks

    with patch.object(tr, "_chat", side_effect=fake_chat):
        out = tr.translate_batch(["ラ", "スマイル"], "ja", "English")
    assert out[1] == "smile in"
    assert not _translate.contains_cjk(out[0])


def test_model_sent_only_for_backends_that_need_it():
    for backend, model, expect in (("llamacpp", "x.gguf", False), ("ollama", "qwen2.5:7b", True)):
        tr = _translator(backend, model)
        sent = {}

        class Resp:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()

        def fake_urlopen(req, timeout=None):
            sent.update(json.loads(req.data))
            return Resp()

        with patch("urllib.request.urlopen", fake_urlopen):
            tr._chat([{"role": "user", "content": "x"}], 10)
        assert ("model" in sent) is expect, backend


def test_unreachable_server_raises_without_trying_other_formats():
    tr = _translator()
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
        with pytest.raises(_translate.TranslationError, match="unreachable"):
            tr.translate_batch(["あ"], "ja", "English")


@pytest.mark.parametrize(
    "reply",
    [
        '<think>\nThe user wants {"x": 1}. 1. first\n</think>\n{"translations": ["a", "b"]}',  # Qwen3, R1
        '{"translations": ["a", "b"]}\nNote: I kept {names} as they are.',  # text after the JSON
        '{"lines": ["a", "b"]}',  # json_object mode: another key
        '["a", "b"]',  # a bare list
        '{"translations": [{"text": "a"}, {"translation": "b"}]}',  # objects for strings
    ],
)
def test_json_replies_small_models_really_give_are_parsed(reply):
    assert _translate.parse_json_list(reply, "translations", 2) == ["a", "b"]  # each was an error or "{'text': 'a'}"


def test_numbered_replies_in_other_styles_are_parsed():
    assert _translate.parse_numbered("**1.** a\n**2.** b", 2) == ["a", "b"]  # was "**1.** a"
    assert _translate.parse_numbered("1: a\n2: b", 2) == ["a", "b"]
    assert _translate.parse_numbered("<think>\n1. hmm\n2. hmm\n</think>\n1. a\n2. b", 2) == ["a", "b"]
    assert _translate.parse_numbered("3:30 at the station\nsee you", 2) == ["3:30 at the station", "see you"]


@pytest.mark.parametrize(
    ("raw", "clean"),
    [
        ("'Cause I love you", "'Cause I love you"),  # was "Cause I love you"
        ("I keep on dreamin'", "I keep on dreamin'"),  # was "dreamin"
        ("「君の名は」という映画", "「君の名は」という映画"),  # was "君の名は」という映画"
        ('"Hello" he said', '"Hello" he said'),
        ('"Hello there"', "Hello there"),
        ("'Hello there'", "Hello there"),
        ("「こんにちは」", "こんにちは"),
        ('"Hello there', "Hello there"),  # a lone quote at an end
        ("Rock 'n' roll", "Rock 'n' roll"),
    ],
)
def test_only_quotes_around_the_whole_translation_are_removed(raw, clean):
    assert _translate.clean_translation(raw) == clean


def test_a_cut_off_reply_does_not_downgrade_the_format_for_good():
    tr = _translator()
    calls = []

    def chat(messages, max_tokens, response_format=None, temperature=0.1):
        calls.append(response_format["type"] if response_format else "plain")
        if len(calls) == 2:
            return '{"translations": ["a", "b'  # cut off at max_tokens
        return json.dumps({"translations": ["a", "b"]})

    tr._chat = chat
    assert tr.translate_batch(["一", "二"], "zh", "French") == ["a", "b"]
    assert tr.translate_batch(["三", "四"], "zh", "French") == ["a", "b"]  # json_object took over once
    calls.clear()
    tr.translate_batch(["五", "六"], "zh", "French")
    assert calls == ["json_schema"]  # was json_object from then on


def test_lines_without_words_are_not_sent():
    tr = _translator()
    sent = []

    def chat(messages, max_tokens, response_format=None, temperature=0.1):
        sent.append(messages[-1]["content"])
        return json.dumps({"translations": ["I love you"]})

    tr._chat = chat
    assert tr.translate_batch(["…", "♪", "愛してる", "  "], "ja", "French") == ["", "", "I love you", ""]
    assert len(sent) == 1 and "♪" not in sent[0]


# ── romanization ──────────────────────────────────────────────────────────────


def test_korean_and_russian_romanization():
    assert _romanize.romanize("ko", "사랑해요") == "saranghaeyo"
    assert _romanize.romanize("ru", "Ещё я тебя люблю") == "Yeshchyo ya tebya lyublyu"
    assert _romanize.romanize("ru", "поешь") == "poyesh"  # е after a vowel -> ye
    assert _romanize.romanize("ru", "лето") == "leto"  # е after a consonant -> e
    assert _romanize.romanize("ar", "مرحبا") is None  # needs the LLM
    assert _romanize.romanize("fr", "bonjour") == ""  # Latin script: nothing to do
    assert _romanize.romanize("ja", "   ") == ""


@pytest.mark.parametrize(
    "hangul, rr",
    [
        ("좋은 꿈", "joeun kkum"),  # silent ㅎ (was "joteun")
        ("감사합니다", "gamsahamnida"),  # nasalization (was "gamsahapnida")
        ("모든 말을", "modeun mareul"),  # liaison, ㄹ → r
        ("마음속에", "maeumsoge"),
        ("그대가 멀리", "geudaega meolli"),  # ㄹㄹ → ll
        ("어떻게", "eotteoke"),  # ㅎ + ㄱ → k
        ("같이", "gachi"),  # palatalization
        ("설날", "seollal"),
        ("종로", "jongno"),
        ("있는", "inneun"),
        ("없지만", "eopjiman"),
        ("행운이", "haenguni"),  # ㅇ final does not move
        ("닭", "dak"),
        ("굳히다", "guchida"),  # ㄷ + 히 palatalized (was "gutida", the RR rules' own example)
        ("닫혀", "dachyeo"),  # was "datyeo"
        ("앉히다", "anchida"),  # was "anhida"
        ("읽고 쓰고", "ilgo sseugo"),  # ㄺ before ㄱ is ㄹ (was "ikgo")
        ("밟다", "bapda"),  # 밟 is said ㅂ before a consonant (was "balda")...
        ("밟는", "bamneun"),
        ("밟아", "balba"),  # ...not before a vowel
        ("넓다", "neolda"),  # other ㄼ stay ㄹ
        ("꽃잎", "kkonnip"),  # ㄴ added before 잎 (was "kkochip")
        ("솔잎", "sollip"),
        ("꽃이", "kkochi"),
    ],
)
def test_korean_romanization_follows_pronunciation(hangul, rr):
    assert _romanize.romanize("ko", hangul) == rr


def test_russian_words_in_capitals_stay_in_capitals():
    assert _romanize.romanize("ru", "ЮЛЯ ЛЮБИТ ТЕБЯ") == "YULYA LYUBIT TEBYA"  # was "YuLYa LYuBIT TEBYa"
    assert _romanize.romanize("ru", "Юля и Ёлка") == "Yulya i Yolka"
    assert _romanize.romanize("ru", "Я") == "Ya"


# ── jobs ──────────────────────────────────────────────────────────────────────


class _Seg:
    def __init__(self, s, e, t):
        self.start, self.end, self.text, self.avg_logprob = s, e, t, -0.2


class _FakeModel:
    def transcribe(self, audio, **kw):
        n = len(audio) / SR
        return iter(
            [_Seg(1.0, 3.0, "こんにちは"), _Seg(min(5.0, n - 1), min(8.0, n), "ご視聴ありがとうございました")]
        ), (types.SimpleNamespace(language="ja", duration=n))


class _FakeTranslator:
    def __init__(self):
        self.calls = 0

    def model_id(self):
        return "fake:1"

    def translate_batch(self, texts, src, tgt, title="", context=None, readings=None):
        self.calls += 1
        return [f"EN({t})" for t in texts]

    def romanize_batch(self, texts, lang):
        return ["roma"] * len(texts)


@pytest.fixture
def pipeline(tmp_path):
    import wave

    wav = tmp_path / "a.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        w.writeframes((_tone_with_gaps(95) * 32767).astype(np.int16).tobytes())
    saved = (_state.model, _state.store, _state.jobs, _state.user_blacklist, _state.vocal_isolation)
    _state.vocal_isolation = False  # never a real separator (or a torch import) in tests
    _state.model = _FakeModel()
    _state.store = _store.Store(tmp_path / "c.db")
    _state.user_blacklist = []
    translator = _FakeTranslator()
    _state.jobs = _jobs.JobManager(translator)
    with (
        patch.object(_download, "download_full_audio", return_value=(str(wav), None)) as dl,
        patch.object(_stream, "download_audio_segment", return_value=None),
        patch.object(_stream, "get_stream_url", return_value=None),
        patch.object(_audio, "remove_temp"),
    ):
        yield types.SimpleNamespace(jobs=_state.jobs, translator=translator, download=dl)
        _state.jobs._stop = True
        _state.jobs.drop_all("teardown")
        # Wait for the jobs' download threads while the fakes are still in place: one
        # that outlived the test ran the real yt-dlp (network) and hit other tests' mocks
        for t in threading.enumerate():
            if t.name.startswith(("prepare-", "preview-")):
                t.join(timeout=10)
    time.sleep(0.2)  # let a worker mid-region finish before the store closes
    _state.store.close()
    _state.model, _state.store, _state.jobs, _state.user_blacklist, _state.vocal_isolation = saved


def _run(jobs, job, timeout=20, playhead=0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        jobs.poll(job, playhead)
        if job.status in ("done", "error"):
            return job.snapshot(0)
        time.sleep(0.05)
    raise AssertionError(f"job stuck in {job.status}")


PARAMS = {"video_id": "vid", "url": "https://www.youtube.com/watch?v=vid", "language": "ja", "target": "English"}


def test_job_transcribes_filters_translates_romanizes(pipeline):
    snap = _run(pipeline.jobs, pipeline.jobs.create(PARAMS))
    assert snap["status"] == "done"
    p = snap["progress"]
    assert p["regions_done"] == p["regions_total"] > 1
    assert p["lines"] == p["translated"] == p["regions_total"]  # one real line per region
    seg = snap["segments"][0]
    assert seg["text"] == "こんにちは" and seg["translation"] == "EN(こんにちは)"
    # hallucinations are never sent on a fresh snapshot
    assert all("視聴" not in s["text"] for s in snap["segments"])
    srt, n = _export.export_subtitles(list(_state.jobs.get(snap["id"]).segments.values()))
    assert n == p["lines"] and "EN(こんにちは)" in srt


def test_second_job_served_from_cache_without_download(pipeline):
    _run(pipeline.jobs, pipeline.jobs.create(PARAMS))
    calls_before = pipeline.translator.calls
    pipeline.jobs.drop_all("test")
    pipeline.download.reset_mock()
    snap = _run(pipeline.jobs, pipeline.jobs.create(PARAMS))
    assert snap["status"] == "done"
    pipeline.download.assert_not_called()
    assert pipeline.translator.calls == calls_before  # translations cached too


def test_other_language_is_a_different_job(pipeline):
    a = pipeline.jobs.create(PARAMS)
    b = pipeline.jobs.create({**PARAMS, "language": "zh"})
    assert a is not b
    assert pipeline.jobs.create(PARAMS) is a  # same params rejoin the same job


def test_blacklist_edit_hides_lines_incrementally(pipeline):
    job = pipeline.jobs.create(PARAMS)
    snap = _run(pipeline.jobs, job)
    rev = snap["rev"]
    _state.user_blacklist = ["こんにちは"]
    pipeline.jobs.refilter()
    changed = job.snapshot(rev)["segments"]
    assert changed and all(s["hidden"] for s in changed)
    assert job.snapshot(0)["progress"]["lines"] == 0


def _raw(s, e, t):
    return {"start": s, "end": e, "text": t, "confidence": -0.3}


def test_stream_preview_covers_a_region_0_cut_before_30s(pipeline):
    # The preview hears [0, 30); the plan cuts region 0 at a quiet point, here
    # 21.5 s. Lines it heard after the cut are region 1's, which transcribes them
    # whole: a fixed cut at 30 s split the first sung line.
    job = _job.Job(("pv", "ja", "English"), PARAMS)
    raw = [_raw(1.0, 6.0, "最初の行"), _raw(24.0, 30.0, "半分の行")]
    with job.lock:
        job.show_preview(raw)  # shown before the plan exists
        job.preview_segments = raw
        assert [s["text"] for s in job.snapshot(0)["segments"]] == ["最初の行", "半分の行"]
        job.set_regions([(0.0, 21.5), (21.5, 47.0)], {})
        pipeline.jobs._adopt_preview(job)
        shown = [s for s in job.segments.values() if not s["hidden"]]
        assert [s["text"] for s in shown] == ["最初の行"]
        assert job.region_state == ["done", "pending"]
    # and it is cached under the plan's region only
    assert set(_state.store.get_transcripts("pv", "ja", job.model)) == {(0.0, 21.5)}


# ── vocal isolation ───────────────────────────────────────────────────────────


def test_songs_are_transcribed_from_the_separated_vocals(pipeline, monkeypatch):
    vocals = _tone_with_gaps(95) * 0.5  # recognisably not the mix
    heard = []
    real_transcribe = _jobs.transcribe_audio

    def transcribe(audio, *a, **kw):
        heard.append(float(np.abs(audio).max()))
        return real_transcribe(audio, *a, **kw)

    monkeypatch.setattr(_separate, "wanted", lambda duration: True)
    monkeypatch.setattr(_separate, "read_stereo", lambda path: np.zeros((2, 10), np.float32))
    monkeypatch.setattr(_separate, "isolate", lambda stereo: vocals)
    monkeypatch.setattr(_jobs, "transcribe_audio", transcribe)
    job = pipeline.jobs.create({**PARAMS, "video_id": "song", "duration": 95})
    assert job.isolate and job.model == _state.model_name + _separate.MODEL_SUFFIX
    snap = _run(pipeline.jobs, job)
    assert snap["status"] == "done"
    assert pipeline.download.call_args.kwargs == {"stereo": True}  # stereo: what the separator needs
    assert heard and max(heard) <= 0.15 + 1e-6  # every region came from the vocals (the mix peaks at 0.3)
    assert any("Vocals separated" in e["msg"] for e in job.events)
    # cached apart from transcripts of the mix
    assert _state.store.get_transcripts("song", "ja", job.model)
    assert not _state.store.get_transcripts("song", "ja", _state.model_name)


def test_a_failed_separation_transcribes_the_mix(pipeline, monkeypatch):
    def boom(stereo):
        raise RuntimeError("CUDA out of memory")

    monkeypatch.setattr(_separate, "wanted", lambda duration: True)
    monkeypatch.setattr(_separate, "read_stereo", lambda path: np.zeros((2, 10), np.float32))
    monkeypatch.setattr(_separate, "isolate", boom)
    job = pipeline.jobs.create({**PARAMS, "video_id": "song2", "duration": 95})
    snap = _run(pipeline.jobs, job)
    assert snap["status"] == "done" and snap["progress"]["lines"] > 0
    assert job.model == _state.model_name and not job.isolate  # cached as the mix's
    assert any("Vocal isolation failed" in e["msg"] and "out of memory" in e["msg"] for e in job.events)


def test_which_videos_are_separated(monkeypatch):
    monkeypatch.setattr(_separate, "available", lambda: (True, ""))
    monkeypatch.setattr(_state, "vocal_isolation", True)
    assert _separate.wanted(240)
    assert not _separate.wanted(0)  # unknown length (a live stream)
    assert not _separate.wanted(_separate.MAX_S + 1)  # a talk, not a song
    monkeypatch.setattr(_state, "vocal_isolation", False)
    assert not _separate.wanted(240)
    monkeypatch.setattr(_state, "vocal_isolation", True)
    monkeypatch.setattr(_separate, "available", lambda: (False, "not installed"))
    assert not _separate.wanted(240)


def test_stereo_downloads_are_read_for_the_separator(tmp_path):
    import wave

    def wav(path, rate, channels):
        with wave.open(str(path), "wb") as w:
            w.setnchannels(channels)
            w.setsampwidth(2)
            w.setframerate(rate)
            w.writeframes(np.array([16384, -16384] * 50, np.int16).tobytes())
        return str(path)

    st = _separate.read_stereo(wav(tmp_path / "s.wav", 44100, 2))
    assert st.shape == (2, 50) and st[0, 0] == pytest.approx(0.5) and st[1, 0] == pytest.approx(-0.5)
    assert _separate.read_stereo(wav(tmp_path / "m.wav", 16000, 1)) is None
    assert _separate.read_stereo(str(tmp_path / "missing.wav")) is None


def test_no_stream_preview_of_the_start_for_a_resumed_video(pipeline):
    with patch.object(_stream, "download_audio_segment", return_value=None) as seg:
        job = pipeline.jobs.create({**PARAMS, "video_id": "resumed", "playhead": 60.0})
        assert job.playhead == 60.0  # transcribed first, before the first poll
        _run(pipeline.jobs, job, playhead=60.0)
        seg.assert_not_called()
        _run(pipeline.jobs, pipeline.jobs.create({**PARAMS, "video_id": "from-start"}))
        seg.assert_called_once()


def test_stream_preview_shown_before_a_plan_that_keeps_its_region(pipeline):
    # Plans cached before the quiet cut (and stream mode) keep region 0 = [0, 30)
    job = _job.Job(("pv30", "ja", "English"), PARAMS)
    raw = [_raw(1.0, 6.0, "最初の行"), _raw(24.0, 29.0, "次の行")]
    with job.lock:
        job.show_preview(raw)
        job.preview_segments = raw
        job.set_regions([(0.0, 30.0), (30.0, 55.0)], {})
        pipeline.jobs._adopt_preview(job)
        assert [s["text"] for s in job.segments.values() if not s["hidden"]] == ["最初の行", "次の行"]
        assert len(job.segments) == 2  # kept, not added again
        assert job.region_state == ["done", "pending"]
    assert set(_state.store.get_transcripts("pv30", "ja", job.model)) == {(0.0, 30.0)}


def test_lines_end_with_their_region_and_window_fillers_are_hidden(pipeline):
    job = _job.Job(("clip", "ko", "English"), {**PARAMS, "language": "ko"})
    with job.lock:
        job.regions = [(0.0, 24.5), (24.5, 52.0), (52.0, 60.3)]
        job.region_state = ["pending"] * 3
        job.add_region(0, [_raw(0.0, 30.0, "다음 노래")])  # alone over the intro
        job.add_region(1, [_raw(25.0, 33.0, "사랑해요"), _raw(40.0, 54.0, "그대")])
        job.add_region(2, [_raw(52.0, 82.0, "안녕")])  # stamped past the end of the video
    segs = {s["text"]: s for s in job.segments.values()}
    assert segs["다음 노래"]["hidden"] and segs["안녕"]["hidden"]
    assert not segs["사랑해요"]["hidden"] and not segs["그대"]["hidden"]
    assert segs["그대"]["end"] == 52.0 and segs["안녕"]["end"] == 60.3
    # a blacklist edit re-filters every line: the fillers stay hidden
    pipeline.jobs.jobs[job.id] = job
    pipeline.jobs.refilter()
    assert segs["다음 노래"]["hidden"] and segs["안녕"]["hidden"] and not segs["그대"]["hidden"]


def test_library_progress_counts_only_the_plan(store):
    seg = [{"start": 1, "end": 2, "text": "あ"}]
    store.put_video("v", "https://x", "t", 40.0, None)
    store.put_transcript("v", "ja", "m", 0.0, 30.0, seg)  # the stream preview, before the plan
    store.put_video("v", "https://x", "t", 40.0, [[0.0, 20.0], [20.0, 40.0]])
    assert store.get_transcripts("v", "ja", "m") == {}  # the preview's region is not in the plan: gone
    store.put_transcript("v", "ja", "m", 0.0, 20.0, seg)
    store.put_transcript("v", "ja", "m", 0.0, 30.0, seg)  # a late preview, after the plan
    (entry,) = store.library()
    assert (entry["regions_done"], entry["regions_total"]) == (1, 2)  # was 2 of 2
    store.put_transcript("v", "ja", "m", 20.0, 40.0, seg)
    assert store.library()[0]["regions_done"] == 2


def test_library_export_follows_the_plan_and_the_filters(pipeline):
    store = _state.store
    store.put_video("lib", "https://x", "t", 40.0, [[0.0, 20.0], [20.0, 40.0]])
    store.put_transcript("lib", "ja", "m", 0.0, 30.0, [_raw(1.0, 5.0, "古い計画")])  # an older plan's region 0
    store.put_transcript("lib", "ja", "m", 0.0, 20.0, [_raw(1.0, 5.0, "一行目")])
    store.put_transcript("lib", "ja", "m", 20.0, 40.0, [_raw(21.0, 25.0, "二行目"), _raw(36.0, 52.0, "三行目")])
    store.put_video("lib2", "https://x", "t", 30.0, [[0.0, 30.0]])
    store.put_transcript("lib2", "ja", "m", 0.0, 30.0, [_raw(0.0, 30.0, "字幕")])  # fills the window
    srt, n = _export.library_export("lib", "ja", "m", None)
    assert n == 3 and "古い計画" not in srt
    assert "00:00:36,000 --> 00:00:40,000" in srt  # ends with the video, not at 52 s
    assert _export.library_export("lib2", "ja", "m", None)[1] == 0


def test_download_failure_reports_error(pipeline):
    with (
        patch.object(_download, "download_full_audio", return_value=(None, "Video unavailable")),
        patch.object(_stream, "get_stream_url", return_value=None),
    ):
        snap = _run(pipeline.jobs, pipeline.jobs.create({**PARAMS, "video_id": "other"}))
    assert snap["status"] == "error" and "unavailable" in snap["error"]


def test_failed_download_streams_each_region_when_the_stream_is_reachable(pipeline):
    with (
        patch.object(_download, "download_full_audio", return_value=(None, "HTTP Error 403")),
        patch.object(_stream, "get_stream_url", return_value="https://rr1.googlevideo.com/a"),
    ):
        job = pipeline.jobs.create({**PARAMS, "video_id": "streamed", "duration": 61})
        deadline = time.time() + 10
        while not job.regions and time.time() < deadline:
            time.sleep(0.05)
    assert job.mode == "stream"
    assert job.regions == _regions.fixed_regions(61)
    assert any("streaming each region" in e["msg"] for e in job.snapshot(0, events=True)["events"])


def test_a_pasted_stream_url_is_downloaded_directly(pipeline, tmp_path):
    with patch.object(_download, "download_direct", return_value=(None, "403")) as direct:
        snap = _run(
            pipeline.jobs, pipeline.jobs.create({**PARAMS, "video_id": "pasted", "stream_url": "https://cdn/x.m3u8"})
        )
    direct.assert_called_once_with("https://cdn/x.m3u8")
    pipeline.download.assert_not_called()
    assert snap["status"] == "error" and snap["error"] == "403"


def test_an_unreadable_download_is_a_job_error(pipeline):
    with patch.object(_audio, "load_audio", side_effect=OSError("truncated WAV")):
        snap = _run(pipeline.jobs, pipeline.jobs.create({**PARAMS, "video_id": "broken"}))
    assert snap["status"] == "error" and snap["error"] == "OSError: truncated WAV"


def test_translation_server_down_does_not_burn_retries(pipeline):
    def down(*a, **k):
        raise _translate.TranslationError("translation server unreachable: refused")

    with patch.object(pipeline.translator, "translate_batch", side_effect=down):
        job = pipeline.jobs.create(PARAMS)
        deadline = time.time() + 10
        while time.time() < deadline and not any(e["level"] == "error" for e in job.events):
            pipeline.jobs.poll(job, 0)
            time.sleep(0.05)
    assert all(s["tries"] == 0 for s in job.segments.values())
    assert job.status in ("transcribing", "translating")  # waiting, lines not given up


def test_tmp_is_clean():
    # load_audio/remove_temp contract: downloads live in yume_* dirs
    d = tempfile.mkdtemp(prefix="yume_")
    p = Path(d) / "x.wav"
    p.write_bytes(b"")
    _audio.remove_temp(str(p))
    assert not Path(d).exists()


def test_translation_cache_key_changes_with_custom_prompt():
    settings = {"host": "h", "port": 1, "backend": "ollama", "model": "m", "prompt": "", "roma_prompt": ""}
    tr = _translate.Translator(lambda: settings)
    plain = tr.model_id()
    settings["prompt"] = "Translate {src} to casual {tgt}."
    assert tr.model_id() != plain
    assert tr.model_id().startswith("ollama:m:")


# ── regressions from the October audit ─────────────────────────────────────────


class _FlakyModel(_FakeModel):
    """Fails every call until `ok` is set (a CUDA hiccup, the GPU busy elsewhere)."""

    ok = False

    def transcribe(self, audio, **kw):
        if not self.ok:
            raise RuntimeError("CUDA failed")
        return super().transcribe(audio, **kw)


def test_enable_again_retries_failed_regions(pipeline):
    flaky = _FlakyModel()
    _state.model = flaky
    first = pipeline.jobs.create(PARAMS)
    snap = _run(pipeline.jobs, first)
    p = snap["progress"]
    assert p["regions_failed"] == p["regions_total"] > 0
    flaky.ok = True
    again = pipeline.jobs.create(PARAMS)  # the user presses Enable again
    assert again is not first
    snap = _run(pipeline.jobs, again)
    assert snap["progress"]["regions_failed"] == 0 and snap["progress"]["lines"] > 0


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


def test_job_request_validation():
    sys.argv = ["server"]
    import _api

    ok, err = _api._job_params(
        {"url": "https://www.youtube.com/watch?v=abc", "language": "JA", "t": "12.5", "target": "  French "}
    )
    assert err is None
    assert (ok["language"], ok["target"], ok["playhead"]) == ("ja", "French", "12.5")
    assert _api._job_params({"url": "https://youtu.be/x", "language": "auto"})[0]["language"] is None
    assert _api._job_params({})[1] == "Missing url"
    assert _api._job_params({"url": "file:///etc/passwd"})[1].startswith("Invalid URL")
    assert _api._job_params({"stream_url": "https://cdn/x.m3u8", "url": "javascript:1"})[1].startswith("Invalid URL")
    assert _api._job_params({"url": "https://youtu.be/x", "language": "japanese"})[1].startswith("Invalid language")


def test_corrupt_cache_database_is_set_aside(tmp_path):
    db = tmp_path / "c.db"
    db.write_bytes(b"this is not a sqlite database" * 100)
    st = _store.Store(db)  # must not raise
    st.put_video("v", "u", "t", 1.0, None)
    assert st.get_video("v")["title"] == "t"
    st.close()
    assert list(tmp_path.glob("c.db.corrupt-*"))


# ── regressions from the "Purple Dream" live test (Whisper tiny on a song) ─────


def test_whisper_loops_are_cut_and_hidden(pipeline):
    import _filter

    loop = "高い夢を見つけるようなパープをチェックしてみて" + "みて" * 150
    assert _filter.collapse_loops(loop) == "高い夢を見つけるようなパープをチェックしてみて"
    assert _filter.collapse_loops("そうそうそうそう") == "そうそうそうそう"  # 4 repeats can be a lyric

    job = pipeline.jobs.create(PARAMS)
    _run(pipeline.jobs, job)
    with job.lock:
        job.regions.append((500.0, 530.0))
        job.region_state.append("pending")
        lost = [{"start": 500 + i, "end": 501 + i, "text": "I don't want to lose you"} for i in range(5)]
        job.add_region(len(job.regions) - 1, [*lost, {"start": 510, "end": 512, "text": loop}])
    segs = [s for s in job.segments.values() if s["region"] == (500.0, 530.0)]
    assert all(s["hidden"] for s in segs if s["text"] == "I don't want to lose you")
    assert [s["text"] for s in segs if not s["hidden"]] == ["高い夢を見つけるようなパープをチェックしてみて"]
    pipeline.jobs.refilter()  # a blacklist edit must not bring the loop back
    assert all(s["hidden"] for s in segs if s["text"] == "I don't want to lose you")


def test_all_caps_translations_are_sentence_cased():
    assert _translate._unshout("IN THE PROVINCE", "くらさきに") == "In the province"
    assert _translate._unshout("SOMARU MACHIBAN", "SOMARU MACHIBAN") == "SOMARU MACHIBAN"  # source was caps
    assert _translate._unshout("I want you", "x") == "I want you"
    assert _translate._unshout("OK", "はい") == "OK"  # too short to be shouting


def test_live_stream_is_reported_not_recorded(monkeypatch):
    # live test (ANN news): yt-dlp's ffmpeg fails on the live HLS with a bare exit
    # code, and the get-url + ffmpeg fallback would record the stream for 15 min
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        err = (
            "[hls @ 0] Opening 'https://rr7.googlevideo.com/videoplayback/id/x.1/itag/234/source/"
            "yt_live_broadcast/live/1/seg.ts' for reading\nERROR: ffmpeg exited with code 3199971767"
        )
        return types.SimpleNamespace(returncode=1, stderr=err, stdout="")

    monkeypatch.setattr(_download.subprocess, "run", fake_run)
    monkeypatch.setattr(_stream, "get_stream_url", lambda url: pytest.fail("fallback must not run"))
    monkeypatch.setattr(_state, "youtube_auth_method", "none")
    path, err = _download.download_full_audio("https://www.youtube.com/watch?v=6PNb0Jv6qbU")
    assert path is None and err == _download.LIVE_ERROR
    assert len(calls) == 1


def test_unreadable_browser_cookies_are_skipped(monkeypatch):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        err = "ERROR: Could not copy Chrome cookie database. See https://github.com/yt-dlp/yt-dlp/issues/7271"
        if "--cookies-from-browser" not in cmd:
            err = "ERROR: [youtube] x: HTTP Error 403: Forbidden"
        return types.SimpleNamespace(returncode=1, stderr=err, stdout="")

    monkeypatch.setattr(_download.subprocess, "run", fake_run)
    monkeypatch.setattr(_ytdlp, "_cookies_failed_at", 0.0)
    monkeypatch.setattr(_stream, "get_stream_url", lambda url: None)
    monkeypatch.setattr(_state, "youtube_auth_method", "cookies")
    monkeypatch.setattr(_state, "cookies_browser", "brave")
    path, err = _download.download_full_audio("https://www.youtube.com/watch?v=0OIVIkmBdL8")
    assert path is None
    with_cookies = [c for c in calls if "--cookies-from-browser" in c]
    assert len(with_cookies) == 1  # was 6: every cookie strategy x 2 format passes
    assert "Brave" in err and "switch to browser cookies" not in err  # they are what failed
    assert _ytdlp.build_auth_args("https://www.youtube.com/watch?v=x") == []


def test_download_errors_point_to_the_settings_menu_not_the_config_file():
    # The overlay told users to edit yume_config.json by hand; the CLI has a menu for it
    for raw in (
        "ERROR: [youtube] x: Sign in to confirm you're not a bot",
        "ERROR: [youtube] x: HTTP Error 403: Forbidden",
        "ERROR: [youtube] x: This video is DRM protected",
        "ERROR: [youtube] x: age-restricted",
        "ERROR: deno: not found",
    ):
        msg = _ytdlp.friendlify_ytdlp_error(raw)
        assert msg != raw and "yume_config.json" not in msg, msg
        assert "python pocket_yume.py settings" in msg, msg


def test_a_failed_custom_stream_url_gives_advice_not_raw_output(monkeypatch, tmp_path):
    def fake_run(cmd, **kw):
        if cmd[0] == "ffmpeg":
            return types.SimpleNamespace(returncode=1, stderr="Server returned 403 Forbidden", stdout="")
        return types.SimpleNamespace(
            returncode=1, stdout="", stderr="[generic] x: Downloading\nERROR: [generic] x: HTTP Error 403: Forbidden\n"
        )

    monkeypatch.setattr(_download.subprocess, "run", fake_run)
    monkeypatch.setattr(_download.tempfile, "mkdtemp", lambda prefix: str(tmp_path))
    path, err = _download.download_direct("https://cdn.example.com/a.m3u8")
    assert path is None
    # was the raw stderr tail ("[generic] x: Downloading ERROR: …"), and the generic 403 advice
    # would say "use the Custom Stream URL option", which is what failed
    assert err.startswith("Could not download the custom stream URL ([generic] x: HTTP Error 403"), err
    assert "Downloading" not in err and "copy a fresh one" in err
    assert _download.download_direct("-o x")[1].startswith("Invalid URL")


def test_old_ytdlp_self_updates_once_a_day(tmp_path, monkeypatch):
    sys.argv = ["server"]
    import _startup

    exe = tmp_path / "yt-dlp.exe"
    exe.write_bytes(b"")
    calls = []

    def run(cmd, **kw):
        calls.append(cmd[-1])
        out = "2026.08.19\n" if cmd[-1] == "--version" else "yt-dlp is up to date"
        return types.SimpleNamespace(returncode=0, stdout=out, stderr="")

    monkeypatch.setattr(_state, "youtube_auth_method", "cookies")
    monkeypatch.setattr(_startup.shutil, "which", lambda name: str(exe))
    monkeypatch.setattr(_startup.subprocess, "run", run)
    assert _startup.maybe_update_ytdlp("2026.08.19") == "2026.08.19 (latest)"  # up to date is not a failure
    assert _startup.maybe_update_ytdlp("2026.08.19") == "2026.08.19" and calls.count("-U") == 1  # once a day
    assert _startup.maybe_update_ytdlp(time.strftime("%Y.%m.%d")) == time.strftime("%Y.%m.%d")  # recent: untouched


def test_japanese_small_tsu_doubles_the_next_consonant():
    if _romanize.get_kakasi() is None:
        pytest.skip("pykakasi not installed")
    ja = _romanize._romanize_japanese_kakasi  # the fallback when cutlet is missing
    assert ja("走って") == "hashitte"  # was "hashitsu te"
    assert ja("知った") == "shitta"
    assert ja("ちょっと待って") == "chotto matte"


@pytest.mark.parametrize(
    "line, romaji",
    [
        # pykakasi's readings and word splits, from a live test against the lyrics
        ("甘いセリフを囁いてほしいわ", "Amai serifu wo sasayaite hoshii wa"),  # was "shou itehoshiiwa"
        ("掠れた声で", "Kasureta koe de"),  # was "ryaku reta koe de"
        ("そんなひと時を", "Sonna hitotoki wo"),  # was "sonnahito toki wo"
        ("紫に染まる街は", "Murasaki ni somaru machi wa"),  # was "soma ru"
        ("痛いほど分かっていても", "Itai hodo wakatte ite mo"),  # was "waka tteitemo"
        ("ドリンクを片手にちらり", "Dorinku wo katate ni chirari"),  # katakana as read, not "Drink"
        ("窓ガラスに映る私", "Mado garasu ni utsuru watashi"),  # not the formal "watakushi"
        ("こんにちは、世界", "Konnichiwa, sekai"),
        ("走って", "Hashitte"),
        ("私は学校へ行く", "Watashi wa gakkou e iku"),
    ],
)
def test_japanese_romanization_by_word(line, romaji):
    pytest.importorskip("fugashi")
    pytest.importorskip("unidic_lite")
    assert _romanize.romanize("ja", line) == romaji


def test_vendored_cutlet_needs_no_mojimoji():
    # mojimoji has no wheels for Python 3.13+ (installing it needs a C compiler);
    # NFKC normalization does both of its conversions
    import ast

    src = Path(__file__).parent.parent / "server" / "_vendor" / "cutlet" / "cutlet.py"
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(ast.parse(src.read_text(encoding="utf-8")))
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert "mojimoji" not in imported
    assert (src.parent / "LICENSE").exists()
    pytest.importorskip("fugashi")
    from _vendor.cutlet import cutlet as vendored

    assert vendored.normalize_text("ＡＢＣ１２３ｶﾀｶﾅ") == "ABC123カタカナ"


def test_missing_server_requirements_are_reported(tmp_path, monkeypatch):
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from yume import utils

    (tmp_path / "server").mkdir()
    (tmp_path / "server" / "requirements.txt").write_text("# pins\nnumpy==2.4.3\nno-such-package-yume==1.0\n")
    monkeypatch.setattr(utils, "BASE_DIR", tmp_path)
    assert utils.missing_requirements() == ["no-such-package-yume==1.0"]


def test_region_0_waits_for_whisper_on_the_preview_not_for_its_download(pipeline):
    job = _job.Job(("v", "ja", "", "m"), {"language": "ja"})
    job.status, job.mode, job.audio = "transcribing", "stream", None
    job.regions, job.region_state = [(0.0, 25.0), (25.0, 50.0)], ["pending", "pending"]
    with pipeline.jobs.lock:
        pipeline.jobs.jobs[job.id] = job

    job.preview_started = job.preview_hearing = time.time()
    assert pipeline.jobs._pick_region() == (job, 1)  # Whisper is on region 0 already

    job.region_state[1] = "pending"
    job.preview_hearing = 0.0  # a 403, then the slow fallback: still fetching
    assert pipeline.jobs._pick_region() == (job, 0)


def test_a_preview_fetched_after_the_worker_took_region_0_is_not_transcribed(pipeline):
    job = _job.Job(("v", "ja", "", "m"), {"language": "ja"})
    job.regions, job.region_state = [(0.0, 25.0)], ["running"]
    job.preview_started = time.time()
    with pipeline.jobs.lock:
        pipeline.jobs.jobs[job.id] = job
    with (
        patch.object(_stream, "download_audio_segment", return_value="p.wav"),
        patch.object(_jobs, "transcribe_audio") as heard,
    ):
        pipeline.jobs._preview(job)
    heard.assert_not_called()
    assert job.preview_started == 0.0


def test_vocals_are_separated_after_the_preview_not_during_it(pipeline):
    job = _job.Job(("v", "ja", "", "m+htdemucs"), {"language": "ja", "url": "https://x/a"})
    job.preview_started = time.time()
    with pipeline.jobs.lock:
        pipeline.jobs.jobs[job.id] = job
    threading.Timer(0.3, lambda: setattr(job, "preview_started", 0.0)).start()
    seen = []
    with patch.object(_separate, "read_stereo", side_effect=lambda _p: seen.append(job.preview_started)):
        assert pipeline.jobs._isolate(job, "a.wav") is None
    assert seen == [0.0]


@pytest.mark.parametrize(
    ("category", "separated"),
    [("News & Politics", False), ("Education", False), ("Music", True), ("Entertainment", True), ("", True)],
)
def test_videos_filed_as_talk_are_not_separated(pipeline, category, separated):
    job = _job.Job(("v", "ar", "", "m+htdemucs"), {"language": "ar", "url": "https://x/a"})
    with (
        patch.object(_stream, "stream_category", return_value=category),
        patch.object(_separate, "read_stereo", return_value=None) as read,
    ):
        assert pipeline.jobs._isolate(job, "a.wav") is None
    assert read.called is separated


@pytest.mark.parametrize(("isolate", "looked_up"), [(True, 1), (False, 0)])
def test_a_video_with_no_preview_still_gets_its_category(pipeline, isolate, looked_up):
    # Yume starting takes ~35 s: the playhead is then past the preview's 30 s
    job = _job.Job(("v", "ar", "", "m"), {"language": "ar", "url": "https://x/a", "playhead": 40})
    job.isolate = isolate
    with patch.object(_stream, "get_stream_url") as url:
        pipeline.jobs._start_preview(job, {})
        for t in threading.enumerate():
            if t.name.startswith("preview-"):
                t.join(timeout=5)
    assert url.call_count == looked_up


def test_stream_url_keeps_the_category(monkeypatch):
    url = "https://www.youtube.com/watch?v=cat"
    monkeypatch.setattr(_state, "stream_url_cache", {})
    reply = types.SimpleNamespace(returncode=0, stdout="https://s/a\nNews & Politics\n", stderr="")
    with patch.object(_stream.subprocess, "run", return_value=reply):
        assert _stream.get_stream_url(url) == "https://s/a"
    assert _stream.stream_category(url) == "News & Politics"
    assert _stream.stream_category("https://elsewhere") == ""


def test_stream_preview_retries_without_unreadable_cookies(monkeypatch):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if "--cookies-from-browser" in cmd:
            return types.SimpleNamespace(
                returncode=1, stdout="", stderr="ERROR: Could not copy Chrome cookie database."
            )
        return types.SimpleNamespace(returncode=0, stdout="https://cdn.example/audio", stderr="")

    monkeypatch.setattr(_stream.subprocess, "run", fake_run)
    monkeypatch.setattr(_ytdlp, "_cookies_failed_at", 0.0)
    monkeypatch.setattr(_state, "youtube_auth_method", "cookies")
    monkeypatch.setattr(_state, "stream_url_cache", {})
    assert _stream.get_stream_url("https://www.youtube.com/watch?v=abc") == "https://cdn.example/audio"
    assert [("--cookies-from-browser" in c) for c in calls] == [True, False]


# ── "One more kiss" intro: Whisper tiny wrote "Azumoto-" x44 stamped 0-30 s ───


def test_line_that_is_only_a_loop_is_hidden_not_trimmed():
    import _filter

    azumoto = {"start": 0.0, "end": 30.0, "text": "Azumoto-" * 44 + "Azum"}
    real = {"start": 226.0, "end": 255.0, "text": "高い夢を見つけるようなパープをチェックしてみて" + "みて" * 150}
    segs, hide = _filter.clean_raw_segments([azumoto, real])
    assert 0 in hide  # nothing but a loop: an instrumental-intro hallucination
    assert 1 not in hide and segs[1]["text"] == "高い夢を見つけるようなパープをチェックしてみて"
    # a vocalised loop at the START does not hide the lyric that follows it
    segs, hide = _filter.clean_raw_segments([{"start": 0, "end": 5, "text": "ラ" * 30 + " 君と夢を見ていた"}])
    assert not hide and segs[0]["text"] == "ラ 君と夢を見ていた"


def test_romaji_identical_to_the_line_is_not_repeated(pipeline):
    job = pipeline.jobs.create(PARAMS)
    _run(pipeline.jobs, job)
    with job.lock:
        job.regions.append((600.0, 630.0))
        job.region_state.append("pending")
        job.add_region(len(job.regions) - 1, [{"start": 601, "end": 603, "text": "One more kiss"}])
    seg = [s for s in job.segments.values() if s["text"] == "One more kiss"][0]
    assert seg["romaji"] == ""
    # with or without the romanization libraries: nothing to do, no LLM call
    assert _romanize.romanize("ja", "Living in a dream!") == ""
    assert _romanize.romanize("ru", "Привет") == "Privet"  # Cyrillic is still romanized
    assert _romanize.romanize("ar", "مرحبا") is None  # and Arabic still goes to the LLM


def test_lines_at_the_playhead_are_translated_first_in_a_small_batch(pipeline):
    pipeline.translator.batches = []
    real = pipeline.translator.translate_batch

    def record(texts, *a, **k):
        pipeline.translator.batches.append(len(texts))
        return real(texts, *a, **k)

    pipeline.translator.translate_batch = record
    job = pipeline.jobs.create({**PARAMS, "video_id": "near"})
    _run(pipeline.jobs, job)
    assert pipeline.translator.batches[0] <= _job.NEAR_BATCH


def test_system_prompt_is_the_same_for_every_batch():
    seen = []
    tr = _translator()

    def chat(messages, max_tokens, response_format=None, temperature=0.1):
        seen.append(messages[0]["content"])
        return json.dumps({"translations": [f"x{i}" for i in range(20)]})

    tr._chat = chat
    tr.translate_batch(["あ", "い"], "ja", "English", "Title", None)
    tr.translate_batch(["う"] * 5, "ja", "English", "Title", [("a", "b")])
    assert len(set(seen)) == 1  # llama.cpp can reuse the cached prompt prefix


def test_first_translation_batch_is_small_even_when_nothing_is_near_the_playhead(pipeline):
    pipeline.translator.batches = []
    real = pipeline.translator.translate_batch

    def record(texts, *a, **k):
        pipeline.translator.batches.append(len(texts))
        return real(texts, *a, **k)

    pipeline.translator.translate_batch = record
    job = pipeline.jobs.create({**PARAMS, "video_id": "far"})
    deadline = time.time() + 20
    while time.time() < deadline and job.status not in ("done", "error"):
        pipeline.jobs.poll(job, 500)  # playhead far past every line (an instrumental intro, say)
        time.sleep(0.05)
    assert pipeline.translator.batches[0] <= _job.NEAR_BATCH


def test_japanese_particles_are_written_as_pronounced():
    if _romanize.get_kakasi() is None:
        pytest.skip("pykakasi not installed")
    ja = _romanize._romanize_japanese_kakasi  # the fallback when cutlet is missing
    assert ja("私は学校へ行く") == "watashi wa gakkou e iku"
    assert ja("本当はもっと") == "hontou wa motto"  # は glued to the next word by pykakasi
    assert ja("こんにちは") == "konnichiwa"
    # は / へ that are not particles stay as they are
    assert ja("はじめまして") == "hajimemashite"
    assert ja("ははは") == "hahaha"


def test_non_ascii_token_header_is_rejected_not_a_crash():
    sys.argv = ["server"]
    import _api

    _state.API_TOKEN = "abc"
    assert _api.token_ok("abc")
    assert not _api.token_ok("\u00e9")  # compare_digest on str raised TypeError -> HTTP 500
    assert not _api.token_ok("")


def test_translation_test_reports_any_failure_as_json(monkeypatch):
    """The health check reads its JSON; an unexpected error was an HTML 500."""
    import _api

    def boom(*a, **k):
        raise ValueError("bad JSON from the model")

    monkeypatch.setattr(_state, "jobs", types.SimpleNamespace(translator=types.SimpleNamespace(translate_batch=boom)))
    request = types.SimpleNamespace(get_json=lambda silent=False: {})
    with patch.object(_api, "request", request), patch.object(_api, "jsonify", lambda d: d):
        body, code = _api.translation_test()
    assert code == 502 and body == {"success": False, "error": "ValueError: bad JSON from the model"}


def _switch(body):
    """POST /model/switch → (status code, json), past the token check. Works
    with real Flask and with the stub test_server_modules installs."""
    import _api

    request = types.SimpleNamespace(get_json=lambda silent=False: body)
    with patch.object(_api, "request", request), patch.object(_api, "jsonify", lambda d: d):
        resp = _api.switch_model()
    return resp[::-1] if isinstance(resp, tuple) else (200, resp)


def test_model_switch_rejects_bad_requests(monkeypatch, tmp_path):
    monkeypatch.setattr(_state, "model_name", "large-v3-turbo")
    assert _switch({}) == (400, {"error": "Missing 'model' field"})
    code, out = _switch({"model": "medium.en"})  # English-only: useless for Yume
    assert code == 400 and "large-v3" in out["valid"]
    assert _switch({"model": "turbo"}) == (200, {"status": "already_loaded", "model": "large-v3-turbo"})
    assert _switch({"model": "models/mine"})[0] == 400  # relative
    assert "'..'" in _switch({"model": str(tmp_path / ".." / "x")})[1]["error"]
    assert "not found" in _switch({"model": str(tmp_path / "none")})[1]["error"]
    (tmp_path / "model.bin").write_bytes(b"")
    assert "config.json" in _switch({"model": str(tmp_path)})[1]["error"]


def test_model_switch_swaps_or_keeps_the_old_model(monkeypatch, tmp_path):
    import _api

    monkeypatch.setattr(_state, "model_name", "small")
    monkeypatch.setattr(_state, "model", "old-whisper")
    monkeypatch.setattr(_state, "jobs", types.SimpleNamespace(drop_all=lambda why: dropped.append(why)))
    dropped, saved = [], []
    monkeypatch.setattr(_api._settings, "persist_whisper_model", saved.append)
    for f in ("model.bin", "config.json"):
        (tmp_path / f).write_bytes(b"")

    with patch.object(_api._model, "load_whisper", side_effect=RuntimeError("CUDA out of memory")):
        code, out = _switch({"model": "large-v3"})
    assert code == 500 and out["model"] == "small" and _state.model == "old-whisper" and not saved

    _state.model_switch_lock.acquire()
    try:
        assert _switch({"model": "large-v3"})[0] == 409  # one switch at a time
    finally:
        _state.model_switch_lock.release()

    with patch.object(_api._model, "load_whisper", return_value="new-whisper"):
        code, out = _switch({"model": str(tmp_path)})
    assert (code, out) == (200, {"status": "ok", "model": str(tmp_path.resolve()), "previous": "small"})
    assert _state.model == "new-whisper" and saved == [str(tmp_path.resolve())] and len(dropped) == 1


def test_cookie_hint_only_where_cookies_could_help(monkeypatch):
    def fake_run(cmd, **kw):
        err = "ERROR: Could not copy Chrome cookie database."
        if "--cookies-from-browser" not in cmd:
            err = "ERROR: [youtube] x: Video unavailable"
        return types.SimpleNamespace(returncode=1, stderr=err, stdout="")

    monkeypatch.setattr(_download.subprocess, "run", fake_run)
    monkeypatch.setattr(_ytdlp, "_cookies_failed_at", 0.0)
    monkeypatch.setattr(_stream, "get_stream_url", lambda url: None)
    monkeypatch.setattr(_state, "youtube_auth_method", "cookies")
    _path, err = _download.download_full_audio("https://www.youtube.com/watch?v=gone")
    assert "unavailable" in err and "cookies" not in err.lower()


def test_audio_that_is_not_16k_mono_is_converted_by_ffmpeg(tmp_path):
    # The stereo downloads for vocal isolation. faster-whisper's decode_audio
    # fails with PyAV 19 (TypeError), so Yume converts with ffmpeg itself.
    import shutil
    import wave

    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not installed")
    path = tmp_path / "stereo.wav"
    tone = (0.25 * np.sin(2 * np.pi * 440 * np.arange(44100) / 44100) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(44100)
        w.writeframes(np.repeat(tone, 2).tobytes())
    audio = _audio.load_audio(str(path))
    assert audio.dtype == np.float32 and abs(len(audio) - 16000) <= 32
    assert 0.2 < np.abs(audio).max() < 0.3
    with pytest.raises(RuntimeError, match="ffmpeg could not read"):
        _audio.load_audio(str(tmp_path / "missing.wav"))


# ── regressions from the October multilingual live run ──────────────────────


@pytest.mark.parametrize(
    ("zh", "pinyin"),
    [
        ("弹着琴唱你爱的歌", "tán zhe qín chàng nǐ ài de gē"),  # was "dàn" (a Jay Chou line)
        ("为什么不好好读书", "wèi shén me bù hǎo hǎo dú shū"),  # was "hǎo hào"
        ("教室裡的燈還亮著", "jiào shì lǐ de dēng hái liàng zhe"),  # was "zhù"
        ("到現在我還在寫著", "dào xiàn zài wǒ hái zài xiě zhe"),
        ("这条路很长", "zhè tiáo lù hěn cháng"),  # was "zhǎng"
        ("他长得很高", "tā zhǎng de hěn gāo"),  # was "dé"
        ("著名的歌手", "zhù míng de gē shǒu"),  # 著 that is not "-ing" stays zhù
        ("我长大了", "wǒ zhǎng dà le"),
        ("不好意思", "bù hǎo yì sī"),
        # AA地 is the adverb "-ly" (de), not 地 "earth" (dì)
        ("慢慢地走", "màn màn de zǒu"),  # was "dì"
        ("轻轻地说", "qīng qīng de shuō"),
        ("靜靜地看著你", "jìng jìng de kàn zhe nǐ"),
        ("静悄悄地", "jìng qiāo qiāo de"),
        ("大地", "dà dì"),  # the earth stays dì
        ("小小地方", "xiǎo xiǎo dì fāng"),  # a small place: no adverb here
        # 只 after a number counts animals (zhī), it is not "only" (zhǐ)
        ("两只老虎", "liǎng zhī lǎo hǔ"),  # was "zhǐ"
        ("三只小猫", "sān zhī xiǎo māo"),
        ("只有你", "zhǐ yǒu nǐ"),
        # 重 + verb: "again" (chóng), not "heavy" (zhòng)
        ("我们重来", "wǒ men chóng lái"),  # was "zhòng"
        ("重回那一天", "chóng huí nà yī tiān"),
        ("重拾回忆", "chóng shí huí yì"),
        ("很重", "hěn zhòng"),
        ("重重地摔", "zhòng zhòng de shuāi"),  # "heavily" (was "chóng chóng dì")
        # 数 as a verb: counting (shǔ)
        ("数着星星", "shǔ zhe xīng xīng"),  # was "shù"
        ("數著日子", "shǔ zhe rì zi"),  # was "shù zhù"
        ("无数", "wú shù"),
        # verb + 得 + how: de, not dé ("to obtain")
        ("你唱得很好", "nǐ chàng de hěn hǎo"),  # was "dé"
        ("只要你过得比我好", "zhǐ yào nǐ guò de bǐ wǒ hǎo"),
        ("距離睡得好遠", "jù lí shuì de hǎo yuǎn"),  # a cached Chinese song line
        ("舍不得你", "shě bù de nǐ"),
        ("他获得了冠军", "tā huò dé le guàn jūn"),  # 获得 "to win" stays dé
        ("不得不说再见", "bù dé bù shuō zài jiàn"),
        # 不着: can't manage to (zháo)
        ("晚上睡不着觉", "wǎn shàng shuì bù zháo jiào"),  # was "zhe jué"
        ("我找不着方向", "wǒ zhǎo bù zháo fāng xiàng"),
        ("他已经睡着了", "tā yǐ jīng shuì zháo le"),
        # 还给 "give back" (huán); 一行行 "line after line" (háng); 樂 "music" (yuè)
        ("還給你", "huán gěi nǐ"),  # was "hái"
        ("我还爱着你", "wǒ hái ài zhe nǐ"),
        ("一行行泪", "yī háng háng lèi"),  # was "xíng"
        ("一行人走进来", "yī xíng rén zǒu jìn lái"),  # a group of people: xíng
        ("音樂響起", "yīn yuè xiǎng qǐ"),  # was "lè"
        ("快樂", "kuài lè"),
    ],
)
def test_chinese_heteronyms_are_read_as_meant(zh, pinyin):
    pytest.importorskip("pypinyin")
    assert _romanize.romanize("zh", zh) == pinyin


@pytest.mark.parametrize(
    ("ja", "romaji"),
    [
        ("去年3月、東京", "Kyonen 3 gatsu, Tokyo"),  # was "3 tsuki"
        ("2025年12月25日", "2025 nen 12 gatsu 25 nichi"),  # was "25ka"
        ("7月1日に", "7 gatsu tsuitachi ni"),
        ("1日", "Ichinichi"),
        ("3日間", "Mikkakan"),  # was "3 kakan"
        ("1人で", "Hitori de"),  # was "1nin de"
        ("2人", "Futari"),
        ("3人が", "3 nin ga"),
        ("20日", "Hatsuka"),
        ("第2回の会議", "Dai 2 kai no kaigi"),  # was "Dai2 kai"
        ("20歳", "20 sai"),  # was "20sai"
        ("第1位", "Dai 1 i"),  # was "Dai1i"
        ("第3話", "Dai 3 wa"),  # was "Dai3 hanashi"
        ("MP3プレーヤー", "MP3 pureeyaa"),  # Latin words with digits stay whole
        ("4Kテレビ", "4K terebi"),
        ("この3つが大事", "Kono mittsu ga daiji"),  # was "3tsu"
        ("4つ目かな", "Yottsu me ka na"),
        ("1個", "1 ko"),  # was "1ko"
        # Counters whose sound changes with the number: MeCab read them one way
        ("1本", "1 pon"),
        ("3本の花", "3 bon no hana"),  # was "3 pon"
        ("2本", "2 hon"),  # was "2 pon"
        ("10分", "10 pun"),  # was "10 fun"
        ("3分間", "3 punkan"),  # was "3 fun aida"
        ("5分", "5 fun"),
        ("3分の1", "3 bun no 1"),  # a third: 分 is not minutes here
        ("1杯", "1 pai"),  # was "1 bai"
        ("4杯", "4 hai"),
        ("3匹", "3 biki"),  # was "3 hiki"
        ("6匹の猫", "6 piki no neko"),
        ("3軒目の店", "3 gen me no mise"),  # was "3 kenme"
        ("1軒家に住む", "1 kenya ni sumu"),  # a house (ikkenya): not the counter
        ("1歩ずつ前へ", "1 po zutsu mae e"),  # was "1 ho"
        ("3歩下がって", "3 po sagatte"),
        ("3足の靴を買った", "3 zoku no kutsu wo katta"),  # pairs of shoes, was "3 ashi"
        ("1足す1は2", "1 tasu 1 wa 2"),  # 足す "to add": not the counter
        ("菅義偉首相が26日", "Suga Yoshihide shushou ga 26 nichi"),  # was "Kan Yoshihide"
        ("菅官房長官は", "Suga kanbou choukan wa"),
        ("菅直人元首相", "Kan Naoto moto shushou"),  # the other 菅: still Kan
        ("菅田将暉", "Suda Masaki"),
        ("菅野美穂", "Kanno Miho"),
    ],
)
def test_japanese_dates_and_people_are_read_correctly(ja, romaji):
    if _romanize.get_cutlet() is None:
        pytest.skip("cutlet (fugashi + unidic-lite) not installed")
    assert _romanize.romanize("ja", ja) == romaji


def _seg(start, end, text):
    return {"start": start, "end": end, "text": text}


def test_cut_off_copies_of_the_next_line_are_hidden():
    import _filter

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
    import _filter

    out, hide = _filter.clean_raw_segments(
        [
            _seg(95.3, 98.0, "而它涉及其他技術的措施"),
            _seg(98.0, 101.9, "而它涉及其他技術的措施"),
            _seg(101.9, 104.9, "但也可使用了"),
        ]
    )
    assert hide == {1} and out[0]["end"] == 101.9


def test_a_fragment_at_a_region_cut_is_hidden_whichever_region_finishes_first(pipeline):
    for order in ((0, 1), (1, 0)):
        job = _job.Job(("frag", "ko", "", "m"), {"language": "ko"})
        job.regions, job.region_state = [(0.0, 28.8), (28.8, 55.0)], ["pending", "pending"]
        raw = {
            0: [_seg(20.0, 24.0, "주요 뉴스입니다."), _seg(28.5, 28.8, "호주 최고한")],
            1: [_seg(28.8, 36.0, "호주 최고 안보기관인 AGO가 경고하면서"), _seg(36.0, 41.9, "주장했습니다.")],
        }
        for idx in order:
            job.add_region(idx, raw[idx])
        hidden = [s["text"] for s in job.segments.values() if s["hidden"]]
        assert hidden == ["호주 최고한"], order


def test_copied_translations_are_retried_line_by_line():
    tr = _translator()
    batch = {"translations": ["These men were arrested.", "These men were arrested.", "Chorus", "Chorus"]}
    singles = []

    def chat(messages, max_tokens, response_format=None, temperature=0.1):
        if response_format:
            return json.dumps(batch)
        singles.append(messages[-1]["content"])
        return "single"

    tr._chat = chat
    out = tr.translate_batch(["이들은 또한", "5대의 차량과", "사랑해", "사랑해"], "ko", "English")
    assert out == ["single", "single", "Chorus", "Chorus"]
    assert len(singles) == 2  # the chorus (the same line twice) may share a translation


def test_english_lines_are_not_sent_for_an_english_translation():
    tr = _translator()
    sent = []

    def chat(messages, max_tokens, response_format=None, temperature=0.1):
        sent.append(messages[-1]["content"])
        return json.dumps({"translations": ["I love you"]})

    tr._chat = chat
    out = tr.translate_batch(["One more kiss", "愛してる", "deeds you could do, don't they?"], "ja", "English")
    assert out == ["", "I love you", ""]
    assert len(sent) == 1 and "One more kiss" not in sent[0]
    assert tr.translate_batch(["Hello"], "ja", "English") == [""]  # no request at all
    assert len(sent) == 1


def test_each_line_is_sent_with_its_reading_for_names():
    tr = _translator()
    sent = []

    def chat(messages, max_tokens, response_format=None, temperature=0.1):
        sent.append(messages[-1]["content"])
        return json.dumps({"translations": ["NHK chairman Inoue said", "it is deeply regrettable."]})

    tr._chat = chat
    out = tr.translate_batch(
        ["NHKの井上会長は、", "痛恨の極みだ。"], "ja", "English", readings=["NHK no Inoue kaichou wa,", ""]
    )
    assert out == ["NHK chairman Inoue said", "it is deeply regrettable."]
    assert "1. NHKの井上会長は、 (reading: NHK no Inoue kaichou wa,)" in sent[0]
    assert "2. 痛恨の極みだ。\n" in sent[0] + "\n"  # no reading: the line alone


def test_a_copied_reading_is_retried_without_it_and_never_kept():
    tr = _translator()
    calls = []

    def chat(messages, max_tokens, response_format=None, temperature=0.1):
        calls.append(messages[-1]["content"])
        if response_format:  # the batch: the model copied the readings
            return json.dumps({"translations": ["Iradachi wo arawa ni shita", "Hoka no hou yoroshii desu ka?"]})
        return "He showed his irritation." if "苛立ち" in messages[-1]["content"] else "hoka no hou yoroshii desu ka"

    tr._chat = chat
    readings = ["Iradachi wo arawa ni shita", "Hoka no hou yoroshii desu ka?"]
    out = tr.translate_batch(["苛立ちをあらわにした", "他の方よろしいですか?"], "ja", "English", readings=readings)
    assert out == ["He showed his irritation.", ""]  # the second copied it again: original only
    assert all("reading" not in c for c in calls[1:])  # retries go without the reading


def test_jobs_send_readings_only_where_names_are_hard_to_read(pipeline):
    seen = []
    real = pipeline.translator.translate_batch

    def record(texts, src, tgt, title="", context=None, readings=None):
        seen.append(readings)
        return real(texts, src, tgt, title, context, readings)

    pipeline.translator.translate_batch = record
    # CI has no Japanese romanizer installed: give every line a reading
    with patch.object(_romanize, "romanize", lambda lang, text: "konnichiwa"):
        _run(pipeline.jobs, pipeline.jobs.create(PARAMS))
    assert seen and all(r and all(r) for r in seen)  # Japanese: every line's romaji
    seen.clear()
    _run(pipeline.jobs, pipeline.jobs.create({**PARAMS, "video_id": "ru1", "language": "ru"}))
    assert seen and all(r is None for r in seen)  # Cyrillic reads fine: no readings


def test_an_unexpected_translation_error_does_not_stop_the_worker(pipeline):
    # Only TranslationError was caught: any other exception ended the LLM thread
    # and every job stayed "translating" for good
    real = pipeline.translator.translate_batch
    calls = []

    def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise ValueError("unexpected reply")
        return real(*args, **kwargs)

    pipeline.translator.translate_batch = flaky
    job = pipeline.jobs.create({**PARAMS, "video_id": "flaky"})
    deadline = time.time() + 20
    while time.time() < deadline and job.status != "done":
        pipeline.jobs.poll(job, 0)
        with job.lock:
            job.llm_backoff_until = 0  # skip the 10 s back-off
        time.sleep(0.05)
    assert job.status == "done" and len(calls) > 1
    assert any("Translation failed: unexpected reply" in e["msg"] for e in job.snapshot(0, events=True)["events"])


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


def test_language_is_checked_while_transcribing_and_shown_in_the_snapshot(pipeline):
    with patch.object(_jobs, "detect_language", return_value=("en", 0.97)) as det:
        snap = _run(pipeline.jobs, pipeline.jobs.create({**PARAMS, "video_id": "wrong-lang"}))
    assert "sounds like English, but Yume is set to Japanese" in snap["notice"]
    assert det.call_count <= _job.LANG_CHECKS
    with patch.object(_jobs, "detect_language", return_value=("ja", 0.99)) as det:
        snap = _run(pipeline.jobs, pipeline.jobs.create({**PARAMS, "video_id": "right-lang"}))
    assert snap["notice"] == "" and det.call_count == 1
    with patch.object(_jobs, "detect_language") as det:  # auto-detect: nothing to check
        _run(pipeline.jobs, pipeline.jobs.create({**PARAMS, "video_id": "auto", "language": None}))
    det.assert_not_called()


def test_a_loop_of_a_long_phrase_is_cut():
    import _filter

    line = "هل تتحدث عن ماذا يمكن أن يتحدث " + "عن ماذا يتحدث " * 30  # 14-char unit: was let through
    out, hide = _filter.clean_raw_segments([_seg(50.0, 77.5, line)])
    assert out[0]["text"] == "هل تتحدث عن ماذا يمكن أن يتحدث عن ماذا يتحدث" and not hide


# ── songs live test (KO/ZH/RU/AR/JA songs on large-v3-turbo + Qwen2.5 7B) ────


def test_a_sung_stutter_is_a_lyric_not_a_loop():
    import _filter

    lines = ["あたし大大大大大大嫌い", "カイカイカイカイカイはない", "ダイダイダイダイダイキライ"]  # "ダイダイ…キライ"
    out, hide = _filter.clean_raw_segments([_seg(i * 2.0, i * 2.0 + 2.0, t) for i, t in enumerate(lines)])
    assert [s["text"] for s in out] == lines and not hide


def test_thanks_for_watching_in_chinese_is_hidden():
    for line in ("谢谢观看", "謝謝觀看", "谢谢收看"):
        assert _lines.is_unwanted(line), line
    assert not _lines.is_unwanted("谢谢你")  # "thank you" in a lyric


def test_a_line_heard_by_both_regions_at_a_cut_is_shown_once(pipeline):
    line = "وظننت أنا أني بذلك حست غنى"
    for order in ((0, 1), (1, 0)):
        job = _job.Job(("twice", "ar", "", "m"), {"language": "ar"})
        job.regions, job.region_state = [(0.0, 20.4), (20.4, 43.2)], ["pending", "pending"]
        raw = {
            0: [_seg(14.3, 19.5, "فبدوت شخصا آخر كي أتفخر"), _seg(19.5, 24.8, line)],  # cut at 20.4
            1: [_seg(20.4, 24.8, line), _seg(24.8, 30.4, "فوجدت أني خاسر فتلك مظاهر")],
        }
        for idx in order:
            job.add_region(idx, raw[idx])
        shown = [(s["start"], s["end"], s["text"]) for s in job.segments.values() if not s["hidden"]]
        assert sorted(shown)[1] == (19.5, 24.8, line), order
        assert [t for _s, _e, t in shown].count(line) == 1, order


LONG = (
    "Я помню чудное мгновенье, передо мной явилась ты, как мимолётное виденье, "
    "как гений чистой красоты, в томленьях грусти безнадежной, в тревогах шумной суеты."
)


def test_a_long_line_heard_by_both_regions_at_a_cut_is_shown_once(pipeline):
    # Too long to read in one go, so each region cuts it into pieces: region 0
    # holds 8.4 s of it (until the cut), region 1 the whole 13.6 s. The pieces
    # are timed differently, so the last piece of one region never equalled the
    # first piece of the next, and the whole line was shown twice.
    for order in ((0, 1), (1, 0)):
        job = _job.Job(("long", "ru", "", "m"), {"language": "ru"})
        job.regions, job.region_state = [(0.0, 20.4), (20.4, 50.0)], ["pending", "pending"]
        raw = {
            0: [_seg(3.0, 11.0, "Я помню."), _seg(12.0, 26.0, LONG)],
            1: [_seg(20.4, 34.0, LONG), _seg(35.0, 40.0, "Шли годы.")],
        }
        for idx in order:
            job.add_region(idx, raw[idx])
        shown = sorted((s["start"], s["end"], s["text"]) for s in job.segments.values() if not s["hidden"])
        assert len(shown) > 3, "the long line is cut into pieces"
        assert " ".join(t for _s, _e, t in shown[1:-1]) == LONG, order  # once, whole
        # Its pieces now span both regions' share of it, one after the other
        assert shown[1][0] == 12.0 and shown[-2][1] == 34.0, order
        assert all(a[1] == b[0] for a, b in zip(shown[1:-1], shown[2:-1])), order


def test_library_export_shows_cached_translations_as_a_live_job_does(pipeline):
    store = _state.store
    store.put_video("caps", "u", "t", 30.0, [[0.0, 30.0]])
    store.put_transcript("caps", "ja", "m", 0.0, 30.0, [_raw(1.0, 2.0, "くらさきに")])
    store.put_translation("ja", "English", "old-model", "くらさきに", "IN THE PROVINCE")  # cached before _unshout
    srt, n = _export.library_export("caps", "ja", "m", "English")
    assert n == 1 and "In the province" in srt and "IN THE PROVINCE" not in srt


def test_library_export_shows_a_long_line_heard_by_both_regions_once(pipeline):
    store = _state.store
    store.put_video("long", "https://x", "t", 50.0, [[0.0, 20.4], [20.4, 50.0]])
    store.put_transcript("long", "ru", "m", 0.0, 20.4, [_raw(12.0, 26.0, LONG)])
    store.put_transcript("long", "ru", "m", 20.4, 50.0, [_raw(20.4, 34.0, LONG), _raw(35.0, 40.0, "Шли годы.")])
    srt, n = _export.library_export("long", "ru", "m", None)
    assert srt.count("Я помню чудное мгновенье") == 1, srt
    assert "00:00:12,000 -->" in srt and "--> 00:00:34,000" in srt


def test_a_long_line_is_kept_in_the_pieces_of_the_region_that_held_more_of_it(pipeline):
    # Region 0 holds 2.5 s of the line: one 157-column piece. Region 1 holds
    # 19.5 s and cuts it in two: those two are kept, over both regions' share.
    for order in ((0, 1), (1, 0)):
        job = _job.Job(("more", "ru", "", "m"), {"language": "ru"})
        job.regions, job.region_state = [(0.0, 14.5), (14.5, 50.0)], ["pending", "pending"]
        raw = {
            0: [_seg(3.0, 11.0, "Я помню."), _seg(12.0, 26.0, LONG)],
            1: [_seg(14.5, 34.0, LONG), _seg(35.0, 40.0, "Шли годы.")],
        }
        for idx in order:
            job.add_region(idx, raw[idx])
        shown = sorted((s["start"], s["end"], s["text"], s["region"]) for s in job.segments.values() if not s["hidden"])
        pieces = shown[1:-1]
        assert len(pieces) == 2 and {p[3] for p in pieces} == {(14.5, 50.0)}, order
        assert " ".join(p[2] for p in pieces) == LONG, order
        assert pieces[0][0] == 12.0 and pieces[0][1] == pieces[1][0] and pieces[1][1] == 34.0, order


TAIL_LEFT = [
    _seg(16.76, 19.18, "싱가포르에 있는 국내 프랜차이즈 빵집"),
    _seg(19.48, 23.82, "매장입니다. 제품 안내판에 한글로 된 로고가 보입니다."),  # stamped past the cut at 20.7
]
TAIL_RIGHT = [
    _seg(20.7, 23.7, "제품 안내판에 한글로 된 로고가 보입니다."),
    _seg(24.0, 29.5, "지난 2월 판매를 시작했습니다."),
]


def test_the_end_of_a_line_heard_again_after_the_cut_is_shown_once(pipeline):
    # SBS Korean news: region 0 wrote its last line on past the cut, region 1
    # wrote what it heard after the cut; both were shown
    for order in ((0, 1), (1, 0)):
        job = _job.Job(("tail", "ko", "", "m"), {"language": "ko"})
        job.regions, job.region_state = [(0.0, 20.7), (20.7, 45.0)], ["pending", "pending"]
        for idx in order:
            job.add_region(idx, (TAIL_LEFT, TAIL_RIGHT)[idx])
        shown = sorted((s["start"], s["end"], s["text"]) for s in job.segments.values() if not s["hidden"])
        assert shown[1] == (19.48, 23.7, TAIL_LEFT[1]["text"]), order
        assert len(shown) == 3, order


@pytest.mark.parametrize(
    ("left", "right"),
    [
        (
            _seg(19.48, 20.5, "매장입니다. 제품 안내판에 한글로 된 로고가 보입니다."),
            TAIL_RIGHT[0],
        ),  # ends before the cut
        (_seg(17.0, 22.0, "너를 사랑해 사랑해"), _seg(20.7, 22.0, "사랑해")),  # a short end is sung again
    ],
)
def test_a_line_that_only_ends_like_the_next_one_is_kept(pipeline, left, right):
    job = _job.Job(("tail", "ko", "", "m"), {"language": "ko"})
    job.regions, job.region_state = [(0.0, 20.7), (20.7, 45.0)], ["pending", "pending"]
    job.add_region(0, [left])
    job.add_region(1, [right])
    assert not any(s["hidden"] for s in job.segments.values())


def test_library_export_shows_the_end_of_a_line_heard_again_once(pipeline):
    store = _state.store
    store.put_video("tail", "https://x", "t", 45.0, [[0.0, 20.7], [20.7, 45.0]])
    store.put_transcript("tail", "ko", "m", 0.0, 20.7, [_raw(s["start"], s["end"], s["text"]) for s in TAIL_LEFT])
    store.put_transcript("tail", "ko", "m", 20.7, 45.0, [_raw(s["start"], s["end"], s["text"]) for s in TAIL_RIGHT])
    srt, n = _export.library_export("tail", "ko", "m", None)
    assert n == 3 and srt.count("로고가 보입니다") == 1
    assert "00:00:19,480 --> 00:00:23,700" in srt


def test_library_export_shows_a_line_heard_by_both_regions_once(pipeline):
    store = _state.store
    line = "وظننت أنا أني بذلك حست غنى"
    store.put_video("twice", "https://x", "t", 43.2, [[0.0, 20.4], [20.4, 43.2]])
    store.put_transcript("twice", "ar", "m", 0.0, 20.4, [_raw(19.5, 24.8, line)])
    store.put_transcript("twice", "ar", "m", 20.4, 43.2, [_raw(20.4, 24.8, line), _raw(24.8, 30.4, "مظاهر")])
    srt, n = _export.library_export("twice", "ar", "m", None)
    assert n == 2 and srt.count(line) == 1
    assert "00:00:19,500 --> 00:00:24,800" in srt


RAP = (
    "Наша любовь как будто открыла ват портал, и меня замотала, и тебя замотала, ты попал, малыш, и я попала, "
    "но зачем ты прикипела, я предупреждала, я тебе ведь говорила, мы слишком разные, я хочу уют в дом, "
    "ты хочешь праздновать, как не подгоняю вы, не сойдутся пазлы, снова в ту же реку, по новой лесте пасла, "
    "я люблю сериал о деньги, тяжелый люкс, люблю искренность бога, и тебя люблю."
)


def test_a_line_too_long_to_read_is_cut_into_timed_pieces():
    import _filter

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
    import _filter

    assert len(_filter.split_long(_seg(0.0, 9.2, "Пусть отболит за ночь, пока ты там совсем не ждёшь, я войду"))) == 1
    assert len(_filter.split_long(_seg(0.0, 3.0, RAP))) == 1  # 3 s: no piece would be readable
    no_break = "而AI的網絡" * 10  # nowhere to cut but inside a word
    assert len(_filter.split_long(_seg(0.0, 20.0, no_break))) == 1


def test_a_long_line_reaches_the_job_in_pieces_each_judged_by_the_filter(pipeline):
    job = _job.Job(("rap", "ru", "", "m"), {"language": "ru"})
    job.regions, job.region_state = [(101.75, 123.3)], ["pending"]
    job.add_region(0, [_seg(101.75, 123.2, RAP)])
    assert len(job.segments) == 5 and not any(s["hidden"] for s in job.segments.values())

    job = _job.Job(("credits", "ru", "", "m"), {"language": "ru"})
    job.regions, job.region_state = [(0.0, 30.0)], ["pending"]
    job.add_region(0, [_seg(0.0, 25.0, "Субтитры создавал DimaTorzok. " + RAP)])
    hidden = [s["text"] for s in job.segments.values() if s["hidden"]]
    assert len(hidden) == 1 and hidden[0].startswith("Субтитры") and len(job.segments) > 2
    # a blacklist edit hides or shows the same pieces a fresh job would have
    with patch.object(_state, "user_blacklist", ["искренность"]):
        pipeline.jobs.jobs[job.id] = job
        pipeline.jobs.refilter()
    assert sum(s["hidden"] for s in job.segments.values()) == 2


def _picker(*jobs):
    """A JobManager with these jobs and no worker threads."""
    mgr = object.__new__(_jobs.JobManager)
    mgr.jobs, mgr.lock = {j.id: j for j in jobs}, threading.Lock()
    return mgr


def _llm_job(params, lines, playhead=0.0):
    job = _job.Job(("pick", "ja", "", "m"), {"language": "ja", **params, "playhead": playhead})
    for i, (start, translation, romaji) in enumerate(lines):
        job.segments[i] = {
            "id": i, "start": start, "end": start + 2, "text": f"行{i}", "hidden": False,
            "translation": translation, "romaji": romaji,
        }  # fmt: skip
    return job


def test_the_llm_works_on_what_is_on_screen_first():
    starts = [5.0 * i for i in range(30)]
    # a job's very first lines go in a small batch, from the playhead on
    job = _llm_job({"target": "English"}, [(s, None, "r") for s in starts], playhead=60.0)
    _j, kind, batch = _picker(job)._pick_llm_batch()
    assert kind == "translate" and [s["start"] for s in batch] == [60.0, 65.0, 70.0]
    # once something is translated: the lines due soon, still in a small batch...
    job.segments[0]["translation"] = "done"
    _j, kind, batch = _picker(job)._pick_llm_batch()
    assert [s["start"] for s in batch] == [60.0, 65.0, 70.0]
    # ...and with nothing due soon, a full batch, then the lines behind the playhead
    job.playhead = 200.0
    _j, kind, batch = _picker(job)._pick_llm_batch()
    assert len(batch) == _job.BATCH_SIZE and batch[0]["start"] == 5.0
    for s in job.segments.values():
        s["translation"] = s["translation"] or "x"
    # translations done: LLM romanization (Arabic, or no romanizer) when asked for
    job.romanize, job.segments[3]["romaji"] = True, None
    assert _picker(job)._pick_llm_batch()[1:] == ("romanize", [job.segments[3]])
    job.romanize = False
    assert _picker(job)._pick_llm_batch() == (None, None, None)


def test_the_llm_skips_unwatched_and_backed_off_jobs():
    idle = _llm_job({"target": "English"}, [(0.0, None, "r")])
    idle.last_poll -= _jobs.ACTIVE_S + 1
    waiting = _llm_job({"target": "English"}, [(0.0, None, "r")])
    waiting.llm_backoff_until = time.time() + 30
    watched = _llm_job({"target": "English"}, [(0.0, None, "r")])
    assert _picker(idle, waiting, watched)._pick_llm_batch()[0] is watched
    assert _picker(idle, waiting)._pick_llm_batch() == (None, None, None)


def test_a_japanese_middle_dot_is_a_space_in_romaji():
    if _romanize.get_cutlet() is None:
        pytest.skip("cutlet (fugashi + unidic-lite) not installed")
    assert _romanize.romanize("ja", "ジョン・レノン") == "Jon Renon"  # was "Jon/Renon"


# ── live run 2026-10-30: raw JSON shown as subtitles (14 of 75 Arabic lines) ───

JSON_REPLY = '{"translations": ["[1] It targeted reinforcements",\n"[2] And the children",\n"[3] and the hospital"]'


@pytest.mark.parametrize(
    "reply",
    [JSON_REPLY, JSON_REPLY + "}", "```json\n" + JSON_REPLY + "}\n```"],
    ids=["no closing brace", "complete", "fenced"],
)
def test_a_numbered_reply_that_is_really_json_is_read_as_json(reply):
    out = _translate.parse_numbered(reply, 3)
    assert out == ["It targeted reinforcements", "And the children", "and the hospital"]


def test_json_cut_off_in_a_numbered_reply_never_becomes_a_subtitle():
    out = _translate.parse_numbered('{"translations": ["[1] One", "[2] Tw', 3)
    assert out == ["One", None, None]  # the rest is retried line by line
    assert _translate.parse_numbered("[1] One\n[2] Two", 2) == ["One", "Two"]  # plain lists still work
    assert _translate.parse_numbered("One\nTwo", 2) == ["One", "Two"]


@pytest.mark.parametrize(
    "line, read_as",
    [
        ("it\\'s fine", "it's fine"),  # JSON has no \'
        ("one\ntwo", "one\ntwo"),  # a raw newline inside a string
        ('a\\"b', 'a"b'),
        ("a\\u12", "a\\u12"),  # cut short
    ],
    ids=["bad escape", "raw newline", "good escape", "short unicode escape"],
)
def test_a_broken_json_reply_keeps_its_lines(line, read_as):
    """One such line used to fail the whole batch ("unparseable plain output")."""
    assert _translate.parse_numbered(f'{{"translations": ["{line}", "next"', 2) == [read_as, "next"]


def test_a_failed_single_line_retry_keeps_the_batch_line():
    tr = _translator()
    with patch.object(tr, "_single", side_effect=_translate.TranslationError("HTTP 500: x")):
        assert tr._checked("拉斯", "smile in拉斯", "", "ja", "English") == "smile in"
        assert tr._checked("拉斯", None, "", "ja", "English") == ""


def test_a_garbled_first_reply_does_not_leave_the_endpoint_on_plain():
    """The first batch after a start, cut off at max_tokens, walked down to "plain"
    and remembered it: every later batch was asked for lines, and answered in JSON."""
    tr = _translator()
    replies = iter(['{"translations": ["cut', '{"translations": ["cut'])
    seen = []

    def chat(messages, max_tokens, response_format=None, temperature=0.1):
        seen.append(response_format["type"] if response_format else "plain")
        if response_format:
            return next(replies, json.dumps({"translations": ["Later"]}))
        return "[1] Plain"

    with patch.object(tr, "_chat", side_effect=chat):
        assert tr.translate_batch(["あ"], "ja", "English") == ["Plain"]
        assert tr.translate_batch(["い"], "ja", "English") == ["Later"]
    assert seen == ["json_schema", "json_object", "plain", "json_schema"]


@pytest.mark.parametrize(
    "backend, url, reply, models",
    [
        (
            "ollama",
            "http://127.0.0.1:5000/api/tags",
            {"models": [{"name": "qwen3", "size": 7}, {"name": "gemma"}]},
            [{"id": "qwen3", "name": "qwen3", "size": 7}, {"id": "gemma", "name": "gemma", "size": 0}],
        ),
        (
            "llamacpp",
            "http://127.0.0.1:5000/v1/models",
            {"data": [{"id": "a.gguf"}, {}]},
            [{"id": "a.gguf", "name": "a.gguf"}, {"id": "?", "name": "?"}],
        ),
    ],
)
def test_translation_models_are_asked_of_the_backend(monkeypatch, backend, url, reply, models):
    import _api

    asked = []

    def urlopen(req, timeout=0):
        asked.append(getattr(req, "full_url", req))
        return io.BytesIO(json.dumps(reply).encode())

    monkeypatch.setattr(_api, "jsonify", lambda d: d)
    monkeypatch.setattr(_api._settings, "reload_config", lambda: None)
    monkeypatch.setattr(_state, "translation_backend", backend)
    monkeypatch.setattr(_state, "translation_host", "127.0.0.1")
    monkeypatch.setattr(_state, "translation_port", 5000)
    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    out = _api.list_translation_models()
    assert asked == [url]
    assert out["models"] == models and out["translation_url"] == "http://127.0.0.1:5000"


def test_plain_mode_does_not_ask_for_json():
    tr = _translator()
    prompts = []

    def chat(messages, max_tokens, response_format=None, temperature=0.1):
        if response_format:
            raise _translate.TranslationError("HTTP 400: response_format not supported")
        prompts.append(messages[0]["content"])
        return "[1] One"

    with patch.object(tr, "_chat", side_effect=chat):
        tr.translate_batch(["あ"], "ja", "English")
    assert "Return JSON" not in prompts[0] and "[1]" in prompts[0]
