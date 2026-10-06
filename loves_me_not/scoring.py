"""可解释的打分模型。

责任分工：:mod:`loves_me_not.metrics` 只负责「算出事实」（中位回复间隔是多少、
谁主动开口几次），本模块负责「把事实换算成一个 0–100 的分数并给出等级结论」。

三条不可让步的规则：

1. **权重必须暴露**。每个维度的权重、实际用到的维度、以及归一化后的权重，
   全部写进结果，报告里可展开查看。没有黑箱。
2. **没有信息 ≠ 0 分**。维度算不出来时直接剔除并重新归一化权重；
   结果里记录 ``skipped``，让人看得见哪些项没参与。
3. **样本不足要收窄结论**。样本量低时分数向 50 收缩（shrinkage），
   并显著降低置信度、在报告顶部直接标注「样本不足」，不给强结论。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from statistics import mean

from .metrics import Analysis, Dimension, Evidence

# --------------------------------------------------------------------------- #
# 权重表：TA 视角（判断「TA 爱不爱我」）与 我的视角
# --------------------------------------------------------------------------- #

#: 判断 TA 的投入度时，各维度权重。选择理由写在下方注释里。
WEIGHTS_PEER: dict[str, float] = {
    # 回复速度最能反映「你在他心里的优先级」——但单独看意义有限，所以给 0.24
    "response": 0.24,
    # 主动性：「有没有想起你」的行为证据
    "initiative": 0.22,
    # 字数与投入度：敷衍 vs 认真
    "length": 0.16,
    # 提问：想不想了解你
    "question": 0.12,
    # 称呼：关系温度的语言痕迹
    "address": 0.10,
    # 谁在收尾：谁先转身
    "ending": 0.08,
    # 情绪走向：是变冷还是变暖
    "sentiment": 0.10,
    # 深夜活跃度只占很小权重——深夜本身不分好坏，只做描述
    "latenight": 0.06,
}

#: 判断「我」的投入度时，可用维度更少（称呼/收尾/深夜是双方共有的指标）
WEIGHTS_ME: dict[str, float] = {
    "response": 0.30,
    "initiative": 0.26,
    "length": 0.20,
    "question": 0.14,
    "sentiment": 0.10,
}

#: 综合「双向互动」的权重：谁付出的多寡同样重要，所以加了对称性
WEIGHTS_PAIR: dict[str, float] = {
    "response": 0.26,
    "initiative": 0.24,
    "length": 0.18,
    "question": 0.14,
    "ending": 0.08,
    "sentiment": 0.10,
}


# --------------------------------------------------------------------------- #
# 等级
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Tier:
    key: str
    low: int
    high: int
    title: str
    one_liner: str
    color: str
    advice: str


TIERS: tuple[Tier, ...] = (
    # 颜色走「品红 → 珊瑚 → 紫 → 灰紫」这条链：分数越高越暖、越饱和，
    # 越低越冷、越接近中性灰，让结论的强弱一眼可辨。
    Tier("hot", 85, 100, "还是很爱你", "主动、秒回、话题不断，这段关系眼下是热的。",
         "#e1487f", "别把这份热当成理所当然——关系是两个人一起维护的，你也主动一点。"),
    Tier("warm", 70, 84, "热度在线", "整体稳定，偶尔降温，但没有脱轨的迹象。",
         "#f2764a", "多留意那些「回得慢了一点」的时刻，及时说开，比憋着好。"),
    Tier("lukewarm", 55, 69, "有点平淡了", "人还在，只是节奏慢下来了——很多长期关系都会走到这里。",
         "#e0a12e", "平淡不等于结束。找一个两个人都松的晚上，认真聊一次。"),
    Tier("cooling", 40, 54, "已经在变淡", "多项指标都偏向单方面：你在推进，他在应付。",
         "#a855c7", "这不是你的错，也不是你不值得。但你值得一个会主动找你的人。"),
    Tier("cold", 20, 39, "基本没戏", "回应稀薄、长期单方面投入，数据上已经很难找出热度。",
         "#7b6ba8", "承认这一点很疼，但看清它是往前走的开始。"),
    Tier("frozen", 0, 19, "几乎可以放手了", "从记录看，这段关系基本已经停摆。",
         "#8d8598", "你可以停在这里了。不是放弃他，是把自己捡回来。"),
)

TIER_UNCLEAR = Tier(
    "unclear", -1, -1, "样本不足，无法给结论",
    "能读到的消息太少，任何分数都是噪声——这里不编结论。",
    "#a49cae",
    "如果真的想知道，试着导出更长时间、更完整的记录再来一次；"
    "或者干脆别测了，直接问他一句。",
)


def tier_for(score: int) -> Tier:
    for t in TIERS:
        if t.low <= score <= t.high:
            return t
    return TIERS[-1] if score > 50 else TIERS[0]


# --------------------------------------------------------------------------- #
# 结果结构
# --------------------------------------------------------------------------- #

@dataclass
class ScorePart:
    """一个视角的评分明细。"""

    name: str
    score: float | None
    used: dict[str, float] = field(default_factory=dict)      # 维度 → 归一化权重
    skipped: list[str] = field(default_factory=list)          # 被剔除的维度 key
    contributions: dict[str, float] = field(default_factory=dict)  # 维度 → 分数贡献（0–100）
    #: 维度 → 本次实际采用的 0–1 分数。
    #: 必须记下来：双向视角在只有一方可得时会取那一方的分数，
    #: 事后用 ``dim.score_me`` 反推会拿到 ``None`` 并算错。
    scores_used: dict[str, float] = field(default_factory=dict)

    @property
    def available(self) -> bool:
        return self.score is not None


@dataclass
class Confidence:
    """样本可信度。"""

    level: str          # high / medium / low / none
    label: str
    reasons: list[str] = field(default_factory=list)
    score: float = 0.0  # 0–1


@dataclass
class ScoreResult:
    #: 0–100 的整数总分（八维模型与量化指标的加权综合）
    total: int
    #: 收缩前的原始分，用于透明展示
    raw_total: float
    tier: Tier
    peer: ScorePart
    me: ScorePart
    pair: ScorePart
    confidence: Confidence
    #: 是否样本不足（报告顶部要显著提示）
    insufficient: bool
    #: 主要拖后腿 / 加分的维度
    top_positive: list[tuple[str, float, float]]   # (label, dim score 0-1, weight)
    top_negative: list[tuple[str, float, float]]
    #: 支撑结论的关键原话
    evidence: list[Evidence] = field(default_factory=list)
    #: 安慰文案
    comfort_title: str = ""
    comfort_body: str = ""
    #: 免责声明（固定文案）
    disclaimer: str = (
        "本报告基于统计学规律生成，仅供娱乐与自我反思，不代表任何一方的真实情感，"
        "重大情感决策请咨询线下专业人士。"
    )
    # ---- 与量化指标综合后的结果 ----
    #: 八维模型的原始分（0–100），未与量化指标混合
    base_score: float | None = None
    #: 量化指标的综合情感倾向（0–100）
    quant_score: float | None = None
    #: 综合后实际采用的两个权重（和为 1）
    blend: dict[str, float] = field(default_factory=dict)
    #: 综合说明
    blend_note: str = ""


# --------------------------------------------------------------------------- #
# 计算
# --------------------------------------------------------------------------- #

def _score_part(
    name: str,
    analysis: Analysis,
    weights: dict[str, float],
) -> ScorePart:
    """按权重表算一个视角的分数，自动剔除无信息的维度并归一化。"""
    used: dict[str, float] = {}
    skipped: list[str] = []
    contributions: dict[str, float] = {}
    scores_used: dict[str, float] = {}
    denom = 0.0

    for key, w in weights.items():
        dim = analysis.dimensions.get(key)
        if dim is None:
            skipped.append(key)
            continue
        score = dim.score_peer if name != "me" else dim.score_me
        if name == "pair":
            # 双向视角：能拿到双方分数就取平均；只有一方可得就用那一方，
            # 不要拿一个不存在的 0 去把对方拉平。
            pair_scores = [s for s in (dim.score_peer, dim.score_me) if s is not None]
            score = mean(pair_scores) if pair_scores else None
        if score is None:
            skipped.append(key)
            continue
        used[key] = w
        denom += w
        contributions[key] = score * 100.0
        scores_used[key] = score

    if denom <= 0:
        return ScorePart(name=name, score=None, used={}, skipped=skipped)

    normalized = {k: v / denom for k, v in used.items()}
    total = sum(contributions[k] * normalized[k] for k in used)
    return ScorePart(
        name=name,
        score=total,
        used=normalized,
        skipped=skipped,
        contributions={k: contributions[k] * normalized[k] for k in used},
        scores_used=scores_used,
    )


def _confidence(analysis: Analysis) -> Confidence:
    """评估样本可信度。这个值会直接影响结论的说服力。"""
    reasons: list[str] = []
    score = 1.0

    n = analysis.total_real
    if n < 30:
        score *= 0.15
        reasons.append(f"有效消息仅 {n} 条")
    elif n < 80:
        score *= 0.5
        reasons.append(f"有效消息 {n} 条，偏少")
    elif n < 200:
        score *= 0.8
        reasons.append(f"有效消息 {n} 条")

    if analysis.active_days < 3:
        score *= 0.3
        reasons.append(f"只有 {analysis.active_days} 天有对话")
    elif analysis.active_days < 10:
        score *= 0.7
        reasons.append(f"{analysis.active_days} 天有对话")

    if analysis.days_span < 7:
        score *= 0.4
        reasons.append(f"时间跨度只有 {analysis.days_span} 天")
    elif analysis.days_span < 30:
        score *= 0.8
        reasons.append(f"时间跨度 {analysis.days_span} 天")

    usable = sum(1 for d in analysis.dimensions.values() if d.score_peer is not None)
    if usable <= 2:
        score *= 0.25
        reasons.append(f"只有 {usable} 个维度有足够样本")
    elif usable <= 4:
        score *= 0.7
        reasons.append(f"仅 {usable}/8 个维度有足够样本")

    if analysis.first_at is None:
        score *= 0.2
        reasons.append("没有解析出时间戳")

    score = max(0.0, min(1.0, score))
    if score >= 0.7:
        level, label = "high", "可信度较高"
    elif score >= 0.45:
        level, label = "medium", "可信度中等"
    elif score > 0.0:
        level, label = "low", "可信度低"
    else:
        level, label = "none", "无法评估"
    return Confidence(level=level, label=label, reasons=reasons, score=score)


#: 样本量达到这个数就不做收缩
SHRINK_FULL_AT = 120.0
#: 最低可信度对应的收缩强度（向 50 靠拢的比例）
SHRINK_MIN_KEEP = 0.35


def _shrink(raw: float, analysis: Analysis, confidence: Confidence) -> float:
    """样本越少，越向 50 收缩——避免「5 条消息得出 92 分」。"""
    n = float(analysis.total_real)
    size_factor = min(1.0, n / SHRINK_FULL_AT)
    # 综合样本量与可信度
    keep = SHRINK_MIN_KEEP + (1.0 - SHRINK_MIN_KEEP) * size_factor * (0.5 + 0.5 * confidence.score)
    keep = max(SHRINK_MIN_KEEP, min(1.0, keep))
    return 50.0 + (raw - 50.0) * keep


# --------------------------------------------------------------------------- #
# 安慰文案：分档，绝不重复
# --------------------------------------------------------------------------- #

COMFORT: dict[str, tuple[str, str]] = {
    "hot": (
        "这份热，是真的",
        "数据里全是他在找你的痕迹：深夜的消息、秒回的速度、说不完的话。"
        "这样的时刻值得被记住，也值得被珍惜。\n\n"
        "但也想提醒你一句：人心是会变的，不是变坏，是本来就会流动。"
        "今天的热度不是永久的保证，所以别把它当成安全感的全部来源——"
        "你可以享受它，同时依然保有自己的生活、朋友和想做的事。\n\n"
        "最好的关系不是「他永远这样」，而是「我们都在，而且都还愿意」。",
    ),
    "warm": (
        "还不错的温度",
        "整体看，他还在，回应也还在。这不是敷衍出来的结果。\n\n"
        "关系很少一路向上，它更像潮汐——有时候靠得很近，有时候各忙各的。"
        "如果你最近觉得有点凉，也许先别急着下结论，找一个两个人都松下来的晚上，"
        "把话说完。\n\n"
        "与其反复测一遍又一遍等一个安心，不如直接问他一句。"
        "人是会变的，但话是能说清楚的。",
    ),
    "lukewarm": (
        "平淡不是终点",
        "分数落在中间，说明这段关系既没有崩坏，也没有在燃烧。"
        "很多走得久的关系都会经过这一段——不是不爱了，是日子太长了。\n\n"
        "有件事想让你知道：一个人回得慢、说得少，原因可能有很多种，"
        "工作、疲惫、性格、甚至只是他本来就不擅长用文字表达。"
        "这份报告看不见这些，所以它不该替你做决定。\n\n"
        "如果你还想要这段关系，就把它当成一个提醒：该主动聊一次了。",
    ),
    "cooling": (
        "你没有做错什么",
        "数据里有很多你主动的痕迹：你在开口，你在追问，你在收尾之后还想再说一句。"
        "而回应在变薄。\n\n"
        "我想说的是——这不是因为你不够好，也不是因为你不够努力。"
        "感情里的热度是会变的，它不总是谁的错，有时候只是两个人走的节奏不一样了。\n\n"
        "你可以再试一次，认真地、不指责地把你的感受说出来。"
        "如果他接住了，那是好事；如果没有，那你至少知道了答案，"
        "而答案比反复猜测要轻得多。\n\n"
        "无论结果如何，你值得一个会主动想起你的人。这一点不因为他的选择而改变。",
    ),
    "cold": (
        "你已经很努力了",
        "从记录里能看到，你在很长一段时间里都是那个先开口的人。"
        "这份坚持本身很珍贵，只是它没有得到对等的回应。\n\n"
        "承认这一点会疼。但看清它，是往前走的开始。"
        "人心会变，这不是对你的判决，而是关于他、关于时间、关于很多事情的一件事实。\n\n"
        "你不必马上放下，也不用逼自己立刻不难过。"
        "只是可以慢慢把注意力挪回来一点——挪回到那些不需要你反复确认也依然在的关系上，"
        "挪回到你自己身上。",
    ),
    "frozen": (
        "可以停在这里了",
        "数据给出的画面很安静：消息少了，回应稀了，主动的那一方几乎一直是你。\n\n"
        "这不是放弃他，是把自己捡回来。"
        "你不需要再为一段已经停下来的对话找理由，也不需要为了一个不回消息的人继续消耗自己。\n\n"
        "人心会变，包括有一天你回头看，会发现自己也没那么在意了——"
        "那一天会来的，比你想象的早。\n\n"
        "在那之前，好好吃饭，好好睡觉，把想说的话先对自己说一遍。"
        "你一直值得被认真对待。",
    ),
    "unclear": (
        "样本太少，先别急着下结论",
        "这一次真的没法给你一个结论——能读到的消息太少了，"
        "任何分数都更像是噪声，而不是他的心意。\n\n"
        "所以与其对着一个不可靠的数字反复揣测，不如换个方式："
        "导出更长、更完整的记录；或者干脆直接问他一句。"
        "猜来猜去消耗的力气，往往比开口多得多。\n\n"
        "无论答案是什么，你都不用一个人扛着。",
    ),
}


# --------------------------------------------------------------------------- #
# 主入口
# --------------------------------------------------------------------------- #

#: 报告中最多展示多少条原话证据（样本越小，能举的证越少）
MAX_EVIDENCE = 10


def _evidence_cap(analysis: Analysis) -> int:
    """样本越小，越不该堆砌证据——否则 5 条消息也能凑出 9 条「举证」。"""
    n = analysis.total_real
    if n < 20:
        return 3
    if n < 50:
        return 5
    if n < 150:
        return 8
    return MAX_EVIDENCE


#: 八维模型与量化指标在总分里的权重。
#: 八维模型覆盖面更广（主动性、字数、提问、称呼、情绪走向），
#: 量化指标更聚焦「TA 的行为信号」（回复速度、破冰、收尾、爆发度、深夜），
#: 所以给八维模型略高的权重。
BLEND_BASE = 0.6
BLEND_QUANT = 0.4


def score(analysis: Analysis) -> ScoreResult:
    """把 :class:`~loves_me_not.metrics.Analysis` 换算成最终结论。

    总分是两个视角的综合：

    1. **八维模型**（:data:`WEIGHTS_PEER`）——覆盖面广；
    2. **五大量化指标**（:mod:`loves_me_not.insights`）——聚焦 TA 的行为信号。

    两者各有权重，先分别得出 0–100，再按 :data:`BLEND_BASE` / :data:`BLEND_QUANT`
    加权。**任何一方缺失就用另一方**，绝不把缺失方当 0 分。
    综合之后再按样本量做收缩。
    """
    peer_part = _score_part("peer", analysis, WEIGHTS_PEER)
    me_part = _score_part("me", analysis, WEIGHTS_ME)
    pair_part = _score_part("pair", analysis, WEIGHTS_PAIR)

    confidence = _confidence(analysis)

    # ---- 两个视角的分 ----
    base_score = peer_part.score              # 0–100 或 None
    quant = getattr(analysis, "quant", None)
    quant_score = getattr(quant, "raw_total", None)

    blend: dict[str, float] = {}
    if base_score is not None and quant_score is not None:
        blend = {"base": BLEND_BASE, "quant": BLEND_QUANT}
        raw = base_score * BLEND_BASE + quant_score * BLEND_QUANT
        blend_note = (f"八维模型 {base_score:.1f} × {BLEND_BASE:.0%} + "
                      f"量化指标 {quant_score:.1f} × {BLEND_QUANT:.0%} = {raw:.1f}")
    elif base_score is not None:
        blend = {"base": 1.0}
        raw = base_score
        blend_note = (f"量化指标样本不足，总分完全来自八维模型（{base_score:.1f}）")
    elif quant_score is not None:
        blend = {"quant": 1.0}
        raw = quant_score
        blend_note = (f"八维模型样本不足，总分完全来自量化指标（{quant_score:.1f}）")
    else:
        blend = {}
        raw = 50.0
        blend_note = "两个视角都因样本不足被剔除，无法给出可靠分数。"

    insufficient = (
        (base_score is None and quant_score is None)
        or analysis.total_real < 20
        or confidence.level in ("low", "none")
    )

    total_f = _shrink(raw, analysis, confidence)
    total = int(round(max(0.0, min(100.0, total_f))))
    if base_score is None and quant_score is None:
        total = 50
        tier = TIER_UNCLEAR
    else:
        tier = tier_for(total)
        if insufficient and (confidence.level == "none" or analysis.total_real < 20):
            # 样本不足时，绝不给强结论
            tier = TIER_UNCLEAR

    # 找出主要加分项 / 拖后腿项。
    # 关键：按「对总分的实际影响」排序，而不是按分数本身——否则会出现
    # 「加分项 0.95、拖后腿项 0.95」这种自相矛盾的展示。
    # 影响 = |维度分 - 0.5| × 归一化权重：把分数推开 50 分基准的力度。
    contrib = peer_part.contributions
    labels = {k: analysis.dimensions[k].label for k in contrib}

    def impact(key: str) -> float:
        dim = analysis.dimensions[key]
        s = dim.score_peer
        if s is None:
            return 0.0
        weight = peer_part.used.get(key, 0.0)
        return abs(s - 0.5) * weight

    def pack(keys: list[str]) -> list[tuple[str, float, float]]:
        out = []
        for k in keys:
            dim = analysis.dimensions[k]
            dim_score = dim.score_peer if dim.score_peer is not None else 0.0
            out.append((labels[k], dim_score, peer_part.used.get(k, 0.0)))
        return out

    # 加分：分数高于 0.5 的，按拉开幅度排序
    lift_keys = sorted(
        (k for k in contrib if (analysis.dimensions[k].score_peer or 0) > 0.5),
        key=impact, reverse=True,
    )[:3]
    # 拖后腿：分数低于 0.5 的，按拉开幅度排序
    drag_keys = sorted(
        (k for k in contrib if (analysis.dimensions[k].score_peer or 0) < 0.5),
        key=impact, reverse=True,
    )[:3]

    top_positive = pack(lift_keys)
    top_negative = pack(drag_keys)

    # 证据挑选：优先「回复速度」「称呼」「收尾」这些最能说明问题的维度。
    # 同一句话可能同时命中「收尾」和「最后一次叫你宝贝」，按文本去重，
    # 否则证据区会被重复原话占满。
    priority = ("response", "address", "ending", "initiative", "sentiment", "question", "length")
    evidence: list[Evidence] = []
    seen_text: set[str] = set()
    cap = _evidence_cap(analysis)
    for key in priority:
        dim = analysis.dimensions.get(key)
        if not dim:
            continue
        for ev in dim.evidence:
            text_key = ev.text.strip()[:40]
            if not text_key or text_key in seen_text:
                continue
            seen_text.add(text_key)
            evidence.append(ev)
            if len(evidence) >= cap:
                break
        if len(evidence) >= cap:
            break

    c_title, c_body = COMFORT.get(tier.key, COMFORT["unclear"])

    # 个性化收尾文案：依赖最终得分（决定夸赞还是鼓励），所以放在这里生成。
    # 挑不出足够独特的观察时保持 None，报告会只展示分档通用文案。
    personal_note = None
    try:
        from . import timeline as _timeline
        personal_note = _timeline.build_personal_note(
            analysis.raw_messages,
            analysis.me,
            analysis.peer,
            analysis.sessions,
            analysis.silences,
            analysis.footprint,
            quant_score if quant_score is not None
            else (base_score if base_score is not None else None),
        )
    except Exception as exc:
        # 个性化文案失败不该拖垮整份报告，但**也不能悄悄吞掉**——
        # 之前这里裸 except 掩盖过一次 NameError。改成留下可查的痕迹。
        analysis.caveats.append(
            f"个性化收尾文案生成失败（{type(exc).__name__}: {exc}），已改用通用文案。"
        )
        personal_note = None
    analysis.personal_note = personal_note

    return ScoreResult(
        total=total,
        raw_total=raw,
        tier=tier,
        peer=peer_part,
        me=me_part,
        pair=pair_part,
        confidence=confidence,
        insufficient=insufficient,
        top_positive=top_positive,
        top_negative=top_negative,
        evidence=evidence,
        comfort_title=c_title,
        comfort_body=c_body,
        base_score=base_score,
        quant_score=quant_score,
        blend=blend,
        blend_note=blend_note,
    )


def explain(result: ScoreResult, analysis: Analysis) -> str:
    """生成纯文本的分账说明，让人能逐条追问「这分数怎么来的」。"""
    lines: list[str] = []
    label_of = {k: d.label for k, d in analysis.dimensions.items()}

    lines.append(f"总分 {result.total}（综合原始分 {result.raw_total:.1f}，"
                 f"样本收缩后 {result.total}）→ {result.tier.title}")
    if result.blend_note:
        lines.append(f"综合方式：{result.blend_note}")
    if result.insufficient:
        lines.append("⚠ 样本不足，结论已降级为「仅供参照」。")
    lines.append(f"可信度：{result.confidence.label}（{result.confidence.score:.2f}）")
    for r in result.confidence.reasons:
        lines.append(f"  - {r}")

    for part in (result.peer, result.me, result.pair):
        title = {"peer": f"TA 的投入度（{analysis.peer}）",
                 "me": f"你的投入度（{analysis.me}）",
                 "pair": "双向互动"}[part.name]
        lines.append("")
        if part.score is None:
            lines.append(f"【{title}】无可用维度，未计算。")
            continue
        lines.append(f"【{title}】{part.score:.1f} / 100")
        for k, w in sorted(part.used.items(), key=lambda kv: -kv[1]):
            dim_score = part.scores_used.get(k, 0.0)
            lines.append(
                f"  {label_of[k]:<12} 维度分 {dim_score * 100:5.1f} × 权重 {w * 100:4.1f}%"
                f" = 贡献 {part.contributions[k]:5.2f}"
            )
        if part.skipped:
            lines.append(f"  已剔除（无信息，权重已重新归一化）："
                         f"{'、'.join(label_of.get(k, k) for k in part.skipped)}")
    return "\n".join(lines)