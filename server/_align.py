"""Word timing for karaoke: when each word of a shown line is sung or said.

Whisper's own word timestamps change what it hears (that decode path dropped
lines on songs: _transcribe.py), so the lines are heard as before and their
final text is aligned to the audio afterwards, with the cross-attention
alignment faster-whisper uses for its word timestamps. One encoder pass per
region. whisper.cpp has no such call: its lines keep the sweep over the line.
"""

import _state

SR = 16000
MAX_FRAMES = 3000  # Whisper's 30 s window; a region is shorter
MIN_LONGEST_S = 1.0  # a held note may last this long whatever the line's pace
# faster-whisper's defaults: punctuation joins the word next to it
PREPEND = "\"'“¿([{-"
APPEND = "\"'.。,，!！?？:：”)]}、"


def _line_words(alignment, per_line):
    """Split the region's aligned words back into its lines, by token count."""
    out, i = [], 0
    for tokens in per_line:
        words, taken = [], 0
        while i < len(alignment) and taken < len(tokens):
            words.append(alignment[i])
            taken += len(alignment[i]["tokens"])
            i += 1
        out.append(words if taken == len(tokens) else None)
    return out


def _longest(words):
    """The longest a word may last: twice the line's median word, as faster-whisper caps
    words at sentence ends. Longer, it took in the silence before it."""
    lengths = sorted(float(w["end"]) - float(w["start"]) for w in words if w["word"].strip())
    lengths = [x for x in lengths if x > 0]
    return max(MIN_LONGEST_S, 2 * lengths[len(lengths) // 2]) if lengths else None


def _spans(words, text, at, start, end):
    """[[start, end, from, to], ...] for one line: seconds in the video, and where each
    word sits in the line's text. None when the words do not spell the text."""
    if words is None or "".join(w["word"] for w in words).strip() != text.strip():
        return None
    longest = _longest(words)
    spans, pos, last = [], len(text) - len(text.lstrip()), start
    for w in words:
        piece = w["word"].strip()
        if not piece:  # punctuation merged into a neighbour
            continue
        found = text.find(piece, pos)
        if found < 0:
            return None
        # Inside the line's own time, and in order: the line is what is on screen
        e = min(max(float(w["end"]) + at, last), end)
        s = min(max(float(w["start"]) + at, last, e - longest if longest else last), e)
        spans.append([round(s, 2), round(e, 2), found, found + len(piece)])
        pos, last = found + len(piece), e
    return spans or None


def word_times(audio, at, lines, language):
    """{index in lines: [[start, end, from, to], ...]} for the lines heard in `audio`,
    which starts `at` seconds into the video. {} when the model cannot align."""
    model = _state.model
    if not lines or not hasattr(model, "find_alignment") or len(audio) < SR // 2:
        return {}
    from faster_whisper.audio import pad_or_trim
    from faster_whisper.tokenizer import Tokenizer
    from faster_whisper.transcribe import merge_punctuations

    tokenizer = Tokenizer(model.hf_tokenizer, model.model.is_multilingual, task="transcribe", language=language)
    per_line = [tokenizer.encode(" " + line["text"].strip()) for line in lines]
    tokens = [t for line in per_line for t in line]
    with _state.transcribe_lock:
        features = model.feature_extractor(audio)
        frames = min(features.shape[-1] - 1, MAX_FRAMES)
        encoded = model.encode(pad_or_trim(features[:, :frames]))
        alignment = model.find_alignment(tokenizer, [tokens], encoded, frames)[0]
    merge_punctuations(alignment, PREPEND, APPEND)
    out = {}
    for i, (line, words) in enumerate(zip(lines, _line_words(alignment, per_line), strict=True)):
        spans = _spans(words, line["text"], at, line["start"], line["end"])
        if spans:
            out[i] = spans
    return out
