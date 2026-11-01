"""Romanization of Japanese, Chinese, Korean, Russian and Arabic lines."""

from __future__ import annotations

import sys
import threading
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "server"))

import _romanize  # noqa: E402


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


def test_a_japanese_middle_dot_is_a_space_in_romaji():
    if _romanize.get_cutlet() is None:
        pytest.skip("cutlet (fugashi + unidic-lite) not installed")
    assert _romanize.romanize("ja", "ジョン・レノン") == "Jon Renon"  # was "Jon/Renon"


class TestRomanize(unittest.TestCase):
    """These libs are not installed in CI; verify graceful None fallback."""

    def test_romanize_japanese_returns_none_when_pykakasi_missing(self):
        # Force lib check without actually loading pykakasi (not installed)
        result = _romanize.romanize_japanese("東京")
        # Either None (lib missing) or a string (lib present)
        self.assertTrue(result is None or isinstance(result, str))

    def test_romanize_chinese_returns_none_when_pypinyin_missing(self):
        result = _romanize.romanize_chinese("测试")
        self.assertTrue(result is None or isinstance(result, str))

    def test_get_kakasi_is_thread_safe(self):
        """Calling get_kakasi() from multiple threads should not raise."""
        results = []
        errors = []

        def _call():
            try:
                results.append(_romanize.get_kakasi())
            except (RuntimeError, ImportError, ValueError) as e:
                errors.append(e)

        threads = [threading.Thread(target=_call) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        self.assertEqual(len(errors), 0, f"Thread errors: {errors}")
        self.assertEqual(len(results), 5)
        # All results must be the same object (singleton) or all None
        unique = {id(r) for r in results if r is not None}
        self.assertLessEqual(len(unique), 1)

    def test_get_kakasi_returns_consistent_result(self):
        r1 = _romanize.get_kakasi()
        r2 = _romanize.get_kakasi()
        self.assertIs(r1, r2)

    def test_romanize_japanese_with_mock_kakasi(self):
        """Verify romanize_japanese calls kakasi.convert() and joins results."""
        mock_kakasi = MagicMock()
        mock_kakasi.convert.return_value = [
            {"hepburn": "tou", "passport": "to", "orig": "東"},
            {"hepburn": "kyou", "passport": "kyo", "orig": "京"},
        ]
        with (
            patch.object(_romanize, "_kakasi", mock_kakasi),
            patch.object(_romanize, "_kakasi_checked", True),
            patch.object(_romanize, "get_cutlet", lambda: None),  # the pykakasi fallback
        ):
            result = _romanize.romanize_japanese("東京")
        self.assertIsNotNone(result)
        self.assertIn("tou", result)
        self.assertIn("kyou", result)

    def test_romanize_japanese_kakasi_exception_returns_none(self):
        mock_kakasi = MagicMock()
        mock_kakasi.convert.side_effect = RuntimeError("kakasi exploded")
        with (
            patch.object(_romanize, "_kakasi", mock_kakasi),
            patch.object(_romanize, "_kakasi_checked", True),
            patch.object(_romanize, "get_cutlet", lambda: None),
        ):
            result = _romanize.romanize_japanese("東京")
        self.assertIsNone(result)

    def test_romanize_chinese_with_mock_pypinyin(self):
        """Verify pypinyin is called correctly when available."""
        fake_pinyin = MagicMock(return_value=[["cè"], ["shì"]])
        fake_style = MagicMock()
        fake_style.TONE = "TONE"
        fake_pypinyin = types.ModuleType("pypinyin")
        fake_pypinyin.pinyin = fake_pinyin
        fake_pypinyin.Style = fake_style
        with patch.dict(sys.modules, {"pypinyin": fake_pypinyin}):
            result = _romanize.romanize_chinese("测试")
        self.assertIsNotNone(result)
        fake_pinyin.assert_called_once()
