"""维度计算、打分模型与报告生成的测试。

重点覆盖三件「承诺过就必须做到」的事：

1. 无信息的维度会被剔除，而不是当成 0 分；
2. 样本不足时会给「样本不足」而不是强结论；
3. 报告是自包含的（没有外部请求）、且所有用户文本都被转义。
"""

from __future__ import annotations

import json
import re
import sys
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from loves_me_not import metrics, parser, report, scoring  # noqa: E402
from loves_me_not.metrics import Message  # noqa: E402

SAMPLES = ROOT / "samples"


def build(pairs: list[tuple[str, str, str]], *, start: str = "2023-01-01 09:00:00",
          step_minutes: int = 3) -> parser.Conversation:
    """按 (speaker, text, ...) 快速造一份对话。"""
    t = datetime.strptime(start, "%Y-%m-%d %H:%M:%S")
    msgs = []
    for i, (speaker, text, _) in enumerate(pairs):
        msgs.append(Message(index=i, speaker=speaker, timestamp=t, text=text))
        t += timedelta(minutes=step_minutes)
    speakers: list[str] = []
    for m in msgs:
        if m.speaker not in speakers:
            speakers.append(m.speaker)
    return parser.Conversation(messages=msgs, speakers=speakers)


class TestSentiment(unittest.TestCase):
    def test_positive_and_negative(self):
        self.assertGreater(metrics.message_sentiment("我好喜欢你"), 0)
        self.assertLess(metrics.message_sentiment("我们分手吧"), 0)

    def test_neutral(self):
        self.assertEqual(metrics.message_sentiment("今天星期三"), 0.0)

    def test_negation_flips_positive(self):
        plain = metrics.message_sentiment("我喜欢你")
        negated = metrics.message_sentiment("我不喜欢你")
        self.assertLess(negated, plain)
        self.assertLess(negated, 0)

    def test_intensifier_amplifies(self):
        self.assertGreater(
            metrics.message_sentiment("我非常想你"),
            metrics.message_sentiment("想你"),
        )

    def test_empty_is_zero(self):
        self.assertEqual(metrics.message_sentiment(""), 0.0)


class TestThreadsAndDelays(unittest.TestCase):
    def test_thread_split_on_gap(self):
        conv = parser.parse_string(
            "2023-01-01 09:00:00 我\n早\n\n"
            "2023-01-01 09:01:00 阿澈\n早\n\n"
            "2023-01-01 20:00:00 我\n在吗\n"
        )
        threads = metrics.build_threads(conv.real_messages)
        self.assertEqual(len(threads), 2)
        self.assertEqual(threads[0][0].speaker, "我")
        self.assertEqual(threads[1][0].speaker, "我")

    def test_reply_delay_measured(self):
        conv = parser.parse_string(
            "2023-01-01 09:00:00 我\n在吗\n\n"
            "2023-01-01 09:05:00 阿澈\n在\n"
        )
        delays = metrics.reply_delays(conv.real_messages, "阿澈")
        self.assertEqual(len(delays), 1)
        self.assertAlmostEqual(delays[0], 300.0, places=1)

    def test_huge_gap_not_a_reply(self):
        # 间隔 30 小时 > MAX_REPLY_GAP，不应算作「回复」
        conv = parser.parse_string(
            "2023-01-01 09:00:00 我\n在吗\n\n"
            "2023-01-02 15:00:00 阿澈\n在\n"
        )
        self.assertEqual(metrics.reply_delays(conv.real_messages, "阿澈"), [])


class TestDimensionAvailability(unittest.TestCase):
    def test_no_intimate_terms_is_not_zero(self):
        """全程没有亲昵称呼 → 该维度必须是「无信息」，而不是 0 分。"""
        pairs = [("我" if i % 2 else "阿澈", "今天天气不错啊我们出去走走吧", "") for i in range(30)]
        conv = build(pairs)
        analysis = metrics.analyze(conv, "我", "阿澈")
        dim = analysis.dimensions["address"]
        self.assertIsNone(dim.score_peer)
        self.assertIsNone(dim.score_me)
        self.assertFalse(dim.available)
        self.assertIn("没有信息", dim.summary)

    def test_tiny_sample_has_no_dimension_scores(self):
        conv = parser.parse_file(SAMPLES / "sample_tiny.txt")
        analysis = metrics.analyze(conv, "我", "阿澈")
        available = [d.key for d in analysis.dimensions.values() if d.available]
        self.assertEqual(available, [])

    def test_warm_sample_has_most_dimensions(self):
        conv = parser.parse_file(SAMPLES / "sample_wechat_warm.txt")
        analysis = metrics.analyze(conv, "我", "阿澈")
        available = [d.key for d in analysis.dimensions.values() if d.available]
        self.assertGreaterEqual(len(available), 6)

    def test_cards_do_not_assert_unreliable_numbers(self):
        """样本不足时，关键数据卡片不能把被剔除的统计量当事实展示。"""
        conv = parser.parse_file(SAMPLES / "sample_tiny.txt")
        analysis = metrics.analyze(conv, "我", "阿澈")
        cards = {title: (value, sub) for title, value, sub in analysis.cards}
        for title in ("中位回复间隔", "TA 主动发起占比", "实质性字数比",
                      "TA 收尾占比", "TA 平均情绪分"):
            value, sub = cards[title]
            self.assertEqual(value, "—", f"{title} 不该显示数值：{value}")
            # 备注必须说清「为什么没有数」：不足 / 太少 / 无数据
            self.assertTrue(
                any(w in sub for w in ("不足", "太少", "无数据")),
                f"{title} 缺少可靠性说明：{sub}",
            )
        # 计数类卡片仍然可以展示——它们不是「推算」，是「数出来的」
        self.assertEqual(cards["有效消息"][0], "5 条")

    def test_cards_show_values_when_sample_is_sufficient(self):
        conv = parser.parse_file(SAMPLES / "sample_wechat_cooling.txt")
        analysis = metrics.analyze(conv, "我", "阿澈")
        cards = {title: value for title, value, _sub in analysis.cards}
        for title in ("中位回复间隔", "TA 主动发起占比", "TA 收尾占比"):
            self.assertNotEqual(cards[title], "—", f"{title} 应当有数值")


class TestChoosePair(unittest.TestCase):
    def test_two_speakers_auto(self):
        conv = parser.parse_file(SAMPLES / "sample_wechat_warm.txt")
        me, peer = metrics.choose_pair(conv, None, None)
        self.assertEqual({me, peer}, {"我", "阿澈"})

    def test_explicit_wins(self):
        conv = parser.parse_file(SAMPLES / "sample_wechat_warm.txt")
        self.assertEqual(metrics.choose_pair(conv, "阿澈", "我"), ("阿澈", "我"))

    def test_only_me(self):
        conv = parser.parse_file(SAMPLES / "sample_wechat_warm.txt")
        me, peer = metrics.choose_pair(conv, "我", None)
        self.assertEqual(me, "我")
        self.assertEqual(peer, "阿澈")

    def test_unknown_name_raises(self):
        conv = parser.parse_file(SAMPLES / "sample_wechat_warm.txt")
        with self.assertRaises(ValueError):
            metrics.choose_pair(conv, "张三", None)


class TestScoring(unittest.TestCase):
    def test_weights_normalized(self):
        conv = parser.parse_file(SAMPLES / "sample_wechat_cooling.txt")
        analysis = metrics.analyze(conv, "我", "阿澈")
        result = scoring.score(analysis)
        total_w = sum(result.peer.used.values())
        self.assertAlmostEqual(total_w, 1.0, places=6)

    def test_skipped_dimensions_excluded_and_renormalized(self):
        conv = parser.parse_file(SAMPLES / "sample_memotrace.csv")
        analysis = metrics.analyze(conv, "我", "阿澈")
        result = scoring.score(analysis)
        # 被剔除的维度不能出现在权重里
        for key in result.peer.skipped:
            self.assertNotIn(key, result.peer.used)
        self.assertAlmostEqual(sum(result.peer.used.values()), 1.0, places=6)

    def test_score_within_bounds(self):
        for name, me, peer in (
            ("sample_wechat_warm.txt", "我", "阿澈"),
            ("sample_wechat_cooling.txt", "我", "阿澈"),
            ("sample_memotrace.csv", "我", "阿澈"),
        ):
            conv = parser.parse_file(SAMPLES / name)
            result = scoring.score(metrics.analyze(conv, me, peer))
            self.assertGreaterEqual(result.total, 0)
            self.assertLessEqual(result.total, 100)

    def test_insufficient_sample_flagged_and_not_strong(self):
        conv = parser.parse_file(SAMPLES / "sample_tiny.txt")
        result = scoring.score(metrics.analyze(conv, "我", "阿澈"))
        self.assertTrue(result.insufficient)
        self.assertEqual(result.tier.key, "unclear")

    def test_small_sample_shrinks_toward_middle(self):
        """样本越小，原始分被拉向 50 的幅度越大。"""
        small = parser.parse_file(SAMPLES / "sample_wechat_warm.txt")
        r_small = scoring.score(metrics.analyze(small, "我", "阿澈"))
        if r_small.peer.score is not None:
            self.assertLess(abs(r_small.total - 50), abs(r_small.raw_total - 50) + 1e-6)

    def test_warm_scores_higher_than_cooling(self):
        """更暖的记录应当得到更高的分——打分方向不能反。"""
        warm = parser.parse_file(SAMPLES / "sample_wechat_warm.txt")
        cool = parser.parse_file(SAMPLES / "sample_wechat_cooling.txt")
        r_warm = scoring.score(metrics.analyze(warm, "我", "阿澈"))
        r_cool = scoring.score(metrics.analyze(cool, "我", "阿澈"))
        self.assertGreater(r_warm.peer.score or 0, r_cool.peer.score or 0)

    def test_winners_are_consistent(self):
        """加分项必须真的高于基准，拖后腿项必须真的低于基准。"""
        conv = parser.parse_file(SAMPLES / "sample_wechat_cooling.txt")
        result = scoring.score(metrics.analyze(conv, "我", "阿澈"))
        for _label, s, _w in result.top_positive:
            self.assertGreater(s, 0.5)
        for _label, s, _w in result.top_negative:
            self.assertLess(s, 0.5)

    def test_each_tier_has_distinct_comfort(self):
        titles = [c[0] for c in scoring.COMFORT.values()]
        self.assertEqual(len(titles), len(set(titles)))
        # 六个评分档 + 样本不足档
        self.assertEqual(len(scoring.COMFORT), len(scoring.TIERS) + 1)

    def test_comfort_is_gentle_not_blaming(self):
        """安慰文案不许说教、不许指责。"""
        forbidden = ("你应该早点", "都是你的错", "活该", "笨", "愚蠢", "自作自受")
        for _key, (_title, body) in scoring.COMFORT.items():
            for word in forbidden:
                self.assertNotIn(word, body, f"{_key} 出现了指责性措辞：{word}")

    def test_tier_boundaries(self):
        self.assertEqual(scoring.tier_for(100).key, "hot")
        self.assertEqual(scoring.tier_for(85).key, "hot")
        self.assertEqual(scoring.tier_for(70).key, "warm")
        self.assertEqual(scoring.tier_for(55).key, "lukewarm")
        self.assertEqual(scoring.tier_for(40).key, "cooling")
        self.assertEqual(scoring.tier_for(20).key, "cold")
        self.assertEqual(scoring.tier_for(0).key, "frozen")

    def test_evidence_cap_scales_with_sample(self):
        conv = parser.parse_file(SAMPLES / "sample_tiny.txt")
        analysis = metrics.analyze(conv, "我", "阿澈")
        result = scoring.score(analysis)
        self.assertLessEqual(len(result.evidence), 3)

    def test_evidence_deduplicated(self):
        conv = parser.parse_file(SAMPLES / "sample_wechat_cooling.txt")
        result = scoring.score(metrics.analyze(conv, "我", "阿澈"))
        texts = [e.text.strip() for e in result.evidence]
        self.assertEqual(len(texts), len(set(texts)))

    def test_explain_is_readable(self):
        conv = parser.parse_file(SAMPLES / "sample_wechat_cooling.txt")
        analysis = metrics.analyze(conv, "我", "阿澈")
        result = scoring.score(analysis)
        text = scoring.explain(result, analysis)
        self.assertIn("总分", text)
        self.assertIn("权重", text)

    def test_swapped_subject_does_not_crash(self):
        """回归：当说话人顺序反过来（自动选人），双向视角会借用一方的分数。

        早期版本在报告里事后反推维度分，遇到 ``score_me is None`` 会直接
        TypeError 崩掉。这里锁住这个行为。
        """
        for me, peer in (("我", "阿澈"), ("阿澈", "我")):
            conv = parser.parse_file(SAMPLES / "sample_memotrace.csv")
            analysis = metrics.analyze(conv, me, peer)
            result = scoring.score(analysis)
            html = report.build_html(analysis, result, conv.report)
            self.assertIn("分数是怎么算出来的", html)
            scoring.explain(result, analysis)

    def test_scores_used_matches_contributions(self):
        """每个维度的贡献必须等于「采用分数 × 归一化权重」，账要能对上。"""
        conv = parser.parse_file(SAMPLES / "sample_wechat_cooling.txt")
        analysis = metrics.analyze(conv, "我", "阿澈")
        result = scoring.score(analysis)
        for part in (result.peer, result.me, result.pair):
            for k, w in part.used.items():
                expected = part.scores_used[k] * 100.0 * w
                self.assertAlmostEqual(part.contributions[k], expected, places=6)

    def test_pair_falls_back_to_single_side(self):
        """只有一方可得分的维度，双向视角不该被不存在的 0 拉平。"""
        conv = parser.parse_file(SAMPLES / "sample_wechat_cooling.txt")
        analysis = metrics.analyze(conv, "我", "阿澈")
        result = scoring.score(analysis)
        pair = result.pair.scores_used
        peer = result.peer.scores_used
        me = result.me.scores_used
        for key in ("ending", "latenight"):
            if key in pair and key not in me and key in peer:
                self.assertAlmostEqual(pair[key], peer[key], places=6)


class TestRedaction(unittest.TestCase):
    def test_phone_redacted(self):
        self.assertNotIn("13812345678", report.redact("我的电话是13812345678"))

    def test_id_card_redacted(self):
        self.assertNotIn("110101199001011234", report.redact("身份证110101199001011234"))

    def test_email_redacted(self):
        self.assertNotIn("a@b.com", report.redact("邮箱 a@b.com"))

    def test_bank_card_redacted(self):
        self.assertNotIn("6222021234567890123", report.redact("卡号 6222021234567890123"))

    def test_wechat_id_redacted(self):
        for text in ("微信是 wechat_cyh123", "微信号：cyh_wechat99", "微信号：小澈1990"):
            self.assertNotIn("cyh", report.redact(text).lower())
            self.assertIn("打码", report.redact(text))

    def test_qq_number_redacted(self):
        self.assertNotIn("389574063", report.redact("QQ: 389574063"))

    def test_address_redacted(self):
        self.assertNotIn("文三路123号", report.redact("我住杭州市西湖区文三路123号"))

    def test_normal_text_untouched(self):
        for text in ("今天天气不错", "我们去公园吧", "晚安", "你吃饭了吗"):
            self.assertEqual(report.redact(text), text)

    def test_redaction_applied_to_report_evidence(self):
        """端到端：带 PII 的记录经 --redact 后，报告里不能残留敏感串。

        注意：敏感文本不一定被选进「原话证据」（取决于打分），
        所以这里断言的是**报告全文**不残留，而不是「未脱敏时一定出现」。
        """
        conv = parser.parse_string(
            "2023-01-01 09:00:00 我\n我的手机号13812345678 你记一下好不好\n\n"
            "2023-01-01 09:01:00 阿澈\n好 我的邮箱 a@b.com 我记下了\n\n"
            "2023-01-01 09:02:00 我\n还有我的身份证110101199001011234\n\n"
            "2023-01-01 09:03:00 阿澈\n收到\n"
        )
        analysis = metrics.analyze(conv, "我", "阿澈")
        result = scoring.score(analysis)
        report._apply_redaction(analysis, result)
        html = report.build_html(analysis, result, conv.report)
        for leak in ("13812345678", "a@b.com", "110101199001011234"):
            self.assertNotIn(leak, html, f"报告里残留了敏感串：{leak}")

    def test_redaction_touches_all_evidence_lists(self):
        """不只 result.evidence，各维度与关键节点里的原话也要一起脱敏。"""
        conv = parser.parse_string(
            "2023-01-01 09:00:00 我\n我的手机号13812345678\n\n"
            "2023-01-01 09:01:00 阿澈\n好的\n"
        )
        analysis = metrics.analyze(conv, "我", "阿澈")
        result = scoring.score(analysis)
        report._apply_redaction(analysis, result)
        for dim in analysis.dimensions.values():
            for ev in dim.evidence:
                self.assertNotIn("13812345678", ev.text)
        # 关键节点会引用「第一次/最后一次说话」的原话，这也是泄漏点
        for node in analysis.timeline_nodes:
            self.assertNotIn("13812345678", node.detail)


class TestReport(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.conv = parser.parse_file(SAMPLES / "sample_wechat_cooling.txt")
        cls.analysis = metrics.analyze(cls.conv, "我", "阿澈")
        cls.result = scoring.score(cls.analysis)
        cls.html = report.build_html(cls.analysis, cls.result, cls.conv.report)

    def test_no_raw_markdown_in_html(self):
        """报告是 HTML，不能漏出 ``**粗体**`` 这类 markdown 记号。"""
        self.assertNotIn("**", self.html)

    def test_no_external_requests(self):
        """单文件必须自包含：不能有任何 http(s) 引用、外链、CDN。

        允许**内联** ``<script>``（章节导航高亮 + 入场动效，以及提前
        判断是否需要禁用动效的一小段），但它们不得含任何 URL，
        也不得用 ``src=`` 引外部文件。
        """
        low = self.html.lower()
        for bad in ("http://", "https://", "src=", "@import", "cdn."):
            self.assertNotIn(bad, low, f"报告里出现了外部引用：{bad}")
        # 脚本必须全部内联，且数量可控（不随数据规模增长）
        self.assertLessEqual(low.count("<script"), 3, "内联脚本数量异常")
        self.assertIn("<script>", low)
        self.assertNotIn("<script src", low)

    def test_no_js_still_shows_content(self):
        """禁用 JS 时内容必须可见（不能因为动效初始 opacity:0 而白屏）。"""
        self.assertIn("<noscript>", self.html)
        self.assertIn("[data-reveal]", self.html)
        # noscript 里的兜底样式必须把透明度还原
        noscript = self.html.split("<noscript>")[1].split("</noscript>")[0]
        self.assertIn("opacity: 1", noscript)

    def test_reduced_motion_supported(self):
        """必须尊重 prefers-reduced-motion。"""
        self.assertIn("prefers-reduced-motion", self.html)

    def test_inline_script_has_valid_syntax(self):
        """内联脚本必须语法正确。

        没有浏览器时用 Node 的 ``--check`` 校验；两个 Node 都找不到就跳过
        （宁可跳过，也不写一个永远通过的假测试）。
        """
        import re as _re
        import shutil
        import subprocess
        import tempfile

        node = shutil.which("node")
        if not node:
            candidates = [
                Path(r"C:\Users\CYH\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\node\bin\node.exe"),
            ]
            node = next((str(c) for c in candidates if c.exists()), None)
        if not node:
            self.skipTest("环境里没有 node，无法校验 JS 语法")

        # 把 <script>…</script> 里的内容逐段抽出来校验
        blocks = _re.findall(r"<script>(.*?)</script>", self.html, _re.S)
        self.assertTrue(blocks, "报告里应当有内联脚本")
        for i, code in enumerate(blocks):
            with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False,
                                             encoding="utf-8") as fh:
                fh.write(code)
                path = fh.name
            proc = subprocess.run([node, "--check", path],
                                  capture_output=True, text=True)
            Path(path).unlink(missing_ok=True)
            self.assertEqual(proc.returncode, 0,
                             f"第 {i + 1} 段内联脚本语法错误：{proc.stderr[:400]}")

    def test_reveal_never_hides_content_without_js(self):
        """入场动画的隐藏状态必须挂在 .anim-ready 上，而不是默认状态。

        做法：把 ``<style>`` 里的规则逐条解析出来，只看**选择器恰好是**
        ``[data-reveal]`` 的规则——带 ``html.anim-ready`` 前缀的、
        以及后代选择器（如 ``[data-reveal]:not(.is-in) .radar``）都不算。
        """
        import re as _re

        styles = "".join(_re.findall(r"<style>(.*?)</style>", self.html, _re.S))
        styles = _re.sub(r"/\*.*?\*/", "", styles, flags=_re.S)   # 去掉 CSS 注释

        bare_rules: list[str] = []
        for selector, body in _re.findall(r"([^{}]+)\{([^{}]*)\}", styles):
            sel = selector.strip()
            if sel == "[data-reveal]":
                bare_rules.append(body)

        self.assertTrue(bare_rules, "没找到 [data-reveal] 的基础规则")
        for body in bare_rules:
            self.assertNotIn(
                "opacity: 0", body.replace(" ", "").replace("opacity:0", "opacity: 0"),
                "默认状态下 [data-reveal] 不能是透明的（会导致无 JS 白屏）",
            )
        self.assertIn("html.anim-ready [data-reveal]", styles)

    def test_reveal_has_last_resort_timeout(self):
        """主脚本必须有最终兜底：无论如何都要把内容显示出来。"""
        self.assertIn("1500", self.html)   # 1.5 秒兜底计时器
        self.assertIn("is-in", self.html)

    def test_section_headings_never_hidden(self):
        """标题不参与入场动画——出现「有内容没标题」的缺口比没动画更糟。"""
        import re as _re
        heads = _re.findall(r'<div class="section-head"[^>]*>', self.html)
        self.assertTrue(heads)
        for h in heads:
            self.assertNotIn("data-reveal", h, f"标题不应参与入场：{h}")

    def test_has_charset_and_viewport(self):
        self.assertIn('<meta charset="utf-8">', self.html)
        self.assertIn("width=device-width", self.html)

    def test_has_in_page_navigation(self):
        """页面内导航：每个区块都有锚点，导航项与区块一一对应。"""
        self.assertIn('class="nav"', self.html)
        self.assertIn('class="layout"', self.html)
        nav_count = self.html.count('class="nav-link"')
        sec_count = self.html.count('class="section"')
        self.assertGreaterEqual(nav_count, 10)
        self.assertEqual(nav_count, sec_count, "导航项与区块数量必须一致")
        # 每个 nav 链接的锚点都要真实存在
        import re as _re
        for href in _re.findall(r'class="nav-link" href="#([\w-]+)"', self.html):
            self.assertIn(f'id="{href}"', self.html, f"锚点缺失：{href}")

    def test_contains_required_sections(self):
        for token in ("情感投入指数", "八维雷达", "互动趋势", "关键数据", "双方投入度",
                      "原话与证据", "自己复核一遍", "分数是怎么算出来的", "写在这里的话",
                      "量化指标", "聊天足迹", "日历热力图", "话题词云", "关键节点"):
            self.assertIn(token, self.html, f"缺少区块：{token}")

    def test_has_all_charts(self):
        self.assertIn('class="gauge"', self.html)
        self.assertIn('class="radar"', self.html)
        self.assertEqual(self.html.count('class="linechart"'), 4)

    def test_disclaimer_present(self):
        self.assertIn("仅供娱乐与自我反思", self.html)
        self.assertIn("不构成", self.html)

    def test_local_processing_notice(self):
        self.assertIn("数据只在本地处理", self.html)

    def test_user_text_is_escaped(self):
        conv = parser.parse_string(
            "2023-01-01 09:00:00 我\n<script>alert(1)</script>\n\n"
            "2023-01-01 09:01:00 阿澈\n<img src=x onerror=alert(2)>\n"
        )
        analysis = metrics.analyze(conv, "我", "阿澈")
        result = scoring.score(analysis)
        html = report.build_html(analysis, result, conv.report)
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertNotIn("<img src=x", html)
        self.assertIn("&lt;script&gt;", html)

    def test_insufficient_banner_shown(self):
        conv = parser.parse_file(SAMPLES / "sample_tiny.txt")
        analysis = metrics.analyze(conv, "我", "阿澈")
        result = scoring.score(analysis)
        html = report.build_html(analysis, result, conv.report)
        self.assertIn("样本不足", html)

    def test_assumed_subject_warning(self):
        """没给 --me 时，报告必须显著提示「我」是推断出来的。"""
        conv = parser.parse_file(SAMPLES / "sample_memotrace.csv")
        analysis = metrics.analyze(conv, "阿澈", "我")
        result = scoring.score(analysis)
        warn_html = report.build_html(analysis, result, conv.report, assumed=True)
        calm_html = report.build_html(analysis, result, conv.report, assumed=False)
        self.assertIn("请先确认「我」是谁", warn_html)
        self.assertNotIn("请先确认「我」是谁", calm_html)

    def test_na_dimensions_rendered_not_as_zero(self):
        conv = parser.parse_file(SAMPLES / "sample_tiny.txt")
        analysis = metrics.analyze(conv, "我", "阿澈")
        result = scoring.score(analysis)
        html = report.build_html(analysis, result, conv.report)
        self.assertIn("没有参与打分", html)
        self.assertIn("N/A", html)

    def test_write_report_creates_file(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            p = report.write_report(Path(tmp) / "sub" / "r.html",
                                    self.analysis, self.result, self.conv.report)
            self.assertTrue(p.exists())
            self.assertGreater(p.stat().st_size, 5000)

    def test_json_output_is_valid(self):
        payload = json.loads(report.build_json(self.analysis, self.result))
        self.assertIn("score", payload)
        self.assertIn("dimensions", payload)
        self.assertEqual(len(payload["dimensions"]), 8)
        self.assertIsInstance(payload["score"]["total"], int)


class TestNoNetwork(unittest.TestCase):
    """守住「全本地处理」这条红线：包内不许出现任何网络/子进程调用。"""

    FORBIDDEN = (
        "urllib", "socket", "requests", "httpx", "aiohttp", "http.client",
        "ftplib", "smtplib", "telnetlib", "webbrowser", "subprocess",
        "openai", "anthropic", "dashscope", "zhipuai",
    )

    def test_package_imports_nothing_networked(self):
        import ast
        pkg = ROOT / "loves_me_not"
        offenders: list[str] = []
        for py in sorted(pkg.glob("*.py")):
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=str(py))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom):
                    names = [node.module or ""]
                else:
                    continue
                for name in names:
                    root = name.split(".")[0].lower()
                    if root in self.FORBIDDEN:
                        offenders.append(f"{py.name}: {name}")
        self.assertEqual(offenders, [], f"发现网络/子进程依赖：{offenders}")

    def test_no_network_urls_in_source(self):
        pkg = ROOT / "loves_me_not"
        for py in sorted(pkg.glob("*.py")):
            text = py.read_text(encoding="utf-8")
            for m in re.finditer(r"https?://[^\s\"')]+", text):
                # 只允许出现在注释里的说明性链接
                line_start = text.rfind("\n", 0, m.start()) + 1
                line = text[line_start: text.find("\n", m.start())]
                self.assertTrue(
                    line.lstrip().startswith("#") or '"""' in line or "'''" in line,
                    f"{py.name} 正文里出现 URL：{m.group(0)}",
                )


class TestEndToEndCli(unittest.TestCase):
    def test_analyze_creates_report(self):
        import tempfile
        from loves_me_not.__main__ import main
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "r.html"
            code = main(["analyze", str(SAMPLES / "sample_wechat_cooling.txt"),
                         "--me", "我", "--peer", "阿澈", "-o", str(out)])
            self.assertEqual(code, 0)
            self.assertTrue(out.exists())

    def test_analyze_bad_input_returns_nonzero(self):
        from loves_me_not.__main__ import main
        code = main(["analyze", str(SAMPLES / "does-not-exist.txt"), "--me", "我"])
        self.assertNotEqual(code, 0)

    def test_inspect_runs(self):
        from loves_me_not.__main__ import main
        self.assertEqual(main(["inspect", str(SAMPLES / "sample_wechat_warm.txt")]), 0)

    def test_redact_flag_runs(self):
        import tempfile
        from loves_me_not.__main__ import main
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "r.html"
            code = main(["analyze", str(SAMPLES / "sample_wechat_cooling.txt"),
                         "--me", "我", "--peer", "阿澈", "--redact", "-o", str(out)])
            self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)