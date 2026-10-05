"""Tests for the server-side subtitle pipeline: regions, cache, translator, jobs.

No GPU, model, network or LLM needed: Whisper and the LLM are faked.
"""

from __future__ import annotations

import json
import sys
import tempfile
import time
import types
import urllib.error
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

import _audio  # noqa: E402
import _jobs  # noqa: E402
import _regions  # noqa: E402
import _romanize  # noqa: E402
import _state  # noqa: E402
import _store  # noqa: E402
import _translate  # noqa: E402

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
    assert regions[0] == (0.0, _regions.FIRST_REGION_S)
    assert regions[-1][1] == pytest.approx(200, abs=0.01)
    for (s1, e1), (s2, _e2) in zip(regions, regions[1:]):
        assert e1 == s2  # contiguous, exclusive
    for s, e in regions[:-1]:
        assert e - s <= _regions.MAX_REGION_S + 1e-6


def test_regions_cut_in_silence():
    regions = _regions.plan_regions(_tone_with_gaps(200, gap_every=10.0))
    for _s, cut in regions[1:-1]:
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
        assert tr.translate_batch(["a", "b"], "ja", "English") == ["Hello", "World"]
        calls.clear()
        tr.translate_batch(["a", "b"], "ja", "English")
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
            tr.translate_batch(["a"], "ja", "English")


# ── romanization ──────────────────────────────────────────────────────────────


def test_korean_and_russian_romanization():
    assert _romanize.romanize("ko", "사랑해요") == "saranghaeyo"
    assert _romanize.romanize("ru", "Ещё я тебя люблю") == "Yeshchyo ya tebya lyublyu"
    assert _romanize.romanize("ru", "поешь") == "poyesh"  # е after a vowel -> ye
    assert _romanize.romanize("ru", "лето") == "leto"  # е after a consonant -> e
    assert _romanize.romanize("ar", "مرحبا") is None  # needs the LLM
    assert _romanize.romanize("fr", "bonjour") == ""  # Latin script: nothing to do
    assert _romanize.romanize("ja", "   ") == ""


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

    def translate_batch(self, texts, src, tgt, title="", context=None):
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
    saved = (_state.model, _state.store, _state.jobs, _state.user_blacklist)
    _state.model = _FakeModel()
    _state.store = _store.Store(tmp_path / "c.db")
    _state.user_blacklist = []
    translator = _FakeTranslator()
    _state.jobs = _jobs.JobManager(translator)
    with (
        patch.object(_audio, "download_full_audio", return_value=(str(wav), None)) as dl,
        patch.object(_audio, "download_audio_segment", return_value=None),
        patch.object(_audio, "remove_temp"),
    ):
        yield types.SimpleNamespace(jobs=_state.jobs, translator=translator, download=dl)
    _state.jobs._stop = True
    _state.jobs.drop_all("teardown")
    time.sleep(0.2)  # let a worker mid-region finish before the store closes
    _state.store.close()
    _state.model, _state.store, _state.jobs, _state.user_blacklist = saved


def _run(jobs, job, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        jobs.poll(job, 0)
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
    srt, n = _jobs.export_subtitles(list(_state.jobs.get(snap["id"]).segments.values()))
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


def test_download_failure_reports_error(pipeline):
    with (
        patch.object(_audio, "download_full_audio", return_value=(None, "Video unavailable")),
        patch.object(_audio, "get_stream_url", return_value=None),
    ):
        snap = _run(pipeline.jobs, pipeline.jobs.create({**PARAMS, "video_id": "other"}))
    assert snap["status"] == "error" and "unavailable" in snap["error"]


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
def test_job_duration_is_sane(bad):
    assert _jobs.Job(("k", "ja", "", "m"), {"duration": bad}).duration == 0.0


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


def test_unreadable_browser_cookies_are_skipped(monkeypatch):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        err = "ERROR: Could not copy Chrome cookie database. See https://github.com/yt-dlp/yt-dlp/issues/7271"
        if "--cookies-from-browser" not in cmd:
            err = "ERROR: [youtube] x: HTTP Error 403: Forbidden"
        return types.SimpleNamespace(returncode=1, stderr=err, stdout="")

    monkeypatch.setattr(_audio.subprocess, "run", fake_run)
    monkeypatch.setattr(_audio, "_cookies_failed_at", 0.0)
    monkeypatch.setattr(_audio, "get_stream_url", lambda url: None)
    monkeypatch.setattr(_state, "youtube_auth_method", "cookies")
    monkeypatch.setattr(_state, "cookies_browser", "brave")
    path, err = _audio.download_full_audio("https://www.youtube.com/watch?v=0OIVIkmBdL8")
    assert path is None
    with_cookies = [c for c in calls if "--cookies-from-browser" in c]
    assert len(with_cookies) == 1  # was 6: every cookie strategy x 2 format passes
    assert "Brave" in err and "switching to cookie auth" not in err
    assert _audio.build_auth_args("https://www.youtube.com/watch?v=x") == []


def test_old_ytdlp_self_updates_once_a_day(tmp_path, monkeypatch):
    sys.argv = ["server"]
    import faster_whisper_server as fws

    exe = tmp_path / "yt-dlp.exe"
    exe.write_bytes(b"")
    calls = []

    def run(cmd, **kw):
        calls.append(cmd[-1])
        out = "2026.08.19\n" if cmd[-1] == "--version" else "yt-dlp is up to date"
        return types.SimpleNamespace(returncode=0, stdout=out, stderr="")

    monkeypatch.setattr(_state, "youtube_auth_method", "cookies")
    monkeypatch.setattr(fws.shutil, "which", lambda name: str(exe))
    monkeypatch.setattr(fws.subprocess, "run", run)
    assert fws._maybe_update_ytdlp("2026.08.19") == "2026.08.19 (latest)"  # up to date is not a failure
    assert fws._maybe_update_ytdlp("2026.08.19") == "2026.08.19" and calls.count("-U") == 1  # once a day
    assert fws._maybe_update_ytdlp(time.strftime("%Y.%m.%d")) == time.strftime("%Y.%m.%d")  # recent: untouched


def test_japanese_small_tsu_doubles_the_next_consonant():
    if _romanize.get_kakasi() is None:
        pytest.skip("pykakasi not installed")
    assert _romanize.romanize("ja", "走って") == "hashitte"  # was "hashitsu te"
    assert _romanize.romanize("ja", "知った") == "shitta"
    assert _romanize.romanize("ja", "ちょっと待って") == "chotto matte"


def test_missing_server_requirements_are_reported(tmp_path, monkeypatch):
    sys.path.insert(0, str(Path(__file__).parent.parent))
    from yume import utils

    (tmp_path / "server").mkdir()
    (tmp_path / "server" / "requirements.txt").write_text("# pins\nnumpy==2.4.3\nno-such-package-yume==1.0\n")
    monkeypatch.setattr(utils, "BASE_DIR", tmp_path)
    assert utils.missing_requirements() == ["no-such-package-yume==1.0"]


def test_stream_preview_retries_without_unreadable_cookies(monkeypatch):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if "--cookies-from-browser" in cmd:
            return types.SimpleNamespace(
                returncode=1, stdout="", stderr="ERROR: Could not copy Chrome cookie database."
            )
        return types.SimpleNamespace(returncode=0, stdout="https://cdn.example/audio", stderr="")

    monkeypatch.setattr(_audio.subprocess, "run", fake_run)
    monkeypatch.setattr(_audio, "_cookies_failed_at", 0.0)
    monkeypatch.setattr(_state, "youtube_auth_method", "cookies")
    monkeypatch.setattr(_state, "stream_url_cache", {})
    assert _audio.get_stream_url("https://www.youtube.com/watch?v=abc") == "https://cdn.example/audio"
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
    segs, hide = _filter.clean_raw_segments([{"start": 0, "end": 5, "text": "ラララララララ 君と夢を見ていた"}])
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
    assert pipeline.translator.batches[0] <= _jobs.NEAR_BATCH


def test_system_prompt_is_the_same_for_every_batch():
    seen = []
    tr = _translator()

    def chat(messages, max_tokens, response_format=None, temperature=0.1):
        seen.append(messages[0]["content"])
        return json.dumps({"translations": ["x"] * 20})

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
    assert pipeline.translator.batches[0] <= _jobs.NEAR_BATCH


def test_japanese_particles_are_written_as_pronounced():
    if _romanize.get_kakasi() is None:
        pytest.skip("pykakasi not installed")
    ja = lambda t: _romanize.romanize("ja", t)  # noqa: E731
    assert ja("私は学校へ行く") == "watashi wa gakkou e iku"
    assert ja("本当はもっと") == "hontou wa motto"  # は glued to the next word by pykakasi
    assert ja("こんにちは") == "konnichiwa"
    # は / へ that are not particles stay as they are
    assert ja("はじめまして") == "hajimemashite"
    assert ja("ははは") == "hahaha"


def test_non_ascii_token_header_is_rejected_not_a_crash():
    sys.argv = ["server"]
    import faster_whisper_server as fws

    _state.API_TOKEN = "abc"
    assert fws._token_ok("abc")
    assert not fws._token_ok("\u00e9")  # compare_digest on str raised TypeError -> HTTP 500
    assert not fws._token_ok("")


def test_cookie_hint_only_where_cookies_could_help(monkeypatch):
    def fake_run(cmd, **kw):
        err = "ERROR: Could not copy Chrome cookie database."
        if "--cookies-from-browser" not in cmd:
            err = "ERROR: [youtube] x: Video unavailable"
        return types.SimpleNamespace(returncode=1, stderr=err, stdout="")

    monkeypatch.setattr(_audio.subprocess, "run", fake_run)
    monkeypatch.setattr(_audio, "_cookies_failed_at", 0.0)
    monkeypatch.setattr(_audio, "get_stream_url", lambda url: None)
    monkeypatch.setattr(_state, "youtube_auth_method", "cookies")
    _path, err = _audio.download_full_audio("https://www.youtube.com/watch?v=gone")
    assert "unavailable" in err and "cookies" not in err.lower()
