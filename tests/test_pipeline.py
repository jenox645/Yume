"""Whole jobs through the server: real code, with a fake Whisper and a fake LLM."""

from __future__ import annotations

import io
import json
import sys
import threading
import time
import types
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
import _romanize  # noqa: E402
import _separate  # noqa: E402
import _state  # noqa: E402
import _store  # noqa: E402
import _stream  # noqa: E402
import _ytdlp  # noqa: E402
from pipeline_fakes import (
    LONG,
    PARAMS,
    RAP,
    TAIL_LEFT,
    TAIL_RIGHT,
    _FlakyModel,
    _llm_job,
    _picker,
    _raw,
    _run,
    _seg,
    _switch,
)  # noqa: E402


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
