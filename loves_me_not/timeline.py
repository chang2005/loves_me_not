"""聊天足迹、时间线关键节点、话题关键词与「TA 是怎样的人」。

这个模块回答的是**描述性问题**（你们什么时候聊得最好、聊了多久、聊什么、
TA 的行为模式像什么），而不是打分。所有结论都能追到具体数字与原始消息。

四个引擎：

* :func:`build_footprint` —— 聊天足迹：跨度、活跃月、时段分布、最长单次、
  最长沉默、总对话时长。
* :func:`build_timeline` —— 关键节点：第一次、最长一次、最暖/最冷的一天、
  最热闹的月份、最长的沉默、热度转折点。
* :func:`extract_topics` —— 话题关键词（本地 n-gram + 停用词过滤，零依赖）。
* :func:`build_persona` —— 「TA 是一个怎样的人」：基于**可观测行为**的画像，
  每条都附支撑数字，并明确声明这不是性格判决。
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import mean
from typing import Iterable, Sequence

from . import lexicon as LEX
from .insights import (
    LATE_END_HOUR,
    LATE_START_HOUR,
    DayStat,
    KeyNode,
    Persona,
    Session,
    SilenceGap,
    TopicWord,
    _fmt_duration,
    _reply_delays,
)
from .parser import Message, has_emoji, looks_like_question

# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #

@dataclass
class HourBucket:
    """某个小时（0–23）的活跃度。"""

    hour: int
    count: int
    me_count: int
    peer_count: int

    @property
    def label(self) -> str:
        return f"{self.hour:02d}:00"


@dataclass
class MonthStat:
    label: str          # 2023-04
    count: int
    me_count: int
    peer_count: int
    active_days: int
    peer_share: float
    span_minutes: float

    @property
    def pretty(self) -> str:
        y, m = self.label.split("-")
        return f"{y} 年 {int(m)} 月"


@dataclass
class Footprint:
    """聊天足迹全貌。"""

    first_at: datetime | None
    last_at: datetime | None
    #: 从第一条消息到**现在**的跨度（天）——用户明确要「从开始到当前」
    span_to_now_days: int
    #: 首尾消息之间的跨度
    span_days: int
    active_days: int
    total_messages: int
    total_sessions: int
    #: 所有对话时长之和（分钟）
    total_chat_minutes: float
    #: 单次最长对话
    longest_session: Session | None
    #: 最长沉默
    longest_silence: SilenceGap | None
    #: 按小时分布
    hours: list[HourBucket] = field(default_factory=list)
    #: 按月份统计
    months: list[MonthStat] = field(default_factory=list)
    #: 按星期统计（0 = 周一）
    weekdays: list[int] = field(default_factory=list)
    #: 每天（热力图数据）
    days: dict[str, DayStat] = field(default_factory=dict)

    # ---- 派生结论（用户点名的几个「聊得最好」的问题） ----

    @property
    def best_hour(self) -> HourBucket | None:
        return max(self.hours, key=lambda h: h.count) if self.hours else None

    @property
    def best_hours(self) -> list[HourBucket]:
        """活跃度最高的连续三小时，描述「啥时段聊得最好」。"""
        if not self.hours:
            return []
        counts = [h.count for h in self.hours]
        best_start, best_sum = 0, -1
        for i in range(24):
            total = sum(counts[(i + k) % 24] for k in range(3))
            if total > best_sum:
                best_start, best_sum = i, total
        return [self.hours[(best_start + k) % 24] for k in range(3)]

    @property
    def best_month(self) -> MonthStat | None:
        return max(self.months, key=lambda m: m.count) if self.months else None

    @property
    def warmest_month(self) -> MonthStat | None:
        """「感情最好」的月份：TA 的发言占比最高、且互动量不小。

        口径：先筛掉消息量低于最高月 25% 的月份（避免「只聊了 3 句但 TA 占 100%」），
        再在剩下的月份里取 TA 占比最高者。这是一个**代理指标**，
        报告里会写清楚它不是「感情的真相」。
        """
        if not self.months:
            return None
        peak = max(m.count for m in self.months)
        eligible = [m for m in self.months if m.count >= max(3, peak * 0.25)]
        if not eligible:
            eligible = list(self.months)
        return max(eligible, key=lambda m: (m.peer_share, m.count))

    @property
    def quietest_month(self) -> MonthStat | None:
        return min(self.months, key=lambda m: m.count) if self.months else None

    @property
    def best_weekday(self) -> int | None:
        if not self.weekdays or sum(self.weekdays) == 0:
            return None
        return max(range(7), key=lambda i: self.weekdays[i])

    @property
    def avg_session_minutes(self) -> float:
        return (self.total_chat_minutes / self.total_sessions) if self.total_sessions else 0.0

    @property
    def avg_messages_per_active_day(self) -> float:
        return (self.total_messages / self.active_days) if self.active_days else 0.0


# --------------------------------------------------------------------------- #
# 足迹
# --------------------------------------------------------------------------- #

WEEKDAY_NAMES = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def build_footprint(messages: Sequence[Message], me: str, peer: str,
                    sessions: Sequence[Session], silences: Sequence[SilenceGap],
                    days: dict[str, DayStat], *, now: datetime | None = None) -> Footprint:
    """汇总聊天足迹。"""
    real = [m for m in messages if not m.is_system and m.timestamp is not None]
    now = now or datetime.now()

    if not real:
        return Footprint(
            first_at=None, last_at=None, span_to_now_days=0, span_days=0,
            active_days=0, total_messages=0, total_sessions=0,
            total_chat_minutes=0.0, longest_session=None, longest_silence=None,
        )

    first = min(m.timestamp for m in real)
    last = max(m.timestamp for m in real)
    assert first is not None and last is not None

    # 按小时
    hour_counts = [[0, 0, 0] for _ in range(24)]   # [总数, me, peer]
    weekday_counts = [0] * 7
    for m in real:
        assert m.timestamp is not None
        h = m.timestamp.hour
        hour_counts[h][0] += 1
        if m.speaker == me:
            hour_counts[h][1] += 1
        elif m.speaker == peer:
            hour_counts[h][2] += 1
        weekday_counts[m.timestamp.weekday()] += 1

    hours = [HourBucket(hour=i, count=c, me_count=a, peer_count=b)
             for i, (c, a, b) in enumerate(hour_counts)]

    # 按月
    month_buckets: dict[str, list[Message]] = {}
    for m in real:
        assert m.timestamp is not None
        month_buckets.setdefault(m.timestamp.strftime("%Y-%m"), []).append(m)

    months: list[MonthStat] = []
    for label in sorted(month_buckets):
        msgs = sorted(month_buckets[label], key=lambda x: x.timestamp or datetime.min)
        me_n = sum(1 for m in msgs if m.speaker == me)
        peer_n = sum(1 for m in msgs if m.speaker == peer)
        total_n = len(msgs)
        first_ts = msgs[0].timestamp
        last_ts = msgs[-1].timestamp
        assert first_ts is not None and last_ts is not None
        months.append(MonthStat(
            label=label,
            count=total_n,
            me_count=me_n,
            peer_count=peer_n,
            active_days=len({m.timestamp.strftime("%Y-%m-%d") for m in msgs if m.timestamp}),
            peer_share=(peer_n / total_n) if total_n else 0.0,
            span_minutes=(last_ts - first_ts).total_seconds() / 60.0,
        ))

    total_chat_minutes = sum(s.duration_minutes for s in sessions)
    longest_session = max(sessions, key=lambda s: s.duration, default=None)
    longest_silence = max(silences, key=lambda g: g.length, default=None)

    return Footprint(
        first_at=first,
        last_at=last,
        span_to_now_days=max(0, (now - first).days),
        span_days=(last - first).days + 1,
        active_days=len(days),
        total_messages=len(real),
        total_sessions=len(sessions),
        total_chat_minutes=total_chat_minutes,
        longest_session=longest_session,
        longest_silence=longest_silence,
        hours=hours,
        months=months,
        weekdays=weekday_counts,
        days=days,
    )


# --------------------------------------------------------------------------- #
# 时间线关键节点
# --------------------------------------------------------------------------- #

def build_timeline(messages: Sequence[Message], me: str, peer: str,
                   sessions: Sequence[Session], silences: Sequence[SilenceGap],
                   footprint: Footprint,
                   sentiment_fn=None) -> list[KeyNode]:
    """挑出关系时间线上的关键节点，按时间排序。"""
    nodes: list[KeyNode] = []
    real = [m for m in messages if not m.is_system and m.timestamp is not None]
    if not real:
        return nodes

    # 1. 第一次说话
    first = min(real, key=lambda m: m.timestamp or datetime.min)
    nodes.append(KeyNode(
        kind="first",
        title="第一次说话",
        when=first.timestamp,
        detail=f"{first.speaker}：「{_short(first.text)}」",
        weight=0.5,
    ))

    # 2. 最后一次说话
    last = max(real, key=lambda m: m.timestamp or datetime.min)
    nodes.append(KeyNode(
        kind="last",
        title="最后一次说话",
        when=last.timestamp,
        detail=f"{last.speaker}：「{_short(last.text)}」",
        weight=0.5,
    ))

    # 3. 单次最长的对话
    if footprint.longest_session:
        s = footprint.longest_session
        nodes.append(KeyNode(
            kind="longest_session",
            title="最长的一次聊天",
            when=s.start,
            detail=f"从 {s.start:%H:%M} 聊到 {s.end:%H:%M}，"
                   f"共 {_fmt_duration(s.duration)}、{s.count} 条消息",
            weight=0.9,
        ))

    # 4. 最长的沉默
    if footprint.longest_silence:
        g = footprint.longest_silence
        who = g.broken_by or "（未知）"
        nodes.append(KeyNode(
            kind="longest_silence",
            title="最长的一次沉默",
            when=g.start,
            detail=f"从 {g.start:%Y-%m-%d} 到 {g.end:%Y-%m-%d}，"
                   f"整整 {_fmt_duration(g.length)} 没有说话；最后由{who}打破",
            weight=0.85,
        ))

    # 5. 最热闹的一天
    if footprint.days:
        busiest = max(footprint.days.values(), key=lambda d: d.count)
        nodes.append(KeyNode(
            kind="busiest_day",
            title="最热闹的一天",
            when=busiest.date,
            detail=f"{busiest.count} 条消息，聊了 {busiest.span_minutes:.0f} 分钟",
            weight=0.7,
        ))
        longest_day = max(footprint.days.values(), key=lambda d: d.span_minutes)
        if longest_day.date_key != busiest.date_key:
            nodes.append(KeyNode(
                kind="longest_day",
                title="聊得最久的一天",
                when=longest_day.date,
                detail=f"从早聊到晚，跨度 {longest_day.span_minutes / 60:.1f} 小时、"
                       f"{longest_day.count} 条消息",
                weight=0.7,
            ))

    # 6. 最热闹 / 最冷清的月份
    if footprint.best_month:
        m = footprint.best_month
        nodes.append(KeyNode(
            kind="busiest_month",
            title="聊得最多的月份",
            when=datetime.strptime(m.label + "-01", "%Y-%m-%d"),
            detail=f"{m.pretty}：{m.count} 条消息，{m.active_days} 天有对话",
            weight=0.75,
        ))
    if footprint.quietest_month and footprint.best_month and \
            footprint.quietest_month.label != footprint.best_month.label:
        m = footprint.quietest_month
        nodes.append(KeyNode(
            kind="quietest_month",
            title="最安静的月份",
            when=datetime.strptime(m.label + "-01", "%Y-%m-%d"),
            detail=f"{m.pretty}：只有 {m.count} 条消息",
            weight=0.6,
        ))

    # 7. 最暖 / 最冷的一天（基于情感词典，按天聚合）
    if sentiment_fn is not None:
        by_day: dict[str, list[Message]] = {}
        for m in real:
            assert m.timestamp is not None
            by_day.setdefault(m.timestamp.strftime("%Y-%m-%d"), []).append(m)
        scored: list[tuple[str, float, int]] = []
        for key, msgs in by_day.items():
            vals = [sentiment_fn(m.text) for m in msgs if not m.is_media and m.text.strip()]
            if len(vals) >= 4:
                scored.append((key, float(mean(vals)), len(vals)))
        if scored:
            warm_key, warm_val, _n = max(scored, key=lambda x: x[1])
            cold_key, cold_val, _n2 = min(scored, key=lambda x: x[1])
            if warm_val > 0.05:
                nodes.append(KeyNode(
                    kind="warmest_day",
                    title="最暖的一天",
                    when=datetime.strptime(warm_key, "%Y-%m-%d"),
                    detail=f"情绪均分 {warm_val:+.2f}（越高越暖）",
                    weight=0.8,
                ))
            if cold_val < -0.05:
                nodes.append(KeyNode(
                    kind="coldest_day",
                    title="最冷的一天",
                    when=datetime.strptime(cold_key, "%Y-%m-%d"),
                    detail=f"情绪均分 {cold_val:+.2f}",
                    weight=0.8,
                ))

    # 8. 热度转折点：把时间轴分成前后两半，找消息密度变化最大的那个月
    if len(footprint.months) >= 3:
        counts = [m.count for m in footprint.months]
        best_idx, best_delta = None, 0.0
        for i in range(1, len(counts)):
            before = mean(counts[max(0, i - 2):i])
            after = mean(counts[i:min(len(counts), i + 2)])
            if before <= 0:
                continue
            delta = (after - before) / before
            if abs(delta) > abs(best_delta):
                best_idx, best_delta = i, delta
        if best_idx is not None and abs(best_delta) >= 0.35:
            m = footprint.months[best_idx]
            direction = "明显变多" if best_delta > 0 else "明显变少"
            nodes.append(KeyNode(
                kind="turning",
                title="热度转折点",
                when=datetime.strptime(m.label + "-01", "%Y-%m-%d"),
                detail=f"从 {m.pretty} 起，聊天量{direction}"
                       f"（{abs(best_delta) * 100:.0f}%）",
                weight=0.95,
            ))

    nodes.sort(key=lambda n: (n.when or datetime.min))
    return nodes


def _short(text: str, limit: int = 26) -> str:
    t = " ".join(text.split())
    return t if len(t) <= limit else t[:limit] + "…"


# --------------------------------------------------------------------------- #
# 话题关键词
# --------------------------------------------------------------------------- #

#: 停用词：高频但没有话题信息
STOPWORDS: frozenset[str] = frozenset("""
的 了 是 在 我 你 他 她 它 们 有 和 就 都 也 还 又 很 太 好 不 没 要 会 能 可以 这 那 哪
什么 怎么 为什么 因为 所以 但是 可是 而且 然后 如果 虽然 已经 一下 一个 一些 一样 一直
现在 今天 明天 昨天 时候 时间 感觉 觉得 应该 可能 知道 不是 就是 还是 只是 真的 有点
嗯 哦 啊 吧 呢 吗 呀 嘛 哈 呵 唉 诶 喂 咦 噢 唉 咯 啦 唷 咋 昂 哎
自己 我们 你们 他们 大家 别人 东西 事情 问题 地方 情况 意思 样子
哈哈 哈哈哈 哈哈哈哈 嘿嘿 嘻嘻 呵呵 嗯嗯 哦哦 好的 好吧 好呀 行吧 好吧
谢谢 抱歉 对不起 没事 没关系 不用 可以 好吧 算了
the and for you are with that this have not but can all was one our out day get
""".split())

#: 话题里值得突出但不算停用词的类别
TOPIC_SEEDS: tuple[str, ...] = (
    "工作", "加班", "上班", "下班", "开会", "项目", "考试", "学习", "论文", "面试",
    "吃饭", "外卖", "火锅", "奶茶", "咖啡", "早餐", "午饭", "晚饭", "夜宵", "零食",
    "电影", "电视剧", "综艺", "音乐", "演唱会", "游戏", "打游戏", "看书", "小说",
    "旅行", "旅游", "出去玩", "周末", "放假", "假期", "海边", "爬山", "露营", "公园",
    "猫", "狗", "宠物", "宝宝",
    "回家", "到家", "出门", "地铁", "打车", "堵车", "路上", "机场", "高铁",
    "天气", "下雨", "降温", "好热", "下雪", "冷",
    "感冒", "生病", "医院", "牙疼", "头疼", "体检", "疫苗",
    "生日", "礼物", "纪念日", "情人节", "圣诞节", "过年", "跨年", "婚礼",
    "朋友", "同事", "家人", "妈妈", "爸爸", "室友", "同学",
    "吵架", "冷战", "分手", "和好", "道歉", "误会", "生气", "委屈", "难过",
    "喜欢你", "想你", "爱你", "在乎", "陪你", "见面", "约会", "牵手", "抱抱",
    "睡觉", "晚安", "早安", "午睡", "熬夜", "失眠", "做梦",
    "手机", "电脑", "照片", "视频", "自拍", "表情包", "语音",
)

_TOKEN_RE = re.compile(r"[\u4e00-\u9fff]+|[A-Za-z][A-Za-z0-9'\-]{1,}")

#: 中文按 2–4 字滑窗切词（不引入 jieba，保持零依赖）
_CJK_NGRAM = (2, 3, 4)


def extract_topics(messages: Sequence[Message], me: str, peer: str,
                   limit: int = 46) -> list[TopicWord]:
    """抽出聊得最多的话题关键词。

    做法（完全本地、零依赖）：

    1. 预置话题种子词优先命中——它们是最有信息量的词（工作、加班、电影……）；
    2. 其余中文文本按 2–4 字滑窗切 n-gram，用停用词表过滤；
    3. 英文/数字按词切；
    4. 只保留出现 ≥2 次、且不是「纯语气词组合」的词；
    5. 按频次排序，并记录主要由谁说的（用于配色）。

    局限：没有分词器，中文 n-gram 会把「天天气」这类跨词片段也算进来。
    所以种子词命中会被优先保留，n-gram 只用于补充话题。
    """
    real = [m for m in messages if not m.is_system and not m.is_media and m.text.strip()]
    if not real:
        return []

    counts: dict[str, int] = {}
    speakers: dict[str, dict[str, int]] = {}

    def bump(word: str, speaker: str) -> None:
        counts[word] = counts.get(word, 0) + 1
        speakers.setdefault(word, {})
        speakers[word][speaker] = speakers[word].get(speaker, 0) + 1

    seeds = [s for s in TOPIC_SEEDS]

    for m in real:
        text = m.text
        # 1) 种子词
        for seed in seeds:
            if seed in text:
                bump(seed, m.speaker)
        # 2) 英文词
        for tok in re.findall(r"[A-Za-z][A-Za-z0-9'\-]{2,}", text):
            low = tok.lower()
            if low not in STOPWORDS and len(low) <= 18:
                bump(low, m.speaker)
        # 3) 中文 n-gram
        for run in re.findall(r"[\u4e00-\u9fff]+", text):
            n = len(run)
            for size in _CJK_NGRAM:
                if n < size:
                    continue
                for i in range(n - size + 1):
                    gram = run[i:i + size]
                    if gram in STOPWORDS:
                        continue
                    if _is_noise_gram(gram):
                        continue
                    bump(gram, m.speaker)

    if not counts:
        return []

    # 种子词加权：它们比 n-gram 更有信息量
    seed_set = set(TOPIC_SEEDS)
    scored: list[tuple[str, float]] = []
    for word, c in counts.items():
        if c < 2:
            continue
        weight = c * (2.2 if word in seed_set else 1.0)
        # 3–4 字 n-gram 通常比 2 字更像一个话题
        weight *= {2: 0.75, 3: 1.05, 4: 1.1}.get(len(word), 1.0)
        if word in seed_set:
            weight *= 1.0   # 已加权，避免重复放大
        scored.append((word, weight))

    if not scored:
        return []

    scored.sort(key=lambda kv: -kv[1])

    # 冗余抑制：n-gram 会把「看情况」切出「看情」「情况」这类碎片。
    # 若一个较短的词总是出现在某个更长、频次相近的词里，就丢掉它。
    keep: list[tuple[str, float]] = []
    for word, weight in scored:
        base = counts[word]
        redundant = False
        for longer, _lw in keep:
            if len(longer) > len(word) and word in longer and counts[longer] >= base * 0.9:
                redundant = True
                break
        if not redundant:
            keep.append((word, weight))

    top = keep[:limit]
    max_w = top[0][1] if top else 1.0

    out: list[TopicWord] = []
    for word, w in top:
        spk = speakers.get(word, {})
        me_n, peer_n = spk.get(me, 0), spk.get(peer, 0)
        if me_n == peer_n:
            dominant = None
        else:
            dominant = me if me_n > peer_n else peer
        out.append(TopicWord(
            word=word,
            count=counts[word],
            weight=round(w / max_w, 4) if max_w else 0.0,
            dominant=dominant,
        ))
    return out


def _is_noise_gram(gram: str) -> bool:
    """过滤明显不是词的 n-gram 片段。"""
    if len(set(gram)) == 1:                 # 「哈哈哈」这类
        return True
    if gram in LEX.DISMISSIVE_REPLIES:
        return True
    # 含单字停用词且整体没有话题感的，多半是跨词片段
    filler = set("的了是在和就都也还又很太不没要会能这那哪吧呢吗呀嘛啊哦嗯哈呵唉诶喂咦噢咯啦")
    if all(ch in filler for ch in gram):
        return True
    # 以虚词开头或结尾的片段通常是切歪的
    if gram[0] in "的了是在和就都也还又很太不没要会能吧呢吗呀嘛啊哦嗯" and len(gram) <= 2:
        return True
    # 由代词/连接词拼出来的片段（「那你」「那我」「我就」）没有话题信息
    pronoun = set("我你他她它咱您这那谁啥哪")
    link = set("的了吗呢吧啊是不就都也还又和跟给把被在有无要会能")
    if all(ch in pronoun | link for ch in gram):
        return True
    # 代词 + 单字动词的碎片
    if len(gram) == 2 and gram[0] in pronoun and gram[1] in "说看想要去来做好是在有":
        return True
    return False


# --------------------------------------------------------------------------- #
# 「TA 是一个怎样的人」
# --------------------------------------------------------------------------- #

def build_persona(messages: Sequence[Message], me: str, peer: str,
                  sessions: Sequence[Session], silences: Sequence[SilenceGap],
                  footprint: Footprint) -> list[Persona]:
    """基于**可观测行为**给 TA 画一张像。

    刻意只描述行为模式（什么时候出现、回多快、说什么），
    绝不写「他是个冷漠的人」这类人格判决。每条观察都带支撑数字。
    """
    real = [m for m in messages if not m.is_system and m.timestamp is not None]
    peer_msgs = [m for m in real if m.speaker == peer]
    me_msgs = [m for m in real if m.speaker == me]
    out: list[Persona] = []

    if not peer_msgs:
        return out

    total_peer = len(peer_msgs)
    total_me = len(me_msgs)

    # ---- 1. 出现的时间习惯 ----
    peer_hours: dict[int, int] = {}
    for m in peer_msgs:
        assert m.timestamp is not None
        peer_hours[m.timestamp.hour] = peer_hours.get(m.timestamp.hour, 0) + 1

    night = sum(c for h, c in peer_hours.items() if LATE_START_HOUR <= h < LATE_END_HOUR)
    evening = sum(c for h, c in peer_hours.items() if 18 <= h < 24)
    work = sum(c for h, c in peer_hours.items() if 9 <= h < 18)
    morning = sum(c for h, c in peer_hours.items() if 6 <= h < 9)

    if night / total_peer >= 0.18:
        peak_hour = max(peer_hours, key=lambda h: peer_hours[h])
        out.append(Persona(
            key="night_owl",
            label="深夜型",
            strength=min(1.0, night / max(1, total_peer) / 0.3),
            evidence_text=f"TA 有 {night / total_peer * 100:.0f}% 的消息发在 "
                          f"{LATE_START_HOUR:02d}:00–{LATE_END_HOUR:02d}:00，"
                          f"最常在 {peak_hour:02d} 点出现",
            support=f"{night} / {total_peer} 条在凌晨",
            tone="warm",
        ))
    elif evening / total_peer >= 0.45:
        out.append(Persona(
            key="evening",
            label="夜晚型",
            strength=min(1.0, evening / max(1, total_peer) / 0.6),
            evidence_text=f"TA 有 {evening / total_peer * 100:.0f}% 的消息在 18:00 之后，"
                          "白天基本不出现",
            support=f"{evening} / {total_peer} 条在晚间",
            tone="neutral",
        ))
    elif work / total_peer >= 0.5:
        out.append(Persona(
            key="daytime",
            label="白天型",
            strength=min(1.0, work / max(1, total_peer) / 0.65),
            evidence_text=f"TA 有 {work / total_peer * 100:.0f}% 的消息在 9:00–18:00，"
                          "看起来习惯在工作时间聊天",
            support=f"{work} / {total_peer} 条在白天",
            tone="neutral",
        ))
    elif morning / total_peer >= 0.15:
        out.append(Persona(
            key="morning",
            label="早起型",
            strength=min(1.0, morning / max(1, total_peer) / 0.25),
            evidence_text=f"TA 有 {morning / total_peer * 100:.0f}% 的消息在 6:00–9:00",
            support=f"{morning} / {total_peer} 条在清晨",
            tone="warm",
        ))

    # ---- 2. 回复节奏 ----
    delays = _reply_delays(messages, peer)
    if len(delays) >= 8:
        med = sorted(delays)[len(delays) // 2]
        fast_share = sum(1 for d in delays if d <= 300) / len(delays)
        slow_share = sum(1 for d in delays if d > 3600) / len(delays)
        if med <= 180:
            out.append(Persona(
                key="fast_replier", label="秒回型",
                strength=min(1.0, (600 - med) / 600),
                evidence_text=f"中位回复只要 {_fmt_duration(med)}，"
                              f"{fast_share * 100:.0f}% 的回复在 5 分钟内",
                support=f"{len(delays)} 次回复，中位 {_fmt_duration(med)}",
                tone="warm",
            ))
        elif med >= 3600:
            out.append(Persona(
                key="slow_replier", label="慢回型",
                strength=min(1.0, med / 14400),
                evidence_text=f"中位回复要 {_fmt_duration(med)}，"
                              f"有 {slow_share * 100:.0f}% 的回复超过 1 小时",
                support=f"{len(delays)} 次回复，中位 {_fmt_duration(med)}",
                tone="cool",
            ))
        else:
            out.append(Persona(
                key="steady_replier", label="稳定节奏型",
                strength=0.6,
                evidence_text=f"中位回复 {_fmt_duration(med)}，不快不慢，节奏比较稳定",
                support=f"{len(delays)} 次回复",
                tone="neutral",
            ))

    # ---- 3. 表达量 ----
    if total_peer >= 15 and total_me >= 15:
        peer_avg = mean(len(m.text.strip()) for m in peer_msgs if not m.is_media) if peer_msgs else 0
        me_avg = mean(len(m.text.strip()) for m in me_msgs if not m.is_media) if me_msgs else 0
        if me_avg > 0:
            ratio = peer_avg / me_avg
            if ratio >= 1.2:
                out.append(Persona(
                    key="talkative", label="话多型",
                    strength=min(1.0, (ratio - 1) / 1.2 + 0.4),
                    evidence_text=f"TA 平均每条 {peer_avg:.1f} 字，比你的 {me_avg:.1f} 字多",
                    support=f"字数比 {ratio:.2f}×",
                    tone="warm",
                ))
            elif ratio <= 0.7:
                out.append(Persona(
                    key="terse", label="惜字如金型",
                    strength=min(1.0, (1 - ratio) / 0.6),
                    evidence_text=f"TA 平均每条只有 {peer_avg:.1f} 字，你平均 {me_avg:.1f} 字",
                    support=f"字数比 {ratio:.2f}×",
                    tone="cool",
                ))

    # ---- 4. 提问倾向 ----
    if total_peer >= 15:
        q_share = sum(1 for m in peer_msgs if looks_like_question(m.text)) / total_peer
        if q_share >= 0.25:
            out.append(Persona(
                key="curious", label="爱问型",
                strength=min(1.0, q_share / 0.4),
                evidence_text=f"TA 有 {q_share * 100:.0f}% 的消息是提问，说明在主动了解你",
                support=f"{sum(1 for m in peer_msgs if looks_like_question(m.text))} 条提问",
                tone="warm",
            ))
        elif q_share <= 0.08:
            out.append(Persona(
                key="passive", label="少问型",
                strength=min(1.0, (0.12 - q_share) / 0.12),
                evidence_text=f"TA 只有 {q_share * 100:.0f}% 的消息是提问，很少主动追问",
                support=f"{sum(1 for m in peer_msgs if looks_like_question(m.text))} 条提问",
                tone="cool",
            ))

    # ---- 5. 表达方式：称呼 / 表情 ----
    intimate_hits = sum(1 for m in peer_msgs if any(t in m.text for t, _tier in LEX.all_intimate_terms()))
    emoji_hits = sum(1 for m in peer_msgs if has_emoji(m.text))
    if total_peer >= 15:
        if intimate_hits / total_peer >= 0.08:
            out.append(Persona(
                key="affectionate", label="会叫人型",
                strength=min(1.0, intimate_hits / total_peer / 0.2),
                evidence_text=f"TA 有 {intimate_hits} 条消息用了亲昵称呼",
                support=f"{intimate_hits} / {total_peer} 条",
                tone="warm",
            ))
        if emoji_hits / total_peer >= 0.25:
            out.append(Persona(
                key="expressive", label="表情丰富型",
                strength=min(1.0, emoji_hits / total_peer / 0.4),
                evidence_text=f"TA 有 {emoji_hits / total_peer * 100:.0f}% 的消息带表情",
                support=f"{emoji_hits} / {total_peer} 条",
                tone="warm",
            ))
        elif emoji_hits == 0 and total_peer >= 30:
            out.append(Persona(
                key="plain", label="纯文字型",
                strength=0.5,
                evidence_text="TA 从不发表情，全程纯文字",
                support=f"0 / {total_peer} 条带表情",
                tone="neutral",
            ))

    # ---- 6. 主动性 ----
    if sessions:
        peer_first = sum(1 for s in sessions if s.messages and s.messages[0].speaker == peer)
        share = peer_first / len(sessions)
        if share >= 0.55:
            out.append(Persona(
                key="initiator", label="主动型",
                strength=min(1.0, share / 0.7),
                evidence_text=f"{len(sessions)} 段对话里有 {peer_first} 段是 TA 先开口"
                              f"（{share * 100:.0f}%）",
                support=f"{peer_first} / {len(sessions)} 段",
                tone="warm",
            ))
        elif share <= 0.3:
            out.append(Persona(
                key="responder", label="回应型",
                strength=min(1.0, (0.45 - share) / 0.45),
                evidence_text=f"{len(sessions)} 段对话里只有 {peer_first} 段是 TA 先开口"
                              f"（{share * 100:.0f}%），多数时候是回应你",
                support=f"{peer_first} / {len(sessions)} 段",
                tone="cool",
            ))

    # ---- 7. 破冰意愿 ----
    big = [g for g in silences if g.length >= timedelta(hours=24) and g.broken_by]
    if len(big) >= 3:
        peer_break = sum(1 for g in big if g.broken_by == peer)
        share = peer_break / len(big)
        out.append(Persona(
            key="icebreaker" if share >= 0.5 else "waiter",
            label="会先低头型" if share >= 0.5 else "等你先开口型",
            strength=min(1.0, abs(share - 0.5) * 2 + 0.4),
            evidence_text=(f"{len(big)} 次超过一天的沉默里，TA 主动打破了 {peer_break} 次"
                           f"（{share * 100:.0f}%）"),
            support=f"{peer_break} / {len(big)} 次",
            tone="warm" if share >= 0.5 else "cool",
        ))

    # ---- 8. 深夜/时段稳定性 ----
    if footprint.best_month and footprint.months and len(footprint.months) >= 2:
        counts = [m.count for m in footprint.months]
        spread = (max(counts) - min(counts)) / max(1, max(counts))
        if spread >= 0.7:
            out.append(Persona(
                key="bursty", label="忽冷忽热型",
                strength=min(1.0, spread),
                evidence_text=(f"聊天量月度波动很大：最多的一个月 {max(counts)} 条，"
                               f"最少的只有 {min(counts)} 条"),
                support=f"波动幅度 {spread * 100:.0f}%",
                tone="cool",
            ))
        else:
            out.append(Persona(
                key="consistent", label="节奏稳定型",
                strength=min(1.0, 1 - spread),
                evidence_text="各月聊天量比较平均，没有明显的忽冷忽热",
                support=f"波动幅度 {spread * 100:.0f}%",
                tone="warm",
            ))

    # 强度降序，最多 6 条（太少没内容，太多变啰嗦）
    out.sort(key=lambda p: -p.strength)
    return out[:6]