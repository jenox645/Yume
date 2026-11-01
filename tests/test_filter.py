"""Hallucination and credits detection."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

import _filter  # noqa: E402
import _state  # noqa: E402


class TestIsHallucination(unittest.TestCase):
    def setUp(self):
        _state.user_blacklist = []

    # ── Built-in patterns ─────────────────────────────────────────────────────

    def test_empty_string_is_hallucination(self):
        self.assertTrue(_filter.is_hallucination(""))

    def test_whitespace_only_is_hallucination(self):
        self.assertTrue(_filter.is_hallucination("   "))

    def test_japanese_thanks_detected(self):
        self.assertTrue(_filter.is_hallucination("ご視聴ありがとうございました"))

    def test_japanese_thanks_short_detected(self):
        self.assertTrue(_filter.is_hallucination("ご視聴ありがとう"))

    def test_japanese_otsukare_detected(self):
        self.assertTrue(_filter.is_hallucination("お疲れ様でした"))

    def test_japanese_otsukare_short_detected(self):
        self.assertTrue(_filter.is_hallucination("お疲れ様"))

    def test_japanese_subtitle_auto_detected(self):
        self.assertTrue(_filter.is_hallucination("字幕は自動生成"))

    def test_subscribe_english_detected(self):
        self.assertTrue(_filter.is_hallucination("Subscribe"))

    def test_subscribe_case_insensitive(self):
        self.assertTrue(_filter.is_hallucination("SUBSCRIBE"))

    def test_thank_you_for_watching_detected(self):
        self.assertTrue(_filter.is_hallucination("Thank you for watching"))

    def test_thanks_for_watching_detected(self):
        self.assertTrue(_filter.is_hallucination("Thanks for watching"))

    def test_music_bracket_detected(self):
        self.assertTrue(_filter.is_hallucination("[Music]"))

    def test_applause_bracket_detected(self):
        self.assertTrue(_filter.is_hallucination("[Applause]"))

    def test_laughter_bracket_detected(self):
        self.assertTrue(_filter.is_hallucination("[Laughter]"))

    def test_music_paren_detected(self):
        self.assertTrue(_filter.is_hallucination("(Music)"))

    def test_instagram_detected(self):
        self.assertTrue(_filter.is_hallucination("Instagram"))

    def test_twitter_detected(self):
        self.assertTrue(_filter.is_hallucination("Twitter"))

    def test_chinese_subscribe_detected(self):
        self.assertTrue(_filter.is_hallucination("请订阅"))

    def test_chinese_thanks_detected(self):
        self.assertTrue(_filter.is_hallucination("感谢观看"))

    def test_russian_subscribe_detected(self):
        self.assertTrue(_filter.is_hallucination("Подписывайтесь на канал"))

    def test_arabic_subscribe_detected(self):
        self.assertTrue(_filter.is_hallucination("اشترك في القناة"))

    def test_sound_hodori_detected(self):
        self.assertTrue(_filter.is_hallucination("Sound Hodori"))

    def test_like_detected(self):
        self.assertTrue(_filter.is_hallucination("Like"))

    def test_share_detected(self):
        self.assertTrue(_filter.is_hallucination("Share"))

    # ── Regression: social words inside real lyrics must NOT be dropped ──────

    def test_like_inside_sentence_allowed(self):
        self.assertFalse(_filter.is_hallucination("I like you more than words can say"))

    def test_follow_inside_sentence_allowed(self):
        self.assertFalse(_filter.is_hallucination("Follow me into the night sky"))

    def test_share_inside_sentence_allowed(self):
        self.assertFalse(_filter.is_hallucination("A dream we share together"))

    # ── Pattern in longer text ────────────────────────────────────────────────

    def test_hallucination_embedded_in_sentence(self):
        self.assertTrue(_filter.is_hallucination("Please Subscribe to my channel!"))

    def test_hallucination_unicode_nfc_normalised(self):
        # NFC-normalised version should still match
        import unicodedata

        normalised = unicodedata.normalize("NFC", "ご視聴ありがとう")
        self.assertTrue(_filter.is_hallucination(normalised))

    # ── Single-word blocklist ─────────────────────────────────────────────────

    def test_music_single_word_blocked(self):
        self.assertTrue(_filter.is_hallucination("music"))

    def test_hmm_blocked(self):
        self.assertTrue(_filter.is_hallucination("hmm"))

    def test_mm_blocked(self):
        self.assertTrue(_filter.is_hallucination("mm"))

    # ── Repeated word spam ────────────────────────────────────────────────────

    def test_six_identical_words_detected(self):
        self.assertTrue(_filter.is_hallucination("30k 30k 30k 30k 30k 30k"))

    def test_seven_words_two_unique_detected(self):
        self.assertTrue(_filter.is_hallucination("ha ha ha ha ha ha ha"))

    def test_five_diverse_words_not_spam(self):
        # 5 words, all different — not spam
        self.assertFalse(_filter.is_hallucination("do re mi fa sol"))

    def test_six_diverse_words_not_spam(self):
        self.assertFalse(_filter.is_hallucination("I love the sun and sky"))

    # ── Concatenated repetition ───────────────────────────────────────────────

    def test_repeated_substring_aaaa_detected(self):
        self.assertTrue(_filter.is_hallucination("aaaaaa"))

    def test_repeated_two_char_pattern_detected(self):
        self.assertTrue(_filter.is_hallucination("abababab"))

    def test_a_word_said_twice_is_a_lyric(self):
        for line in [
            "きらきら",
            "どんどん",
            "もっともっと",
            "そうそう",
            "더 그리워 더 그리워",
            "I love you I love you",
        ]:
            self.assertFalse(_filter.is_hallucination(line), line)
        self.assertTrue(_filter.is_hallucination("la la la la la la"))

    def test_a_word_said_three_times_is_a_lyric_four_times_filler(self):
        # はいはいはい, 無理無理無理 and ほうほうほう were hidden in a Japanese talk show
        for line in ["ダメダメダメ", "無理無理無理", "はいはいはい", "Baby, baby, baby", "あ、そうそうそうそう"]:
            self.assertFalse(_filter.is_hallucination(line), line)
        for line in ["うんうんうんうん", "ああああああ", "aaaaaa", "ラララララ"]:
            self.assertTrue(_filter.is_hallucination(line), line)

    def test_outro_lines_in_every_language_hidden(self):
        for line in [
            "最後までご視聴いただきありがとうございました",
            "ご視聴頂きありがとうございます",
            "欢迎订阅我的频道",
            "記得點贊訂閱轉發",
            "구독 좋아요 알림 설정 부탁드립니다",
            "좋아요와 구독 부탁드려요",
            "Подпишись на канал!",
            "Подписывайтесь на наш канал и ставьте лайки",
            "شكرا لكم على المشاهدة",
            "اشتركوا في القناة",
            "شكراً للمشاهدة",  # tanween: was not matched
        ]:
            self.assertTrue(_filter.is_hallucination(line), line)

    def test_lyrics_near_the_outro_patterns_shown(self):
        for line in [
            "君の歌を聴いていた",
            "为你点赞 为你唱",
            "换个频道 换个心情",
            "알림이 울려도 너는 없어",
            "너의 좋아요 한마디에",
            "Мы плыли по каналу",
            "Я подписываю письмо",
            "شكرا على كل شيء",
            "شكراً يا حبيبي",
        ]:
            self.assertFalse(_filter.is_hallucination(line), line)

    def test_blacklist_matches_with_or_without_arabic_marks(self):
        _state.user_blacklist = ["مُرحباً"]
        self.assertTrue(_filter.is_hallucination("مرحبا بكم"))

    def test_normal_short_word_not_flagged(self):
        self.assertFalse(_filter.is_hallucination("la"))

    # ── Real speech NOT flagged ───────────────────────────────────────────────

    def test_japanese_lyrics_not_flagged(self):
        self.assertFalse(_filter.is_hallucination("今日はいい天気ですね"))

    def test_english_lyrics_not_flagged(self):
        self.assertFalse(_filter.is_hallucination("Hello everyone, how are you"))

    def test_chinese_normal_text_not_flagged(self):
        self.assertFalse(_filter.is_hallucination("我爱你"))

    def test_real_arabic_not_flagged(self):
        # Make sure common real Arabic words aren't caught as hallucinations
        self.assertFalse(_filter.is_hallucination("مرحبا بالعالم"))

    # ── User blacklist ────────────────────────────────────────────────────────

    def test_user_blacklist_item_detected(self):
        _state.user_blacklist = ["custom spam"]
        self.assertTrue(_filter.is_hallucination("This is custom spam text"))

    def test_user_blacklist_case_insensitive(self):
        _state.user_blacklist = ["Custom Spam"]
        self.assertTrue(_filter.is_hallucination("this is custom spam"))

    def test_user_blacklist_matches_whole_words(self):
        _state.user_blacklist = ["спам", "banana"]
        self.assertTrue(_filter.is_hallucination("это спам и всё"))
        self.assertTrue(_filter.is_hallucination("He ate a banana on the way home"))
        self.assertFalse(_filter.is_hallucination("Он отказался от антиспама в почте"))
        self.assertFalse(_filter.is_hallucination("They were growing bananas near the river"))

    def test_a_short_user_blacklist_item_must_be_the_whole_line(self):
        _state.user_blacklist = ["а", "не"]
        self.assertFalse(_filter.is_hallucination("Рабочая неделя началась как обычно"))
        self.assertFalse(_filter.is_hallucination("Он не пришёл на работу сегодня утром"))
        self.assertTrue(_filter.is_hallucination("Не."))

    def test_user_blacklist_reads_inside_lines_without_spaces(self):
        _state.user_blacklist = ["ご視聴ありがとう", "구독과 좋아요"]
        self.assertTrue(_filter.is_hallucination("最後までご視聴ありがとうございました"))
        self.assertTrue(_filter.is_hallucination("여러분의 구독과 좋아요를 부탁드립니다"))

    def test_user_blacklist_empty_is_safe(self):
        _state.user_blacklist = []
        self.assertFalse(_filter.is_hallucination("今日はいい天気"))

    def test_user_blacklist_blank_item_ignored(self):
        _state.user_blacklist = ["", "  "]
        self.assertFalse(_filter.is_hallucination("今日はいい天気"))

    def tearDown(self):
        _state.user_blacklist = []


class TestIsCreditsLine(unittest.TestCase):
    def test_vocals_credits_detected(self):
        self.assertTrue(_filter.is_credits_line("vocals: Hatsune Miku"))

    def test_guitar_credits_detected(self):
        self.assertTrue(_filter.is_credits_line("guitar: Yamaha"))

    def test_piano_credits_detected(self):
        self.assertTrue(_filter.is_credits_line("piano arrangement"))

    def test_mix_mastering_detected(self):
        self.assertTrue(_filter.is_credits_line("mix & mastering by Studio X"))

    def test_japanese_interpunct_short_line(self):
        # Short line with Japanese interpunct (・) = credits separator
        self.assertTrue(_filter.is_credits_line("作詞・作曲：田中"))

    def test_japanese_interpunct_long_line_not_credits(self):
        # Long line (>60 chars) with interpunct is legitimate lyrics, not credits
        # "空と・・海と大地の間に生きる私たち" = 17 chars, *5 = 85 chars > 60-char threshold
        self.assertFalse(_filter.is_credits_line("空と・・海と大地の間に生きる私たち" * 5))

    def test_illustration_credits_detected(self):
        self.assertTrue(_filter.is_credits_line("Illustration by Artist Name"))

    def test_animation_credits_detected(self):
        self.assertTrue(_filter.is_credits_line("animation: Studio Ghibli"))

    def test_role_words_inside_real_lines_are_not_credits(self):
        # Substring matching used to hide all of these
        for line in [
            "I watched the video again",
            "remix the night away",
            "Piano man, sing us a song",
            "Video killed the radio star",
            "この曲を作曲した人",
            "トム・クルーズ",
            "Musical night",
        ]:
            self.assertFalse(_filter.is_credits_line(line), line)
            self.assertFalse(_filter.is_hallucination(line), line)

    def test_role_lists_are_credits(self):
        self.assertTrue(_filter.is_credits_line("作詞 作曲 編曲 初音ミク"))
        self.assertTrue(_filter.is_credits_line("Music: Someone"))

    def test_normal_lyric_not_credits(self):
        self.assertFalse(_filter.is_credits_line("あなたに会いたい"))

    def test_english_lyric_not_credits(self):
        self.assertFalse(_filter.is_credits_line("I walk alone in the dark"))

    def test_japanese_word_music_not_caught(self):
        # "音楽" (music) is a Japanese word that legitimately appears in lyrics
        self.assertFalse(_filter.is_credits_line("音楽が流れる夜"))

    def test_cjk_roles_need_no_separator(self):
        # live test, Jay Chou "晴天": "詞曲 李宗盛" was shown for 30 s
        for line in ["詞曲 李宗盛", "作曲 周杰倫", "작사 김이나", "编曲：钟兴民"]:
            self.assertTrue(_filter.is_credits_line(line), line)
        for line in ["この曲を作曲した", "作曲家になりたい"]:
            self.assertFalse(_filter.is_credits_line(line), line)

    def test_live_test_hallucinations_are_caught(self):
        # Seen in live tests (Korean, Chinese, Russian, Arabic music videos)
        for line in [
            "한글자막 by 한효정",
            "다음 영상에서 만나요.",
            "字幕志愿者 李宗盛",
            "中文字幕志愿者 李宗盛",
            "优优独播剧场——YoYo Television Series Exclusive",
            "Субтитры сделал DimaTorzok",
            "Субтитры подогнал «Симон»",
            "Продолжение следует...",
            "ترجمة نانسي قنقر",
        ]:
            self.assertTrue(_filter.is_hallucination(line), line)
        for line in ["Группа крови на рукаве", "그대가 멀리 사라져버릴 것 같아", "还要多久 我才能在你身边"]:
            self.assertFalse(_filter.is_hallucination(line), line)

    def test_line_filling_a_silent_window_is_a_hallucination(self):
        lone = _filter.window_hallucinations
        # alone across a whole region (an intro, an instrumental)
        self.assertEqual(lone([{"start": 0.0, "end": 30.0, "text": "x"}], (0.0, 24.5)), {0})
        self.assertEqual(lone([{"start": 30.0, "end": 60.0, "text": "x"}], (30.0, 58.25)), {0})
        # stamped 30 s long over an 8 s last region
        self.assertEqual(lone([{"start": 274.5, "end": 304.5, "text": "x"}], (274.5, 282.83)), {0})
        # real lines: alone but not filling the window, or not alone
        self.assertEqual(lone([{"start": 274.5, "end": 280.0, "text": "x"}], (274.5, 282.83)), set())
        self.assertEqual(lone([{"start": 30.0, "end": 41.0, "text": "x"}], (30.0, 56.0)), set())
        # a chorus line Whisper stamped across its whole 20 s region (live test)
        self.assertEqual(lone([{"start": 224.4, "end": 244.7, "text": "x"}], (224.45, 244.7)), set())
        two = [{"start": 0.0, "end": 12.0, "text": "a"}, {"start": 12.0, "end": 26.0, "text": "b"}]
        self.assertEqual(lone(two, (0.0, 26.0)), set())

    def test_thank_you_at_the_end_of_a_region_is_a_hallucination(self):
        lone = _filter.window_hallucinations
        segs = [{"start": 96.0, "end": 120.0, "text": "a"}, {"start": 123.2, "end": 125.2, "text": "감사합니다."}]
        self.assertEqual(lone(segs, (96.2, 123.35)), {1})
        self.assertEqual(lone([{"start": 200.0, "end": 210.0, "text": "شكراً"}], (178.0, 212.0)), {0})
        # in the middle of a region it can be a real line
        mid = [{"start": 100.0, "end": 102.0, "text": "감사합니다"}, {"start": 102.0, "end": 110.0, "text": "b"}]
        self.assertEqual(lone(mid, (96.0, 123.0)), set())

    def test_credits_patterns_are_all_strings(self):
        for p in _filter.CREDITS_PATTERNS:
            self.assertIsInstance(p, str)

    def test_hallucination_patterns_all_nonempty(self):
        for p in _filter.HALLUCINATION_PATTERNS:
            self.assertGreater(len(p), 0)
