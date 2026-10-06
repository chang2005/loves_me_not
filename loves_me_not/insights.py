"""量化指标引擎：把聊天记录压成「爱你还是不爱你」的五个行为信号。

与 :mod:`loves_me_not.metrics` 的分工：

* ``metrics`` —— **描述性**的八个维度（回复间隔、主动性、字数……），
  每个维度都算双方对比，输出 0–1 的维度分。
* ``insights``（本模块）—— **结论性**的五个量化指标。
  它们全都只看「TA 的行为」，不问「你们聊得好不好」，
  目的是回答一个更窄的问题：**这个人在这段关系里的投入信号有多强。**

## 五个指标的计算口径（全部在代码里可审计）

| 指标 | 口径 | 数据来源 |
|---|---|---|
| **回复速度** | TA 每次回复的上一条对方消息，到 TA 这条消息之间的间隔秒数；取中位数与均值，并给出 5 分钟内回复率。只在间隔 ≤24h 时算作「回复」。 | 每条消息的时间戳 + 说话人 |
| **每 5 分钟发消息次数** | 「消息爆发度」：把 TA 的消息按 5 分钟滑窗分桶，取**有消息的窗口里平均条数**（= TA 连续说话时的密度），另给单窗口峰值。反映「TA 想不想跟你多说几句」。 | 时间戳 + 说话人 |
| **凌晨发消息次数** | 00:00–05:00 的 TA 消息条数，及占 TA 总量的比例。凌晨是自控力最弱、情绪最真的时段。 | 时间戳小时数 |
| **破冰次数** | 沉默 ≥24 小时后，下一次对话由谁先开口。统计 TA 破冰次数 / 总破冰次数。 | 会话分段 + 每条消息的时间戳 |
| **最后发言次数** | 一段对话（30 分钟无新消息即结束）里最后一条消息的归属。统计 TA 收尾次数 / 总对话数。 | 会话分段 |

**综合情感倾向** = 上述五项各自映射到 0–100 后加权：
回复速度 30%、破冰 25%、最后发言 20%、消息爆发度 15%、凌晨 10%。

凌晨权重刻意最低——凌晨消息多可能是关心，也可能只是作息晚，
它是「情感浓度」的信号而不是「投入」的信号。

所有指标在样本不足时返回 ``None`` 并被剔除、权重重新归一化，
**不会被当成 0 分**。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import mean, median
from typing import Sequence

from .parser import Message

# --------------------------------------------------------------------------- #
# 口径阈值：全部集中在这里，改口径只改这里
# --------------------------------------------------------------------------- #

#: 一次回复超过这个时长就不算「同一轮里的回复」
MAX_REPLY_GAP = timedelta(hours=24)

#: 「秒回」阈值（用于展示，不直接参与打分）
FAST_REPLY = timedelta(minutes=5)

#: 消息爆发度的滑窗大小
BURST_WINDOW = timedelta(minutes=5)

#: 深夜时段（跨零点）。与 README / metrics 的口径保持一致：23:00–03:00。
LATE_START_HOUR = 23
LATE_END_HOUR = 3


def is_late_night_hour(hour: int) -> bool:
    """判断某个小时是否落在深夜时段（跨零点，含 23 与 0/1/2）。"""
    if LATE_START_HOUR <= LATE_END_HOUR:      # 不跨零点
        return LATE_START_HOUR <= hour < LATE_END_HOUR
    return hour >= LATE_START_HOUR or hour < LATE_END_HOUR


#: 深夜时段的展示文本
LATE_LABEL = f"{LATE_START_HOUR:02d}:00–{LATE_END_HOUR:02d}:00"

#: 沉默多久之后算「断档」，下一次开口算「破冰」
ICEBREAK_GAP = timedelta(hours=24)

#: 多久没有新消息就算「这段对话结束了」
SESSION_GAP = timedelta(minutes=30)

#: 各指标的最小样本量
MIN_REPLIES = 10
MIN_BURST_MESSAGES = 20
MIN_LATE_MESSAGES = 40
MIN_ICEBREAKS = 3
MIN_SESSIONS = 8


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #

@dataclass
class Session:
    """一段连续的对话（相邻消息间隔不超过 :data:`SESSION_GAP`）。"""

    index: int
    start: datetime
    end: datetime
    messages: list[Message]

    @property
    def duration(self) -> timedelta:
        return self.end - self.start

    @property
    def duration_minutes(self) -> float:
        return self.duration.total_seconds() / 60.0

    @property
    def count(self) -> int:
        return len(self.messages)

    @property
    def speaker_counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for m in self.messages:
            out[m.speaker] = out.get(m.speaker, 0) + 1
        return out

    @property
    def last_speaker(self) -> str:
        return self.messages[-1].speaker

    @property
    def date_key(self) -> str:
        return self.start.strftime("%Y-%m-%d")


@dataclass
class SilenceGap:
    """一段沉默（两个会话之间的空档）。"""

    start: datetime
    end: datetime
    before_session: int
    after_session: int
    broken_by: str | None

    @property
    def length(self) -> timedelta:
        return self.end - self.start

    @property
    def days(self) -> float:
        return self.length.total_seconds() / 86400.0


@dataclass
class DayStat:
    """热力图上的一个格子。"""

    date: datetime
    count: int
    #: 当天首尾消息的跨度（分钟）——注意这不等于「聊了多久」：
    #: 早上说一句、晚上说一句也会算出十几个小时。
    span_minutes: float
    me_count: int
    peer_count: int
    #: 当天各段对话的时长之和（分钟）——**热力图用它上色**。
    #: 这才是「当次聊天时长」的诚实口径。
    chat_minutes: float = 0.0

    @property
    def date_key(self) -> str:
        return self.date.strftime("%Y-%m-%d")


@dataclass
class KeyNode:
    """关系时间线上的一个关键节点。"""

    kind: str          # first / longest / warmest / coldest / turning / busiest / silence
    title: str
    when: datetime | None
    detail: str
    #: 用于排序与展示强度的值
    weight: float = 0.0


@dataclass
class Persona:
    """「TA 是一个怎样的人」——一条基于行为的观察，不是性格判决。"""

    key: str
    label: str
    #: 0–1，越高表示这个特征越明显
    strength: float
    evidence_text: str
    #: 支撑这条观察的数字
    support: str
    tone: str = "neutral"   # warm / cool / neutral


@dataclass
class TopicWord:
    """词云里的一个词。"""

    word: str
    count: int
    #: 0–1，用于字号映射
    weight: float
    #: 主要由谁说的（占比更高的一方），用于配色
    dominant: str | None = None


@dataclass
class QuantMetric:
    """一个量化指标。"""

    key: str
    label: str
    #: 原始值（含义随指标而定：秒、条、比例……）
    value: float | None
    #: 展示文本
    display: str
    #: 0–1 的分数；``None`` = 样本不足，不参与打分
    score: float | None
    #: 计算口径（报告里会原样展示，让用户能复核）
    formula: str
    #: 数据来源
    source: str
    #: 补充说明
    note: str = ""

    @property
    def available(self) -> bool:
        return self.score is not None


@dataclass
class QuantResult:
    """五个量化指标 + 综合情感倾向。"""

    metrics: dict[str, QuantMetric]
    #: 综合情感倾向（0–100 的原始分，未做样本收缩）
    raw_total: float | None
    #: 实际采用的权重（已剔除无信息项并归一化）
    weights: dict[str, float] = field(default_factory=dict)
    contributions: dict[str, float] = field(default_factory=dict)
    #: 一句话总结
    summary: str = ""
    #: 被剔除的指标
    skipped: list[str] = field(default_factory=list)
    #: 5 分钟爆发度的峰值窗口
    burst_peak: tuple[datetime, int] | None = None


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #

def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _squash(value: float, mid: float, sharpness: float = 1.0) -> float:
    """把正数压到 0–1，``mid`` 处约为 0.5。"""
    if value <= 0:
        return 0.0
    return _clamp(1.0 / (1.0 + (value / mid) ** sharpness))


def balance_score(share: float, ideal: float = 0.65, floor: float = 0.0) -> float:
    """把「某一方的占比」映射成 0–1 的分数。

    用于「破冰」与「最后发言」这类**没有绝对好坏**的指标：
    占比落在 ``ideal`` 附近最健康，偏得越远越低，
    到达 ``floor`` 附近记 0 分。

    * ``share == ideal`` → 1.0
    * ``share == floor`` → 0.0
    * 超过 ``ideal`` → 缓慢回落（另一方偶尔也该收尾，但不至于扣到 0）
    """
    if share is None:
        return 0.0
    if share >= ideal:
        # 超过理想值后温和回落，最低不低于 0.75
        overshoot = min(1.0, (share - ideal) / max(1e-6, 1.0 - ideal))
        return _clamp(1.0 - 0.25 * overshoot)
    span = max(1e-6, ideal - floor)
    return _clamp(share / span)


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


def _fmt_duration(td: timedelta | float) -> str:
    """把时长格式化成「X 天 Y 小时」这种人话。"""
    secs = td.total_seconds() if isinstance(td, timedelta) else float(td)
    if secs < 60:
        return f"{secs:.0f} 秒"
    if secs < 3600:
        return f"{secs / 60:.0f} 分钟"
    if secs < 86400:
        h = secs / 3600
        return f"{h:.1f} 小时"
    days = secs / 86400
    if days < 30:
        return f"{days:.1f} 天"
    return f"{days / 30:.1f} 个月"


# --------------------------------------------------------------------------- #
# 会话分段 / 沉默 / 日历
# --------------------------------------------------------------------------- #

def build_sessions(messages: Sequence[Message],
                   gap: timedelta = SESSION_GAP) -> list[Session]:
    """把消息切成「一段对话」。

    口径：相邻两条消息间隔超过 ``gap``（默认 30 分钟）就断开。
    无时间戳的消息会被跳过（不能凭猜分段）。
    """
    dated = [m for m in messages if not m.is_system and m.timestamp is not None]
    if not dated:
        return []

    sessions: list[Session] = []
    current: list[Message] = [dated[0]]
    for prev, m in zip(dated, dated[1:]):
        assert prev.timestamp is not None and m.timestamp is not None
        if m.timestamp - prev.timestamp > gap:
            sessions.append(Session(
                index=len(sessions),
                start=current[0].timestamp,      # type: ignore[arg-type]
                end=current[-1].timestamp,       # type: ignore[arg-type]
                messages=current,
            ))
            current = [m]
        else:
            current.append(m)
    sessions.append(Session(
        index=len(sessions),
        start=current[0].timestamp,              # type: ignore[arg-type]
        end=current[-1].timestamp,               # type: ignore[arg-type]
        messages=current,
    ))
    return sessions


def build_silences(sessions: Sequence[Session],
                   gap: timedelta = SESSION_GAP) -> list[SilenceGap]:
    """算出会话之间的沉默空档，并标注是谁打破的。"""
    out: list[SilenceGap] = []
    for a, b in zip(sessions, sessions[1:]):
        length = b.start - a.end
        if length <= gap:
            continue
        out.append(SilenceGap(
            start=a.end,
            end=b.start,
            before_session=a.index,
            after_session=b.index,
            broken_by=b.messages[0].speaker if b.messages else None,
        ))
    return out


def build_days(messages: Sequence[Message], me: str, peer: str,
               sessions: Sequence[Session] | None = None) -> dict[str, DayStat]:
    """按自然日聚合：消息数 + 当次聊天时长。

    两个时长口径都存在，因为它们的含义完全不同：

    * ``span_minutes`` = 当天首尾消息的时间差。**不是**「聊了多久」——
      早上说一句、晚上说一句也会算出十几个小时。
    * ``chat_minutes`` = 当天各段对话（30 分钟无消息即断开）的时长之和。
      这才是「当次聊天时长」，**热力图用它上色**。
    """
    buckets: dict[str, list[Message]] = {}
    for m in messages:
        if m.is_system or m.timestamp is None:
            continue
        buckets.setdefault(m.timestamp.strftime("%Y-%m-%d"), []).append(m)

    # 按天累计各段对话的时长
    chat_minutes: dict[str, float] = {}
    for s in (sessions or []):
        chat_minutes[s.date_key] = chat_minutes.get(s.date_key, 0.0) + s.duration_minutes

    out: dict[str, DayStat] = {}
    for key, msgs in buckets.items():
        msgs.sort(key=lambda x: x.timestamp or datetime.min)
        first = msgs[0].timestamp
        last = msgs[-1].timestamp
        assert first is not None and last is not None
        me_count = sum(1 for m in msgs if m.speaker == me)
        peer_count = sum(1 for m in msgs if m.speaker == peer)
        span = (last - first).total_seconds() / 60.0
        out[key] = DayStat(
            date=first,
            count=len(msgs),
            span_minutes=span,
            me_count=me_count,
            peer_count=peer_count,
            # 没有会话信息时退回跨度，但至少不会高估得离谱
            chat_minutes=chat_minutes.get(key, span),
        )
    return out


# --------------------------------------------------------------------------- #
# 指标 1：回复速度
# --------------------------------------------------------------------------- #

def _reply_delays(messages: Sequence[Message], speaker: str) -> list[float]:
    """``speaker`` 每次回复对方所花的秒数。"""
    delays: list[float] = []
    pending: datetime | None = None
    for m in messages:
        if m.is_system or m.timestamp is None:
            continue
        if m.speaker == speaker:
            if pending is not None:
                delta = (m.timestamp - pending).total_seconds()
                if 0 <= delta <= MAX_REPLY_GAP.total_seconds():
                    delays.append(delta)
            pending = None
        else:
            pending = m.timestamp
    return delays


def metric_reply_speed(messages: Sequence[Message], peer: str) -> QuantMetric:
    delays = _reply_delays(messages, peer)
    n = len(delays)
    if n == 0:
        return QuantMetric(
            key="reply_speed", label="回复速度", value=None, display="—", score=None,
            formula="TA 每次回复与上一条对方消息的时间差（仅统计 ≤24 小时的间隔），取中位数",
            source="每条消息的时间戳与说话人",
            note="没有可统计的回复",
        )

    med = float(median(delays))
    avg = float(mean(delays))
    fast = sum(1 for d in delays if d <= FAST_REPLY.total_seconds()) / n
    # 中位间隔映射：5 分钟 → 0.5；越快越高
    score = _squash(med, 300.0, 0.85) if n >= MIN_REPLIES else None
    return QuantMetric(
        key="reply_speed", label="回复速度",
        value=med,
        display=f"中位 {_fmt_seconds(med)}",
        score=score,
        formula="TA 每次回复与上一条对方消息的时间差（仅统计 ≤24 小时的间隔），取中位数；"
                "映射曲线：中位 5 分钟 ≈ 50 分，越快越高",
        source="每条消息的时间戳与说话人",
        note=f"基于 {n} 次回复 · 均值 {_fmt_seconds(avg)} · 5 分钟内回复率 {fast * 100:.0f}%"
             + ("" if n >= MIN_REPLIES else f" · 少于 {MIN_REPLIES} 次，不参与打分"),
    )


# --------------------------------------------------------------------------- #
# 指标 2：每 5 分钟发消息次数（消息爆发度）
# --------------------------------------------------------------------------- #

def _burst_windows(messages: Sequence[Message], speaker: str,
                   window: timedelta = BURST_WINDOW) -> list[tuple[datetime, int]]:
    """把某人的消息按固定 5 分钟网格分桶，返回 ``(窗口起点, 条数)``。"""
    stamps = sorted(m.timestamp for m in messages
                    if not m.is_system and m.speaker == speaker and m.timestamp is not None)
    if not stamps:
        return []

    w = window.total_seconds()
    buckets: dict[float, int] = {}
    for ts in stamps:
        assert ts is not None
        key = (ts.timestamp() // w) * w
        buckets[key] = buckets.get(key, 0) + 1
    return [
        (datetime.fromtimestamp(k), v)
        for k, v in sorted(buckets.items())
    ]


def metric_burst(messages: Sequence[Message], peer: str) -> QuantMetric:
    windows = _burst_windows(messages, peer)
    peer_count = sum(1 for m in messages
                     if not m.is_system and m.speaker == peer and m.timestamp is not None)
    if not windows:
        return QuantMetric(
            key="burst", label="每 5 分钟发消息次数", value=None, display="—", score=None,
            formula="把 TA 的消息按 5 分钟滑窗分桶，取有消息窗口的平均条数",
            source="消息时间戳与说话人",
            note="没有可统计的消息",
        )

    counts = [c for _ts, c in windows]
    avg = float(mean(counts))
    peak = max(counts)
    peak_at = next(ts for ts, c in windows if c == peak)
    score = _clamp(avg / 4.0) if peer_count >= MIN_BURST_MESSAGES else None
    return QuantMetric(
        key="burst", label="每 5 分钟发消息次数",
        value=avg,
        display=f"平均 {avg:.1f} 条 / 5 分钟",
        score=score,
        formula="把 TA 的消息按 5 分钟窗口分桶，统计「有消息的窗口」的平均条数；"
                "映射曲线：平均 4 条 / 5 分钟 ≈ 100 分，1 条 ≈ 25 分",
        source="每条消息的时间戳（只算 TA）",
        note=f"共 {len(windows)} 个活跃窗口 · 峰值 {peak} 条"
             f"（{peak_at:%Y-%m-%d %H:%M}）"
             + ("" if peer_count >= MIN_BURST_MESSAGES else f" · 消息少于 {MIN_BURST_MESSAGES} 条，不参与打分"),
    )


# --------------------------------------------------------------------------- #
# 指标 3：凌晨发消息次数
# --------------------------------------------------------------------------- #

def metric_late_night(messages: Sequence[Message], peer: str) -> QuantMetric:
    peer_msgs = [m for m in messages
                 if not m.is_system and not m.is_media and m.speaker == peer and m.timestamp]
    if not peer_msgs:
        return QuantMetric(
            key="late_night", label="深夜发消息次数", value=None, display="—", score=None,
            formula=f"TA 在 {LATE_LABEL} 发出的消息条数",
            source="每条消息的时间戳小时数",
            note="没有可统计的消息",
        )

    late = [m for m in peer_msgs
            if is_late_night_hour(m.timestamp.hour)]  # type: ignore[union-attr]
    rate = len(late) / len(peer_msgs)
    # 深夜占比映射：10% → 0.5；权重最低，因为「深夜多」也可能只是作息晚
    score = _clamp(rate / 0.20) if len(peer_msgs) >= MIN_LATE_MESSAGES else None
    return QuantMetric(
        key="late_night", label="深夜发消息次数",
        value=float(len(late)),
        display=f"{len(late)} 条",
        score=score,
        formula=f"TA 在 {LATE_LABEL} 发出的消息条数，"
                f"并换算成占 TA 总量的比例（比例 20% ≈ 100 分）",
        source="每条消息的时间戳小时数",
        note=f"占 TA 全部消息的 {rate * 100:.0f}%"
             + ("" if len(peer_msgs) >= MIN_LATE_MESSAGES else f" · 消息少于 {MIN_LATE_MESSAGES} 条，不参与打分"),
    )


# --------------------------------------------------------------------------- #
# 指标 4：破冰次数（沉默 ≥24 小时后主动开口）
# --------------------------------------------------------------------------- #

def metric_icebreak(silences: Sequence[SilenceGap], peer: str) -> QuantMetric:
    breaks = [g for g in silences if g.length >= ICEBREAK_GAP and g.broken_by]
    total = len(breaks)
    peer_breaks = sum(1 for g in breaks if g.broken_by == peer)
    share = (peer_breaks / total) if total else None
    score = balance_score(share, ideal=0.5, floor=0.0) if (total >= MIN_ICEBREAKS and share is not None) else None

    longest = max(breaks, key=lambda g: g.length, default=None)
    note = f"共 {total} 次 ≥24 小时的沉默"
    if longest is not None:
        who = longest.broken_by or "（未知）"
        note += f" · 最长一次 {_fmt_duration(longest.length)}，由{who}打破"
    if total < MIN_ICEBREAKS:
        note += f" · 少于 {MIN_ICEBREAKS} 次，不参与打分"

    return QuantMetric(
        key="icebreak", label="打破僵局次数",
        value=float(peer_breaks),
        display=f"{peer_breaks} / {total} 次" if total else "—",
        score=score,
        formula="沉默 ≥24 小时后，下一段对话的第一条消息由谁发出；"
                "统计 TA 破冰次数 ÷ 总破冰次数。"
                "理想值是双方各半（50% 得满分），全由一方承担则得分下降",
        source="会话分段（30 分钟无消息即断开）+ 每条消息的时间戳与说话人",
        note=note,
    )


# --------------------------------------------------------------------------- #
# 指标 5：最后发言次数（谁在收尾）
# --------------------------------------------------------------------------- #

def metric_last_word(sessions: Sequence[Session], peer: str) -> QuantMetric:
    if not sessions:
        return QuantMetric(
            key="last_word", label="最后发言次数", value=None, display="—", score=None,
            formula="每段对话（30 分钟无新消息即结束）最后一条消息由谁发出",
            source="会话分段 + 每条消息的说话人",
            note="没有可统计的对话",
        )
    total = len(sessions)
    peer_last = sum(1 for s in sessions if s.last_speaker == peer)
    share = peer_last / total
    # 收尾既不天然好也不天然坏：双方各半最健康。
    # 理想值取 0.5，一方全包（0% 或 100%）都会掉到低分。
    score = balance_score(share, ideal=0.5, floor=0.0) if total >= MIN_SESSIONS else None
    return QuantMetric(
        key="last_word", label="最后发言次数",
        value=float(peer_last),
        display=f"{peer_last} / {total} 段",
        score=score,
        formula="每段对话最后一条消息的归属；TA 占 50% 最均衡，"
                "越偏离 50%（无论谁多）说明收尾越固定在一方",
        source="会话分段（30 分钟无消息即结束）+ 每条消息的说话人",
        note=f"TA 收尾占比 {share * 100:.0f}%"
             + ("" if total >= MIN_SESSIONS else f" · 对话少于 {MIN_SESSIONS} 段，不参与打分"),
    )


# --------------------------------------------------------------------------- #
# 综合情感倾向
# --------------------------------------------------------------------------- #

#: 五项指标的权重。凌晨最低：它是「情感浓度」而非「投入」信号。
QUANT_WEIGHTS: dict[str, float] = {
    "reply_speed": 0.30,
    "icebreak": 0.25,
    "last_word": 0.20,
    "burst": 0.15,
    "late_night": 0.10,
}


def compute_quantifiers(messages: Sequence[Message], sessions: Sequence[Session],
                        silences: Sequence[SilenceGap], peer: str) -> QuantResult:
    """算出五个量化指标与综合情感倾向。"""
    metrics = {
        "reply_speed": metric_reply_speed(messages, peer),
        "burst": metric_burst(messages, peer),
        "late_night": metric_late_night(messages, peer),
        "icebreak": metric_icebreak(silences, peer),
        "last_word": metric_last_word(sessions, peer),
    }

    used: dict[str, float] = {}
    contributions: dict[str, float] = {}
    skipped: list[str] = []
    denom = 0.0
    for key, w in QUANT_WEIGHTS.items():
        m = metrics[key]
        if m.score is None:
            skipped.append(key)
            continue
        used[key] = w
        denom += w
        contributions[key] = m.score * 100.0 * w

    if denom <= 0:
        return QuantResult(metrics=metrics, raw_total=None, skipped=skipped,
                           summary="这五个指标都因为样本不足被剔除，无法给出量化倾向。")

    weights = {k: v / denom for k, v in used.items()}
    total = sum(contributions[k] / denom for k in used)

    # 一句话总结：挑贡献最大的加分项与最大的拖累项
    ranked = sorted(used, key=lambda k: metrics[k].score or 0, reverse=True)  # type: ignore[arg-type]
    best = metrics[ranked[0]]
    worst = metrics[ranked[-1]]
    if total >= 65:
        tone = "TA 的行为信号整体偏积极"
    elif total >= 45:
        tone = "TA 的行为信号好坏参半"
    else:
        tone = "TA 的行为信号整体偏弱"
    summary = (f"{tone}：最强的一项是「{best.label}」（{best.display}），"
               f"最弱的一项是「{worst.label}」（{worst.display}）。")

    windows = _burst_windows(messages, peer)
    peak = max(windows, key=lambda kv: kv[1]) if windows else None

    return QuantResult(
        metrics=metrics,
        raw_total=total,
        weights=weights,
        contributions={k: contributions[k] / denom for k in used},
        summary=summary,
        skipped=skipped,
        burst_peak=peak,
    )


def explain_quant(result: QuantResult) -> str:
    """把五个指标的账摊开。"""
    lines: list[str] = []
    if result.raw_total is None:
        lines.append("五个量化指标均样本不足，未计算综合情感倾向。")
    else:
        lines.append(f"综合情感倾向（量化指标）＝ {result.raw_total:.1f} / 100")
        for k, w in sorted(result.weights.items(), key=lambda kv: -kv[1]):
            m = result.metrics[k]
            lines.append(f"  {m.label:<14} {m.display:<20} 得分 {(m.score or 0) * 100:5.1f}"
                         f" × 权重 {w * 100:4.1f}% = 贡献 {result.contributions[k]:5.2f}")
    lines.append("")
    lines.append("计算口径：")
    for m in result.metrics.values():
        lines.append(f"  · {m.label}：{m.formula}")
        lines.append(f"    数据来源：{m.source}")
    if result.skipped:
        names = "、".join(result.metrics[k].label for k in result.skipped)
        lines.append(f"  已剔除（样本不足，权重已重新归一化）：{names}")
    return "\n".join(lines)