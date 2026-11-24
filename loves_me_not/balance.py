"""投入对称性诊断（独立于 0–100 总分的旁路指标）。

来源与适配
----------
思路来自 ``she-love-me`` 的 ``scripts/scoring_calibration.py``：用
``2 * min(left, right) / (left + right)`` 衡量双方投入是否对等，并要求
有效权重覆盖率达到阈值才给结论。

**没有照搬它的权重表和输出格式**：那套模型用 3h 切轮、0–10 分、``stats_fields``
点分路径引用，与本包 6h ``THREAD_GAP``、0–100 八维模型、``SubMetric`` 结构冲突。
这里全部改用 :mod:`loves_me_not.metrics` 自己的口径和命名。

红线
----
本模块的结果**不参与** :func:`loves_me_not.scoring.score` 的总分计算，
也不改变任何既有维度权重。它只回答「这段关系是不是单方面在推」，
不回答「TA 爱不爱我」。对称性高不等于感情好，两个人一起冷也算对称。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from typing import Sequence

from . import metrics
from .metrics import Analysis
from .parser import Conversation, Message

# --------------------------------------------------------------------------- #
# 配置
# --------------------------------------------------------------------------- #

#: 四项诊断权重。沿用 she-love-me 的相对次序（发起/续谈最重要），
#: 但键名全部换成 loves_me_not 的说法。
WEIGHTS: dict[str, float] = {
    "initiative": 0.30,   # 谁先开口
    "response": 0.20,     # 回复速度是否对等
    "resumption": 0.30,   # 长时间沉默后谁把话接回来
    "messages": 0.20,     # 消息量是否对等
}

#: 每个诊断项的最小样本量，低于它就当「没信息」，不参与计分。
MINIMUMS: dict[str, int] = {
    "initiative": 10,
    "response": 8,
    "resumption": 4,
    "messages": 20,
}

#: 有效权重覆盖率低于此值时拒绝给 0–10 分，只输出原因。
MIN_COVERAGE = 0.70

#: 「续谈」的静默门槛：比 THREAD_GAP 长，专门看断档之后谁先回来。
RESUMPTION_GAP = timedelta(hours=24)

LABELS = {
    "initiative": "开口主动性",
    "response": "回复速度",
    "resumption": "断档后重连",
    "messages": "消息量",
}

SOURCES = {
    "initiative": "metrics.build_threads(6h)",
    "response": "metrics.reply_delays(24h 上限)",
    "resumption": f"metrics.build_threads({int(RESUMPTION_GAP.total_seconds() // 3600)}h)",
    "messages": "Analysis.me_messages / peer_messages",
}


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #

@dataclass
class BalanceDimension:
    """一个对称性诊断项。"""

    key: str
    label: str
    weight: float
    #: 0–1；``None`` 表示样本不足
    value: float | None
    sample_count: int
    minimum_samples: int
    reason: str = ""
    source: str = ""

    @property
    def available(self) -> bool:
        return self.value is not None


@dataclass
class BalanceResult:
    """对称性诊断的完整结果。"""

    #: 0–10；样本不足时为 ``None``
    symmetry: float | None
    coverage: float
    evidence_level: str        # strong / moderate / weak / insufficient
    dimensions: dict[str, BalanceDimension] = field(default_factory=dict)
    #: 投入方向：``"peer"`` / ``"me"`` / ``"balanced"`` / ``"unknown"``
    investment_direction: str = "unknown"
    #: 双方占比，供人复核
    shares: dict[str, float | None] = field(default_factory=dict)
    limitations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "symmetry": None if self.symmetry is None else round(self.symmetry, 2),
            "coverage": round(self.coverage, 4),
            "evidence_level": self.evidence_level,
            "dimensions": {
                k: {
                    "label": d.label,
                    "weight": d.weight,
                    "value": None if d.value is None else round(d.value, 4),
                    "sample_count": d.sample_count,
                    "minimum_samples": d.minimum_samples,
                    "reason": d.reason,
                    "source": d.source,
                }
                for k, d in self.dimensions.items()
            },
            "investment_direction": self.investment_direction,
            "shares": {
                k: (None if v is None else round(v, 4)) for k, v in self.shares.items()
            },
            "limitations": self.limitations,
            "note": (
                "对称性只描述双方投入是否对等，不代表感情好坏；"
                "两个人一起冷淡同样会得到高分。本结果不参与情感投入总分。"
            ),
        }


# --------------------------------------------------------------------------- #
# 计算
# --------------------------------------------------------------------------- #

def _symmetry_ratio(left: float | None, right: float | None) -> float | None:
    """交换双方不变的对称度：完全对等为 1，一边倒向 0。"""
    if left is None or right is None:
        return None
    total = abs(left) + abs(right)
    if total <= 0:
        return None
    return max(0.0, min(1.0, 2.0 * min(abs(left), abs(right)) / total))


def _count_starts(threads: Sequence[Sequence[Message]], speaker: str) -> int:
    return sum(1 for t in threads if t and t[0].speaker == speaker)


def compute_balance(conv: Conversation, analysis: Analysis,
                    me: str, peer: str) -> BalanceResult:
    """基于已解析的对话和已算好的分析，输出独立的对称性诊断。

    不修改 ``analysis`` / ``conv``，也不触碰打分结果。
    """
    real = [m for m in conv.messages if not m.is_system and m.speaker in {me, peer}]

    # ① 开口主动性：用与八维完全一致的 6h 轮次口径
    threads = metrics.build_threads(real)
    starts_me = _count_starts(threads, me)
    starts_peer = _count_starts(threads, peer)
    init_value = _symmetry_ratio(starts_me, starts_peer)
    init_n = starts_me + starts_peer

    # ② 回复速度：中位回复间隔越接近越对称（注意：慢得一样慢也算对称）
    delays_me = metrics.reply_delays(real, me)
    delays_peer = metrics.reply_delays(real, peer)
    median_me = metrics._safe_median(delays_me)
    median_peer = metrics._safe_median(delays_peer)
    resp_value = _symmetry_ratio(median_me, median_peer)
    resp_n = min(len(delays_me), len(delays_peer))

    # ③ 断档后重连：24h 静默后的第一条由谁发出
    long_threads = metrics.build_threads(real, gap=RESUMPTION_GAP)
    resume_me = _count_starts(long_threads, me)
    resume_peer = _count_starts(long_threads, peer)
    resume_value = _symmetry_ratio(resume_me, resume_peer)
    resume_n = resume_me + resume_peer

    # ④ 消息量
    msg_value = _symmetry_ratio(float(analysis.me_messages), float(analysis.peer_messages))
    msg_n = analysis.me_messages + analysis.peer_messages

    raw = {
        "initiative": (init_value, init_n),
        "response": (resp_value, resp_n),
        "resumption": (resume_value, resume_n),
        "messages": (msg_value, msg_n),
    }
    dimensions: dict[str, BalanceDimension] = {}
    for key, weight in WEIGHTS.items():
        value, n = raw[key]
        minimum = MINIMUMS[key]
        if value is None or n < minimum:
            reason = (
                f"样本 {n} < 最小要求 {minimum}，已剔除；"
                f"有效样本为 {n}，本项不参与对称性计分。"
            )
            dimensions[key] = BalanceDimension(
                key, LABELS[key], weight, None, n, minimum, reason, SOURCES[key]
            )
        else:
            dimensions[key] = BalanceDimension(
                key, LABELS[key], weight, value, n, minimum,
                f"{n} 条有效样本，双方占比对称度 {value:.2f}。", SOURCES[key]
            )

    used_weight = sum(d.weight for d in dimensions.values() if d.available)
    coverage = used_weight / sum(WEIGHTS.values())
    limitations: list[str] = []
    if coverage < MIN_COVERAGE:
        limitations.append(
            f"有效权重覆盖 {coverage:.0%} < {MIN_COVERAGE:.0%}，不给 0–10 分结论。"
        )
        symmetry = None
        level = "insufficient"
    else:
        symmetry = round(
            10.0 * sum(d.value * d.weight for d in dimensions.values() if d.available)
            / used_weight, 2
        )
        level = "strong" if coverage >= 0.95 else "moderate"

    # 投入方向：谁的消息/开口占比更高。只在四项里有明确偏向时给方向。
    me_share = None if init_n == 0 else starts_me / init_n
    peer_share = None if init_n == 0 else starts_peer / init_n
    if me_share is None:
        direction = "unknown"
    elif me_share >= 0.6:
        direction = "me"
    elif peer_share >= 0.6:
        direction = "peer"
    else:
        direction = "balanced"

    limitations.extend([
        "对称性不等于感情：双方一起冷淡会得到高分。",
        "回复速度按中位间隔计算，口径与八维的 5 分钟秒回率不同，不可直接比较。",
        "本结果不参与情感投入总分，也不改变任何既有维度权重。",
    ])

    return BalanceResult(
        symmetry=symmetry,
        coverage=coverage,
        evidence_level=level,
        dimensions=dimensions,
        investment_direction=direction,
        shares={"me_initiative": me_share, "peer_initiative": peer_share},
        limitations=limitations,
    )


def format_text(result: BalanceResult, me: str, peer: str) -> str:
    """人话版控制台输出。"""
    lines = ["=" * 66, "投入对称性诊断（独立指标，不影响上面的总分）"]
    if result.symmetry is None:
        lines.append(f"无法给结论：{result.limitations[0]}")
    else:
        lines.append(f"对称性：{result.symmetry:.1f} / 10（有效权重覆盖 {result.coverage:.0%}）")
    direction = {
        "me": f"{me} 主动更多", "peer": f"{peer} 主动更多",
        "balanced": "双方大致对等", "unknown": "样本不足，方向不明",
    }[result.investment_direction]
    lines.append(f"投入方向：{direction}")
    for key, d in result.dimensions.items():
        state = "—" if d.value is None else f"{d.value:.2f}"
        lines.append(f"  · {d.label}：{state}（样本 {d.sample_count}/{d.minimum_samples}）")
    lines.append("注意：对称性高只说明双方投入接近，不代表关系好。")
    lines.append("=" * 66)
    return "\n".join(lines)
