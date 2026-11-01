"""Fake Whisper and LLM and the helpers the server pipeline tests share."""

from __future__ import annotations

import sys
import threading
import time
import types
from pathlib import Path
from unittest.mock import patch

import numpy as np

sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

import _audio  # noqa: E402
import _download  # noqa: E402
import _job  # noqa: E402
import _jobs  # noqa: E402
import _state  # noqa: E402
import _store  # noqa: E402
import _stream  # noqa: E402
import _translate  # noqa: E402


SR = 16000


def _tone_with_gaps(seconds, gap_every=10.0):
    """Tone with a 1 s silence every `gap_every` seconds."""
    t = np.arange(int(seconds * SR)) / SR
    audio = (0.3 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)
    for g in np.arange(gap_every, seconds, gap_every):
        audio[int(g * SR) : int((g + 1) * SR)] = 0
    return audio


def store_env(tmp_path):
    st = _store.Store(tmp_path / "c.db")
    yield st
    st.close()


def _translator(backend="llamacpp", model=""):
    return _translate.Translator(
        lambda: {"host": "127.0.0.1", "port": 1, "backend": backend, "model": model, "prompt": "", "roma_prompt": ""}
    )


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


def pipeline_env(tmp_path):
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


def _raw(s, e, t):
    return {"start": s, "end": e, "text": t, "confidence": -0.3}


class _FlakyModel(_FakeModel):
    """Fails every call until `ok` is set (a CUDA hiccup, the GPU busy elsewhere)."""

    ok = False

    def transcribe(self, audio, **kw):
        if not self.ok:
            raise RuntimeError("CUDA failed")
        return super().transcribe(audio, **kw)


def _switch(body):
    """POST /model/switch → (status code, json), past the token check. Works
    with real Flask and with the stub server_stubs installs."""
    import _api

    request = types.SimpleNamespace(get_json=lambda silent=False: body)
    with patch.object(_api, "request", request), patch.object(_api, "jsonify", lambda d: d):
        resp = _api.switch_model()
    return resp[::-1] if isinstance(resp, tuple) else (200, resp)


def _seg(start, end, text):
    return {"start": start, "end": end, "text": text}


LONG = (
    "Я помню чудное мгновенье, передо мной явилась ты, как мимолётное виденье, "
    "как гений чистой красоты, в томленьях грусти безнадежной, в тревогах шумной суеты."
)


TAIL_LEFT = [
    _seg(16.76, 19.18, "싱가포르에 있는 국내 프랜차이즈 빵집"),
    _seg(19.48, 23.82, "매장입니다. 제품 안내판에 한글로 된 로고가 보입니다."),  # stamped past the cut at 20.7
]


TAIL_RIGHT = [
    _seg(20.7, 23.7, "제품 안내판에 한글로 된 로고가 보입니다."),
    _seg(24.0, 29.5, "지난 2월 판매를 시작했습니다."),
]


RAP = (
    "Наша любовь как будто открыла ват портал, и меня замотала, и тебя замотала, ты попал, малыш, и я попала, "
    "но зачем ты прикипела, я предупреждала, я тебе ведь говорила, мы слишком разные, я хочу уют в дом, "
    "ты хочешь праздновать, как не подгоняю вы, не сойдутся пазлы, снова в ту же реку, по новой лесте пасла, "
    "я люблю сериал о деньги, тяжелый люкс, люблю искренность бога, и тебя люблю."
)


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


JSON_REPLY = '{"translations": ["[1] It targeted reinforcements",\n"[2] And the children",\n"[3] and the hospital"]'
