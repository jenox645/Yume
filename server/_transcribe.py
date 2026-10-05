"""Whisper transcription — serialised through transcribe_lock (CTranslate2 is not thread-safe)."""

import time

import _state

SAMPLE_RATE = 16000


def transcribe_audio(audio, language, offset=0.0, is_first_region=False):
    """Transcribe a float32 16 kHz mono array. Returns RAW segments (unfiltered):
    [{"start", "end", "text", "confidence"}], times shifted by `offset` seconds.

    Raw, so the durable cache keeps everything Whisper produced and filter or
    blacklist changes apply retroactively (see filter_segments).

    Parameters are tuned for music (Yume's main use):
      * vad_filter=False — Silero VAD drops singing.
      * word_timestamps=False — the word-timestamp decode path drops segments.
      * condition_on_previous_text=False — stops hallucination loops carrying over.
      * no_speech_threshold 0.6 (0.7 for the first region, which starts cold on
        an instrumental intro). A segment is dropped only when no_speech_prob is
        above it AND avg_logprob is below log_prob_threshold; lower values were
        tried and silently dropped the first verse. The hallucination filter and
        user blacklist are the quality gate for non-speech, not this threshold.
    """
    model = _state.model
    if model is None:
        raise RuntimeError("Model is still loading — try again in a few seconds")
    if len(audio) < SAMPLE_RATE // 2:
        return []

    params = dict(
        language=language,
        beam_size=5,
        vad_filter=False,
        word_timestamps=False,
        condition_on_previous_text=False,
        temperature=0.0,
        compression_ratio_threshold=2.4,
        log_prob_threshold=-2.0,
        no_speech_threshold=0.7 if is_first_region else 0.6,
    )

    t_start = time.time()
    # CRITICAL: Serialise model access. CTranslate2 is NOT thread-safe.
    with _state.transcribe_lock:
        segments_iter, info = model.transcribe(audio, **params)
        raw = list(segments_iter)  # consume inside the lock
    elapsed = time.time() - t_start

    segments = []
    for seg in raw:
        text = seg.text.strip()
        if text:
            segments.append(
                {
                    "start": round(seg.start + offset, 2),
                    "end": round(seg.end + offset, 2),
                    "text": text,
                    "confidence": round(getattr(seg, "avg_logprob", 0.0), 2),
                }
            )

    with _state.stats_lock:
        st = _state.server_stats
        st["regions_transcribed"] += 1
        st["segments_produced"] += len(segments)
        st["total_audio_seconds"] += len(audio) / SAMPLE_RATE
        st["total_whisper_time"] += elapsed
        st["last_region_whisper_time"] = round(elapsed, 1)
        st["last_region_segments"] = len(segments)
    print(
        f"[Yume] Transcribed {len(audio) / SAMPLE_RATE:.0f}s at {offset:.0f}s: {len(segments)} segments in {elapsed:.1f}s"
    )
    return segments
