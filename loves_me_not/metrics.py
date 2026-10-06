"""八个分析维度的计算。

设计原则（与 README 的承诺一一对应）：

1. **每个维度都做双方对比**。在亲密关系里，绝对值意义有限——「他 40 分钟才回」
   本身说明不了什么，但「你 2 分钟、他 40 分钟」说明了很多。所以每个维度
   都同时算「我」和「TA」，并计算不对称度。
2. **算不出来就说算不出来**。任何一条子指标在样本不足时返回 ``None``，
   由 :mod:`loves_me_not.scoring` 剔除并重新归一化权重，**绝不当成 0 分**。
   「全程没出现过任何亲昵称呼」不等于「TA 不爱你」——它只是没信息。
3. **结论必须能追溯到原话**。每个维度都会带上 ``evidence``（真实聊天片段），
   报告里原样展示，让人自己复核。

本模块零网络请求、零第三方依赖。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import mean, median
from typing import Callable, Iterable, Sequence

from . import lexicon as LEX
from .parser import Conversation, Message, has_emoji, looks_like_question

# --------------------------------------------------------------------------- #
# 配置：所有阈值集中在这里，便于审阅与调参
# --------------------------------------------------------------------------- #

#: 一次回复间隔超过这个时长，就不再算「同一轮对话」里的回复
MAX_REPLY_GAP = timedelta(hours=24)

#: 「秒回」阈值
FAST_REPLY = timedelta(minutes=5)

#: 「拖了很久」阈值
SLOW_REPLY = timedelta(hours=1)

#: 静默多久之后的开口算「冷启动 / 重新发起对话」
THREAD_GAP = timedelta(hours=6)

#: 连续多条消息算一个「消息段」
RUN_WINDOW = timedelta(minutes=3)

#: 什么算「实质性消息」
SUBSTANTIVE_MIN_CHARS = 6
SUBSTANTIVE_MAX_CHARS = 300

#: 深夜时段
LATE_NIGHT_START = 23
LATE_NIGHT_END = 3

#: 各维度的最小样本量
MIN_FOR_REPLY = 10
MIN_FOR_INITIATIVE = 30
MIN_FOR_LENGTH = 30
MIN_FOR_QUESTION = 20
MIN_FOR_ENDING = 8
MIN_FOR_SENTIMENT = 30


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #

@dataclass
class SubMetric:
    """一条子指标：既给出数值，也给出它映射后的 0–1 分数。"""

    key: str
    label: str
    value: float | None
    #: 归一化到 0–1；``None`` 表示样本不足或无法计算
    score: float | None
    #: 展示用文本，例如 ``"38%"``、``"12 分钟"`
    display: str
    note: str = ""

    @property
    def available(self) -> bool:
        return self.score is not None


@dataclass
class Evidence:
    """支撑某个结论的真实聊天片段。"""

    speaker: str
    timestamp: datetime | None
    text: str
    reason: str

    def when(self) -> str:
        if not self.timestamp:
            return ""
        return self.timestamp.strftime("%m-%d %H:%M")


@dataclass
class Dimension:
    """一个分析维度的完整结果（双方视角合并）。"""

    key: str
    label: str
    #: 给「TA 对我的投入」的分数（0–1）
    score_peer: float | None
    #: 给「我对 TA 的投入」的分数（0–1）
    score_me: float | None
    summary: str
    subs: list[SubMetric] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    #: 人话版的不对称描述，例如「TA 平均比你慢 12 倍」
    asymmetry: str = ""
    #: 雷达图等窄版式用的短名（长名会把标签挤出画布）
    short: str = ""

    def __post_init__(self) -> None:
        if not self.short:
            self.short = self.label

    @property
    def available(self) -> bool:
        return self.score_peer is not None or self.score_me is not None


@dataclass
class Period:
    """时间分段（用于趋势图）。"""

    label: str
    start: datetime
    end: datetime
    messages: list[Message] = field(default_factory=list)

    def count(self, speaker: str | None = None) -> int:
        if speaker is None:
            return len([m for m in self.messages if not m.is_system])
        return len([m for m in self.messages if not m.is_system and m.speaker == speaker])

    @property
    def share_me(self) -> float | None:
        return None  # 由 analysis 填充（见 PeriodStat）


@dataclass
class PeriodStat:
    """趋势图上的一个点。"""

    label: str
    start: datetime
    end: datetime
    total: int
    me_count: int
    peer_count: int
    initiative_me: int
    initiative_peer: int
    sentiment_peer: float | None
    sentiment_me: float | None
    reply_median_peer_sec: float | None
    reply_median_me_sec: float | None


@dataclass
class Analysis:
    """整份分析结果。"""

    me: str
    peer: str
    speakers: list[str]
    total_messages: int
    total_real: int
    me_messages: int
    peer_messages: int
    first_at: datetime | None
    last_at: datetime | None
    days_span: int
    active_days: int
    is_group: bool
    dimensions: dict[str, Dimension] = field(default_factory=dict)
    periods: list[PeriodStat] = field(default_factory=list)
    #: 关键数据卡片
    cards: list[tuple[str, str, str]] = field(default_factory=list)
    #: 无法计算的维度说明
    caveats: list[str] = field(default_factory=list)
    #: 高互动片段 / 降温片段等定性素材
    highlights: dict[str, list[Evidence]] = field(default_factory=dict)


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #

def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _fmt_seconds(sec: float | None) -> str:
    if sec is None:
        return "—"
    if sec < 60:
        return f"{sec:.0f} 秒"
    if sec < 3600:
        return f"{sec / 60:.0f} 分钟"
    if sec < 86400:
        return f"{sec / 3600:.1f} 小时"
    return f"{sec / 86400:.1f} 天"


def _ratio_display(ratio: float | None) -> str:
    if ratio is None:
        return "—"
    return f"{ratio:.2f}×"


def _safe_median(values: Sequence[float]) -> float | None:
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    return float(median(vals))


def _safe_mean(values: Sequence[float]) -> float | None:
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    return float(mean(vals))


def _squash(value: float, mid: float, sharpness: float = 1.0) -> float:
    """把任意正数压到 0–1：``mid`` 处约等于 0.5。"""
    if value <= 0:
        return 0.0
    return _clamp(1.0 / (1.0 + (value / mid) ** sharpness))


def _find_terms(text: str, terms: Iterable[str]) -> list[str]:
    """在一段文本里找出命中的词（不区分大小写）。"""
    if not text:
        return []
    low = text.lower()
    hits: list[str] = []
    for t in terms:
        if not t:
            continue
        if t.lower() in low:
            hits.append(t)
    return hits


# --------------------------------------------------------------------------- #
# 分组 / 分轮
# --------------------------------------------------------------------------- #

def build_threads(messages: Sequence[Message], gap: timedelta = THREAD_GAP) -> list[list[Message]]:
    """把消息切成「对话轮次」：相邻两条间隔超过 ``gap`` 就切开。"""
    threads: list[list[Message]] = []
    current: list[Message] = []
    prev: Message | None = None
    for m in messages:
        if m.is_system:
            continue
        if prev is not None and m.timestamp and prev.timestamp:
            if m.timestamp - prev.timestamp > gap:
                if current:
                    threads.append(current)
                current = []
        current.append(m)
        prev = m
    if current:
        threads.append(current)
    return threads


def build_runs(messages: Sequence[Message], window: timedelta = RUN_WINDOW) -> list[list[Message]]:
    """把消息切成「同一人连续发言段」：换人或间隔过久就切段。"""
    runs: list[list[Message]] = []
    current: list[Message] = []
    prev: Message | None = None
    for m in messages:
        if m.is_system:
            continue
        boundary = (
            prev is None
            or m.speaker != prev.speaker
            or (m.timestamp is not None and prev.timestamp is not None and m.timestamp - prev.timestamp > window)
        )
        if boundary:
            if current:
                runs.append(current)
            current = []
        current.append(m)
        prev = m
    if current:
        runs.append(current)
    return runs


def reply_delays(messages: Sequence[Message], speaker: str) -> list[float]:
    """算出 ``speaker`` 每次回复对方所用的秒数。

    只统计「对方说了话之后 speaker 的第一条回应」，且间隔不超过
    :data:`MAX_REPLY_GAP`（否则那是新的一轮，不是回复）。
    """
    delays: list[float] = []
    pending: datetime | None = None
    for m in messages:
        if m.is_system:
            continue
        if m.speaker == speaker:
            if pending is not None and m.timestamp is not None:
                delta = (m.timestamp - pending).total_seconds()
                if 0 <= delta <= MAX_REPLY_GAP.total_seconds():
                    delays.append(delta)
                pending = None
        else:
            if m.timestamp is not None:
                pending = m.timestamp
    return delays


# --------------------------------------------------------------------------- #
# 情绪打分（基于本地词典）
# --------------------------------------------------------------------------- #

#: 匹配词典里所有词（长词优先，避免「喜欢」吃掉「不喜欢」）
_SENTIMENT_RE = re.compile(
    "|".join(re.escape(k) for k in LEX.SENTIMENT_KEYS_BY_LENGTH)
)
_INTENSIFIER_RE = re.compile("|".join(re.escape(k) for k in LEX.INTENSIFIERS))
_NEGATION_RE = re.compile("|".join(re.escape(k) for k in LEX.NEGATION_WORDS))

#: 否定词的作用窗口（字符数）
_NEG_WINDOW = 3
#: 程度副词的作用窗口
_INT_WINDOW = 2


def message_sentiment(text: str) -> float:
    """给一条消息打情感分，返回 **-1 ~ +1**。

    朴素但可审计：逐个命中词典的词，往前看否定词与程度副词做修正。
    反讽和方言会误判，所以报告里一定附原话。
    """
    if not text:
        return 0.0
    low = text.lower()
    hits = 0
    total = 0.0

    for m in _SENTIMENT_RE.finditer(low):
        word = m.group(0)
        weight = LEX.sentiment_weight(word)
        if weight == 0.0:
            continue

        # 往前看：程度副词放大
        pre = low[max(0, m.start() - 6): m.start()]
        for im in _INTENSIFIER_RE.finditer(pre):
            if m.start() - (max(0, m.start() - 6) + im.end()) <= _INT_WINDOW:
                weight *= LEX.INTENSIFIERS[im.group(0)]
                break

        # 往前看：否定词翻转/削弱
        neg_pre = low[max(0, m.start() - _NEG_WINDOW): m.start()]
        if _NEGATION_RE.search(neg_pre):
            # 「不难过」→ 削弱负向；「不喜欢」→ 把正向拉负
            if weight > 0:
                weight = -weight * 0.6
            else:
                weight = -weight * 0.35

        # 感叹号 / 重复字符放大
        if word and word[-1] * 2 in low:
            weight *= 1.15

        total += weight
        hits += 1

    if hits == 0:
        # 没有任何词典命中：happy face / 表情也算一点点正向
        if has_emoji(text):
            return 0.15
        return 0.0

    # 多条命中会累加，用 hits 做软归一，避免长消息天然分高
    score = total / max(1.0, hits ** 0.75)
    return max(-1.0, min(1.0, score))


def sentiment_of(messages: Sequence[Message]) -> float | None:
    """一组消息的平均情感分（只统计有内容的文本消息）。"""
    vals = [message_sentiment(m.text) for m in messages if not m.is_system and not m.is_media and m.text.strip()]
    if not vals:
        return None
    return float(mean(vals))


# --------------------------------------------------------------------------- #
# 维度 1：回复间隔与响应速度
# --------------------------------------------------------------------------- #

def dimension_response(messages: Sequence[Message], me: str, peer: str) -> Dimension:
    peer_delays = reply_delays(messages, peer)
    me_delays = reply_delays(messages, me)

    subs: list[SubMetric] = []
    evidence: list[Evidence] = []

    p_med = _safe_median(peer_delays)
    m_med = _safe_median(me_delays)
    p_fast = (sum(1 for d in peer_delays if d <= FAST_REPLY.total_seconds()) / len(peer_delays)) if peer_delays else None
    m_fast = (sum(1 for d in me_delays if d <= FAST_REPLY.total_seconds()) / len(me_delays)) if me_delays else None
    p_slow = (sum(1 for d in peer_delays if d > SLOW_REPLY.total_seconds()) / len(peer_delays)) if peer_delays else None
    m_slow = (sum(1 for d in me_delays if d > SLOW_REPLY.total_seconds()) / len(me_delays)) if me_delays else None

    enough = len(peer_delays) >= MIN_FOR_REPLY

    subs.append(SubMetric(
        "peer_median", "TA 的中位回复间隔", p_med,
        _squash(p_med, 900.0, 0.6) if (p_med is not None and enough) else None,
        _fmt_seconds(p_med),
    ))
    subs.append(SubMetric(
        "me_median", "你的中位回复间隔", m_med,
        _squash(m_med, 900.0, 0.6) if (m_med is not None and enough) else None,
        _fmt_seconds(m_med),
    ))
    subs.append(SubMetric(
        "peer_fast", "TA 的 5 分钟内回复率", p_fast,
        _clamp(p_fast / 0.7) if (p_fast is not None and enough) else None,
        f"{p_fast * 100:.0f}%" if p_fast is not None else "—",
    ))
    subs.append(SubMetric(
        "me_fast", "你的 5 分钟内回复率", m_fast,
        _clamp(m_fast / 0.7) if (m_fast is not None and enough) else None,
        f"{m_fast * 100:.0f}%" if m_fast is not None else "—",
    ))
    subs.append(SubMetric(
        "peer_slow", "TA 超过 1 小时才回的比例", p_slow,
        _clamp(1.0 - p_slow / 0.5) if (p_slow is not None and enough) else None,
        f"{p_slow * 100:.0f}%" if p_slow is not None else "—",
    ))
    subs.append(SubMetric(
        "me_slow", "你超过 1 小时才回的比例", m_slow,
        _clamp(1.0 - m_slow / 0.5) if (m_slow is not None and enough) else None,
        f"{m_slow * 100:.0f}%" if m_slow is not None else "—",
    ))

    # 不对称度：TA 比 我 慢多少
    ratio = (p_med / m_med) if (p_med is not None and m_med not in (None, 0)) else None
    if ratio is not None and enough:
        # ratio=1 → 0.5；TA 快 → 高分；TA 慢 → 低分
        asy_score = _clamp(1.0 / (1.0 + ratio ** 1.2) * 1.6)
        subs.append(SubMetric(
            "asymmetry", "回复速度不对称度", ratio, asy_score, _ratio_display(ratio),
            note="数值 = TA 的中位回复间隔 ÷ 你的",
        ))

    # 找最典型的「秒回」与「长时间不回」片段。
    # 注意：只有在确实存在差异时才举证——「最快 30 秒、最慢 2 分钟」这种
    # 其实全程都在秒回的情况，把「最慢」当证据会误导人。
    if peer_delays:
        fastest = min(peer_delays)
        slowest = max(peer_delays)
        if fastest <= FAST_REPLY.total_seconds():
            ev = _evidence_at_reply(messages, peer, fastest, "TA 最快的一次回复")
            if ev:
                evidence.append(ev)
        if slowest > SLOW_REPLY.total_seconds():
            ev = _evidence_at_reply(messages, peer, slowest, "TA 最慢的一次回复")
            if ev:
                evidence.append(ev)

    if not enough:
        summary = f"可用于统计的回复次数不足（TA 侧 {len(peer_delays)} 次），该维度不参与打分。"
    elif ratio is None:
        summary = f"TA 中位回复 {_fmt_seconds(p_med)}，你 {_fmt_seconds(m_med)}。"
    elif ratio >= 2:
        summary = f"TA 中位回复 {_fmt_seconds(p_med)}，比你慢约 {ratio:.1f} 倍（你 {_fmt_seconds(m_med)}）。"
    elif ratio <= 0.5:
        summary = f"TA 中位回复 {_fmt_seconds(p_med)}，比你快约 {1 / max(ratio, 1e-6):.1f} 倍（你 {_fmt_seconds(m_med)}）。"
    else:
        summary = f"双方回复节奏接近：TA {_fmt_seconds(p_med)}，你 {_fmt_seconds(m_med)}。"

    return Dimension(
        key="response",
        label="回复间隔与响应速度",
        short="回复速度",
        score_peer=_aggregate_subs(subs, ("peer_median", "peer_fast", "peer_slow", "asymmetry")),
        score_me=_aggregate_subs(subs, ("me_median", "me_fast", "me_slow")),
        summary=summary,
        subs=subs,
        evidence=evidence,
        asymmetry=_ratio_display(ratio) if ratio is not None else "",
    )


def _evidence_at_reply(
    messages: Sequence[Message], speaker: str, delay: float, reason: str
) -> Evidence | None:
    """找到产生某个回复延迟的那条消息。

    用「延迟值 + 消息文本」双重匹配：单看延迟值会在多条间隔相同（例如都是 60 秒）
    时挑错那一条。
    """
    pending: Message | None = None
    for m in messages:
        if m.is_system:
            continue
        if m.speaker == speaker:
            if pending is not None and m.timestamp and pending.timestamp:
                if abs((m.timestamp - pending.timestamp).total_seconds() - delay) < 1:
                    return Evidence(
                        speaker=speaker,
                        timestamp=m.timestamp,
                        text=m.text or "（非文本消息）",
                        reason=f"{reason}（间隔 {_fmt_seconds(delay)}）",
                    )
            pending = None
        else:
            pending = m
    return None


# --------------------------------------------------------------------------- #
# 维度 2：主动发起对话次数
# --------------------------------------------------------------------------- #

def dimension_initiative(messages: Sequence[Message], me: str, peer: str) -> Dimension:
    real = [m for m in messages if not m.is_system]
    threads = build_threads(real)
    runs = build_runs(real)

    starts_peer = sum(1 for t in threads if t and t[0].speaker == peer)
    starts_me = sum(1 for t in threads if t and t[0].speaker == me)
    runs_peer = sum(1 for r in runs if r and r[0].speaker == peer)
    runs_me = sum(1 for r in runs if r and r[0].speaker == me)

    # 冷启动：静默 6 小时后的第一条
    cold_peer = cold_me = 0
    for t in threads:
        if not t:
            continue
        if t[0].speaker == peer:
            cold_peer += 1
        elif t[0].speaker == me:
            cold_me += 1

    # 每日首条归属
    daily_peer = daily_me = 0
    by_day: dict[str, list[Message]] = {}
    for m in real:
        if m.timestamp:
            by_day.setdefault(m.timestamp.strftime("%Y-%m-%d"), []).append(m)
    for day_msgs in by_day.values():
        first = day_msgs[0]
        if first.speaker == peer:
            daily_peer += 1
        elif first.speaker == me:
            daily_me += 1

    total_threads = starts_peer + starts_me
    share_peer = (starts_peer / total_threads) if total_threads else None
    share_runs_peer = (runs_peer / (runs_peer + runs_me)) if (runs_peer + runs_me) else None
    daily_share_peer = (daily_peer / (daily_peer + daily_me)) if (daily_peer + daily_me) else None

    enough = len(real) >= MIN_FOR_INITIATIVE and total_threads >= 5

    subs = [
        SubMetric("peer_threads", "TA 主动开口的轮次占比", share_peer,
                  _clamp((share_peer or 0) / 0.65) if (share_peer is not None and enough) else None,
                  f"{share_peer * 100:.0f}%" if share_peer is not None else "—",
                  note=f"共 {total_threads} 轮（静默 6 小时算新的一轮）"),
        SubMetric("me_threads", "你主动开口的轮次占比", (1 - share_peer) if share_peer is not None else None,
                  _clamp((1 - share_peer) / 0.65) if (share_peer is not None and enough) else None,
                  f"{(1 - share_peer) * 100:.0f}%" if share_peer is not None else "—"),
        SubMetric("peer_runs", "TA 的连续发言段占比", share_runs_peer,
                  _clamp((share_runs_peer or 0) / 0.6) if (share_runs_peer is not None and enough) else None,
                  f"{share_runs_peer * 100:.0f}%" if share_runs_peer is not None else "—",
                  note=f"共 {runs_peer + runs_me} 段"),
        SubMetric("me_runs", "你的连续发言段占比", (1 - share_runs_peer) if share_runs_peer is not None else None,
                  None, f"{(1 - share_runs_peer) * 100:.0f}%" if share_runs_peer is not None else "—"),
        SubMetric("peer_daily", "TA 占每日首条消息的比例", daily_share_peer,
                  _clamp((daily_share_peer or 0) / 0.6) if (daily_share_peer is not None and enough) else None,
                  f"{daily_share_peer * 100:.0f}%" if daily_share_peer is not None else "—",
                  note=f"有消息的天数 {len(by_day)} 天"),
        SubMetric("me_daily", "你占每日首条消息的比例", (1 - daily_share_peer) if daily_share_peer is not None else None,
                  None, f"{(1 - daily_share_peer) * 100:.0f}%" if daily_share_peer is not None else "—"),
        SubMetric("cold_peer", "TA 的「冷启动」次数", float(cold_peer) if total_threads else None,
                  _clamp(cold_peer / max(1, total_threads) / 0.6) if (total_threads and enough) else None,
                  f"{cold_peer} 次", note="静默后主动开口"),
    ]

    evidence: list[Evidence] = []
    for t in threads:
        if t and t[0].speaker == peer:
            evidence.append(Evidence(peer, t[0].timestamp, t[0].text or "（非文本消息）", "TA 主动开启的一轮对话"))
        if len(evidence) >= 4:
            break

    if not enough:
        summary = f"样本不足以稳定判断主动性（有效消息 {len(real)} 条、对话轮次 {total_threads} 轮）。"
    elif share_peer is None:
        summary = "没能识别出对话轮次边界，该维度不参与打分。"
    elif share_peer >= 0.6:
        summary = f"多数对话由 TA 开口：{total_threads} 轮里 TA 发起 {starts_peer} 轮（{share_peer * 100:.0f}%）。"
    elif share_peer <= 0.4:
        summary = f"多数对话由你开口：{total_threads} 轮里你发起 {starts_me} 轮，TA 只占 {share_peer * 100:.0f}%。"
    else:
        summary = f"双方发起次数接近：{total_threads} 轮里 TA 占 {share_peer * 100:.0f}%。"

    return Dimension(
        key="initiative",
        label="主动发起对话次数",
        short="主动发起",
        score_peer=_aggregate_subs(subs, ("peer_threads", "peer_runs", "peer_daily", "cold_peer")),
        score_me=_aggregate_subs(subs, ("me_threads", "me_runs", "me_daily")),
        summary=summary,
        subs=subs,
        evidence=evidence,
        asymmetry=_ratio_display(share_peer / (1 - share_peer)) if share_peer not in (None, 1, 0) else "",
    )


# --------------------------------------------------------------------------- #
# 维度 3：平均字数 / 投入度
# --------------------------------------------------------------------------- #

def dimension_length(messages: Sequence[Message], me: str, peer: str) -> Dimension:
    real = [m for m in messages if not m.is_system and m.timestamp is not None]
    peer_msgs = [m for m in real if m.speaker == peer]
    me_msgs = [m for m in real if m.speaker == me]

    def substantive(msgs: Sequence[Message]) -> list[Message]:
        return [
            m for m in msgs
            if not m.is_media and m.char_count >= SUBSTANTIVE_MIN_CHARS and not _is_dismissive(m.text)
        ]

    p_all = [m.char_count for m in peer_msgs]
    m_all = [m.char_count for m in me_msgs]
    p_sub = [m.char_count for m in substantive(peer_msgs)]
    m_sub = [m.char_count for m in substantive(me_msgs)]

    p_mean_all = _safe_mean(p_all)
    m_mean_all = _safe_mean(m_all)
    p_mean_sub = _safe_mean(p_sub)
    m_mean_sub = _safe_mean(m_sub)

    p_short = (sum(1 for c in p_all if c <= 3) / len(p_all)) if p_all else None
    m_short = (sum(1 for c in m_all if c <= 3) / len(m_all)) if m_all else None

    enough = len(p_all) >= MIN_FOR_LENGTH and len(m_all) >= MIN_FOR_LENGTH

    ratio = (p_mean_sub / m_mean_sub) if (p_mean_sub and m_mean_sub) else None
    ratio_all = (p_mean_all / m_mean_all) if (p_mean_all and m_mean_all) else None

    subs = [
        SubMetric("peer_mean", "TA 平均每条消息字数", p_mean_all,
                  _squash(p_mean_all or 0, 12.0, 0.9) if (p_mean_all is not None and enough) else None,
                  f"{p_mean_all:.1f} 字" if p_mean_all is not None else "—"),
        SubMetric("me_mean", "你平均每条消息字数", m_mean_all,
                  _squash(m_mean_all or 0, 12.0, 0.9) if (m_mean_all is not None and enough) else None,
                  f"{m_mean_all:.1f} 字" if m_mean_all is not None else "—"),
        SubMetric("peer_substantive", "TA 的实质性消息平均字数", p_mean_sub,
                  _squash(p_mean_sub or 0, 14.0, 0.9) if (p_mean_sub is not None and len(p_sub) >= 8) else None,
                  f"{p_mean_sub:.1f} 字" if p_mean_sub is not None else "—",
                  note=f"实质性消息 {len(p_sub)} 条（≥{SUBSTANTIVE_MIN_CHARS} 字且非敷衍语）"),
        SubMetric("me_substantive", "你的实质性消息平均字数", m_mean_sub,
                  _squash(m_mean_sub or 0, 14.0, 0.9) if (m_mean_sub is not None and len(m_sub) >= 8) else None,
                  f"{m_mean_sub:.1f} 字" if m_mean_sub is not None else "—",
                  note=f"实质性消息 {len(m_sub)} 条"),
        SubMetric("peer_ratio", "实质性消息字数比（TA ÷ 你）", ratio,
                  _clamp(1.0 / (1.0 + (1.0 / ratio if ratio else 99) ** 0.9) * 1.5) if (ratio and enough) else None,
                  _ratio_display(ratio)),
        SubMetric("peer_short", "TA 的极短消息（≤3 字）占比", p_short,
                  _clamp(1.0 - (p_short or 0) / 0.5) if (p_short is not None and enough) else None,
                  f"{p_short * 100:.0f}%" if p_short is not None else "—",
                  note='如「嗯」「哦」「好」'),        SubMetric("me_short", "你的极短消息占比", m_short,
                  None, f"{m_short * 100:.0f}%" if m_short is not None else "—"),
    ]

    evidence: list[Evidence] = []
    if p_sub:
        longest = max(substantive(peer_msgs), key=lambda m: m.char_count, default=None)
        if longest:
            evidence.append(Evidence(peer, longest.timestamp, longest.text, f"TA 最长的一条（{longest.char_count} 字）"))
    if m_sub:
        longest = max(substantive(me_msgs), key=lambda m: m.char_count, default=None)
        if longest:
            evidence.append(Evidence(me, longest.timestamp, longest.text, f"你最长的一条（{longest.char_count} 字）"))
    short_peer = [m for m in peer_msgs if m.char_count <= 3 and m.text.strip()]
    for m in short_peer[:2]:
        evidence.append(Evidence(peer, m.timestamp, m.text, "TA 的极短回复"))

    if not enough:
        summary = f"双方消息量不足以稳定比较字数（TA {len(p_all)} 条 / 你 {len(m_all)} 条）。"
    elif ratio is None:
        summary = f"TA 平均 {p_mean_all:.1f} 字、你平均 {m_mean_all:.1f} 字。" if p_mean_all and m_mean_all else "字数无法比较。"
    elif ratio >= 1.25:
        summary = (
            f"TA 说得比你多：实质性消息平均 {p_mean_sub:.1f} 字，是你的 {ratio:.2f} 倍"
            f"（你 {m_mean_sub:.1f} 字）。"
        )
    elif ratio <= 0.75:
        summary = (
            f"你比 TA 说得多：你的实质性消息平均 {m_mean_sub:.1f} 字，TA 只有 {p_mean_sub:.1f} 字"
            f"（{ratio:.2f} 倍）。"
        )
    else:
        summary = f"双方字数相当：TA 平均 {p_mean_all:.1f} 字，你 {m_mean_all:.1f} 字。"

    return Dimension(
        key="length",
        label="平均字数与投入度",
        short="平均字数",
        score_peer=_aggregate_subs(subs, ("peer_mean", "peer_substantive", "peer_ratio", "peer_short")),
        score_me=_aggregate_subs(subs, ("me_mean", "me_substantive", "me_short")),
        summary=summary,
        subs=subs,
        evidence=evidence,
        asymmetry=_ratio_display(ratio_all) if ratio_all else "",
    )


# --------------------------------------------------------------------------- #
# 维度 4：提问与追问比例
# --------------------------------------------------------------------------- #

def dimension_question(messages: Sequence[Message], me: str, peer: str) -> Dimension:
    real = [m for m in messages if not m.is_system and not m.is_media]
    peer_msgs = [m for m in real if m.speaker == peer]
    me_msgs = [m for m in real if m.speaker == me]

    p_q = [m for m in peer_msgs if m.is_question]
    m_q = [m for m in me_msgs if m.is_question]
    p_rate = (len(p_q) / len(peer_msgs)) if peer_msgs else None
    m_rate = (len(m_q) / len(me_msgs)) if me_msgs else None

    # 追问：提问后 10 分钟内又发了下一条
    def followup_rate(msgs: Sequence[Message], speaker: str) -> float | None:
        qs = [m for m in msgs if m.is_question and m.timestamp]
        if not qs:
            return None
        followups = 0
        for q in qs:
            later = [
                m for m in messages
                if m.speaker == speaker and m.timestamp and m.timestamp > q.timestamp  # type: ignore[operator]
                and (m.timestamp - q.timestamp) <= timedelta(minutes=10)  # type: ignore[operator]
            ]
            if later:
                followups += 1
        return followups / len(qs)

    p_follow = followup_rate(peer_msgs, peer)
    m_follow = followup_rate(me_msgs, me)

    # 未被回应的提问：提问后 2 小时内对方没回
    def unanswered_rate(msgs: Sequence[Message], speaker: str, other: str) -> float | None:
        qs = [m for m in msgs if m.is_question and m.timestamp]
        if len(qs) < 3:
            return None
        missed = 0
        for q in qs:
            replied = any(
                m.speaker == other and m.timestamp and q.timestamp
                and timedelta(0) < (m.timestamp - q.timestamp) <= timedelta(hours=2)
                for m in messages
            )
            if not replied:
                missed += 1
        return missed / len(qs)

    p_unanswered = unanswered_rate(peer_msgs, peer, me)

    enough = len(peer_msgs) >= MIN_FOR_QUESTION and len(me_msgs) >= MIN_FOR_QUESTION
    ratio = (p_rate / m_rate) if (p_rate and m_rate) else None

    subs = [
        SubMetric("peer_rate", "TA 的消息中提问占比", p_rate,
                  _clamp((p_rate or 0) / 0.3) if (p_rate is not None and enough) else None,
                  f"{p_rate * 100:.0f}%" if p_rate is not None else "—",
                  note=f"提问 {len(p_q)} 条 / 共 {len(peer_msgs)} 条"),
        SubMetric("me_rate", "你的消息中提问占比", m_rate,
                  _clamp((m_rate or 0) / 0.3) if (m_rate is not None and enough) else None,
                  f"{m_rate * 100:.0f}%" if m_rate is not None else "—"),
        SubMetric("peer_followup", "TA 提问后的追问率", p_follow,
                  _clamp((p_follow or 0) / 0.6) if (p_follow is not None and len(p_q) >= 5) else None,
                  f"{p_follow * 100:.0f}%" if p_follow is not None else "—",
                  note="提问后 10 分钟内继续说"),
        SubMetric("me_followup", "你的追问率", m_follow,
                  _clamp((m_follow or 0) / 0.6) if (m_follow is not None and len(m_q) >= 5) else None,
                  f"{m_follow * 100:.0f}%" if m_follow is not None else "—"),
        SubMetric("peer_unanswered", "TA 的提问里没被回应的比例", p_unanswered,
                  _clamp(1.0 - (p_unanswered or 0) / 0.5) if (p_unanswered is not None) else None,
                  f"{p_unanswered * 100:.0f}%" if p_unanswered is not None else "—",
                  note="2 小时内没有收到你的回复"),
        SubMetric("peer_ratio", "提问占比之比（TA ÷ 你）", ratio,
                  _clamp(ratio / 1.0) if (ratio is not None and enough) else None,
                  _ratio_display(ratio)),
    ]

    evidence: list[Evidence] = []
    for m in p_q[:3]:
        evidence.append(Evidence(peer, m.timestamp, m.text, "TA 的提问"))
    for m in m_q[:2]:
        evidence.append(Evidence(me, m.timestamp, m.text, "你的提问"))

    if not enough:
        summary = f"提问样本不足（TA {len(peer_msgs)} 条 / 你 {len(me_msgs)} 条有效消息）。"
    elif ratio is None:
        summary = "一方几乎没有提问，无法比较。"
    elif ratio >= 1.5:
        summary = f"TA 更常发问：TA 有 {p_rate * 100:.0f}% 的消息是提问，你是 {m_rate * 100:.0f}%。"
    elif ratio <= 0.67:
        summary = f"你更常发问：你有 {m_rate * 100:.0f}% 的消息是提问，TA 只有 {p_rate * 100:.0f}%。"
    else:
        summary = f"双方提问比例接近（TA {p_rate * 100:.0f}% / 你 {m_rate * 100:.0f}%）。"

    return Dimension(
        key="question",
        label="提问与追问比例",
        short="提问追问",
        score_peer=_aggregate_subs(subs, ("peer_rate", "peer_followup", "peer_unanswered", "peer_ratio")),
        score_me=_aggregate_subs(subs, ("me_rate", "me_followup")),
        summary=summary,
        subs=subs,
        evidence=evidence,
        asymmetry=_ratio_display(ratio) if ratio else "",
    )


# --------------------------------------------------------------------------- #
# 维度 5：称呼与表情使用
# --------------------------------------------------------------------------- #

#: 由强到弱的档位分值
_TIER_SCORE = {
    "最高亲密": 1.0,
    "高亲密": 0.9,
    "中等亲密": 0.7,
    "低亲密/调侃": 0.5,
}

_GENERIC_RE = re.compile("|".join(re.escape(w) for w in LEX.GENERIC_ADDRESS))


def _address_terms(text: str) -> list[tuple[str, str]]:
    """返回消息里命中的 ``(称呼, 档位)``。"""
    hits: list[tuple[str, str]] = []
    for term, tier in LEX.all_intimate_terms():
        if term and term in text:
            hits.append((term, tier))
    return hits


def dimension_address(messages: Sequence[Message], me: str, peer: str) -> Dimension:
    real = [m for m in messages if not m.is_system and not m.is_media]
    peer_msgs = [m for m in real if m.speaker == peer]
    me_msgs = [m for m in real if m.speaker == me]

    def collect(msgs: Sequence[Message]) -> list[tuple[Message, str, str]]:
        out: list[tuple[Message, str, str]] = []
        for m in msgs:
            for term, tier in _address_terms(m.text):
                out.append((m, term, tier))
                break  # 一条消息只记最强的一个称呼
        return out

    p_terms = collect(peer_msgs)
    m_terms = collect(me_msgs)

    p_rate = (len(p_terms) / len(peer_msgs)) if peer_msgs else None
    m_rate = (len(m_terms) / len(me_msgs)) if me_msgs else None

    def emoji_rate(msgs: Sequence[Message]) -> float | None:
        if not msgs:
            return None
        return sum(1 for m in msgs if has_emoji(m.text)) / len(msgs)

    p_emoji = emoji_rate(peer_msgs)
    m_emoji = emoji_rate(me_msgs)

    # 称呼演变：把有称呼的消息按时间排序，前 1/3 与后 1/3 的档位均值比较
    def tier_trend(terms: Sequence[tuple[Message, str, str]]) -> float | None:
        dated = [(m.timestamp, tier) for m, _t, tier in terms if m.timestamp]
        if len(dated) < 6:
            return None
        dated.sort(key=lambda x: x[0])
        cut = max(1, len(dated) // 3)
        early = [_TIER_SCORE[t] for _ts, t in dated[:cut]]
        late = [_TIER_SCORE[t] for _ts, t in dated[-cut:]]
        return float(mean(late) - mean(early))

    p_trend = tier_trend(p_terms)
    m_trend = tier_trend(m_terms)

    # 亲昵称呼是不是「很久没出现」了
    def recent_drought(terms: Sequence[tuple[Message, str, str]]) -> float | None:
        dated = [m.timestamp for m, _t, _tier in terms if m.timestamp]
        all_stamps = [m.timestamp for m in real if m.timestamp]
        if not dated or not all_stamps:
            return None
        span = (max(all_stamps) - min(all_stamps)).total_seconds()
        if span <= 0:
            return None
        drought = (max(all_stamps) - max(dated)).total_seconds() / span
        return drought

    p_drought = recent_drought(p_terms)

    # 这个维度什么时候「有信息」？
    #   - 记录里出现过亲昵称呼（任何一方）；或者
    #   - 至少一方真的在用表情。
    # 两者都没有，而且双方表情率都是 0，说明「表情」这条线索本身不可得
    # （很多导出格式根本不留表情），此时给 0 分就是在冤枉人——必须判为无信息。
    has_any_intimate = bool(p_terms or m_terms)
    has_emoji_signal = (p_emoji or 0) > 0 or (m_emoji or 0) > 0
    informative = has_any_intimate or has_emoji_signal
    enough = len(real) >= 20 and informative

    subs = [
        SubMetric("peer_rate", "TA 使用亲昵称呼的消息占比", p_rate,
                  _clamp((p_rate or 0) / 0.2) if (p_rate is not None and enough and has_any_intimate) else None,
                  f"{p_rate * 100:.0f}%" if p_rate is not None else "—",
                  note=f"命中 {len(p_terms)} 条"),
        SubMetric("me_rate", "你使用亲昵称呼的消息占比", m_rate,
                  _clamp((m_rate or 0) / 0.2) if (m_rate is not None and enough and has_any_intimate) else None,
                  f"{m_rate * 100:.0f}%" if m_rate is not None else "—",
                  note=f"命中 {len(m_terms)} 条"),
        SubMetric("peer_emoji", "TA 的表情/表情包使用率", p_emoji,
                  _clamp((p_emoji or 0) / 0.25) if (p_emoji is not None and enough) else None,
                  f"{p_emoji * 100:.0f}%" if p_emoji is not None else "—"),
        SubMetric("me_emoji", "你的表情使用率", m_emoji,
                  _clamp((m_emoji or 0) / 0.25) if (m_emoji is not None and enough) else None,
                  f"{m_emoji * 100:.0f}%" if m_emoji is not None else "—"),
        SubMetric("peer_trend", "TA 的亲昵称呼随时间的变化", p_trend,
                  _clamp(0.5 + (p_trend or 0) * 1.5) if (p_trend is not None and enough) else None,
                  ("+" if (p_trend or 0) >= 0 else "") + f"{(p_trend or 0):.2f}",
                  note="正数=越来越亲昵，负数=在降温"),
        SubMetric("peer_drought", "TA 最后一次叫你亲昵称呼距今占比", p_drought,
                  _clamp(1.0 - (p_drought or 0) * 1.2) if (p_drought is not None and enough and p_terms) else None,
                  f"{p_drought * 100:.0f}%" if p_drought is not None else "—",
                  note="0% = 最近还在叫，接近 100% = 很久没叫了"),
    ]

    evidence: list[Evidence] = []
    for m, term, _tier in p_terms[:2]:
        evidence.append(Evidence(peer, m.timestamp, m.text, f"TA 叫你「{term}」"))
    for m, term, _tier in p_terms[-1:]:
        evidence.append(Evidence(peer, m.timestamp, m.text, f"TA 最后一次叫你「{term}」"))
    for m, term, _tier in m_terms[:1]:
        evidence.append(Evidence(me, m.timestamp, m.text, f"你叫 TA「{term}」"))

    if not informative:
        # 关键：没出现过亲昵称呼 ≠ 不爱；连表情都没有 ≠ 冷淡。
        # 这是「这一项没有信息」，必须剔除而不是给 0 分。
        summary = ("整段记录里既没有亲昵称呼、也没有表情使用——"
                   "这一项没有信息，不参与打分。")
    elif not enough:
        summary = f"消息量偏少（{len(real)} 条），称呼趋势仅供参考。"
    elif not has_any_intimate and has_emoji_signal:
        summary = (f"整段记录里没有亲昵称呼，但表情用得不少"
                   f"（TA {p_emoji * 100:.0f}% / 你 {m_emoji * 100:.0f}%）——"
                   "看来你们的表达方式不靠称呼。")
    elif p_rate is not None and p_rate == 0:
        summary = "TA 在这段记录里从未用亲昵称呼叫你，你叫过 TA。"
    elif p_trend is not None and p_trend < -0.1:
        summary = f"TA 的称呼在降级：早期偏亲昵，后期偏向泛化称呼（变化 {p_trend:+.2f}）。"
    elif p_drought is not None and p_drought > 0.5:
        summary = f"TA 最后一次叫你亲昵称呼，距今已经占了整段记录的 {p_drought * 100:.0f}%。"
    elif p_rate is not None:
        summary = f"TA 有 {p_rate * 100:.0f}% 的消息带亲昵称呼，表情使用率 {p_emoji * 100:.0f}%。" if p_emoji is not None else f"TA 有 {p_rate * 100:.0f}% 的消息带亲昵称呼。"
    else:
        summary = "称呼数据不足。"

    return Dimension(
        key="address",
        label="称呼与表情使用",
        short="称呼表情",
        score_peer=_aggregate_subs(subs, ("peer_rate", "peer_emoji", "peer_trend", "peer_drought")),
        score_me=_aggregate_subs(subs, ("me_rate", "me_emoji")),
        summary=summary,
        subs=subs,
        evidence=evidence,
    )


# --------------------------------------------------------------------------- #
# 维度 6：谁在结束对话
# --------------------------------------------------------------------------- #

def _is_dismissive(text: str) -> bool:
    t = text.strip().strip("。.!！~～…")
    if not t:
        return True
    if t in LEX.DISMISSIVE_REPLIES:
        return True
    # 纯 emoji / 纯标点
    if not re.sub(r"[\W_]+", "", t, flags=re.UNICODE):
        return True
    return False


def _is_closing(text: str) -> bool:
    t = text.strip()
    return any(p in t for p in LEX.CLOSING_PHRASES)


def dimension_ending(messages: Sequence[Message], me: str, peer: str) -> Dimension:
    real = [m for m in messages if not m.is_system and m.timestamp is not None]
    threads = build_threads(real)
    endings = [t[-1] for t in threads if t]

    p_end = sum(1 for m in endings if m.speaker == peer)
    m_end = sum(1 for m in endings if m.speaker == me)
    total = p_end + m_end
    share = (p_end / total) if total else None

    # 谁在对方提问后 30 分钟内不接话就离开
    def dropped_questions(target: str, other: str) -> tuple[int, int]:
        dropped = 0
        asked = 0
        for i, t in enumerate(threads):
            questions = [m for m in t if m.speaker == other and m.is_question]
            for q in questions:
                asked += 1
                # 该轮里 q 之后 target 有没有说话
                after = [m for m in t if m.timestamp and q.timestamp and m.timestamp > q.timestamp]
                if not any(m.speaker == target for m in after):
                    dropped += 1
        return dropped, asked

    p_dropped, p_asked = dropped_questions(peer, me)
    m_dropped, m_asked = dropped_questions(me, peer)

    p_drop_rate = (p_dropped / p_asked) if p_asked >= 5 else None
    m_drop_rate = (m_dropped / m_asked) if m_asked >= 5 else None

    # 用收尾语结束对话的比例
    p_closing = sum(1 for m in endings if m.speaker == peer and _is_closing(m.text))
    m_closing = sum(1 for m in endings if m.speaker == me and _is_closing(m.text))

    enough = total >= MIN_FOR_ENDING

    subs = [
        SubMetric("peer_share", "TA 结束对话的比例", share,
                  _clamp(1.0 - (share or 0)) if (share is not None and enough) else None,
                  f"{share * 100:.0f}%" if share is not None else "—",
                  note=f"共 {total} 轮对话"),
        SubMetric("me_share", "你结束对话的比例", (1 - share) if share is not None else None,
                  None, f"{(1 - share) * 100:.0f}%" if share is not None else "—"),
        SubMetric("peer_drop", "TA 忽略你的提问、直接走开的比例", p_drop_rate,
                  _clamp(1.0 - (p_drop_rate or 0) / 0.5) if p_drop_rate is not None else None,
                  f"{p_drop_rate * 100:.0f}%" if p_drop_rate is not None else "—",
                  note=f"你的提问 {p_asked} 次"),
        SubMetric("me_drop", "你忽略 TA 提问的比例", m_drop_rate,
                  None, f"{m_drop_rate * 100:.0f}%" if m_drop_rate is not None else "—",
                  note=f"TA 的提问 {m_asked} 次"),
        SubMetric("peer_closing", "TA 用收尾语结束的次数", float(p_closing) if enough else None,
                  _clamp(p_closing / max(1, total) / 0.25) if enough else None,
                  f"{p_closing} 次", note='如「晚安」「我先忙」'),
    ]

    evidence: list[Evidence] = []
    peer_endings = [m for m in endings if m.speaker == peer]
    me_endings = [m for m in endings if m.speaker == me]
    for m in peer_endings[:3]:
        evidence.append(Evidence(peer, m.timestamp, m.text or "（非文本消息）", "这一轮由 TA 收尾"))
    for m in me_endings[:2]:
        evidence.append(Evidence(me, m.timestamp, m.text or "（非文本消息）", "这一轮由你收尾"))

    if not enough:
        summary = f"对话轮次太少（{total} 轮），无法判断谁在收尾。"
    elif share is None:
        summary = "没能识别出对话轮次边界。"
    elif share >= 0.65:
        summary = f"多数对话由 TA 收尾（{total} 轮里占 {share * 100:.0f}%）。"
    elif share <= 0.35:
        summary = f"多数对话由你收尾（{total} 轮里 {share * 100:.0f}% 是 TA 说的最后一句）。"
    else:
        summary = f"双方收尾次数接近（TA {share * 100:.0f}%）。"

    return Dimension(
        key="ending",
        label="谁在结束对话",
        short="谁在收尾",
        score_peer=_aggregate_subs(subs, ("peer_share", "peer_drop", "peer_closing")),
        score_me=_aggregate_subs(subs, ("me_share", "me_drop")),
        summary=summary,
        subs=subs,
        evidence=evidence,
    )


# --------------------------------------------------------------------------- #
# 维度 7：深夜及特定时段活跃度
# --------------------------------------------------------------------------- #

def dimension_latenight(messages: Sequence[Message], me: str, peer: str) -> Dimension:
    real = [m for m in messages if not m.is_system and not m.is_media and m.timestamp is not None]

    def hour_of(m: Message) -> int:
        assert m.timestamp is not None
        return m.timestamp.hour

    def is_late(h: int) -> bool:
        return h >= LATE_NIGHT_START or h < LATE_NIGHT_END

    peer_msgs = [m for m in real if m.speaker == peer]
    me_msgs = [m for m in real if m.speaker == me]

    p_late = sum(1 for m in peer_msgs if is_late(hour_of(m)))
    m_late = sum(1 for m in me_msgs if is_late(hour_of(m)))
    p_late_rate = (p_late / len(peer_msgs)) if peer_msgs else None
    m_late_rate = (m_late / len(me_msgs)) if me_msgs else None

    # 深夜对话里 TA 的占比
    late_all = [m for m in real if is_late(hour_of(m))]
    late_share = (sum(1 for m in late_all if m.speaker == peer) / len(late_all)) if late_all else None

    # 各自的活跃时段
    def peak_hours(msgs: Sequence[Message]) -> str:
        buckets: dict[str, int] = {"凌晨 0-6": 0, "上午 6-12": 0, "下午 12-18": 0, "晚上 18-24": 0}
        for m in msgs:
            h = hour_of(m)
            if h < 6:
                buckets["凌晨 0-6"] += 1
            elif h < 12:
                buckets["上午 6-12"] += 1
            elif h < 18:
                buckets["下午 12-18"] += 1
            else:
                buckets["晚上 18-24"] += 1
        if not msgs:
            return "—"
        best = max(buckets.items(), key=lambda kv: kv[1])
        return best[0]

    enough = len(real) >= 40

    subs = [
        SubMetric("peer_late_rate", "TA 的深夜消息占比", p_late_rate,
                  None,  # 深夜多寡本身不分好坏，只做描述，不参与打分
                  f"{p_late_rate * 100:.0f}%" if p_late_rate is not None else "—",
                  note=f"{LATE_NIGHT_START}:00–{LATE_NIGHT_END}:00"),
        SubMetric("me_late_rate", "你的深夜消息占比", m_late_rate,
                  None, f"{m_late_rate * 100:.0f}%" if m_late_rate is not None else "—"),
        SubMetric("late_share", "深夜时段里 TA 的发言占比", late_share,
                  _clamp((late_share or 0) / 0.6) if (late_share is not None and enough) else None,
                  f"{late_share * 100:.0f}%" if late_share is not None else "—",
                  note=f"深夜共 {len(late_all)} 条"),
        SubMetric("peer_peak", "TA 最活跃的时段", None, None, peak_hours(peer_msgs)),
        SubMetric("me_peak", "你最活跃的时段", None, None, peak_hours(me_msgs)),
    ]

    evidence: list[Evidence] = []
    late_peer = [m for m in peer_msgs if is_late(hour_of(m))]
    for m in late_peer[:3]:
        evidence.append(Evidence(peer, m.timestamp, m.text, "TA 的深夜消息"))

    if not enough:
        summary = f"消息量不足以分析活跃时段（{len(real)} 条）。"
    elif late_share is None:
        summary = "整段记录里没有深夜消息。"
    else:
        summary = (
            f"深夜（{LATE_NIGHT_START}:00–{LATE_NIGHT_END}:00）共 {len(late_all)} 条，"
            f"其中 TA 占 {late_share * 100:.0f}%。TA 最活跃在{peak_hours(peer_msgs)}，"
            f"你最活跃在{peak_hours(me_msgs)}。"
        )

    return Dimension(
        key="latenight",
        label="深夜及特定时段活跃度",
        short="深夜活跃",
        # 只把「深夜里谁更主动」计入打分；深夜本身不是好坏指标
        score_peer=_aggregate_subs(subs, ("late_share",)),
        score_me=None,
        summary=summary,
        subs=subs,
        evidence=evidence,
    )


# --------------------------------------------------------------------------- #
# 维度 8：情绪倾向随时间变化
# --------------------------------------------------------------------------- #

def dimension_sentiment(messages: Sequence[Message], me: str, peer: str,
                        periods: Sequence["PeriodStat"] | None = None) -> Dimension:
    real = [m for m in messages if not m.is_system and not m.is_media and m.text.strip()]

    p_vals = [(m, message_sentiment(m.text)) for m in real if m.speaker == peer]
    m_vals = [(m, message_sentiment(m.text)) for m in real if m.speaker == me]

    p_mean = _safe_mean([v for _m, v in p_vals])
    m_mean = _safe_mean([v for _m, v in m_vals])

    # 趋势：按时间八等分，看后半段与前半段的均值差
    def trend_of(vals: Sequence[tuple[Message, float]]) -> float | None:
        dated = [(m.timestamp, v) for m, v in vals if m.timestamp]
        if len(dated) < 10:
            return None
        dated.sort(key=lambda x: x[0])
        half = len(dated) // 2
        first = mean([v for _t, v in dated[:half]])
        second = mean([v for _t, v in dated[half:]])
        return float(second - first)

    p_trend = trend_of(p_vals)
    m_trend = trend_of(m_vals)

    def hard_negative_rate(vals: Sequence[tuple[Message, float]]) -> float | None:
        if not vals:
            return None
        return sum(1 for _m, v in vals if v <= -0.5) / len(vals)

    p_hard = hard_negative_rate(p_vals)

    enough = len(p_vals) >= MIN_FOR_SENTIMENT and len(m_vals) >= MIN_FOR_SENTIMENT

    subs = [
        SubMetric("peer_mean", "TA 消息的平均情绪分", p_mean,
                  _clamp((p_mean + 1) / 2) if (p_mean is not None and enough) else None,
                  f"{p_mean:+.2f}" if p_mean is not None else "—",
                  note="范围 -1（冷）~ +1（暖）"),
        SubMetric("me_mean", "你的平均情绪分", m_mean,
                  _clamp((m_mean + 1) / 2) if (m_mean is not None and enough) else None,
                  f"{m_mean:+.2f}" if m_mean is not None else "—"),
        SubMetric("peer_trend", "TA 的情绪走向", p_trend,
                  _clamp(0.5 + (p_trend or 0) * 2.0) if (p_trend is not None and enough) else None,
                  ("+" if (p_trend or 0) >= 0 else "") + f"{(p_trend or 0):.2f}",
                  note="后半段平均 − 前半段平均；负数=在变冷"),
        SubMetric("me_trend", "你的情绪走向", m_trend,
                  _clamp(0.5 + (m_trend or 0) * 2.0) if (m_trend is not None and enough) else None,
                  ("+" if (m_trend or 0) >= 0 else "") + f"{(m_trend or 0):.2f}"),
        SubMetric("peer_hard", "TA 的强负向消息占比", p_hard,
                  _clamp(1.0 - (p_hard or 0) / 0.2) if (p_hard is not None and enough) else None,
                  f"{p_hard * 100:.0f}%" if p_hard is not None else "—",
                  note="情绪分 ≤ -0.5，如吵架、冷淡"),
    ]

    # 最暖 / 最冷 的原话
    evidence: list[Evidence] = []
    if p_vals:
        warmest = max(p_vals, key=lambda x: x[1])
        coldest = min(p_vals, key=lambda x: x[1])
        if warmest[1] > 0.2:
            evidence.append(Evidence(peer, warmest[0].timestamp, warmest[0].text,
                                     f"TA 说过最暖的一句（情绪分 {warmest[1]:+.2f}）"))
        if coldest[1] < -0.2:
            evidence.append(Evidence(peer, coldest[0].timestamp, coldest[0].text,
                                     f"TA 说过最冷的一句（情绪分 {coldest[1]:+.2f}）"))

    if not enough:
        summary = f"可用于情绪分析的文本不足（TA {len(p_vals)} 条 / 你 {len(m_vals)} 条）。"
    elif p_trend is None:
        summary = f"TA 平均情绪分 {p_mean:+.2f}，样本不足以判断走向。" if p_mean is not None else "情绪数据不足。"
    elif p_trend <= -0.08:
        summary = f"TA 的情绪在变冷：后半段比前半段低 {abs(p_trend):.2f}（平均 {p_mean:+.2f}）。"
    elif p_trend >= 0.08:
        summary = f"TA 的情绪在变暖：后半段比前半段高 {p_trend:.2f}（平均 {p_mean:+.2f}）。"
    else:
        summary = f"TA 的情绪基本平稳（平均 {p_mean:+.2f}，走向 {p_trend:+.2f}）。"

    return Dimension(
        key="sentiment",
        label="情绪倾向随时间变化",
        short="情绪走向",
        score_peer=_aggregate_subs(subs, ("peer_mean", "peer_trend", "peer_hard")),
        score_me=_aggregate_subs(subs, ("me_mean", "me_trend")),
        summary=summary,
        subs=subs,
        evidence=evidence,
    )


# --------------------------------------------------------------------------- #
# 子指标聚合
# --------------------------------------------------------------------------- #

def _aggregate_subs(subs: Sequence[SubMetric], keys: Sequence[str]) -> float | None:
    """把若干子指标聚合成一个 0–1 分数。

    只使用 ``score is not None`` 的子指标；一个都没有就返回 ``None``
    （表示「无信息」，而不是「0 分」）。等权平均，因为每一项都已经
    在自己的映射函数里做过量纲归一。
    """
    picked = [s for s in subs if s.key in keys and s.score is not None]
    if not picked:
        return None
    return float(mean([s.score for s in picked]))  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# 时间段划分
# --------------------------------------------------------------------------- #

def build_periods(real: Sequence[Message], me: str, peer: str) -> list[PeriodStat]:
    """按时间跨度自动选择月/周分段，并算出每个点的趋势数据。"""
    dated = [m for m in real if m.timestamp is not None]
    if not dated:
        return []
    start = min(m.timestamp for m in dated)
    end = max(m.timestamp for m in dated)
    assert start is not None and end is not None
    span_days = max(1, (end - start).days)

    if span_days <= 45:
        bucket_days = 7
    elif span_days <= 150:
        bucket_days = 14
    elif span_days <= 400:
        bucket_days = 30
    else:
        bucket_days = 60

    buckets: dict[datetime, list[Message]] = {}
    for m in dated:
        assert m.timestamp is not None
        delta_days = (m.timestamp - start).days
        idx = delta_days // bucket_days
        key = start + timedelta(days=idx * bucket_days)
        buckets.setdefault(key, []).append(m)

    stats: list[PeriodStat] = []
    for key in sorted(buckets):
        msgs = buckets[key]
        msgs.sort(key=lambda m: m.timestamp or start)
        period_end = key + timedelta(days=bucket_days)
        threads = build_threads(msgs)
        init_me = sum(1 for t in threads if t and t[0].speaker == me)
        init_peer = sum(1 for t in threads if t and t[0].speaker == peer)

        p_delays = reply_delays(msgs, peer)
        m_delays = reply_delays(msgs, me)

        label = f"{key.month}/{key.day}"
        stats.append(PeriodStat(
            label=label,
            start=key,
            end=period_end,
            total=len([m for m in msgs if not m.is_system]),
            me_count=len([m for m in msgs if m.speaker == me and not m.is_system]),
            peer_count=len([m for m in msgs if m.speaker == peer and not m.is_system]),
            initiative_me=init_me,
            initiative_peer=init_peer,
            sentiment_peer=sentiment_of([m for m in msgs if m.speaker == peer]),
            sentiment_me=sentiment_of([m for m in msgs if m.speaker == me]),
            reply_median_peer_sec=_safe_median(p_delays),
            reply_median_me_sec=_safe_median(m_delays),
        ))
    return stats


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #

DIMENSION_BUILDERS: tuple[Callable[..., Dimension], ...] = (
    dimension_response,
    dimension_initiative,
    dimension_length,
    dimension_question,
    dimension_address,
    dimension_ending,
    dimension_latenight,
    dimension_sentiment,
)


def choose_pair(conv: Conversation, me: str | None, peer: str | None) -> tuple[str, str]:
    """决定「我」和「TA」分别是谁。

    顺序：显式指定 > 常见自称 > 消息量最多的两个。
    只有在**能确定**时才返回；两个说话人以下都可以自动定，多了必须指定。
    """
    speakers = conv.speakers
    if not speakers:
        raise ValueError("这份记录里没有识别出任何说话人。")

    if me and me not in speakers:
        # 允许用户用别名指代
        matches = [s for s in speakers if me in s or s in me]
        if len(matches) == 1:
            me = matches[0]
        else:
            raise ValueError(f"找不到你指定的说话人「{me}」。记录里的说话人是：{'、'.join(speakers)}")

    if peer and peer not in speakers:
        matches = [s for s in speakers if peer in s or s in peer]
        if len(matches) == 1:
            peer = matches[0]
        else:
            raise ValueError(f"找不到你指定的说话人「{peer}」。记录里的说话人是：{'、'.join(speakers)}")

    if me and peer:
        return me, peer

    if me and not peer:
        others = [s for s in speakers if s != me]
        if len(others) == 1:
            return me, others[0]
        # 群聊：选互动最多的
        counts = {s: 0 for s in others}
        for m in conv.real_messages:
            if m.speaker in counts:
                counts[m.speaker] += 1
        if counts:
            return me, max(counts, key=lambda s: counts[s])
        raise ValueError("无法确定「TA」是谁，请用 --peer 指定。")

    if peer and not me:
        others = [s for s in speakers if s != peer]
        if len(others) == 1:
            return others[0], peer
        counts = {s: 0 for s in others}
        for m in conv.real_messages:
            if m.speaker in counts:
                counts[m.speaker] += 1
        if counts:
            return max(counts, key=lambda s: counts[s]), peer
        raise ValueError("无法确定「我」是谁，请用 --me 指定。")

    if len(speakers) == 2:
        # 默认把「消息少的那个」当 TA？不。按首次出现顺序：先出现的通常是自己导出的
        # 但导出工具习惯不同，所以不做主观猜测——按出现顺序，报告里显著标注。
        return speakers[0], speakers[1]

    # 三个以上说话人：取消息量最多的两个
    counts: dict[str, int] = {s: 0 for s in speakers}
    for m in conv.real_messages:
        counts[m.speaker] = counts.get(m.speaker, 0) + 1
    top = sorted(counts, key=lambda s: -counts[s])[:2]
    if len(top) < 2:
        raise ValueError("无法确定「我」和「TA」，请用 --me/--peer 指定。")
    return top[0], top[1]


def analyze(conv: Conversation, me: str, peer: str) -> Analysis:
    """跑完八个维度，返回完整的 :class:`Analysis`。"""
    real = conv.real_messages
    if not real:
        raise ValueError("这份记录里没有任何有效消息，无法分析（不会生成虚构报告）。")

    dated = [m for m in real if m.timestamp]
    first_at = min((m.timestamp for m in dated), default=None)
    last_at = max((m.timestamp for m in dated), default=None)
    span_days = ((last_at - first_at).days + 1) if (first_at and last_at) else 0
    active_days = len({m.timestamp.strftime("%Y-%m-%d") for m in dated})

    me_msgs = [m for m in real if m.speaker == me]
    peer_msgs = [m for m in real if m.speaker == peer]

    periods = build_periods(real, me, peer)

    dims: dict[str, Dimension] = {}
    for builder in DIMENSION_BUILDERS:
        if builder is dimension_sentiment:
            dim = builder(real, me, peer, periods)
        else:
            dim = builder(real, me, peer)
        dims[dim.key] = dim

    caveats: list[str] = []
    if first_at is None:
        caveats.append("这份记录里没有解析出任何时间戳，所有与时间相关的维度都无法计算。")
    if len(real) < 50:
        caveats.append(f"有效消息只有 {len(real)} 条，样本偏少，结论可能不稳定。")
    if conv.report.inferred_timestamps:
        caveats.append(
            f"有 {conv.report.inferred_timestamps} 条消息的时间戳是推断出来的（原始文件缺日期或缺时间），"
            "趋势图仅供参考。"
        )
    for d in dims.values():
        if not d.available:
            caveats.append(f"「{d.label}」无足够样本，已从总分中剔除。")

    # 关键数据卡片。
    # 重要：卡片不能把「样本不足、已被剔除」的数字当成事实展示。
    # 例如只有 5 条消息时，「中位回复间隔 1.5 小时」看着像结论，
    # 但它其实是 2 次回复的中位数——必须显式标注不可靠。
    resp = dims["response"]
    init = dims["initiative"]
    leng = dims["length"]
    end = dims["ending"]
    cards: list[tuple[str, str, str]] = [
        ("有效消息", f"{len(real)} 条", f"{me} {len(me_msgs)} · {peer} {len(peer_msgs)}"),
        ("时间跨度", f"{span_days} 天" if span_days else "—",
         f"首尾相差 {span_days} 天，其中 {active_days} 天有对话"),
    ]

    p_med_sub = next((s for s in resp.subs if s.key == "peer_median"), None)
    m_med_sub = next((s for s in resp.subs if s.key == "me_median"), None)
    if p_med_sub and p_med_sub.score is not None:
        cards.append(("中位回复间隔", p_med_sub.display,
                      f"{me}：{m_med_sub.display if m_med_sub else '—'}"))
    else:
        cards.append(("中位回复间隔", "—", "回复样本不足，未参与统计"))

    p_share_sub = next((s for s in init.subs if s.key == "peer_threads"), None)
    if p_share_sub and p_share_sub.score is not None:
        cards.append(("TA 主动发起占比", p_share_sub.display,
                      f"共 {len(build_threads(real))} 轮对话"))
    else:
        cards.append(("TA 主动发起占比", "—", "消息太少，未参与统计"))

    ratio_sub = next((s for s in leng.subs if s.key == "peer_ratio"), None)
    if ratio_sub and ratio_sub.score is not None:
        cards.append(("实质性字数比", ratio_sub.display, f"{peer} ÷ {me}"))
    else:
        cards.append(("实质性字数比", "—", "实质性消息不足，未参与统计"))

    p_end_sub = next((s for s in end.subs if s.key == "peer_share"), None)
    if p_end_sub and p_end_sub.score is not None:
        cards.append(("TA 收尾占比", p_end_sub.display, "对话最后一句话的归属"))
    else:
        cards.append(("TA 收尾占比", "—", "对话轮次不足，未参与统计"))

    p_late_sub = next((s for s in dims["latenight"].subs if s.key == "peer_late_rate"), None)
    if p_late_sub and p_late_sub.value is not None:
        cards.append(("TA 深夜消息占比", p_late_sub.display, "23:00–03:00"))
    else:
        cards.append(("TA 深夜消息占比", "—", "无时间信息"))

    p_sent_sub = next((s for s in dims["sentiment"].subs if s.key == "peer_mean"), None)
    if p_sent_sub and p_sent_sub.score is not None:
        cards.append(("TA 平均情绪分", p_sent_sub.display, "-1 冷 ~ +1 暖"))
    else:
        cards.append(("TA 平均情绪分", "—", "文本样本不足，未参与统计"))

    # 定性素材：最暖的片段
    highlights: dict[str, list[Evidence]] = {}
    warm = [e for e in dims["sentiment"].evidence if "+" in e.reason]
    cold = [e for e in dims["sentiment"].evidence if "冷" in e.reason]
    highlights["warm"] = warm
    highlights["cold"] = cold

    return Analysis(
        me=me,
        peer=peer,
        speakers=conv.speakers,
        total_messages=len(conv.messages),
        total_real=len(real),
        me_messages=len(me_msgs),
        peer_messages=len(peer_msgs),
        first_at=first_at,
        last_at=last_at,
        days_span=span_days,
        active_days=active_days,
        is_group=len(conv.speakers) > 2,
        dimensions=dims,
        periods=periods,
        cards=cards,
        caveats=caveats,
        highlights=highlights,
    )