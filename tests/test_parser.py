"""解析器测试：覆盖各种真实导出格式与容错路径。"""

from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from loves_me_not import parser  # noqa: E402
from loves_me_not.parser import (  # noqa: E402
    ParseError,
    decode_bytes,
    has_emoji,
    looks_like_question,
    media_tag_of,
    parse_file,
    parse_string,
    parse_timestamp,
)

SAMPLES = ROOT / "samples"


class TestTimestamp(unittest.TestCase):
    def test_standard(self):
        self.assertEqual(parse_timestamp("2023-04-01 21:33:02"), datetime(2023, 4, 1, 21, 33, 2))

    def test_compact_and_cjk(self):
        self.assertEqual(parse_timestamp("2023/4/1 21:33"), datetime(2023, 4, 1, 21, 33, 0))
        self.assertEqual(parse_timestamp("2023年4月1日 21:33"), datetime(2023, 4, 1, 21, 33, 0))
        self.assertEqual(parse_timestamp("2023.4.1 9:05"), datetime(2023, 4, 1, 9, 5, 0))

    def test_fullwidth_colon_and_digits(self):
        self.assertEqual(parse_timestamp("2023-04-01 ２１：３３：０２"), datetime(2023, 4, 1, 21, 33, 2))

    def test_ampm(self):
        self.assertEqual(parse_timestamp("2023年4月1日 下午9:33"), datetime(2023, 4, 1, 21, 33, 0))
        self.assertEqual(parse_timestamp("2023年4月1日 上午9:33"), datetime(2023, 4, 1, 9, 33, 0))

    def test_time_only_uses_fallback(self):
        got = parse_timestamp("[21:33:02]", fallback_date=datetime(2023, 4, 1))
        self.assertEqual(got, datetime(2023, 4, 1, 21, 33, 2))

    def test_garbage_returns_none(self):
        for bad in ("", "   ", "阿澈", "你好呀", "202341", "abc:def"):
            self.assertIsNone(parse_timestamp(bad), bad)

    def test_month_day_without_year(self):
        got = parse_timestamp("04-01 21:33", fallback_date=datetime(2023, 1, 1))
        self.assertEqual(got, datetime(2023, 4, 1, 21, 33))


class TestHeuristics(unittest.TestCase):
    def test_questions(self):
        for q in ("你吃饭了吗", "在哪", "为什么这样", "周末有空吗？", "几点到", "好不好"):
            self.assertTrue(looks_like_question(q), q)

    def test_non_questions(self):
        for n in ("好的", "我今天很累", "晚安", "图片", ""):
            self.assertFalse(looks_like_question(n), n)

    def test_media_tags(self):
        self.assertEqual(media_tag_of("[图片]"), "图片")
        self.assertEqual(media_tag_of("【语音】"), "语音")
        self.assertIsNone(media_tag_of("这是[图片]混在文字里"))
        self.assertIsNone(media_tag_of("普通消息"))

    def test_emoji(self):
        self.assertTrue(has_emoji("今天好开心😄"))
        self.assertTrue(has_emoji("[微笑]"))
        self.assertTrue(has_emoji("QAQ"))
        self.assertFalse(has_emoji("今天好开心"))


class TestEncoding(unittest.TestCase):
    def test_bom(self):
        text, enc = decode_bytes("你好".encode("utf-8-sig"))
        self.assertEqual(text, "你好")
        self.assertEqual(enc, "utf-8-sig")

    def test_gbk(self):
        text, enc = decode_bytes("阿澈：晚安".encode("gb18030"))
        self.assertEqual(text, "阿澈：晚安")
        self.assertIn(enc, ("gb18030", "utf-8"))

    def test_utf16(self):
        text, enc = decode_bytes("阿澈 晚安".encode("utf-16"))
        self.assertEqual(text, "阿澈 晚安")


class TestTextFormats(unittest.TestCase):
    def test_wechat_two_line(self):
        conv = parse_string(
            "2023-04-01 21:33:02 阿澈\n到家了跟我说一声\n\n2023-04-01 21:35:11 我\n到啦\n"
        )
        self.assertEqual(len(conv.messages), 2)
        self.assertEqual(conv.speakers, ["阿澈", "我"])
        self.assertEqual(conv.messages[0].speaker, "阿澈")
        self.assertEqual(conv.messages[0].text, "到家了跟我说一声")
        self.assertEqual(conv.messages[0].timestamp, datetime(2023, 4, 1, 21, 33, 2))

    def test_bracket_format(self):
        conv = parse_string("[2023-04-01 21:33:02] 阿澈\n到家了\n")
        self.assertEqual(conv.messages[0].speaker, "阿澈")
        self.assertEqual(conv.messages[0].text, "到家了")

    def test_qq_with_id(self):
        conv = parse_string("2023-04-01 21:33:02 阿澈(12345678)\n到家了\n")
        self.assertIn("阿澈", conv.messages[0].speaker)
        self.assertEqual(conv.messages[0].text, "到家了")

    def test_name_before_timestamp(self):
        conv = parse_string("阿澈 2023-04-01 21:33:02\n到家了\n")
        self.assertEqual(conv.messages[0].speaker, "阿澈")
        self.assertEqual(conv.messages[0].timestamp, datetime(2023, 4, 1, 21, 33, 2))

    def test_multiline_message_merged(self):
        conv = parse_string(
            "2023-04-01 21:33:02 阿澈\n第一行\n第二行\n第三行\n\n2023-04-01 21:35:00 我\n好\n"
        )
        self.assertEqual(len(conv.messages), 2)
        self.assertEqual(conv.messages[0].text, "第一行\n第二行\n第三行")
        self.assertTrue(conv.messages[0].multiline)

    def test_colon_form(self):
        conv = parse_string("阿澈: 到家了\n我: 到啦\n")
        self.assertEqual([m.speaker for m in conv.messages], ["阿澈", "我"])

    def test_colon_form_with_timestamp(self):
        conv = parse_string("2023-04-01 21:33:02 阿澈: 到家了\n")
        self.assertEqual(conv.messages[0].speaker, "阿澈")
        self.assertEqual(conv.messages[0].text, "到家了")

    def test_url_not_treated_as_speaker(self):
        # URL 里的冒号不能被当成「昵称: 正文」
        conv = parse_string("2023-04-01 21:33:02 阿澈\n看这个\nhttps://example.com/a:b\n")
        self.assertEqual(conv.speakers, ["阿澈"])
        self.assertIn("example.com", conv.messages[0].text)

    def test_system_messages_flagged(self):
        conv = parse_string(
            "2023-04-01 21:33:02 阿澈\n到家了\n\n"
            "2023-04-01 21:34:00 我\n\"阿澈\"撤回了一条消息\n\n"
            "2023-04-01 21:35:00 阿澈\n好\n"
        )
        self.assertEqual(sum(1 for m in conv.messages if m.is_system), 1)
        self.assertEqual(len(conv.real_messages), 2)

    def test_media_flagged_and_zero_length(self):
        conv = parse_string("2023-04-01 21:33:02 阿澈\n[图片]\n\n2023-04-01 21:35:00 我\n好看\n")
        self.assertTrue(conv.messages[0].is_media)
        self.assertTrue(conv.messages[0].is_empty)

    def test_time_only_rolls_over_midnight(self):
        conv = parse_string("2023-04-01 23:50:00 阿澈\n晚安\n\n[00:10:00] 我\n嗯\n")
        self.assertEqual(conv.messages[1].timestamp.day, 2)
        self.assertEqual(conv.messages[1].timestamp.hour, 0)

    def test_time_only_infers_date(self):
        conv = parse_string("[21:33:02] 阿澈\n到家了\n")
        self.assertIsNotNone(conv.messages[0].timestamp)
        self.assertEqual(conv.messages[0].timestamp.hour, 21)

    def test_standalone_timestamp_line_then_name(self):
        conv = parse_string("2023-04-01 21:33:02\n阿澈\n到家了\n")
        self.assertEqual(conv.messages[0].speaker, "阿澈")
        self.assertEqual(conv.messages[0].timestamp, datetime(2023, 4, 1, 21, 33, 2))
        self.assertEqual(conv.messages[0].text, "到家了")

    def test_empty_file_raises(self):
        with self.assertRaises(ParseError):
            parse_string("\n\n   \n")

    def test_binary_garbage_raises(self):
        with self.assertRaises(ParseError):
            parse_string("\x00\x01\x02\x03" * 50)


class TestCsv(unittest.TestCase):
    def test_memotrace_like(self):
        conv = parse_file(SAMPLES / "demo_table.csv")
        self.assertEqual(len(conv.messages), 39)
        self.assertEqual(set(conv.speakers), {"阿澈", "我"})
        self.assertEqual(conv.messages[0].speaker, "阿澈")
        self.assertEqual(conv.messages[0].text, "下班了吗")
        self.assertEqual(conv.messages[0].timestamp, datetime(2023, 6, 1, 20, 11, 3))

    def test_chinese_headers(self):
        conv = parse_string("时间,发送者,内容\n2023-04-01 21:33:02,阿澈,到家了\n", fmt="csv")
        self.assertEqual(conv.messages[0].speaker, "阿澈")
        self.assertEqual(conv.messages[0].text, "到家了")

    def test_no_header(self):
        conv = parse_string("2023-04-01 21:33:02,阿澈,到家了\n2023-04-01 21:35:00,我,好\n", fmt="csv")
        self.assertEqual([m.speaker for m in conv.messages], ["阿澈", "我"])

    def test_tab_delimited(self):
        conv = parse_string("Time\tSender\tContent\n2023-04-01 21:33:02\t阿澈\t到家了\n", fmt="csv")
        self.assertEqual(conv.messages[0].text, "到家了")

    def test_content_with_commas_quoted(self):
        conv = parse_string(
            'localId,Time,Sender,Content\n1,2023-04-01 21:33:02,阿澈,"好,我知道了"\n', fmt="csv"
        )
        self.assertEqual(conv.messages[0].text, "好,我知道了")

    def test_media_in_csv(self):
        conv = parse_string("Time,Sender,Content\n2023-04-01 21:33:02,阿澈,[图片]\n", fmt="csv")
        self.assertTrue(conv.messages[0].is_media)

    def test_single_column_raises(self):
        with self.assertRaises(ParseError):
            parse_string("a\nb\nc\n", fmt="csv")


    def test_sample_files_all_parse(self):
        """samples/ 下每个数据文件都必须能被解析——它们是文档与 demo 的来源。

        用 ``demo_*`` 前缀而不是逐个硬编文件名：以后加一个示例就会自动被覆盖到。
        目录里允许有 README 之类的说明文件，所以按扩展名过滤。
        """
        files = sorted(
            f for f in SAMPLES.glob("demo_*")
            if f.suffix.lower() in (".txt", ".csv", ".log", ".json")
        )
        self.assertGreaterEqual(len(files), 5, f"示例数据太少：{[f.name for f in files]}")
        for f in files:
            with self.subTest(file=f.name):
                conv = parse_file(f)
                self.assertGreater(len(conv.messages), 0, f"{f.name} 没解析出消息")
                self.assertTrue(conv.speakers, f"{f.name} 没解析出说话人")


class TestWechatPcPaste(unittest.TestCase):
    """微信 PC 端「复制」出来的格式：``昵称  日期 时间``（两个空格）。"""

    def test_parses_fully(self):
        conv = parse_file(SAMPLES / "demo_paste_oneline.txt")
        self.assertEqual(set(conv.speakers), {"阿澈", "我"})
        self.assertEqual(conv.messages[0].speaker, "阿澈")
        self.assertEqual(conv.messages[0].text, "今天加班到好晚")
        self.assertEqual(conv.messages[0].timestamp, datetime(2023, 11, 5, 22, 14, 3))

    def test_retracted_message_is_system(self):
        conv = parse_file(SAMPLES / "demo_paste_oneline.txt")
        self.assertEqual(sum(1 for m in conv.messages if m.is_system), 1)

    def test_no_text_leaked_into_speaker_names(self):
        """正文绝不能被当成昵称——这会静默毁掉整份报告。"""
        conv = parse_file(SAMPLES / "demo_paste_oneline.txt")
        for m in conv.messages:
            self.assertIn(m.speaker, {"阿澈", "我"},
                          f"出现了非预期说话人：{m.speaker!r}")
            self.assertNotIn("辛苦", m.speaker)
            self.assertNotIn("今天", m.speaker)


class TestSampleFiles(unittest.TestCase):
    def test_cooling_sample_parses(self):
        conv = parse_file(SAMPLES / "demo_two_block_cooling.txt")
        self.assertGreater(len(conv.messages), 50)
        self.assertEqual(set(conv.speakers), {"阿澈", "我"})

    def test_warm_sample_parses(self):
        conv = parse_file(SAMPLES / "demo_two_block_warm.txt")
        self.assertGreater(len(conv.messages), 30)

    def test_tiny_sample_parses(self):
        conv = parse_file(SAMPLES / "demo_too_short.txt")
        self.assertEqual(len(conv.messages), 5)

    def test_describe_runs(self):
        conv = parse_file(SAMPLES / "demo_two_block_warm.txt")
        text = parser.describe(conv)
        self.assertIn("解析消息数", text)

    def test_missing_file_raises(self):
        with self.assertRaises(ParseError):
            parse_file(SAMPLES / "nope-does-not-exist.txt")


if __name__ == "__main__":
    unittest.main(verbosity=2)