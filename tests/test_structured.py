"""结构化导出格式（JSON / 网页内嵌数据）的解析测试。

样例都是**手写合成**的（``samples/demo_export.json`` 与
``samples/demo_export_webfile.html``），不含任何真实聊天记录。
"""

from __future__ import annotations

import json
import unittest
from datetime import datetime
from pathlib import Path

from loves_me_not import parser
from loves_me_not import structured as S
from loves_me_not.parser import ParseError, is_system_text

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"
JSON_SAMPLE = SAMPLES / "demo_export.json"
HTML_SAMPLE = SAMPLES / "demo_export_webfile.html"


# --------------------------------------------------------------------------- #
# 时间解析
# --------------------------------------------------------------------------- #

class TestParseTimeValue(unittest.TestCase):
    def test_unix_seconds(self):
        dt = S.parse_time_value(1704067200)
        self.assertEqual(dt, datetime(2024, 1, 1, 8, 0, 0))

    def test_unix_millis(self):
        """毫秒时间戳要自动识别，否则会算出 5 万年后的日期。"""
        dt = S.parse_time_value(1704067200000)
        self.assertIsNotNone(dt)
        self.assertEqual(dt.year, 2024)

    def test_numeric_string(self):
        self.assertEqual(S.parse_time_value("1704067200"),
                         datetime(2024, 1, 1, 8, 0, 0))

    def test_iso_string(self):
        dt = S.parse_time_value("2024-01-01T08:00:00")
        self.assertEqual(dt, datetime(2024, 1, 1, 8, 0, 0))

    def test_slash_and_chinese_formats(self):
        self.assertEqual(S.parse_time_value("2024/01/01 08:00:00"),
                         datetime(2024, 1, 1, 8, 0, 0))
        self.assertEqual(S.parse_time_value("2024年1月1日 08:00"),
                         datetime(2024, 1, 1, 8, 0, 0))

    def test_garbage_returns_none(self):
        """认不出来就返回 None，绝不瞎猜一个时间。"""
        for bad in (None, "", "昨天", True, 0, -5, "not a time"):
            self.assertIsNone(S.parse_time_value(bad), f"{bad!r} 不该被解析出时间")


# --------------------------------------------------------------------------- #
# 内嵌数据提取
# --------------------------------------------------------------------------- #

class TestExtractEmbeddedJson(unittest.TestCase):
    def test_window_assignment(self):
        html = '<script>window.D = [{"t":"a"}];</script>'
        self.assertEqual(S.extract_embedded_json(html), [{"t": "a"}])

    def test_var_object(self):
        html = '<script>var data = {"messages": [1,2]};</script>'
        self.assertEqual(S.extract_embedded_json(html), {"messages": [1, 2]})

    def test_script_type_json(self):
        html = '<script type="application/json">{"a": 1}</script>'
        self.assertEqual(S.extract_embedded_json(html), {"a": 1})

    def test_braces_inside_strings_do_not_truncate(self):
        """正文里有花括号时不能被截断——这是不用正则匹配整个对象的原因。"""
        payload = [{"text": "他说 {不要} 这样", "n": 1}]
        html = "<script>window.D = " + json.dumps(payload, ensure_ascii=False) + ";</script>"
        self.assertEqual(S.extract_embedded_json(html), payload)

    def test_escaped_quotes_inside_strings(self):
        payload = [{"html": '<img src="a.jpg" alt="图片" />'}]
        html = "<script>window.D = " + json.dumps(payload, ensure_ascii=False) + ";</script>"
        got = S.extract_embedded_json(html)
        self.assertEqual(got, payload)

    def test_no_data_returns_none(self):
        self.assertIsNone(S.extract_embedded_json("<html><body>普通网页</body></html>"))


# --------------------------------------------------------------------------- #
# 正文 HTML → 纯文本
# --------------------------------------------------------------------------- #

class TestHtmlToText(unittest.TestCase):
    def test_time_div_is_stripped(self):
        """时间标签必须去掉，否则每条消息正文都会被时间污染。"""
        raw = ('<div class="message-time">2024-01-01 08:00:00</div>'
               '<div class="message-content"><div class="message-text">早</div></div>')
        self.assertEqual(S._html_to_text(raw), "早")

    def test_media_becomes_tag(self):
        raw = ('<div class="message-content">'
               '<img class="message-media image" src="a.jpg" alt="图片消息" /></div>')
        self.assertEqual(S._html_to_text(raw), "[图片]")

    def test_media_label_normalized(self):
        self.assertEqual(S._normalize_media_label("图片消息"), "图片")
        self.assertEqual(S._normalize_media_label("图片"), "图片")
        self.assertEqual(S._normalize_media_label(""), "图片")

    def test_entities_decoded(self):
        raw = '<div class="message-text">a &amp; b &lt;c&gt;</div>'
        self.assertEqual(S._html_to_text(raw), "a & b <c>")

    def test_plain_text_untouched(self):
        self.assertEqual(S._html_to_text("就是普通文本"), "就是普通文本")


# --------------------------------------------------------------------------- #
# 合成样例：JSON
# --------------------------------------------------------------------------- #

class TestJsonSample(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conv = parser.parse_file(JSON_SAMPLE)

    def test_parses_all_messages(self):
        self.assertEqual(len(self.conv.messages), 18)

    def test_speakers_from_session_remark(self):
        """会话对象的 remark（备注）优先——那是用户在聊天列表里看到的名字。"""
        self.assertIn("小满同学", self.conv.report.speakers)
        self.assertIn("我", self.conv.report.speakers)

    def test_only_two_speakers(self):
        """自己另一台设备产生的记录不该变成第三个说话人。"""
        self.assertEqual(len(self.conv.report.speakers), 2,
                         f"说话人应为 2 个，实际 {self.conv.report.speakers}")

    def test_timestamps_all_parsed(self):
        self.assertTrue(all(m.timestamp for m in self.conv.messages))

    def test_time_range(self):
        times = [m.timestamp for m in self.conv.messages]
        self.assertEqual(min(times).date(), datetime(2024, 1, 1).date())

    def test_system_message_flagged(self):
        sys_msgs = [m for m in self.conv.messages if m.is_system]
        self.assertTrue(sys_msgs, "应识别出系统消息")

    def test_system_message_text_has_no_double_prefix(self):
        """系统消息正文不应出现「[系统] [系统]…」这种重复前缀。"""
        for m in self.conv.messages:
            self.assertNotIn("[系统] [系统]", m.text, f"重复前缀：{m.text!r}")

    def test_media_messages_flagged(self):
        media = [m for m in self.conv.messages if m.is_media]
        self.assertTrue(media, "应识别出媒体消息")

    def test_file_message_keeps_filename(self):
        """文件消息应保留文件名，不能被压成纯标记。"""
        files = [m for m in self.conv.messages if "[文件]" in m.text]
        self.assertTrue(files, "应识别出文件消息")
        self.assertTrue(any("课程表" in m.text for m in files))

    def test_no_empty_text(self):
        for m in self.conv.messages:
            self.assertTrue(m.text.strip(), f"第 {m.index} 条正文为空")

    def test_file_kind_mentions_json(self):
        self.assertIn("JSON", self.conv.report.file_kind)

    def test_sender_flag_direction(self):
        """isSend=1 的应是我；方向搞反整份结论会颠倒。"""
        first = self.conv.messages[0]
        self.assertEqual(first.text, "早")
        self.assertEqual(first.speaker, "我")


# --------------------------------------------------------------------------- #
# 合成样例：网页内嵌数据
# --------------------------------------------------------------------------- #

class TestWebFileSample(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conv = parser.parse_file(HTML_SAMPLE)

    def test_parses_all_messages(self):
        self.assertEqual(len(self.conv.messages), 7)

    def test_contact_name_from_title(self):
        """网页导出的对方名字在 <title> 里，应被取出来。"""
        self.assertIn("小满同学", self.conv.report.speakers)
        self.assertNotIn("对方", self.conv.report.speakers)

    def test_time_stripped_from_text(self):
        for m in self.conv.messages:
            self.assertNotRegex(m.text, r"^\d{4}-\d{2}-\d{2}",
                                f"正文仍以时间开头：{m.text!r}")

    def test_first_message_text(self):
        self.assertEqual(self.conv.messages[0].text, "早")
        self.assertEqual(self.conv.messages[0].speaker, "我")

    def test_timestamps_parsed(self):
        self.assertTrue(all(m.timestamp for m in self.conv.messages))

    def test_media_detected_from_body(self):
        """没有类型字段时，靠正文里的 media 容器反推。"""
        media = [m for m in self.conv.messages if m.is_media]
        self.assertEqual(len(media), 1)
        self.assertIn("[图片]", media[0].text)

    def test_inline_emoji_kept(self):
        emoji = [m for m in self.conv.messages if "[笑脸]" in m.text]
        self.assertTrue(emoji, "内联表情文字应保留")

    def test_quote_marked(self):
        quoted = [m for m in self.conv.messages if "[引用]" in m.text]
        self.assertTrue(quoted, "引用消息应带标记")

    def test_file_kind_mentions_web(self):
        self.assertIn("网页", self.conv.report.file_kind)


# --------------------------------------------------------------------------- #
# 错误处理：认不出来就报错
# --------------------------------------------------------------------------- #

class TestStructuredErrors(unittest.TestCase):
    def test_plain_webpage_rejected(self):
        with self.assertRaises(ParseError):
            S.parse_structured("<html><body><p>就是一篇普通网页</p></body></html>")

    def test_json_without_messages_rejected(self):
        with self.assertRaises(ParseError):
            S.parse_structured('{"hello": "world"}')

    def test_empty_array_rejected(self):
        with self.assertRaises(ParseError):
            S.parse_structured("[]")

    def test_broken_json_rejected(self):
        with self.assertRaises(ParseError):
            S.parse_structured('[{"content": "x", ')

    def test_records_without_text_field_rejected(self):
        with self.assertRaises(ParseError):
            S.parse_structured('[{"a": 1, "b": 2}]')

    def test_looks_like_structured(self):
        self.assertTrue(S.looks_like_structured('{"a":1}'))
        self.assertTrue(S.looks_like_structured("  [1,2]"))
        self.assertTrue(S.looks_like_structured("<!DOCTYPE html><html>"))
        self.assertFalse(S.looks_like_structured("2024-01-01 08:00 小明\n你好"))


# --------------------------------------------------------------------------- #
# 格式自动识别
# --------------------------------------------------------------------------- #

class TestFormatDetection(unittest.TestCase):
    def test_json_extension(self):
        self.assertEqual(
            parser._detect_format(Path("a.json"), '{"messages":[]}'), "structured")

    def test_html_extension(self):
        self.assertEqual(
            parser._detect_format(Path("a.html"), "<html></html>"), "structured")

    def test_csv_extension(self):
        self.assertEqual(parser._detect_format(Path("a.csv"), "a,b,c\n1,2,3"), "csv")

    def test_text_extension(self):
        self.assertEqual(parser._detect_format(Path("a.txt"), "任意文本"), "text")

    def test_unknown_extension_sniffs_content(self):
        """扩展名没线索时按内容判断。"""
        self.assertEqual(
            parser._detect_format(Path("a.dat"), 'window.X = [{"a":1}]'), "structured")
        self.assertEqual(
            parser._detect_format(Path("a.dat"), "<!DOCTYPE html><html>"), "structured")
        self.assertEqual(
            parser._detect_format(Path("a.dat"), "2024-01-01 08:00 小明\n你好"), "text")


# --------------------------------------------------------------------------- #
# 与既有分析链路打通
# --------------------------------------------------------------------------- #

class TestStructuredEndToEnd(unittest.TestCase):
    def test_json_sample_runs_full_pipeline(self):
        """结构化输入必须能一路走到打分与报告，不需要上层特殊照顾。"""
        from loves_me_not.metrics import analyze, choose_pair
        from loves_me_not.scoring import score

        conv = parser.parse_file(JSON_SAMPLE)
        me, peer = choose_pair(conv, "我", "小满同学")
        analysis = analyze(conv, me, peer)
        result = score(analysis)
        self.assertGreaterEqual(result.total, 0)
        self.assertLessEqual(result.total, 100)
        self.assertTrue(result.tier)

    def test_web_sample_runs_full_pipeline(self):
        from loves_me_not.metrics import analyze, choose_pair
        from loves_me_not.scoring import score

        conv = parser.parse_file(HTML_SAMPLE)
        me, peer = choose_pair(conv, "我", "小满同学")
        analysis = analyze(conv, me, peer)
        result = score(analysis)
        self.assertGreaterEqual(result.total, 0)
        self.assertLessEqual(result.total, 100)

    def test_report_renders_for_structured_input(self):
        from loves_me_not.metrics import analyze, choose_pair
        from loves_me_not.report import build_html
        from loves_me_not.scoring import score

        conv = parser.parse_file(JSON_SAMPLE)
        me, peer = choose_pair(conv, "我", "小满同学")
        analysis = analyze(conv, me, peer)
        html = build_html(analysis, score(analysis), conv.report)
        self.assertIn("<!DOCTYPE html>", html)
        self.assertIn("小满同学", html)


if __name__ == "__main__":
    unittest.main()
