"""关键窗口采样与有预算的上下文检索。

来源与适配
----------
算法思路来自 ``she-love-me`` 的 ``scripts/build_chat_history.py``
（负向词密度滑窗、长沉默后的修复片段）和 ``scripts/query_chat_context.py``
（按锚点取前后文 + 条数/字符双预算 + 摘要防漂移）。

**没有照搬它的数据契约**：那套用 ``msg_<64hex>``、``schema_version 2.1/2.2``、
``identity_namespace``；本包已经有稳定的 :class:`loves_me_not.parser.Message.index`，
所以这里直接用 ``index`` 做锚点，并改用本包自己的 :data:`loves_me_not.lexicon.NEGATIVE_WORDS`
算负向密度。输出是普通 ``dict``，不引入新的中间文件。

本模块零网络、零第三方依赖，也不修改传入的消息对象。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Sequence

from . import lexicon as LEX
from .parser import Message

# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #

NEGATIVE_SCAN_WINDOW = 100       # 冲突滑窗宽度（消息条数）
REPAIR_SILENCE_GAP = timedelta(hours=24)
REPAIR_FOLLOWUP = 20             # 长沉默后取多少条
DEFAULT_MAX_CHARS = 20000
DEFAULT_MAX_MESSAGES = 100
_NEG_TERMS = sorted(LEX.NEGATIVE_WORDS, key=len, reverse=True)


class ContextError(ValueError):
    """上下文检索失败（摘要不匹配、参数非法等）。"""


# --------------------------------------------------------------------------- #
# 数据摘要：防止采样时数据已经被换掉
# --------------------------------------------------------------------------- #

def messages_digest(messages: Sequence[Message]) -> str:
    """对消息序列做稳定摘要，用于检测输入漂移。

    与 she-love-me 的 ``msg_<hex>`` 方案不同：这里不生成新 ID，
    只把本包已有的 ``index / speaker / timestamp / text`` 规范化后散列。
    """
    payload = [
        [
            m.index,
            m.speaker,
            m.timestamp.isoformat(timespec="seconds") if m.timestamp else None,
            m.text,
        ]
        for m in messages
    ]
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# 采样
# --------------------------------------------------------------------------- #

def _negative_density(messages: Sequence[Message]) -> float:
    """一段消息的负向词密度：命中权重绝对值之和 ÷ 总字数。

    只用于排序，不是情感结论。词表来自本包 lexicon，沿用关系语境下的权重。
    """
    total_chars = 0
    penalty = 0.0
    for m in messages:
        if m.is_system:
            continue
        text = m.text or ""
        total_chars += len(text.strip())
        low = text.lower()
        for term in _NEG_TERMS:
            if term in low:
                penalty += abs(LEX.NEGATIVE_WORDS[term])
    if total_chars == 0:
        return 0.0
    return penalty / total_chars


@dataclass
class Window:
    """一段被挑出来的关键窗口。"""

    kind: str                  # conflict / repair
    start_index: int
    end_index: int
    score: float
    detail: str
    messages: list[Message]

    def to_dict(self, *, include_messages: bool = False) -> dict:
        out = {
            "kind": self.kind,
            "start_index": self.start_index,
            "end_index": self.end_index,
            "score": round(self.score, 4),
            "detail": self.detail,
            "message_count": len(self.messages),
        }
        if include_messages:
            out["messages"] = [_brief(m) for m in self.messages]
        return out


def find_conflict_window(messages: Sequence[Message],
                         window: int = NEGATIVE_SCAN_WINDOW) -> Window | None:
    """找出负向词密度最高的连续 ``window`` 条消息。

    这是**定位工具**，不是「吵架鉴定」：生活琐事里的「烦」「算了」也会命中。
    """
    real = [m for m in messages if not m.is_system]
    if not real:
        return None
    size = min(window, len(real))
    best_start = 0
    best_score = -1.0
    for i in range(0, len(real) - size + 1):
        score = _negative_density(real[i:i + size])
        if score > best_score:
            best_score = score
            best_start = i
    chunk = real[best_start:best_start + size]
    return Window(
        kind="conflict",
        start_index=chunk[0].index,
        end_index=chunk[-1].index,
        score=best_score,
        detail=f"负向词密度最高的连续 {size} 条（密度 {best_score:.3f}，仅用于定位）",
        messages=list(chunk),
    )


def find_repair_moments(messages: Sequence[Message],
                        gap: timedelta = REPAIR_SILENCE_GAP,
                        followup: int = REPAIR_FOLLOWUP) -> list[Window]:
    """找出每次长沉默之后的第一批消息——通常是「谁先把话接回来」。"""
    real = [m for m in messages if not m.is_system]
    moments: list[Window] = []
    for i in range(1, len(real)):
        prev, cur = real[i - 1], real[i]
        if not prev.timestamp or not cur.timestamp:
            continue
        if cur.timestamp - prev.timestamp < gap:
            continue
        chunk = real[i:i + followup]
        days = (cur.timestamp - prev.timestamp).total_seconds() / 86400
        moments.append(Window(
            kind="repair",
            start_index=cur.index,
            end_index=chunk[-1].index,
            score=days,
            detail=f"{days:.1f} 天沉默后的 {len(chunk)} 条，首条由 {cur.speaker} 发出",
            messages=list(chunk),
        ))
    moments.sort(key=lambda w: w.score, reverse=True)
    return moments


# --------------------------------------------------------------------------- #
# 有预算的上下文查询
# --------------------------------------------------------------------------- #

def _brief(m: Message) -> dict:
    return {
        "index": m.index,
        "speaker": m.speaker,
        "time": m.timestamp.isoformat(sep=" ") if m.timestamp else None,
        "text": m.text,
        "is_system": m.is_system,
        "is_media": m.is_media,
    }


def _parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise ContextError(f"时间格式应为 ISO 8601（如 2025-08-10 或 2025-08-10T20:00）：{value}") from exc


def query_context(messages: Sequence[Message], *,
                  keyword: str | None = None,
                  speaker: str | None = None,
                  since: str | None = None,
                  until: str | None = None,
                  before: int = 10,
                  after: int = 10,
                  max_matches: int = 5,
                  max_messages: int = DEFAULT_MAX_MESSAGES,
                  max_chars: int = DEFAULT_MAX_CHARS,
                  expected_digest: str | None = None) -> dict:
    """按关键词/说话人/时间找锚点，取前后文，受条数与字符双预算约束。

    返回普通 ``dict``，可直接用于 ``--json`` 输出。
    """
    if before < 0 or after < 0:
        raise ContextError("before / after 不能为负数")
    if max_messages <= 0 or max_chars <= 0:
        raise ContextError("max_messages / max_chars 必须为正数")

    digest = messages_digest(messages)
    if expected_digest and expected_digest != digest:
        raise ContextError("输入数据已变化（摘要不匹配），请重新取摘要后再查询")

    start = _parse_time(since)
    end = _parse_time(until)
    needle = (keyword or "").lower()
    anchors: list[int] = []
    for pos, m in enumerate(messages):
        if m.is_system:
            continue
        if speaker and m.speaker != speaker:
            continue
        if start and m.timestamp and m.timestamp < start:
            continue
        if end and m.timestamp and m.timestamp > end:
            continue
        if needle and needle not in (m.text or "").lower():
            continue
        anchors.append(pos)

    matched = len(anchors)
    selected: set[int] = set()
    for pos in anchors[:max_matches]:
        selected.update(range(max(0, pos - before), min(len(messages), pos + after + 1)))

    ordered = sorted(selected)
    truncated = False
    if len(ordered) > max_messages:
        ordered = ordered[:max_messages]
        truncated = True

    returned = [_brief(messages[i]) for i in ordered]
    while returned and len(json.dumps(returned, ensure_ascii=False)) > max_chars:
        returned.pop()
        truncated = True

    return {
        "messages_digest": digest,
        "query": {
            "keyword": keyword, "speaker": speaker, "since": since, "until": until,
            "before": before, "after": after, "max_matches": max_matches,
        },
        "matched_count": matched,
        "anchor_indexes": [messages[i].index for i in anchors[:max_matches]],
        "returned_anchor_indexes": [
            messages[i].index for i in anchors[:max_matches] if i in ordered
        ],
        "returned_count": len(returned),
        "truncated": truncated,
        "budget": {"max_messages": max_messages, "max_chars": max_chars},
        "messages": returned,
        "note": "context 只做定位与取样，不参与情感评分；关键词匹配不区分大小写。",
    }


def describe_windows(windows: Sequence[Window], *, messages: Sequence[Message] | None = None,
                     include_messages: bool = False) -> dict:
    """把采样结果打包成可 JSON 化的结构。"""
    out: dict = {
        "count": len(windows),
        "windows": [w.to_dict(include_messages=include_messages) for w in windows],
    }
    if messages is not None:
        out["digest"] = messages_digest(messages)
    return out
