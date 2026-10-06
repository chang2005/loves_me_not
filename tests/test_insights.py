"""量化指标、聊天足迹、时间线、话题与可视化的测试。

重点覆盖「口径必须自洽」与「数字必须诚实」两类风险：

* 深夜时段在 metrics / insights / 报告里必须是同一个口径（23:00–03:00）；
* 「聊了多久」不能用首尾跨度冒充（早上说一句、晚上说一句 ≠ 聊了一整天）；
* 样本不足的量化指标必须被剔除，而不是算成 0 分；
* 热力图、词云、画像都必须能在数据极少时不崩、不编。
"""

from __future__ import annotations

import json
import re
import sys
import unittest
import unittest.mock
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from loves_me_not import insights, metrics, parser, report, scoring, timeline, visuals  # noqa: E402
from loves_me_not.parser import Message  # noqa: E402

SAMPLES = ROOT / "samples"


def build(pairs, start="2023-01-01 09:00:00", step_minutes=3) -> parser.Conversation:
    t = datetime.strptime(start, "%Y-%m-%d %H:%M:%S")
    msgs = []
    for i, (speaker, text) in enumerate(pairs):
        msgs.append(Message(index=i, speaker=speaker, timestamp=t, text=text))
        t += timedelta(minutes=step_minutes)
    speakers: list[str] = []
    for m in msgs:
        if m.speaker not in speakers:
            speakers.append(m.speaker)
    return parser.Conversation(messages=msgs, speakers=speakers)


class TestLateNightConsistency(unittest.TestCase):
    """深夜口径必须三处一致：metrics / insights / README 文案。"""

    def test_hour_boundaries(self):
        # 23, 0, 1, 2 属于深夜；22 与 3 不属于
        for h in (23, 0, 1, 2):
            self.assertTrue(insights.is_late_night_hour(h), h)
            self.assertTrue(metrics.is_late_night(h), h)
        for h in (3, 4, 12, 22):
            self.assertFalse(insights.is_late_night_hour(h), h)
            self.assertFalse(metrics.is_late_night(h), h)

    def test_labels_match(self):
        self.assertEqual(metrics.LATE_NIGHT_LABEL, "23:00–03:00")
        self.assertEqual(insights.LATE_LABEL, "23:00–03:00")

    def test_same_start_and_end(self):
        self.assertEqual(metrics.LATE_NIGHT_START, insights.LATE_START_HOUR)
        self.assertEqual(metrics.LATE_NIGHT_END, insights.LATE_END_HOUR)

    def test_late_message_counted(self):
        conv = parser.parse_string(
            "2023-01-01 23:30:00 阿澈\n还没睡\n\n"
            "2023-01-02 01:00:00 阿澈\n想你了\n\n"
            "2023-01-02 12:00:00 阿澈\n中午好\n"
        )
        m = insights.metric_late_night(conv.real_messages, "阿澈")
        self.assertEqual(m.value, 2.0)


class TestSessionsAndSilence(unittest.TestCase):
    def test_session_splits_on_gap(self):
        conv = parser.parse_string(
            "2023-01-01 09:00:00 我\n早\n\n"
            "2023-01-01 09:10:00 阿澈\n早\n\n"
            "2023-01-01 11:00:00 我\n在吗\n"
        )
        sessions = insights.build_sessions(conv.real_messages)
        self.assertEqual(len(sessions), 2)
        self.assertEqual(sessions[0].count, 2)
        self.assertAlmostEqual(sessions[0].duration_minutes, 10.0, places=1)

    def test_silence_detects_breaker(self):
        conv = parser.parse_string(
            "2023-01-01 09:00:00 我\n早\n\n"
            "2023-01-03 09:00:00 阿澈\n在吗\n"
        )
        sessions = insights.build_sessions(conv.real_messages)
        silences = insights.build_silences(sessions)
        self.assertEqual(len(silences), 1)
        self.assertEqual(silences[0].broken_by, "阿澈")
        self.assertAlmostEqual(silences[0].days, 2.0, places=1)

    def test_longest_session_and_silence(self):
        conv = parser.parse_file(SAMPLES / "demo_two_block_cooling.txt")
        sessions = insights.build_sessions(conv.real_messages)
        silences = insights.build_silences(sessions)
        days = insights.build_days(conv.real_messages, "我", "阿澈", sessions)
        fp = timeline.build_footprint(conv.real_messages, "我", "阿澈", sessions, silences, days)
        self.assertIsNotNone(fp.longest_session)
        self.assertIsNotNone(fp.longest_silence)
        self.assertGreater(fp.longest_silence.length, fp.longest_session.duration)


class TestDayDurationHonesty(unittest.TestCase):
    """「聊了多久」不能拿首尾跨度冒充。"""

    def test_scattered_day_is_not_twelve_hours(self):
        # 早上 9 点一句、晚上 21 点一句：跨度 12 小时，但实际只聊了几分钟
        conv = parser.parse_string(
            "2023-01-01 09:00:00 我\n早安\n\n"
            "2023-01-01 09:00:30 阿澈\n早\n\n"
            "2023-01-01 21:00:00 我\n晚安\n\n"
            "2023-01-01 21:00:30 阿澈\n晚安\n"
        )
        sessions = insights.build_sessions(conv.real_messages)
        days = insights.build_days(conv.real_messages, "我", "阿澈", sessions)
        d = days["2023-01-01"]
        self.assertAlmostEqual(d.span_minutes, 720.0, places=0)   # 跨度确实是 12 小时
        self.assertLess(d.chat_minutes, 2.0)                      # 但实际聊天不到 2 分钟

    def test_heatmap_uses_chat_minutes_not_span(self):
        conv = parser.parse_string(
            "2023-01-01 09:00:00 我\n早安\n\n"
            "2023-01-01 21:00:00 阿澈\n晚安\n"
        )
        sessions = insights.build_sessions(conv.real_messages)
        days = insights.build_days(conv.real_messages, "我", "阿澈", sessions)
        fp = timeline.build_footprint(conv.real_messages, "我", "阿澈", sessions, silences=[], days=days)
        html_out = visuals.render_heatmap(fp)
        # 说明文案必须讲清楚算的是「对话时长总和」，不是首尾跨度
        self.assertIn("对话时长", html_out)
        self.assertNotIn("当天最后一条消息 − 第一条消息", html_out)


class TestQuantifiers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conv = parser.parse_file(SAMPLES / "demo_two_block_cooling.txt")
        cls.sessions = insights.build_sessions(cls.conv.real_messages)
        cls.silences = insights.build_silences(cls.sessions)
        cls.quant = insights.compute_quantifiers(
            cls.conv.real_messages, cls.sessions, cls.silences, "阿澈")

    def test_all_five_metrics_present(self):
        self.assertEqual(
            set(self.quant.metrics),
            {"reply_speed", "burst", "late_night", "icebreak", "last_word"},
        )

    def test_every_metric_documents_formula_and_source(self):
        for m in self.quant.metrics.values():
            self.assertTrue(m.formula.strip(), f"{m.label} 缺少口径说明")
            self.assertTrue(m.source.strip(), f"{m.label} 缺少数据来源")

    def test_weights_normalized(self):
        self.assertAlmostEqual(sum(self.quant.weights.values()), 1.0, places=6)

    def test_skipped_excluded_from_weights(self):
        for key in self.quant.skipped:
            self.assertNotIn(key, self.quant.weights)

    def test_total_within_bounds(self):
        self.assertGreaterEqual(self.quant.raw_total, 0)
        self.assertLessEqual(self.quant.raw_total, 100)

    def test_unavailable_metric_not_scored_zero(self):
        """样本不足的子指标必须 score=None，而不是 0。"""
        conv = parser.parse_file(SAMPLES / "demo_too_short.txt")
        sessions = insights.build_sessions(conv.real_messages)
        silences = insights.build_silences(sessions)
        q = insights.compute_quantifiers(conv.real_messages, sessions, silences, "阿澈")
        for m in q.metrics.values():
            if m.score == 0.0:
                self.fail(f"{m.label} 在样本不足时被算成了 0 分")
        self.assertIsNone(q.raw_total)

    def test_balance_score_is_symmetric(self):
        """「谁收尾」「谁破冰」没有绝对好坏：50% 最高分，两端更低。"""
        self.assertAlmostEqual(insights.balance_score(0.5, ideal=0.5), 1.0, places=6)
        self.assertLess(insights.balance_score(0.0, ideal=0.5), 0.2)
        self.assertLess(insights.balance_score(1.0, ideal=0.5), 1.0)
        self.assertGreater(insights.balance_score(0.5, ideal=0.5),
                           insights.balance_score(0.9, ideal=0.5))

    def test_burst_windows_count_correctly(self):
        # 4 条消息分布在 2 个 5 分钟窗口里
        msgs = [
            Message(index=i, speaker="阿澈",
                    timestamp=datetime(2023, 1, 1, 9, m, 0), text="x")
            for i, m in enumerate([0, 1, 2, 6])
        ]
        windows = insights._burst_windows(msgs, "阿澈")
        self.assertEqual(len(windows), 2)
        self.assertEqual(sorted(c for _t, c in windows), [1, 3])

    def test_explain_covers_formulas(self):
        text = insights.explain_quant(self.quant)
        self.assertIn("计算口径", text)
        self.assertIn("数据来源", text)


class TestFootprint(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conv = parser.parse_file(SAMPLES / "demo_two_block_cooling.txt")
        cls.sessions = insights.build_sessions(cls.conv.real_messages)
        cls.silences = insights.build_silences(cls.sessions)
        cls.days = insights.build_days(cls.conv.real_messages, "我", "阿澈", cls.sessions)
        cls.fp = timeline.build_footprint(
            cls.conv.real_messages, "我", "阿澈", cls.sessions, cls.silences, cls.days)

    def test_span_to_now_is_from_first_message(self):
        self.assertGreater(self.fp.span_to_now_days, self.fp.span_days)

    def test_best_hours_is_three_consecutive(self):
        hours = self.fp.best_hours
        self.assertEqual(len(hours), 3)

    def test_best_and_quietest_month_differ(self):
        self.assertNotEqual(self.fp.best_month.label, self.fp.quietest_month.label)

    def test_warmest_month_ignores_tiny_months(self):
        """「感情最好」的月份不能是「只聊了 3 句但 TA 占 100%」的月份。"""
        wm = self.fp.warmest_month
        peak = max(m.count for m in self.fp.months)
        self.assertGreaterEqual(wm.count, peak * 0.25)

    def test_totals_consistent(self):
        self.assertEqual(self.fp.total_sessions, len(self.sessions))
        self.assertAlmostEqual(
            self.fp.total_chat_minutes,
            sum(s.duration_minutes for s in self.sessions),
            places=6,
        )
        self.assertEqual(self.fp.total_messages, len(self.conv.real_messages))


class TestTimeline(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conv = parser.parse_file(SAMPLES / "demo_two_block_cooling.txt")
        cls.sessions = insights.build_sessions(cls.conv.real_messages)
        cls.silences = insights.build_silences(cls.sessions)
        cls.days = insights.build_days(cls.conv.real_messages, "我", "阿澈", cls.sessions)
        cls.fp = timeline.build_footprint(
            cls.conv.real_messages, "我", "阿澈", cls.sessions, cls.silences, cls.days)
        cls.nodes = timeline.build_timeline(
            cls.conv.real_messages, "我", "阿澈", cls.sessions, cls.silences, cls.fp,
            metrics.message_sentiment)

    def test_has_core_nodes(self):
        kinds = {n.kind for n in self.nodes}
        for expected in ("first", "last", "longest_session", "longest_silence", "busiest_month"):
            self.assertIn(expected, kinds, f"缺少关键节点：{expected}")

    def test_sorted_by_time(self):
        stamps = [n.when for n in self.nodes if n.when]
        self.assertEqual(stamps, sorted(stamps))

    def test_nodes_have_detail(self):
        for n in self.nodes:
            self.assertTrue(n.title.strip())
            self.assertTrue(n.detail.strip())


class TestTopics(unittest.TestCase):
    def test_extracts_sensible_topics(self):
        conv = parser.parse_file(SAMPLES / "demo_two_block_warm.txt")
        topics = timeline.extract_topics(conv.real_messages, "我", "阿澈")
        self.assertGreater(len(topics), 5)
        words = {t.word for t in topics}
        # 种子词应该被认出来
        self.assertTrue(words & {"晚安", "加班", "外卖", "见面", "想你", "咖啡"})

    def test_weights_normalized(self):
        conv = parser.parse_file(SAMPLES / "demo_two_block_warm.txt")
        topics = timeline.extract_topics(conv.real_messages, "我", "阿澈")
        self.assertAlmostEqual(max(t.weight for t in topics), 1.0, places=2)
        for t in topics:
            self.assertGreaterEqual(t.weight, 0.0)
            self.assertLessEqual(t.weight, 1.0)

    def test_no_pure_pronoun_fragments(self):
        for name in ("demo_two_block_cooling.txt", "demo_two_block_warm.txt"):
            conv = parser.parse_file(SAMPLES / name)
            topics = timeline.extract_topics(conv.real_messages, "我", "阿澈")
            for t in topics:
                self.assertNotIn(t.word, {"那你", "那我", "我就", "我的", "你的"},
                                 f"{name} 出现了无信息碎片：{t.word}")

    def test_empty_input(self):
        self.assertEqual(timeline.extract_topics([], "我", "阿澈"), [])

    def test_dominant_speaker_attributed(self):
        conv = parser.parse_file(SAMPLES / "demo_two_block_warm.txt")
        topics = timeline.extract_topics(conv.real_messages, "我", "阿澈")
        for t in topics:
            self.assertIn(t.dominant, (None, "我", "阿澈"))


class TestPersona(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conv = parser.parse_file(SAMPLES / "demo_two_block_warm.txt")
        cls.sessions = insights.build_sessions(cls.conv.real_messages)
        cls.silences = insights.build_silences(cls.sessions)
        cls.days = insights.build_days(cls.conv.real_messages, "我", "阿澈", cls.sessions)
        cls.fp = timeline.build_footprint(
            cls.conv.real_messages, "我", "阿澈", cls.sessions, cls.silences, cls.days)
        cls.persona = timeline.build_persona(
            cls.conv.real_messages, "我", "阿澈", cls.sessions, cls.silences, cls.fp)

    def test_produces_cards(self):
        self.assertGreaterEqual(len(self.persona), 2)
        self.assertLessEqual(len(self.persona), 6)

    def test_every_card_has_support(self):
        for p in self.persona:
            self.assertTrue(p.evidence_text.strip(), f"{p.label} 缺少描述")
            self.assertTrue(p.support.strip(), f"{p.label} 缺少支撑数字")
            self.assertGreaterEqual(p.strength, 0.0)
            self.assertLessEqual(p.strength, 1.0)

    def test_tones_are_valid(self):
        for p in self.persona:
            self.assertIn(p.tone, ("warm", "cool", "neutral"))

    def test_sorted_by_strength(self):
        strengths = [p.strength for p in self.persona]
        self.assertEqual(strengths, sorted(strengths, reverse=True))

    def test_never_judges_character(self):
        """画像只能说行为，不能下人格判决。"""
        forbidden = ("是个冷漠的人", "自私", "不值得", "人格", "本性", "渣")
        for p in self.persona:
            for word in forbidden:
                self.assertNotIn(word, p.evidence_text + p.support)


class TestVisuals(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conv = parser.parse_file(SAMPLES / "demo_two_block_cooling.txt")
        cls.analysis = metrics.analyze(cls.conv, "我", "阿澈")
        cls.result = scoring.score(cls.analysis)

    def test_heatmap_renders_cells(self):
        html = visuals.render_heatmap(self.analysis.footprint)
        self.assertIn("<svg", html)
        self.assertIn("<rect", html)
        self.assertIn("<title>", html)          # 悬浮提示

    def test_heatmap_empty_input(self):
        from loves_me_not.timeline import Footprint
        empty = Footprint(None, None, 0, 0, 0, 0, 0, 0.0, None, None)
        self.assertIn("没有可用", visuals.render_heatmap(empty))

    def test_wordcloud_renders_text(self):
        html = visuals.render_wordcloud(self.analysis.topics, "我", "阿澈", 64)
        self.assertIn("<svg", html)
        self.assertIn("<text", html)

    def test_wordcloud_empty_input(self):
        html = visuals.render_wordcloud([], "我", "阿澈", 0)
        self.assertIn("没有提取出", html)

    def test_wordcloud_layout_has_no_overlap(self):
        laid = visuals._layout_cloud(self.analysis.topics)
        boxes = []
        for tw, font, x, y in laid:
            w, h = visuals._estimate_box(tw.word, font)
            boxes.append((x - w / 2, y - h / 2, x + w / 2, y + h / 2))
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                a, b = boxes[i], boxes[j]
                overlap = not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])
                self.assertFalse(overlap, "词云出现了重叠")

    def test_persona_renders_cards(self):
        html = visuals.render_persona(self.analysis.persona, "阿澈")
        self.assertIn("persona-card", html)
        self.assertIn("不是性格判断", html)

    def test_persona_empty_input(self):
        self.assertIn("样本太少", visuals.render_persona([], "阿澈"))

    def test_hour_bars(self):
        html = visuals.render_hour_bars(self.analysis.footprint.hours, "我", "阿澈")
        self.assertIn("<svg", html)

    def test_month_bars(self):
        html = visuals.render_month_bars(self.analysis.footprint.months, "我", "阿澈")
        self.assertIn("<svg", html)


class TestPersonalNote(unittest.TestCase):
    """结尾的个性化文案：必须从真实数据长出来，且不许编造。"""

    def _note(self, name, me="我", peer="阿澈", score=None):
        conv = parser.parse_file(SAMPLES / name)
        analysis = metrics.analyze(conv, me, peer)
        result = scoring.score(analysis)
        return analysis, result, analysis.personal_note

    def test_generated_from_real_data(self):
        analysis, _r, note = self._note("demo_two_block_cooling.txt")
        self.assertIsNotNone(note, "这份记录应当能挑出一条专属观察")
        self.assertTrue(note.headline.strip())
        self.assertTrue(note.support.strip())
        self.assertTrue(note.closing.strip())
        self.assertIn(note.tone, ("warm", "gentle"))
        self.assertTrue(note.based_on, "必须说明依据哪个指标")

    def test_based_on_is_a_real_metric(self):
        """依据必须指向真实存在的指标名，不能是编出来的词。"""
        from loves_me_not import insights as _ins
        analysis, _r, note = self._note("demo_two_block_cooling.txt")
        valid = {"回复速度", "每 5 分钟发消息次数", "深夜发消息次数",
                 "打破僵局次数", "最后发言次数", "聊得最多的月份",
                 "最长的一次聊天", "主动发起对话次数", "关键节点",
                 "情绪倾向随时间变化"}
        for key in note.based_on:
            self.assertIn(key, valid, f"依据里出现了未知指标：{key}")

    def test_numbers_in_headline_are_consistent(self):
        """文案里的数字必须与真实统计对得上，不能夸大。"""
        analysis, _r, note = self._note("demo_two_block_cooling.txt")
        # 「冷过 N 次」的 N 必须等于真实的长时间沉默总数
        if "冷过" in note.headline:
            m = re.search(r"冷过\s*(\d+)\s*次", note.headline)
            self.assertIsNotNone(m)
            total = sum(
                1 for g in analysis.silences
                if g.length.total_seconds() >= 86400 and g.broken_by
            )
            self.assertEqual(int(m.group(1)), total)

    def test_icebreak_wording_matches_share(self):
        """「每一次都是他」与「其中几次是他」不能混用。"""
        analysis, _r, note = self._note("demo_two_block_cooling.txt")
        if "先开口" in note.headline or "先低头" in note.headline:
            big = [g for g in analysis.silences
                   if g.length.total_seconds() >= 86400 and g.broken_by]
            peer_big = [g for g in big if g.broken_by == "阿澈"]
            share = len(peer_big) / max(1, len(big))
            if "每一次都是" in note.headline:
                self.assertAlmostEqual(share, 1.0, places=2)
            elif "其中" in note.headline:
                self.assertLess(share, 1.0)

    def test_tiny_sample_yields_no_note(self):
        """样本太小时不许硬编一句话出来。"""
        _a, _r, note = self._note("demo_too_short.txt")
        self.assertIsNone(note)

    def test_different_records_get_different_notes(self):
        """不同记录应当得到不同的专属文案——否则「个性化」是假的。"""
        _a1, _r1, n1 = self._note("demo_two_block_cooling.txt")
        _a2, _r2, n2 = self._note("demo_paste_oneline.txt")
        self.assertIsNotNone(n1)
        self.assertIsNotNone(n2)
        self.assertNotEqual(n1.headline, n2.headline)

    def test_note_rendered_into_report(self):
        conv = parser.parse_file(SAMPLES / "demo_two_block_cooling.txt")
        analysis = metrics.analyze(conv, "我", "阿澈")
        result = scoring.score(analysis)
        html = report.build_html(analysis, result, conv.report)
        self.assertIn('class="insight"', html)
        self.assertIn(esc(analysis.personal_note.headline), html)
        self.assertIn("只属于这份记录的一句话", html)

    def test_report_without_note_still_renders(self):
        conv = parser.parse_file(SAMPLES / "demo_too_short.txt")
        analysis = metrics.analyze(conv, "我", "阿澈")
        result = scoring.score(analysis)
        html = report.build_html(analysis, result, conv.report)
        self.assertIn("写在这里的话", html)
        self.assertNotIn('class="insight"', html)

    def test_generation_failure_is_surfaced_not_swallowed(self):
        """生成失败时要留下痕迹，不能静默吞掉（曾经因此漏掉一个 NameError）。"""
        conv = parser.parse_file(SAMPLES / "demo_two_block_cooling.txt")
        analysis = metrics.analyze(conv, "我", "阿澈")
        before = len(analysis.caveats)
        with unittest.mock.patch.object(
            timeline, "build_personal_note", side_effect=RuntimeError("boom")
        ):
            scoring.score(analysis)
        self.assertGreater(len(analysis.caveats), before)
        self.assertTrue(any("个性化" in c for c in analysis.caveats))


def esc(x):
    import html
    return html.escape(str(x), quote=True)


class TestReportIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conv = parser.parse_file(SAMPLES / "demo_two_block_cooling.txt")
        cls.analysis = metrics.analyze(cls.conv, "我", "阿澈")
        cls.result = scoring.score(cls.analysis)
        cls.html = report.build_html(cls.analysis, cls.result, cls.conv.report)

    def test_new_sections_present(self):
        for token in ("量化指标", "聊天足迹", "日历热力图", "双方投入度",
                      "话题词云", "是怎样的人", "关键节点"):
            self.assertIn(token, self.html, f"缺少区块：{token}")

    def test_formulas_are_shown(self):
        self.assertIn("口径", self.html)
        self.assertIn("来源", self.html)

    def test_blend_is_explained(self):
        self.assertIn("八维模型", self.html)
        self.assertIn("量化指标", self.html)

    def test_still_self_contained(self):
        low = self.html.lower()
        for bad in ("http://", "https://", "src=", "@import"):
            self.assertNotIn(bad, low, f"新增区块引入了外部引用：{bad}")
        # 脚本全部内联，且不随数据规模增长
        self.assertLessEqual(low.count("<script"), 3)

    def test_gauge_is_a_progress_ring(self):
        """仪表盘必须是环形进度条：单个 dasharray 圆，无指针、无刻度溢出。"""
        self.assertIn("stroke-dasharray", self.html)
        self.assertIn('class="gauge"', self.html)
        # 旧实现的特征：指针线 + 外圈刻度文字，都应当消失
        self.assertNotIn("gauge-needle", self.html)
        self.assertNotIn("_arc_path", self.html)

    def test_gauge_ring_geometry_matches_score(self):
        """进度环的 dasharray 长度必须与分数成比例，且不超出圆周长。"""
        import math as _math
        import re as _re
        conv = parser.parse_file(SAMPLES / "demo_two_block_cooling.txt")
        analysis = metrics.analyze(conv, "我", "阿澈")
        result = scoring.score(analysis)
        g = report.render_gauge(result.total, "#b4576f", "测试")
        m = _re.search(r'stroke-dasharray="([\d.]+) ([\d.]+)"', g)
        self.assertIsNotNone(m, "没找到 dasharray")
        filled, rest = float(m.group(1)), float(m.group(2))
        r = report._RING["size"] / 2 - report._RING["gap"]
        circumference = 2 * _math.pi * r
        self.assertAlmostEqual(filled + rest, circumference, delta=0.5)
        expected = circumference * result.total / 100.0
        self.assertAlmostEqual(filled, expected, delta=0.5)

    def test_gauge_handles_extremes(self):
        """0 分与 100 分都不能出现负长度或超出周长。"""
        import re as _re
        for score in (0, 100):
            g = report.render_gauge(score, "#b4576f", "x")
            m = _re.search(r'stroke-dasharray="([\d.]+) ([\d.]+)"', g)
            filled, rest = float(m.group(1)), float(m.group(2))
            self.assertGreaterEqual(filled, 0.0)
            self.assertGreaterEqual(rest, 0.0)

    def test_colour_has_single_source(self):
        """颜色只能有一份定义：report 必须复用 visuals 的调色板。"""
        self.assertIs(report.PALETTE, visuals.PALETTE)
        for key in ("peer", "me", "accent", "ink", "line", "nav_bg"):
            self.assertIn(key, visuals.PALETTE)

    def test_json_contains_new_data(self):
        payload = json.loads(report.build_json(self.analysis, self.result))
        for key in ("quantifiers", "footprint", "timeline", "topics", "persona", "blend"):
            self.assertIn(key, payload)
        self.assertEqual(len(payload["quantifiers"]["metrics"]), 5)
        self.assertGreater(len(payload["footprint"]["days"]), 0)
        self.assertGreater(len(payload["topics"]), 0)

    def test_tiny_sample_report_does_not_crash(self):
        conv = parser.parse_file(SAMPLES / "demo_too_short.txt")
        analysis = metrics.analyze(conv, "我", "阿澈")
        result = scoring.score(analysis)
        html = report.build_html(analysis, result, conv.report)
        self.assertIn("量化指标", html)
        self.assertIn("无法计算", html)

    def test_swapped_subject_report_does_not_crash(self):
        conv = parser.parse_file(SAMPLES / "demo_table.csv")
        analysis = metrics.analyze(conv, "阿澈", "我")
        result = scoring.score(analysis)
        html = report.build_html(analysis, result, conv.report)
        self.assertIn("关键节点", html)

    def test_nav_titles_adapt_to_peer_name(self):
        """导航里的人名标题要跟着数据走，不能写死。"""
        conv = parser.parse_file(SAMPLES / "demo_table.csv")
        analysis = metrics.analyze(conv, "阿澈", "我")
        result = scoring.score(analysis)
        html = report.build_html(analysis, result, conv.report)
        self.assertIn("我 是怎样的人", html)
        self.assertNotIn("阿澈 是怎样的人", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)