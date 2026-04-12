"""Whisper transcription — serialised through transcribe_lock (CTranslate2 is not thread-safe)."""

import os
import time

import _state
from _filter import is_hallucination, is_credits_line


def transcribe_file(audio_path, language, start_offset=0.0, is_first_chunk=False):
    """Transcribe audio file.  Uses v2.0.7 parameters proven to work for music.

    Args:
        audio_path:      Path to the audio file to transcribe.
        language:        Language code ("ja", "zh", …) or None for auto-detect.
        start_offset:    Seconds to add to all segment timestamps (chunk start time).
        is_first_chunk:  True for chunk 0 — relaxes no_speech_threshold to compensate
                         for the absence of pre-roll audio (chunks 1+ overlap the previous
                         chunk by 5 s, giving Whisper warm-up context; chunk 0 starts cold).

    Returns a dict with keys: text, segments, language, duration, start_offset.
    """
    if _state.model is None:
        raise RuntimeError("Model is still loading — try again in a few seconds")

    print(f"[Yume] Transcribing {audio_path} (offset: {start_offset}s, first={is_first_chunk})")
    t_start = time.time()

    file_size = os.path.getsize(audio_path)
    if file_size < 10_000:
        print(f"[Yume] Skipping tiny file ({file_size} bytes)")
        return {"text": "", "segments": [], "language": language, "duration": 0, "start_offset": start_offset}

    # v2.0.7 parameters — proven to work for Japanese music.
    # DO NOT use word_timestamps — different decode path drops segments.
    #
    # no_speech_threshold semantics (faster-whisper):
    #   A segment is dropped when BOTH of these are true:
    #     1. no_speech_prob > no_speech_threshold   (sounds like non-speech)
    #     2. avg_logprob    < log_prob_threshold     (model is uncertain)
    #   Default is 0.6.  Using 0.6 here is intentional — lower values were
    #   previously tried (0.45, 0.3) and caused the first chunk to be silently
    #   dropped when an instrumental intro pushed no_speech_prob to ~0.35 while
    #   the cold-start (no pre-roll) depressed avg_logprob enough to trigger
    #   condition 2 simultaneously.  The hallucination filter + user blacklist
    #   are the correct quality gate for non-speech content, not this threshold.
    #
    # is_first_chunk uses a slightly higher threshold (0.7) because chunk 0 has
    # no pre-roll audio (chunks 1+ overlap the previous chunk by 5 s), making
    # Whisper's initial no_speech estimate less reliable on cold audio.
    no_speech_thresh = 0.7 if is_first_chunk else 0.6

    params = dict(
        language=language,
        beam_size=5,
        vad_filter=False,  # MUST be False — Silero VAD drops singing
        word_timestamps=False,  # v2.0.7: False (different decode path when True)
        condition_on_previous_text=False,
        temperature=0.0,
        compression_ratio_threshold=2.4,  # v2.0.7 value
        log_prob_threshold=-2.0,  # widened from -1.5: catch more speech at low confidence
        no_speech_threshold=no_speech_thresh,
    )

    # CRITICAL: Serialise model access. CTranslate2 is NOT thread-safe.
    with _state.transcribe_lock:
        segments_iter, info = _state.model.transcribe(audio_path, **params)  # type: ignore[arg-type]
        raw_segments = list(segments_iter)  # consume inside lock

    segments = []
    full_text_parts = []
    dropped = 0

    print(f"[Yume] Whisper returned {len(raw_segments)} raw segments")

    for seg in raw_segments:
        text = seg.text.strip()
        if not text:
            continue
        if is_hallucination(text):
            print(f"[Yume] Dropped hallucination: {text!r}")
            dropped += 1
            continue
        if is_credits_line(text):
            print(f"[Yume] Dropped credits: {text!r}")
            dropped += 1
            continue
        # No secondary logprob filter here. Whisper's internal log_prob_threshold=-2.0
        # handles truly low-confidence segments. A stricter server-side filter drops
        # valid vocals mixed with background music (the primary use case for Yume).
        # Hallucination patterns + user blacklist are the correct quality filter.
        segments.append(
            {
                "start": round(seg.start + start_offset, 2),
                "end": round(seg.end + start_offset, 2),
                "text": text,
                "confidence": round(seg.avg_logprob, 2) if hasattr(seg, "avg_logprob") else 0,
            }
        )
        full_text_parts.append(text)

    print(f"[Yume] Transcription done: {len(segments)} segments ({dropped} dropped)")

    whisper_elapsed = time.time() - t_start
    with _state.stats_lock:
        _state.server_stats["chunks_transcribed"] += 1
        _state.server_stats["segments_produced"] += len(segments)
        _state.server_stats["hallucinations_filtered"] += dropped
        _state.server_stats["total_audio_seconds"] += info.duration
        _state.server_stats["total_whisper_time"] += whisper_elapsed
        _state.server_stats["last_chunk_whisper_time"] = round(whisper_elapsed, 1)
        _state.server_stats["last_chunk_segments"] = len(segments)

    return {
        "text": " ".join(full_text_parts),
        "segments": segments,
        "language": info.language,
        "duration": info.duration,
        "start_offset": start_offset,
    }
