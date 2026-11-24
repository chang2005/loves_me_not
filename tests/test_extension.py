"""扩展模块的测试：对称性诊断、上下文检索、可选记忆。

覆盖三类风险：

* 扩展不能改变原有打分与输出（默认路径逐字段兼容）；
* 对称性必须交换双方不变，且样本不足时降级而不是编数；
* 记忆必须显式同意才建库，写入可撤销，删除要确认。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from loves_me_not import balance, context, memory, metrics, parser, report, scoring  # noqa: E402
from loves_me_not.parser import Message  # noqa: E402

SAMPLES = ROOT / "samples"


def build_conv(n: int = 60) -> parser.Conversation:
    """造一段有轮次和断档的对话，样本足以触发所有对称性诊断。

    每 6 条一组、组内 2 分钟间隔；组间交替 7h / 30h，
    这样既能切出足够轮次，也能切出 24h 以上的重连点。
    """
    start = datetime(2025, 1, 1, 9, 0, 0)
    msgs: list[Message] = []
    now = start
    for i in range(n):
        if i and i % 6 == 0:
            now += timedelta(hours=7 if (i // 6) % 2 else 30)
        speaker = "我" if i % 2 == 0 else "TA"
        msgs.append(Message(
            index=i,
            speaker=speaker,
            timestamp=now,
            text=f"第{i}条，聊点正事，今天怎么样",
        ))
        now += timedelta(minutes=2)
    return parser.Conversation(messages=msgs, speakers=["我", "TA"])


class TestBalance(unittest.TestCase):
    def test_symmetry_is_swap_invariant(self):
        conv = build_conv()
        analysis = metrics.analyze(conv, "我", "TA")
        a = balance.compute_balance(conv, analysis, "我", "TA")
        b = balance.compute_balance(conv, analysis, "TA", "我")
        self.assertEqual(a.symmetry, b.symmetry)
        self.assertEqual(a.dimensions["messages"].value,
                         b.dimensions["messages"].value)

    def test_full_sample_gives_score(self):
        conv = build_conv()
        analysis = metrics.analyze(conv, "我", "TA")
        result = balance.compute_balance(conv, analysis, "我", "TA")
        self.assertIsNotNone(result.symmetry)
        self.assertGreaterEqual(result.symmetry, 0)
        self.assertLessEqual(result.symmetry, 10)
        self.assertEqual(result.evidence_level, "strong")

    def test_tiny_sample_is_downgraded(self):
        conv = build_conv(n=4)
        analysis = metrics.analyze(conv, "我", "TA")
        result = balance.compute_balance(conv, analysis, "我", "TA")
        self.assertIsNone(result.symmetry)
        self.assertEqual(result.evidence_level, "insufficient")
        self.assertTrue(all(not d.available for d in result.dimensions.values()))
        self.assertIn("有效权重覆盖", result.limitations[0])

    def test_to_dict_is_json_serializable(self):
        conv = build_conv()
        analysis = metrics.analyze(conv, "我", "TA")
        payload = balance.compute_balance(conv, analysis, "我", "TA").to_dict()
        text = json.dumps(payload, ensure_ascii=False)
        self.assertIn("symmetry", json.loads(text))

    def test_does_not_mutate_analysis(self):
        conv = build_conv()
        analysis = metrics.analyze(conv, "我", "TA")
        before = analysis.me_messages, analysis.peer_messages, len(analysis.dimensions)
        balance.compute_balance(conv, analysis, "我", "TA")
        self.assertEqual(before, (analysis.me_messages, analysis.peer_messages,
                                  len(analysis.dimensions)))


class TestContext(unittest.TestCase):
    def test_digest_stable_and_sensitive(self):
        conv = build_conv()
        d1 = context.messages_digest(conv.messages)
        self.assertEqual(d1, context.messages_digest(conv.messages))
        changed = [Message(m.index, m.speaker, m.timestamp, m.text + "!")
                   for m in conv.messages]
        self.assertNotEqual(d1, context.messages_digest(changed))

    def test_expected_digest_guard(self):
        conv = build_conv()
        with self.assertRaises(context.ContextError):
            context.query_context(conv.messages, expected_digest="0" * 64)

    def test_budget_respected(self):
        conv = build_conv()
        result = context.query_context(
            conv.messages, keyword="正事", before=5, after=5,
            max_matches=3, max_messages=7, max_chars=500,
        )
        self.assertLessEqual(result["returned_count"], 7)
        self.assertLessEqual(len(json.dumps(result["messages"], ensure_ascii=False)), 500)
        self.assertTrue(result["truncated"])

    def test_keyword_and_speaker_filter(self):
        conv = build_conv()
        result = context.query_context(
            conv.messages, keyword="第10条", before=0, after=0,
        )
        self.assertEqual(result["matched_count"], 1)
        self.assertEqual(result["messages"][0]["index"], 10)
        by_speaker = context.query_context(
            conv.messages, speaker="TA", before=0, after=0, max_messages=100,
        )
        self.assertTrue(all(m["speaker"] == "TA" for m in by_speaker["messages"]))

    def test_repair_moments(self):
        conv = build_conv()
        conv.messages[30] = Message(
            index=30, speaker="我",
            timestamp=conv.messages[29].timestamp + timedelta(days=3),
            text="好久没聊了",
        )
        moments = context.find_repair_moments(conv.messages)
        self.assertTrue(moments)
        self.assertEqual(moments[0].start_index, 30)
        self.assertIn("好久没聊了", [m.text for m in moments[0].messages])

    def test_conflict_window_never_empty(self):
        conv = build_conv()
        win = context.find_conflict_window(conv.messages, window=10)
        self.assertIsNotNone(win)
        self.assertEqual(win.end_index - win.start_index, 9)


class TestMemoryPort(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old = os.environ.get("LOVES_ME_NOT_MEMORY_DIR")
        os.environ["LOVES_ME_NOT_MEMORY_DIR"] = self._tmp.name

    def tearDown(self):
        if self._old is None:
            os.environ.pop("LOVES_ME_NOT_MEMORY_DIR", None)
        else:
            os.environ["LOVES_ME_NOT_MEMORY_DIR"] = self._old
        self._tmp.cleanup()

    def test_no_db_before_consent(self):
        self.assertEqual(memory.main(["status"]), 0)
        self.assertFalse(Path(self._tmp.name, "memory.sqlite3").exists())
        self.assertEqual(memory.main(["apply", "--json", json.dumps({
            "scope": "user", "subject_id": "user", "field": "mbti",
            "value": "INTJ", "source_type": "user_explicit", "confidence": "high",
        }, ensure_ascii=False)]), 1)

    def test_enable_requires_confirm(self):
        self.assertEqual(memory.main(["enable"]), 1)
        self.assertEqual(memory.main(["enable", "--confirm"]), 0)
        self.assertTrue(Path(self._tmp.name, "memory.sqlite3").exists())

    def test_apply_show_undo(self):
        memory.main(["enable", "--confirm"])
        payload = json.dumps({
            "scope": "user", "subject_id": "user", "field": "mbti",
            "value": "INTJ", "source_type": "user_explicit", "confidence": "high",
        }, ensure_ascii=False)
        self.assertEqual(memory.main(["apply", "--json", payload]), 0)
        self.assertEqual(memory.main(["undo"]), 0)
        import sqlite3
        conn = sqlite3.connect(Path(self._tmp.name) / "memory.sqlite3")
        try:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0], 0)
        finally:
            conn.close()

    def test_scope_source_matrix(self):
        delta = {
            "scope": "object", "subject_id": "文宁宁", "field": "age",
            "value": "20", "source_type": "loves_me_not", "confidence": "low",
        }
        with self.assertRaises(memory.MemoryErrorWithCode) as ctx:
            memory.validate_delta(delta)
        self.assertEqual(ctx.exception.code, "SOURCE_NOT_ELIGIBLE")


class TestCompatibility(unittest.TestCase):
    def test_default_json_has_no_balance_key(self):
        conv = parser.parse_file(SAMPLES / "demo_two_block_warm.txt")
        analysis = metrics.analyze(conv, "我", "阿澈")
        result = scoring.score(analysis)
        payload = json.loads(report.build_json(analysis, result))
        self.assertNotIn("balance", payload)
        self.assertIn("score", payload)
        self.assertIn("dimensions", payload)

    def test_balance_opt_in_only(self):
        conv = parser.parse_file(SAMPLES / "demo_two_block_warm.txt")
        analysis = metrics.analyze(conv, "我", "阿澈")
        result = scoring.score(analysis)
        obj = balance.compute_balance(conv, analysis, "我", "阿澈")
        payload = json.loads(report.build_json(analysis, result, balance_obj=obj))
        self.assertIn("balance", payload)
        self.assertEqual(payload["score"]["total"], result.total)

    def test_report_html_unchanged(self):
        conv = parser.parse_file(SAMPLES / "demo_two_block_warm.txt")
        analysis = metrics.analyze(conv, "我", "阿澈")
        result = scoring.score(analysis)
        html = report.build_html(analysis, result, conv.report)
        self.assertNotIn("投入对称性诊断", html)
        self.assertIn("情感投入指数", html)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
