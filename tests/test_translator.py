"""The LLM client: structured output, fallbacks, reading what local models reply."""

from __future__ import annotations

import sys
import json
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

import _translate  # noqa: E402
from pipeline_fakes import JSON_REPLY, _translator  # noqa: E402


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


def test_all_caps_translations_are_sentence_cased():
    assert _translate._unshout("IN THE PROVINCE", "くらさきに") == "In the province"
    assert _translate._unshout("SOMARU MACHIBAN", "SOMARU MACHIBAN") == "SOMARU MACHIBAN"  # source was caps
    assert _translate._unshout("I want you", "x") == "I want you"
    assert _translate._unshout("OK", "はい") == "OK"  # too short to be shouting


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


@pytest.mark.parametrize("reply", ["{", "}'}", ":", "[", "%s", "  "])
def test_pieces_of_the_format_are_no_translation(reply):
    assert _translate.is_junk_reply(reply, "Сначала маршрут разминирует.")


@pytest.mark.parametrize(
    ("reply", "text"), [("Yes", "Да, конечно."), ("1930", "Тысяча девятьсот тридцатый"), ("好", "はい"), (":", "ね")]
)
def test_a_short_answer_can_be_a_real_one(reply, text):
    assert not _translate.is_junk_reply(reply, text)


def test_a_batch_cannot_generate_far_past_what_it_needs():
    asked = []

    class T(_translate.Translator):
        def _structured(self, system, message, key, n, max_tokens, *a, **k):
            asked.append(max_tokens)
            return ["ok"] * n

    t = T(lambda: {"host": "h", "port": 1, "backend": "llamacpp", "model": "", "prompt": "", "roma_prompt": ""})
    t.translate_batch(["Сначала маршрут разминирует." * 2] * 10, "ru", "English")
    assert asked[0] < 1100  # it was 2800: the batches that ran away reached 2500 tokens
