"""Vocal isolation in the pipeline (no real Demucs)."""

from __future__ import annotations

import sys
import tempfile
import time
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
import _regions  # noqa: E402
import _separate  # noqa: E402
import _state  # noqa: E402
import _stream  # noqa: E402
import _translate  # noqa: E402
from pipeline_fakes import PARAMS, _raw, _run, _tone_with_gaps  # noqa: E402


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


def test_the_stream_preview_is_of_where_the_viewer_is(pipeline):
    with patch.object(_stream, "download_audio_segment", return_value=None) as seg:
        job = pipeline.jobs.create({**PARAMS, "video_id": "resumed", "playhead": 60.0})
        assert job.playhead == 60.0  # transcribed first, before the first poll
        deadline = time.time() + 10
        while not seg.called and time.time() < deadline:
            time.sleep(0.02)
        assert seg.call_args.args[1] == 60.0
        _run(pipeline.jobs, pipeline.jobs.create({**PARAMS, "video_id": "from-start"}))
        assert seg.call_args.args[1] == 0.0


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
