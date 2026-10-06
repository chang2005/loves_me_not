"""单文件 HTML 可视化报告生成。

**约束**：输出必须是一个自包含的 ``.html``，双击即开——

* 不引用任何 CDN、字体、图片或外部 JS；
* 所有图表都是服务端算好后写进 HTML 的**内联 SVG**，不依赖浏览器端绘图库；
* 不发起任何网络请求（本模块也没有网络调用），符合「数据只在本地处理」的红线。

移动端优先：单列布局、``viewBox`` 自适应、字号用相对单位、长文本自动换行。
"""

from __future__ import annotations

import html
import json
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Iterable, Sequence

from .metrics import Analysis, Dimension, Evidence, PeriodStat
from . import scoring
from .scoring import ScoreResult
from . import __version__
from . import visuals as V

# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #

def esc(text: object) -> str:
    """HTML 转义（用户聊天内容必须转义，否则会把报告结构冲掉）。"""
    return html.escape(str(text if text is not None else ""), quote=True)


def _reveal(delay_ms: int = 0, *, count: float | None = None) -> str:
    """生成入场动效属性。

    ``data-reveal`` 由页面脚本用 IntersectionObserver 加上 ``.is-in``；
    ``--d`` 控制错峰延迟（卡片依次浮现的关键）；
    ``data-count`` 让元素内的数字从 0 滚到目标值。
    """
    attrs = ' data-reveal'
    if delay_ms:
        attrs += f' style="--d:{delay_ms}ms"'
    if count is not None:
        attrs += f' data-count="{count}"'
    return attrs


def _stagger(index: int, step: int = 70, cap: int = 700) -> int:
    """按序号算错峰延迟，并设上限——否则长列表最后几项要等很久才出现。"""
    return min(index * step, cap)


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


# --------------------------------------------------------------------------- #
# 脱敏：报告要能安全地发给别人看
# --------------------------------------------------------------------------- #

_REDACT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    # 手机号（中国大陆）
    (re.compile(r"(?<!\d)(?:\+?86[-\s]?)?1[3-9]\d{9}(?!\d)"), "1**********"),
    # 身份证
    (re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"), "[身份证已打码]"),
    # 银行卡 / 长数字串
    (re.compile(r"(?<!\d)\d{16,19}(?!\d)"), "[卡号已打码]"),
    # 邮箱
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[邮箱已打码]"),
    # 微信号 / QQ 号声明（微信号允许中文、字母、数字、下划线、减号）
    (re.compile(r"(?:微信|weixin|wechat|vx|VX|qq|QQ)\s*(?:号|ID|id)?\s*[:：]?\s*"
                r"[A-Za-z0-9_\-\u4e00-\u9fa5]{5,24}"), "[账号已打码]"),
    # 详细地址特征
    (re.compile(r"[\u4e00-\u9fa5]{2,8}(?:省|市|区|县)[\u4e00-\u9fa5]{0,12}(?:路|街|道|巷)\d*号?"),
     "[地址已打码]"),
    # 门牌号
    (re.compile(r"\d{1,4}\s*(?:栋|幢|单元|室|号楼)"), "[门牌已打码]"),
)


def redact(text: str) -> str:
    """对常见敏感串做打码（手机号、身份证、卡号、邮箱、账号、地址）。

    这是「我要把报告发给别人看」时的降低风险措施，不是匿名化保证——
    聊天内容本身仍然可能暴露身份，请自行判断。
    """
    if not text:
        return text
    out = text
    for pattern, repl in _REDACT_PATTERNS:
        out = pattern.sub(repl, out)
    return out


def _apply_redaction(analysis: Analysis, result: ScoreResult) -> None:
    """就地脱敏：报告里**所有**会展示原始文本的地方都要过一遍。

    这是一份清单而不是「顺手改几个地方」——漏掉任何一处，
    ``--redact`` 就会给出虚假的安全感。目前覆盖：

    * ``result.evidence``（支撑结论的原话）
    * 每个维度的 ``evidence``（维度自己的举证）
    * ``analysis.highlights``（最暖/最冷片段）
    * ``analysis.timeline_nodes[].detail``（关键节点里的原话）
    """
    for dim in analysis.dimensions.values():
        for ev in dim.evidence:
            ev.text = redact(ev.text)
    for ev in result.evidence:
        ev.text = redact(ev.text)
    for group in analysis.highlights.values():
        for ev in group:
            ev.text = redact(ev.text)
    for node in getattr(analysis, "timeline_nodes", []) or []:
        detail = getattr(node, "detail", None)
        if isinstance(detail, str):
            node.detail = redact(detail)


# --------------------------------------------------------------------------- #
# 调色板：直接复用 :mod:`loves_me_not.visuals` 的定义，保证图表与页面同色
# --------------------------------------------------------------------------- #

PALETTE = V.PALETTE


# --------------------------------------------------------------------------- #
# SVG：总分仪表盘
# --------------------------------------------------------------------------- #

#: 环形仪表盘的几何参数（唯一来源，避免各处硬编码不一致）
_RING = {
    "size": 260.0,
    "stroke": 18.0,
    "gap": 26.0,        # 半径留白，保证描边不越界
}


def _polar(cx: float, cy: float, r: float, angle_deg: float) -> tuple[float, float]:
    """极坐标转直角坐标；角度以「12 点方向为 0、顺时针为正」计。"""
    rad = math.radians(angle_deg - 90.0)
    return cx + r * math.cos(rad), cy + r * math.sin(rad)


def render_gauge(score: int, tier_color: str, label: str) -> str:
    """总分仪表盘：**环形进度条**（用 ``stroke-dasharray`` 画），不是弧线。

    为什么换掉原来的弧线方案
    ------------------------
    旧实现用 ``path A`` 画一段 250° 的弧，还配了一根指针和一圈刻度。
    它有三个先天缺陷：

    1. 弧线端点、指针角度、刻度位置由**三套独立的三角度量**算出，
       任何一处口径不一致（例如 sweep-flag 取反）就会立刻「乱」；
    2. 刻度与数字标签被推到半径 +28 的位置，而 ``viewBox`` 没有留够余量，
       窄屏缩放后标签会溢出或互相压住；
    3. 250° 的弧本身不对称，在窄栏里看起来就是歪的。

    环形进度条是纯几何：一个整圆 + ``dasharray`` 控制走多少。
    没有端点计算、没有指针、没有溢出——缩放后永远不变形。
    ``rotate(-90)`` 让进度从 12 点方向开始。
    """
    size = _RING["size"]
    stroke = _RING["stroke"]
    r = size / 2 - _RING["gap"]
    c = 2 * math.pi * r
    frac = max(0.0, min(1.0, score / 100.0))
    filled = c * frac

    return f"""
<svg viewBox="0 0 {size:.0f} {size:.0f}" class="gauge" role="img"
     aria-label="情感投入指数 {score} 分，满分 100 分">
  <defs>
    <linearGradient id="gaugeGrad" x1="0" y1="0" x2="1" y2="1">
      <stop offset="0%" stop-color="{tier_color}" stop-opacity="0.78"/>
      <stop offset="100%" stop-color="{tier_color}"/>
    </linearGradient>
  </defs>

  <!-- 底环 -->
  <circle cx="{size / 2}" cy="{size / 2}" r="{r:.1f}" fill="none"
          stroke="{PALETTE['line']}" stroke-width="{stroke}"/>
  <!-- 进度环：从 12 点方向顺时针 -->
  <circle cx="{size / 2}" cy="{size / 2}" r="{r:.1f}" fill="none"
          stroke="url(#gaugeGrad)" stroke-width="{stroke}" stroke-linecap="round"
          stroke-dasharray="{filled:.2f} {c - filled:.2f}"
          transform="rotate(-90 {size / 2} {size / 2})"/>

  <text x="{size / 2}" y="{size / 2 - 2}" text-anchor="middle"
        dominant-baseline="middle" class="gauge-num"
        fill="{PALETTE['ink']}" data-count="{score}">{score}</text>
  <text x="{size / 2}" y="{size / 2 + 40}" text-anchor="middle"
        dominant-baseline="middle" class="gauge-denom"
        fill="{PALETTE['ink_faint']}">/ 100</text>
</svg>
<p class="gauge-label" style="color:{tier_color}">{esc(label)}</p>"""


def render_dual_bars(result: ScoreResult, analysis: Analysis) -> str:
    """双方投入度对比条。"""
    rows = []
    for name, part, color, who in (
        ("peer", result.peer, PALETTE["peer"], analysis.peer),
        ("me", result.me, PALETTE["me"], analysis.me),
    ):
        val = part.score
        if val is None:
            rows.append(f"""
      <div class="bar-row">
        <div class="bar-head"><span class="who" style="color:{color}">{esc(who)}</span>
          <span class="bar-val faint">样本不足</span></div>
        <div class="bar-track"><div class="bar-fill" style="width:0%;background:{color}"></div></div>
      </div>""")
            continue
        rows.append(f"""
      <div class="bar-row">
        <div class="bar-head"><span class="who" style="color:{color}">{esc(who)}</span>
          <span class="bar-val" style="color:{color}">{val:.0f}</span></div>
        <div class="bar-track"><div class="bar-fill" style="width:{max(1.0, val):.1f}%;background:{color}"></div></div>
      </div>""")

    pair = ""
    if result.pair.score is not None:
        pair = f'<p class="tiny faint">双向互动综合：{result.pair.score:.0f} / 100</p>'
    return f'<div class="bars">{"".join(rows)}{pair}</div>'


# --------------------------------------------------------------------------- #
# SVG：雷达图
# --------------------------------------------------------------------------- #

def render_radar(dims: Sequence[Dimension], labels_me: str, labels_peer: str) -> str:
    """八维雷达图。

    无信息的维度画成「虚线空轴」并标注 N/A——**不画成 0**，避免视觉上冤枉人。
    画布刻意留宽（520），并给每个轴配短名（``Dimension.short``），
    否则中文长标签会被裁掉。
    """
    cx, cy, r = 260.0, 200.0, 118.0
    n = len(dims)
    if n == 0:
        return '<p class="faint">没有可用于绘图的维度。</p>'

    def point(i: int, value: float) -> tuple[float, float]:
        angle = 360.0 * i / n
        return _polar(cx, cy, r * max(0.0, min(1.0, value)), angle)

    # 网格
    grid = []
    for ring in (0.25, 0.5, 0.75, 1.0):
        pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in (point(i, ring) for i in range(n)))
        grid.append(f'<polygon points="{pts}" fill="none" stroke="{PALETTE["line"]}" stroke-width="1"/>')
    for i in range(n):
        x, y = point(i, 1.0)
        grid.append(f'<line x1="{cx}" y1="{cy}" x2="{x:.1f}" y2="{y:.1f}" '
                    f'stroke="{PALETTE["line"]}" stroke-width="1"/>')

    def polygon(scores: Sequence[float | None], stroke: str, fill: str) -> str:
        usable = [(i, s) for i, s in enumerate(scores) if s is not None]
        if len(usable) < 3:
            return ""
        pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in (point(i, s) for i, s in usable))  # type: ignore[arg-type]
        dots = "".join(
            f'<circle cx="{point(i, s)[0]:.1f}" cy="{point(i, s)[1]:.1f}" r="3" fill="{stroke}"/>'
            for i, s in usable  # type: ignore[arg-type]
        )
        return (f'<polygon points="{pts}" fill="{fill}" stroke="{stroke}" '
                f'stroke-width="2" stroke-linejoin="round"/>{dots}')

    peer_scores = [d.score_peer for d in dims]
    me_scores = [d.score_me for d in dims]

    # 轴标签：用短名 + 稍大的标签半径，确保不被画布裁切
    axis_labels = []
    for i, d in enumerate(dims):
        lx, ly = _polar(cx, cy, r + 34, 360.0 * i / n)
        anchor = "middle"
        if lx > cx + 12:
            anchor = "start"
        elif lx < cx - 12:
            anchor = "end"
        text = d.short or d.label
        suffix = "" if d.available else " N/A"
        color = PALETTE["ink_soft"] if d.available else PALETTE["ink_faint"]
        axis_labels.append(
            f'<text x="{lx:.1f}" y="{ly:.1f}" text-anchor="{anchor}" '
            f'dominant-baseline="middle" font-size="12" fill="{color}">'
            f'{esc(text + suffix)}</text>'
        )

    return f"""
<svg viewBox="0 0 520 400" class="radar" role="img" aria-label="八维雷达图">
  {''.join(grid)}
  {polygon(me_scores, PALETTE['me'], PALETTE['me'] + '33')}
  {polygon(peer_scores, PALETTE['peer'], PALETTE['peer'] + '3d')}
  {''.join(axis_labels)}
</svg>
<div class="legend">
  <span><i style="background:{PALETTE['peer']}"></i>{esc(labels_peer)}（TA）</span>
  <span><i style="background:{PALETTE['me']}"></i>{esc(labels_me)}（我）</span>
  <span class="faint">标注 N/A = 该维度样本不足，不参与打分</span>
</div>"""


# --------------------------------------------------------------------------- #
# SVG：趋势折线图
# --------------------------------------------------------------------------- #

def _line_chart(
    periods: Sequence[PeriodStat],
    title: str,
    subtitle: str,
    series: Sequence[tuple[str, str, list[float | None]]],
    *,
    y_min: float | None = None,
    y_max: float | None = None,
    y_fmt=lambda v: f"{v:.0f}",
    height: int = 190,
) -> str:
    if not periods or not series:
        return f'<div class="chart-card"><h4>{esc(title)}</h4><p class="faint tiny">数据不足</p></div>'

    width = 660
    pad_l, pad_r, pad_t, pad_b = 42, 14, 18, 34
    plot_w = width - pad_l - pad_r
    plot_h = height - pad_t - pad_b

    values = [v for _n, _c, vals in series for v in vals if v is not None]
    if not values:
        return f'<div class="chart-card"><h4>{esc(title)}</h4><p class="faint tiny">数据不足</p></div>'
    lo = y_min if y_min is not None else min(values)
    hi = y_max if y_max is not None else max(values)
    if hi <= lo:
        hi = lo + 1.0

    n = len(periods)

    def x_at(i: int) -> float:
        if n == 1:
            return pad_l + plot_w / 2
        return pad_l + plot_w * i / (n - 1)

    def y_at(v: float) -> float:
        return pad_t + plot_h * (1.0 - (v - lo) / (hi - lo))

    # 网格与 y 轴刻度
    grid = []
    for k in range(5):
        v = lo + (hi - lo) * k / 4
        y = y_at(v)
        grid.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{width - pad_r}" y2="{y:.1f}" '
                    f'stroke="{PALETTE["line"]}" stroke-width="1"/>')
        grid.append(f'<text x="{pad_l - 6}" y="{y:.1f}" text-anchor="end" '
                    f'dominant-baseline="middle" font-size="9" fill="{PALETTE["ink_faint"]}">'
                    f'{esc(y_fmt(v))}</text>')

    # x 轴标签（最多显示 8 个，避免挤在一起）
    step = max(1, math.ceil(n / 8))
    xlabels = []
    for i, p in enumerate(periods):
        if i % step and i != n - 1:
            continue
        xlabels.append(f'<text x="{x_at(i):.1f}" y="{height - pad_b + 16}" text-anchor="middle" '
                       f'font-size="9" fill="{PALETTE["ink_faint"]}">{esc(p.label)}</text>')

    paths = []
    for name, color, vals in series:
        segs: list[str] = []
        cur: list[str] = []
        dots: list[str] = []
        for i, v in enumerate(vals):
            if v is None:
                if len(cur) > 1:
                    segs.append("M " + " L ".join(cur))
                cur = []
                continue
            x, y = x_at(i), y_at(v)
            cur.append(f"{x:.1f} {y:.1f}")
            dots.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="2.8" fill="{color}"/>')
        if len(cur) > 1:
            segs.append("M " + " L ".join(cur))
        paths.append(
            "".join(f'<path d="{d}" fill="none" stroke="{color}" stroke-width="2.2" '
                    f'stroke-linejoin="round" stroke-linecap="round"/>' for d in segs) + "".join(dots)
        )

    legend = "".join(
        f'<span><i style="background:{color}"></i>{esc(name)}</span>' for name, color, _v in series
    )

    return f"""
<div class="chart-card" data-reveal>
  <h4>{esc(title)}</h4>
  <p class="tiny faint">{esc(subtitle)}</p>
  <svg viewBox="0 0 {width} {height}" class="linechart" role="img" aria-label="{esc(title)}">
    {''.join(grid)}
    {''.join(paths)}
    {''.join(xlabels)}
  </svg>
  <div class="legend">{legend}</div>
</div>"""


def render_trends(analysis: Analysis, periods: Sequence[PeriodStat]) -> str:
    if not periods:
        return '<p class="faint">没有可用于绘制趋势的时间分段。</p>'

    me, peer = analysis.me, analysis.peer
    charts = [
        _line_chart(
            periods,
            "消息量走势",
            "每一段的双方消息条数",
            [
                (peer, PALETTE["peer"], [float(p.peer_count) for p in periods]),
                (me, PALETTE["me"], [float(p.me_count) for p in periods]),
            ],
        ),
        _line_chart(
            periods,
            "谁在主动开口",
            "每一段里由谁开启的对话轮次",
            [
                (f"{peer} 发起", PALETTE["peer"], [float(p.initiative_peer) for p in periods]),
                (f"{me} 发起", PALETTE["me"], [float(p.initiative_me) for p in periods]),
            ],
        ),
        _line_chart(
            periods,
            "情绪倾向走势",
            "基于本地情感词典的均分（-1 冷 ~ +1 暖）",
            [
                (peer, PALETTE["peer"], [p.sentiment_peer for p in periods]),
                (me, PALETTE["me"], [p.sentiment_me for p in periods]),
            ],
            y_min=-1.0,
            y_max=1.0,
            y_fmt=lambda v: f"{v:+.1f}",
        ),
        _line_chart(
            periods,
            "回复速度走势",
            "每一段的中位回复间隔（分钟，越低越快）",
            [
                (peer, PALETTE["peer"],
                 [(p.reply_median_peer_sec / 60.0) if p.reply_median_peer_sec is not None else None
                  for p in periods]),
                (me, PALETTE["me"],
                 [(p.reply_median_me_sec / 60.0) if p.reply_median_me_sec is not None else None
                  for p in periods]),
            ],
            y_min=0.0,
            y_fmt=lambda v: f"{v:.0f}",
        ),
    ]
    return f'<div class="chart-grid">{"".join(charts)}</div>'


# --------------------------------------------------------------------------- #
# 各个区块
# --------------------------------------------------------------------------- #

def render_verdict(result: ScoreResult, analysis: Analysis, *, assumed: bool = False) -> str:
    """第一屏：大字直白给结论。"""
    banner = ""
    if result.insufficient:
        banner = (
            '<div class="banner">'
            "<strong>⚠ 样本不足</strong>"
            f"<span>能读到的有效消息只有 <b>{analysis.total_real}</b> 条"
            f"（{analysis.active_days} 天有对话，跨度 {analysis.days_span} 天）。"
            "下面的分数已向中间值收缩，<b>不足以作为结论</b>，请只当作一次自我观察的起点。</span>"
            "</div>"
        )
    if assumed:
        banner += (
            '<div class="banner" style="background:#fdf1f3;border-color:#ecc9d3;color:#8a5566">'
            "<strong>⚠ 请先确认「我」是谁</strong>"
            f"<span>你没有指定 <code>--me</code>，报告按文件里说话人出现的先后顺序，"
            f"把 <b>{esc(analysis.me)}</b> 当成了「我」、<b>{esc(analysis.peer)}</b> 当成了「TA」。"
            "如果搞反了，整份报告的结论会完全颠倒——"
            "请用 <code>--me \"你的昵称\" --peer \"TA的昵称\"</code> 重新生成一次。</span>"
            "</div>"
        )

    conf_reasons = "".join(f"<li>{esc(r)}</li>" for r in result.confidence.reasons)
    group_note = ""
    if analysis.is_group:
        group_note = (
            f'<p class="tiny faint">这份记录里有 {len(analysis.speakers)} 个说话人（群聊），'
            f"只分析了 <b>{esc(analysis.me)}</b> 与 <b>{esc(analysis.peer)}</b>。"
            "其他人说的话在这个报告里完全没算。</p>"
        )

    return f"""
<section class="verdict">
  {banner}
  <p class="kicker">情感投入指数</p>
  <div class="verdict-grid">
    <div class="gauge-wrap">{render_gauge(result.total, result.tier.color, result.tier.title)}</div>
    <div class="verdict-text">
      <h1 style="color:{result.tier.color}">{esc(result.tier.title)}</h1>
      <p class="one-liner">{esc(result.tier.one_liner)}</p>
      <p class="tiny faint">
        这个分数是<b>综合分</b>：八维模型
        {f"{result.base_score:.0f} 分" if result.base_score is not None else "样本不足"} × 60%
        ＋ 五大量化指标
        {f"{result.quant_score:.0f} 分" if result.quant_score is not None else "样本不足"} × 40%，
        再按样本量收缩。两部分的完整分账见下方「分数是怎么算出来的」。
      </p>
      <p class="tiny faint">
        判断的是 <b>{esc(analysis.peer)}</b> 对 <b>{esc(analysis.me)}</b> 的投入度·
        共解析 {analysis.total_real} 条有效消息·
        区间 {esc(_fmt_span(analysis))}·
        {esc(result.confidence.label)}
      </p>
      <details class="conf">
        <summary>这个分数有多可信？</summary>
        <ul>{conf_reasons or '<li>样本量充足</li>'}</ul>
        <p class="tiny faint">
          原始分 {result.raw_total:.1f}，按样本量收缩后为 {result.total} 分。
          原话证据只有 {len(result.evidence)} 条。
        </p>
      </details>
      {group_note}
    </div>
  </div>
  <div class="advice" style="border-color:{result.tier.color}55">
    <p>{esc(result.tier.advice)}</p>
  </div>
  <div class="disclaimer-top">本报告基于统计学规律生成，仅供娱乐与自我反思，不代表任何一方的真实情感，重大情感决策请咨询线下专业人士。</div>
</section>"""


def _fmt_span(analysis: Analysis) -> str:
    if not analysis.first_at or not analysis.last_at:
        return "（无时间信息）"
    return f"{analysis.first_at:%Y-%m-%d} → {analysis.last_at:%Y-%m-%d}"


def render_dimensions(analysis: Analysis) -> str:
    cards = []
    for i, d in enumerate(analysis.dimensions.values()):
        chips = []
        for s in d.subs:
            cls = "chip" if s.score is not None else "chip chip-na"
            note = f'<span class="chip-note">{esc(s.note)}</span>' if s.note else ""
            chips.append(
                f'<div class="{cls}"><span class="chip-label">{esc(s.label)}</span>'
                f'<span class="chip-val">{esc(s.display)}</span>{note}</div>'
            )
        bars = ""
        for who, val, color in ((analysis.peer, d.score_peer, PALETTE["peer"]),
                                (analysis.me, d.score_me, PALETTE["me"])):
            if val is None:
                bars += (f'<div class="mini"><span>{esc(who)}</span>'
                         f'<div class="mini-track"><div class="mini-fill" style="width:0"></div></div>'
                         f'<em class="faint">N/A</em></div>')
            else:
                bars += (f'<div class="mini"><span>{esc(who)}</span>'
                         f'<div class="mini-track"><div class="mini-fill" '
                         f'style="width:{max(1.0, val * 100):.1f}%;background:{color}"></div></div>'
                         f'<em style="color:{color}">{val * 100:.0f}</em></div>')
        na_note = ""
        if not d.available:
            na_note = '<p class="tiny" style="color:%s">这一项样本不足，<b>没有参与打分</b>。</p>' % PALETTE["warn"]
        cards.append(f"""
  <article class="dim-card{'' if d.available else ' dim-na'}"{_reveal(_stagger(i, 60))}>
    <h3>{esc(d.label)}</h3>
    {na_note}
    <p class="dim-summary">{esc(d.summary)}</p>
    <div class="mini-bars">{bars}</div>
    <div class="chips">{''.join(chips)}</div>
  </article>""")
    return f'<div class="dim-grid">{"".join(cards)}</div>'


def render_cards(analysis: Analysis) -> str:
    out = []
    for i, (title, value, sub) in enumerate(analysis.cards):
        out.append(
            f'<div class="stat"{_reveal(_stagger(i))}>'
            f'<span class="stat-title">{esc(title)}</span>'
            f'<span class="stat-value">{esc(value)}</span>'
            f'<span class="stat-sub">{esc(sub)}</span></div>'
        )
    return f'<div class="stats">{"".join(out)}</div>'


def render_evidence(result: ScoreResult, analysis: Analysis) -> str:
    if not result.evidence:
        return ('<p class="faint">这份记录里没有挑出足够清晰的原话片段。'
                "结论只基于统计量，请谨慎参考。</p>")
    items = []
    for i, ev in enumerate(result.evidence):
        color = PALETTE["peer"] if ev.speaker == analysis.peer else PALETTE["me"]
        when = ev.when()
        items.append(f"""
    <li class="ev"{_reveal(_stagger(i, 55))}>
      <div class="ev-head">
        <span class="ev-who" style="color:{color}">{esc(ev.speaker)}</span>
        <span class="ev-when">{esc(when)}</span>
      </div>
      <blockquote>{esc(ev.text)}</blockquote>
      <span class="ev-reason">{esc(ev.reason)}</span>
    </li>""")
    return f'<ul class="ev-list">{"".join(items)}</ul>'


def render_winners(result: ScoreResult) -> str:
    def block(title: str, rows: Sequence[tuple[str, float, float]], color: str,
              empty: str, delay: int) -> str:
        if not rows:
            return (f'<div class="win"{_reveal(delay)}><h4>{esc(title)}</h4>'
                    f'<p class="faint tiny">{esc(empty)}</p></div>')
        lis = "".join(
            f'<li><span>{esc(label)}</span>'
            f'<span class="win-score" style="color:{color}">{score * 100:.0f}</span>'
            f'<span class="tiny faint">权重 {_pct(w)}</span></li>'
            for label, score, w in rows
        )
        return f'<div class="win"{_reveal(delay)}><h4>{esc(title)}</h4><ul>{lis}</ul></div>'

    return (
        '<div class="wins">'
        + block("主要加分项", result.top_positive, PALETTE["good"],
                "没有明显高于基准的维度。", 0)
        + block("主要拖后腿", result.top_negative, PALETTE["warn"],
                "没有明显低于基准的维度。", 90)
        + "</div>"
    )


def render_breakdown(result: ScoreResult, analysis: Analysis) -> str:
    """把「分数怎么来的」摊开给人看。"""
    labels = {k: d.label for k, d in analysis.dimensions.items()}

    def table(part, title: str, delay: int = 0) -> str:
        if part.score is None:
            return (f'<div class="bd"{_reveal(delay)}><h4>{esc(title)}</h4>'
                    f'<p class="faint tiny">无可用维度。</p></div>')
        rows = []
        for k, w in sorted(part.used.items(), key=lambda kv: -kv[1]):
            # 用打分时真正采用的分数，而不是事后反推——
            # 双向视角会在只有一方可得时借用那一方的分数。
            dim_score = part.scores_used.get(k)
            if dim_score is None:
                dim_score = 0.0
            rows.append(
                f"<tr><td>{esc(labels[k])}</td>"
                f"<td class='num'>{dim_score * 100:.0f}</td>"
                f"<td class='num'>{_pct(w)}</td>"
                f"<td class='num'>{part.contributions[k]:.1f}</td></tr>"
            )
        skipped = ""
        if part.skipped:
            names = "、".join(labels.get(k, k) for k in part.skipped)
            skipped = (f'<p class="tiny faint">已剔除（无信息，权重已重新归一化）：{esc(names)}</p>')
        return f"""
    <div class="bd"{_reveal(delay)}>
      <h4>{esc(title)} <span class="bd-total">{part.score:.1f}</span></h4>
      <table>
        <thead><tr><th>维度</th><th class="num">维度分</th><th class="num">权重</th><th class="num">贡献</th></tr></thead>
        <tbody>{''.join(rows)}</tbody>
      </table>
      {skipped}
    </div>"""

    return (
        '<div class="bds">'
        + table(result.peer, f"{analysis.peer} 的投入度（决定上面的总分）", 0)
        + table(result.me, f"{analysis.me} 的投入度", 80)
        + table(result.pair, "双向互动综合", 160)
        + "</div>"
    )


def render_caveats(analysis: Analysis, conv_report=None) -> str:
    items = list(analysis.caveats)
    if conv_report is not None:
        for w in getattr(conv_report, "warnings", [])[:6]:
            items.append(w)
    if not items:
        items = ["没有发现需要特别说明的数据问题。"]
    lis = "".join(f"<li>{esc(i)}</li>" for i in items)
    return f'<ul class="caveats">{lis}</ul>'


def render_comfort(result: ScoreResult, analysis: Analysis | None = None) -> str:
    """结尾：分档通用文案 + （如果有）从真实数据里长出来的专属文案。"""
    paragraphs = "".join(
        f"<p>{esc(p.strip())}</p>" for p in result.comfort_body.split("\n\n") if p.strip()
    )

    personal = getattr(analysis, "personal_note", None) if analysis is not None else None
    personal_html = ""
    if personal is not None:
        tone_label = "说给你听" if personal.tone == "warm" else "也想提醒你"
        based = "、".join(personal.based_on)
        personal_html = f"""
    <div class="insight">
      <div class="insight-head">
        <span class="insight-badge">{esc(tone_label)}</span>
        <span class="insight-from tiny">只属于这份记录的一句话</span>
      </div>
      <p class="insight-lead">{esc(personal.headline)}</p>
      <p class="insight-support tiny">{esc(personal.support)}</p>
      <p class="insight-closing">{esc(personal.closing)}</p>
      {f'<p class="insight-based tiny">依据：{esc(based)}</p>' if based else ''}
    </div>"""

    return f"""
<section class="comfort" id="closing">
  <div class="comfort-inner">
    <p class="kicker">写在这里的话</p>
    <h2>{esc(result.comfort_title)}</h2>
    {paragraphs}
    {personal_html}
  </div>
</section>"""


# --------------------------------------------------------------------------- #
# 新增区块：量化指标 / 足迹 / 热力图 / 词云 / 画像 / 时间线
# --------------------------------------------------------------------------- #

def render_quantifiers(analysis: Analysis) -> str:
    """五个量化指标 + 综合情感倾向，每项都写明计算口径与数据来源。"""
    quant = analysis.quant
    if quant is None or not getattr(quant, "metrics", None):
        return '<p class="faint">没有可用的量化指标。</p>'

    total = quant.raw_total
    if total is None:
        headline = (
            '<div class="quant-head quant-na">'
            "<strong>综合情感倾向：无法计算</strong>"
            "<span>五个量化指标都因为样本不足被剔除。"
            "这不是「TA 不爱你」，而是这份记录还不够回答这个问题。</span>"
            "</div>"
        )
    else:
        color = scoring.tier_for(int(round(total))).color
        headline = (
            f'<div class="quant-head" style="border-color:{color}55">'
            f'<strong style="color:{color}">综合情感倾向：{total:.0f} / 100</strong>'
            f"<span>{esc(quant.summary)}</span>"
            "</div>"
        )

    rows = []
    for key, m in quant.metrics.items():
        weight = quant.weights.get(key)
        chip = ""
        if weight is not None:
            chip = (f'<span class="q-weight">权重 {weight * 100:.0f}% · '
                    f'贡献 {quant.contributions.get(key, 0):.1f}</span>')
        elif key in quant.skipped:
            chip = '<span class="q-weight q-skip">样本不足 · 未参与打分</span>'
        bar = ""
        if m.score is not None:
            hue = V.PALETTE["peer"]
            bar = (f'<div class="q-bar"><div style="width:{max(2.0, m.score * 100):.0f}%;'
                   f'background:{hue}"></div></div>')
        rows.append(f"""
    <div class="q-item" data-reveal>
      <div class="q-line">
        <span class="q-label">{esc(m.label)}</span>
        <span class="q-value{' faint' if m.score is None else ''}">{esc(m.display)}</span>
        {chip}
      </div>
      {bar}
      <p class="tiny faint q-formula">
        <b>口径</b>：{esc(m.formula)}<br>
        <b>来源</b>：{esc(m.source)}
        {f'<br><b>补充</b>：{esc(m.note)}' if m.note else ''}
      </p>
    </div>""")

    return f"""
<div class="quant-wrap">
  {headline}
  <div class="quant-list">{''.join(rows)}</div>
  <div class="quant-legend tiny faint">
    这五个指标只看 <b>{esc(analysis.peer)}</b> 的行为信号。「打破僵局」与「最后发言」
    没有绝对好坏——双方各半最健康，全由一方承担会降低得分。
    样本不足的指标会被剔除，权重重新归一化，<b>不会被当成 0 分</b>。
  </div>
</div>"""


def render_footprint(analysis: Analysis) -> str:
    """聊天足迹：跨度、聊得最好的时段、最好/最差的月份、最长一次、最长沉默。"""
    fp = analysis.footprint
    if fp is None or fp.first_at is None:
        return '<p class="faint">这份记录里没有可用的时间信息，无法生成聊天足迹。</p>'

    me, peer = analysis.me, analysis.peer

    def stat(title: str, value: str, sub: str = "") -> str:
        sub_html = f'<span class="stat-sub">{esc(sub)}</span>' if sub else ""
        return (f'<div class="stat"><span class="stat-title">{esc(title)}</span>'
                f'<span class="stat-value">{esc(value)}</span>{sub_html}</div>')

    stats = [
        stat("聊天跨度（首条消息起）", f"{fp.span_to_now_days} 天",
             f"从 {fp.first_at:%Y-%m-%d} 到今天"),
        stat("首尾消息间隔", f"{fp.span_days} 天",
             f"{fp.first_at:%Y-%m-%d} → {fp.last_at:%Y-%m-%d}" if fp.last_at else ""),
        stat("有对话的天数", f"{fp.active_days} 天",
             f"平均每天 {fp.avg_messages_per_active_day:.1f} 条消息"),
        stat("对话段数", f"{fp.total_sessions} 段",
             f"平均每段 {fp.avg_session_minutes:.0f} 分钟"),
        stat("累计聊天时长", f"{fp.total_chat_minutes / 60:.1f} 小时",
             "各段对话时长之和"),
    ]

    if fp.longest_session:
        s = fp.longest_session
        stats.append(stat("最长的一次聊天", _fmt_minutes(s.duration_minutes),
                          f"{s.start:%Y-%m-%d %H:%M} 起 · {s.count} 条消息"))
    if fp.longest_silence:
        g = fp.longest_silence
        stats.append(stat("最长的一次沉默", _fmt_minutes(g.length.total_seconds() / 60),
                          f"{g.start:%Y-%m-%d} → {g.end:%Y-%m-%d} · "
                          f"由{esc(g.broken_by or '未知')}打破"))

    best_hours = fp.best_hours
    best_hour_txt = (f"{best_hours[0].label}–{best_hours[-1].hour + 1:02d}:00"
                     if best_hours else "—")
    if best_hours:
        stats.append(stat("聊得最好的时段", best_hour_txt,
                          f"这三小时共 {sum(h.count for h in best_hours)} 条消息"))

    if fp.best_month:
        stats.append(stat("聊得最多的月份", fp.best_month.pretty,
                          f"{fp.best_month.count} 条 · {fp.best_month.active_days} 天有对话"))
    if fp.warmest_month:
        wm = fp.warmest_month
        stats.append(stat("互动最均衡的月份", wm.pretty,
                          f"TA 占 {wm.peer_share * 100:.0f}% · 消息量 {wm.count} 条"))
    if fp.quietest_month:
        stats.append(stat("最安静的月份", fp.quietest_month.pretty,
                          f"只有 {fp.quietest_month.count} 条消息"))
    if fp.best_weekday is not None:
        from .timeline import WEEKDAY_NAMES
        stats.append(stat("聊得最多的星期", WEEKDAY_NAMES[fp.best_weekday],
                          f"{fp.weekdays[fp.best_weekday]} 条消息"))

    hour_chart = V.render_hour_bars(fp.hours, me, peer)
    month_chart = V.render_month_bars(fp.months, me, peer)

    disclaimer = (
        '<p class="tiny faint">「互动最均衡的月份」是<b>代理指标</b>：'
        "取消息量不低于最高月 25% 的月份里、TA 发言占比最高者。"
        "它衡量的是「双方都在说话的均衡度」，<b>不是感情好坏的真相</b>——"
        "聊得多、聊得均衡，也可能只是那段日子比较闲。</p>"
    )

    return f"""
<div class="stats stats-wide">{''.join(stats)}</div>

<div class="chart-grid" style="margin-top:12px">
  <div class="chart-card">
    <h4>一天里什么时候在聊</h4>
    <p class="tiny faint">按小时统计消息条数，堆叠展示双方贡献</p>
    {hour_chart}
  </div>
  <div class="chart-card">
    <h4>哪个月聊得最多</h4>
    <p class="tiny faint">按月统计消息条数，已标出最多与最少的月份</p>
    {month_chart}
  </div>
</div>
{disclaimer}"""


def _fmt_minutes(minutes: float) -> str:
    if minutes < 1:
        return f"{minutes * 60:.0f} 秒"
    if minutes < 60:
        return f"{minutes:.0f} 分钟"
    if minutes < 1440:
        return f"{minutes / 60:.1f} 小时"
    return f"{minutes / 1440:.1f} 天"


def render_heatmap_section(analysis: Analysis) -> str:
    fp = analysis.footprint
    if fp is None:
        return '<p class="faint">没有可用于绘制热力图的数据。</p>'
    return V.render_heatmap(fp)


def render_topics(analysis: Analysis) -> str:
    return V.render_wordcloud(analysis.topics, analysis.me, analysis.peer,
                              analysis.total_real)


def render_persona_section(analysis: Analysis) -> str:
    return V.render_persona(analysis.persona, analysis.peer)


_TAG_COLOR = {
    "first": V.PALETTE["good"],
    "last": V.PALETTE["ink_soft"],
    "longest_session": V.PALETTE["accent"],
    "longest_silence": V.PALETTE["me"],
    "busiest_day": V.PALETTE["peer"],
    "longest_day": V.PALETTE["accent"],
    "busiest_month": V.PALETTE["peer"],
    "quietest_month": V.PALETTE["ink_faint"],
    "warmest_day": V.PALETTE["good"],
    "coldest_day": V.PALETTE["me"],
    "turning": V.PALETTE["warn"],
}


def render_timeline(analysis: Analysis) -> str:
    """关键节点时间线。"""
    nodes = analysis.timeline_nodes
    if not nodes:
        return '<p class="faint">这份记录里没有挑出足够的关键节点。</p>'

    items = []
    for i, n in enumerate(nodes):
        color = _TAG_COLOR.get(n.kind, V.PALETTE["ink_soft"])
        when = f"{n.when:%Y-%m-%d}" if n.when else "—"
        if n.when and n.kind in ("longest_session",):
            when = f"{n.when:%Y-%m-%d %H:%M}"
        items.append(f"""
    <li class="tl-node" title="{esc(n.title)}">
      <span class="tl-dot" style="background:{color}"></span>
      <div class="tl-body" data-reveal style="--d:{_stagger(i, 55)}">
        <div class="tl-head">
          <span class="tl-title" style="color:{color}">{esc(n.title)}</span>
          <span class="tl-when">{esc(when)}</span>
        </div>
        <p class="tl-detail">{esc(n.detail)}</p>
      </div>
    </li>""")
    return f'<ol class="timeline" data-timeline>{"".join(items)}</ol>'


# --------------------------------------------------------------------------- #
# 样式
# --------------------------------------------------------------------------- #

def _css() -> str:
    """整页样式。

    配色见 :data:`PALETTE`；样式集中在这里，方便整体调色。
    """
    p = PALETTE
    return f""":root {{
  --bg: {p['bg']};
  --surface: {p['surface']};
  --surface-alt: {p['surface_alt']};
  --ink: {p['ink']};
  --ink-soft: {p['ink_soft']};
  --ink-faint: {p['ink_faint']};
  --line: {p['line']};
  --line-soft: {p['line_soft']};
  --peer: {p['peer']};
  --peer-soft: {p['peer_soft']};
  --me: {p['me']};
  --me-soft: {p['me_soft']};
  --accent: {p['accent']};
  --accent-soft: {p['accent_soft']};
  --good: {p['good']};
  --warn: {p['warn']};
  --nav-bg: {p['nav_bg']};
  --nav-ink: {p['nav_ink']};
  --radius: 16px;
  --radius-sm: 10px;
  --shadow: 0 1px 2px rgba(42, 39, 49, .04), 0 8px 24px -12px rgba(42, 39, 49, .14);
  --shadow-nav: 0 10px 30px -12px rgba(34, 31, 40, .5);
  --nav-w: 232px;
  --fs-base: 15px;
}}

* {{ box-sizing: border-box; }}
html {{ -webkit-text-size-adjust: 100%; scroll-behavior: smooth; }}
@media (prefers-reduced-motion: reduce) {{
  html {{ scroll-behavior: auto; }}
  * {{ transition: none !important; animation: none !important; }}
}}
body {{
  margin: 0;
  background: var(--bg);
  color: var(--ink);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
               "Hiragino Sans GB", "Microsoft YaHei", "Noto Sans SC", sans-serif;
  font-size: var(--fs-base);
  line-height: 1.72;
  -webkit-font-smoothing: antialiased;
  text-rendering: optimizeLegibility;
}}
h1, h2, h3, h4 {{ line-height: 1.32; margin: 0 0 .6em; font-weight: 650; letter-spacing: -.01em; }}
p {{ margin: 0 0 .85em; }}
img, svg {{ max-width: 100%; }}
.tiny {{ font-size: .775rem; line-height: 1.65; }}
.faint {{ color: var(--ink-faint); }}
.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
.kicker {{
  font-size: .68rem; letter-spacing: .18em; text-transform: uppercase;
  color: var(--ink-faint); margin: 0 0 .4em; font-weight: 600;
}}

/* ===================== 双栏骨架 ===================== */
.layout {{ display: block; }}

.nav {{
  position: sticky; top: 0; z-index: 60;
  background: color-mix(in srgb, var(--bg) 88%, transparent);
  backdrop-filter: saturate(140%) blur(10px);
  -webkit-backdrop-filter: saturate(140%) blur(10px);
  border-bottom: 1px solid var(--line);
}}
.nav-brand {{
  display: flex; align-items: center; gap: 9px;
  padding: 11px 16px 8px;
}}
.brand-mark {{
  width: 26px; height: 26px; border-radius: 8px; flex: none;
  background: linear-gradient(135deg, var(--peer), var(--accent));
  display: grid; place-items: center;
  color: #fff; font-size: 13px; font-weight: 700;
}}
.brand-text {{ display: flex; flex-direction: column; line-height: 1.25; min-width: 0; }}
.brand-title {{ font-size: .88rem; font-weight: 660; }}
.brand-sub {{ font-size: .68rem; color: var(--ink-faint); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }}

.nav-list {{
  display: flex; gap: 4px; list-style: none; margin: 0;
  padding: 0 12px 9px; overflow-x: auto; scrollbar-width: none;
}}
.nav-list::-webkit-scrollbar {{ display: none; }}
/* 侧栏脚注只在桌面端出现；移动端隐藏，否则会夹在标签栏与正文之间 */
.nav-foot {{ display: none; }}
.nav-link {{
  display: block; white-space: nowrap; text-decoration: none;
  color: var(--ink-soft); font-size: .8rem; font-weight: 550;
  padding: 6px 11px; border-radius: 99px; border: 1px solid transparent;
  transition: background .18s, color .18s, border-color .18s;
}}
.nav-link:hover {{ background: var(--surface); color: var(--ink); }}
.nav-link .nav-ico {{ margin-right: 4px; opacity: .85; }}
.nav-link.is-active {{
  background: var(--ink); color: #fff; border-color: var(--ink);
}}
.nav-link.is-active .nav-ico {{ opacity: 1; }}

.main {{ padding: 20px 16px 60px; max-width: 100%; }}

/* ===================== 区块 ===================== */
.section {{ margin: 0 0 40px; scroll-margin-top: 84px; }}
.section-head {{ margin: 0 0 14px; }}
.section-head h2 {{
  font-size: 1.22rem; margin: 0 0 .2em;
  display: flex; align-items: center; gap: 9px;
}}
.section-num {{
  font-size: .7rem; font-weight: 700; letter-spacing: .06em;
  color: var(--accent); background: var(--accent-soft);
  padding: 2px 7px; border-radius: 6px; flex: none;
}}
.section-desc {{ color: var(--ink-soft); font-size: .84rem; margin: 0; max-width: 68ch; }}

.card {{
  background: var(--surface); border: 1px solid var(--line);
  border-radius: var(--radius); padding: 18px 16px; box-shadow: var(--shadow);
}}
.card + .card {{ margin-top: 12px; }}
.card-flat {{ box-shadow: none; }}

/* ===================== 结论首屏 ===================== */
.banner {{
  display: flex; gap: 10px; border-radius: var(--radius-sm);
  padding: 12px 14px; margin-bottom: 14px; font-size: .84rem;
  line-height: 1.62; border: 1px solid transparent;
}}
.banner-ico {{ flex: none; font-size: 1rem; line-height: 1.4; }}
.banner strong {{ display: block; margin-bottom: 2px; font-size: .9rem; }}
.banner-insufficient {{ background: #fdf7ec; border-color: #efdcbb; color: #8a6524; }}
.banner-assumed {{ background: #fdf1f4; border-color: #eecbd6; color: #8d4f62; }}

.verdict-grid {{ display: block; }}
.gauge-wrap {{ max-width: 300px; margin: 0 auto 6px; }}
svg.gauge {{ width: 100%; height: auto; display: block; }}
.gauge-num {{ font-size: 74px; font-weight: 700; letter-spacing: -.03em; }}
.gauge-denom {{ font-size: 15px; font-weight: 500; }}
.gauge-label {{
  text-align: center; font-size: 1.06rem; font-weight: 650; margin: 2px 0 0;
}}

.verdict-text h1 {{ font-size: 1.62rem; margin: .05em 0 .24em; }}
.one-liner {{ color: var(--ink-soft); margin: 0 0 .7em; font-size: 1rem; }}
.verdict-meta {{ font-size: .775rem; color: var(--ink-faint); line-height: 1.7; }}
.verdict-blend {{
  margin-top: 10px; padding: 9px 12px; border-radius: var(--radius-sm);
  background: var(--surface-alt); border: 1px solid var(--line-soft);
  font-size: .775rem; color: var(--ink-soft); line-height: 1.68;
}}
.verdict-blend b {{ color: var(--ink); font-variant-numeric: tabular-nums; }}

details.conf {{ margin: .7em 0 0; }}
details.conf summary {{
  cursor: pointer; font-size: .8rem; color: var(--ink-soft);
  padding: 5px 0; list-style: none; font-weight: 550;
}}
details.conf summary::-webkit-details-marker {{ display: none; }}
details.conf summary::before {{ content: "▸ "; color: var(--ink-faint); font-size: .7rem; }}
details.conf[open] summary::before {{ content: "▾ "; }}
details.conf ul {{ margin: 6px 0 8px; padding-left: 20px; font-size: .8rem; color: var(--ink-soft); }}
details.conf li {{ margin-bottom: .2em; }}

.advice {{
  background: var(--surface); border: 1px solid var(--line);
  border-left-width: 3px; border-radius: var(--radius-sm);
  padding: 12px 15px; margin: 16px 0 0; box-shadow: var(--shadow);
}}
.advice p {{ margin: 0; color: var(--ink-soft); font-size: .875rem; }}
.disclaimer-top {{
  margin-top: 14px; padding: 11px 13px; border-radius: var(--radius-sm);
  background: var(--surface-alt); border: 1px solid var(--line-soft);
  color: var(--ink-faint); font-size: .745rem; line-height: 1.66;
}}

/* ===================== 统计卡 ===================== */
.stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(148px, 1fr)); gap: 10px; }}
.stat {{
  background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius-sm);
  padding: 12px 13px; display: flex; flex-direction: column; gap: 1px;
  box-shadow: var(--shadow);
}}
.stat-title {{ font-size: .715rem; color: var(--ink-faint); letter-spacing: .01em; }}
.stat-value {{
  font-size: 1.16rem; font-weight: 680; font-variant-numeric: tabular-nums;
  letter-spacing: -.015em; line-height: 1.35;
}}
.stat-sub {{ font-size: .705rem; color: var(--ink-faint); line-height: 1.5; }}

/* ===================== 进度条 ===================== */
.bars {{ margin-top: 4px; }}
.bar-row {{ margin-bottom: 14px; }}
.bar-row:last-child {{ margin-bottom: 4px; }}
.bar-head {{ display: flex; justify-content: space-between; align-items: baseline; font-size: .85rem; margin-bottom: 6px; }}
.bar-head .who {{ font-weight: 620; }}
.bar-val {{ font-variant-numeric: tabular-nums; font-weight: 680; font-size: 1.05rem; }}
.bar-track {{ height: 9px; background: var(--line-soft); border-radius: 99px; overflow: hidden; }}
.bar-fill {{ height: 100%; border-radius: 99px; transition: width .5s cubic-bezier(.2, .7, .3, 1); }}

/* ===================== 量化指标 ===================== */
.quant-head {{
  display: flex; flex-direction: column; gap: 3px;
  background: var(--surface); border: 1px solid var(--line); border-left-width: 3px;
  border-radius: var(--radius-sm); padding: 13px 15px; margin-bottom: 12px;
  box-shadow: var(--shadow);
}}
.quant-head strong {{ font-size: 1.02rem; letter-spacing: -.01em; }}
.quant-head span {{ font-size: .83rem; color: var(--ink-soft); line-height: 1.65; }}
.quant-na {{ background: #fdf7ec; border-color: #efdcbb; }}
.quant-list {{ display: grid; gap: 10px; }}
.q-item {{
  background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius-sm);
  padding: 13px 14px; box-shadow: var(--shadow);
}}
.q-line {{ display: flex; flex-wrap: wrap; align-items: baseline; gap: 8px; margin-bottom: 7px; }}
.q-label {{ font-weight: 620; font-size: .9rem; }}
.q-value {{ font-variant-numeric: tabular-nums; font-weight: 650; color: var(--peer); font-size: .9rem; }}
.q-weight {{
  font-size: .68rem; color: var(--ink-faint); margin-left: auto;
  background: var(--surface-alt); border-radius: 99px; padding: 2px 9px;
}}
.q-skip {{ color: var(--warn); background: #fdf7ec; }}
.q-bar {{ height: 6px; background: var(--line-soft); border-radius: 99px; overflow: hidden; margin-bottom: 8px; }}
.q-bar > div {{ height: 100%; border-radius: 99px; }}
.q-formula {{ line-height: 1.68; margin: 0; color: var(--ink-faint); }}
.q-formula b {{ color: var(--ink-soft); font-weight: 600; }}
.quant-legend {{
  margin-top: 12px; padding: 11px 13px; background: var(--surface-alt);
  border: 1px solid var(--line-soft); border-radius: var(--radius-sm); line-height: 1.7;
}}

/* ===================== 图表栅格 ===================== */
.chart-grid {{ display: grid; grid-template-columns: 1fr; gap: 12px; }}
.chart-card {{
  background: var(--surface); border: 1px solid var(--line); border-radius: var(--radius);
  padding: 15px; box-shadow: var(--shadow);
}}
.chart-card h4 {{ margin: 0 0 .15em; font-size: .95rem; }}
svg.linechart, svg.hourbars, svg.monthbars {{ width: 100%; height: auto; display: block; margin: 8px 0 2px; }}

.legend {{
  display: flex; flex-wrap: wrap; gap: 12px; font-size: .73rem;
  color: var(--ink-soft); margin-top: 7px;
}}
.legend span {{ display: inline-flex; align-items: center; gap: 5px; }}
.legend i {{ width: 9px; height: 9px; border-radius: 3px; display: inline-block; flex: none; }}

/* ===================== 热力图 ===================== */
.heat-wrap {{
  background: var(--surface); border: 1px solid var(--line);
  border-radius: var(--radius); padding: 15px; box-shadow: var(--shadow);
}}
.heat-scroll {{ overflow-x: auto; padding-bottom: 7px; -webkit-overflow-scrolling: touch; }}
svg.heatmap {{ display: block; }}
svg.heatmap rect {{ transition: opacity .15s; }}
svg.heatmap rect:hover {{ opacity: .68; }}
.heat-legend {{ display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-top: 11px; }}
svg.heat-scale {{ display: block; border-radius: 3px; }}
.heat-facts {{ display: flex; flex-wrap: wrap; gap: 6px 16px; margin-top: 9px; color: var(--ink-soft); }}
.heat-note {{ margin: 9px 0 0; line-height: 1.7; }}

/* ===================== 词云 ===================== */
.cloud-wrap {{
  background: var(--surface); border: 1px solid var(--line);
  border-radius: var(--radius); padding: 15px; box-shadow: var(--shadow);
}}
svg.wordcloud {{ width: 100%; height: auto; display: block; }}
svg.wordcloud text {{ font-family: inherit; }}

/* ===================== 人物画像 ===================== */
.persona-note {{
  background: var(--surface-alt); border: 1px solid var(--line-soft);
  border-radius: var(--radius-sm); padding: 11px 13px; line-height: 1.7; margin-bottom: 12px;
}}
.persona-grid {{ display: grid; grid-template-columns: 1fr; gap: 10px; }}
.persona-card {{
  background: var(--surface); border: 1px solid var(--line);
  border-radius: var(--radius-sm); padding: 14px; box-shadow: var(--shadow);
}}
.persona-head {{ display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 7px; }}
.persona-label {{ font-weight: 670; font-size: .98rem; }}
.persona-tone {{ border-radius: 99px; padding: 2px 9px; white-space: nowrap; font-size: .68rem; font-weight: 600; }}
.persona-text {{ font-size: .855rem; color: var(--ink-soft); margin: 0 0 9px; line-height: 1.68; }}
.persona-bar {{ height: 5px; background: var(--line-soft); border-radius: 99px; overflow: hidden; margin-bottom: 6px; }}
.persona-bar > div {{ height: 100%; border-radius: 99px; }}
.persona-support {{ display: block; }}

/* ===================== 维度卡 ===================== */
.dim-grid {{ display: grid; grid-template-columns: 1fr; gap: 12px; }}
.dim-card {{
  background: var(--surface); border: 1px solid var(--line);
  border-radius: var(--radius); padding: 16px 15px; box-shadow: var(--shadow);
}}
.dim-card.dim-na {{ background: var(--surface-alt); border-style: dashed; box-shadow: none; }}
.dim-card h3 {{ font-size: 1rem; }}
.dim-summary {{ font-size: .86rem; color: var(--ink-soft); margin: 0 0 .8em; line-height: 1.68; }}
.mini-bars {{ margin: 0 0 .8em; }}
.mini {{
  display: grid; grid-template-columns: 66px 1fr 36px; align-items: center; gap: 9px;
  font-size: .755rem; margin-bottom: 6px;
}}
.mini span {{ color: var(--ink-soft); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.mini em {{ font-style: normal; text-align: right; font-variant-numeric: tabular-nums; font-weight: 660; }}
.mini-track {{ height: 6px; background: var(--line-soft); border-radius: 99px; overflow: hidden; }}
.mini-fill {{ height: 100%; border-radius: 99px; }}
.chips {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(136px, 1fr)); gap: 7px; }}
.chip {{
  background: var(--surface-alt); border: 1px solid var(--line-soft);
  border-radius: var(--radius-sm); padding: 8px 10px;
  display: flex; flex-direction: column; gap: 1px;
}}
.chip-na {{ background: #f4f3f4; border-color: transparent; opacity: .78; }}
.chip-label {{ font-size: .69rem; color: var(--ink-faint); line-height: 1.42; }}
.chip-val {{ font-size: .85rem; font-weight: 630; font-variant-numeric: tabular-nums; }}
.chip-note {{ font-size: .655rem; color: var(--ink-faint); line-height: 1.42; }}

/* ===================== 雷达 ===================== */
.radar {{ width: 100%; max-width: 440px; height: auto; display: block; margin: 0 auto; }}

/* ===================== 证据 ===================== */
.ev-list {{ list-style: none; padding: 0; margin: 0; display: grid; gap: 10px; }}
.ev {{
  background: var(--surface); border: 1px solid var(--line);
  border-radius: var(--radius-sm); padding: 13px 14px; box-shadow: var(--shadow);
}}
.ev-head {{ display: flex; justify-content: space-between; font-size: .765rem; margin-bottom: 7px; }}
.ev-who {{ font-weight: 660; }}
.ev-when {{ color: var(--ink-faint); font-variant-numeric: tabular-nums; }}
.ev blockquote {{
  margin: 0 0 7px; padding: 9px 12px; background: var(--surface-alt);
  border-left: 2px solid var(--line); border-radius: 0 var(--radius-sm) var(--radius-sm) 0;
  font-size: .865rem; white-space: pre-wrap; word-break: break-word; line-height: 1.68;
}}
.ev-reason {{ font-size: .71rem; color: var(--ink-faint); }}

/* ===================== 加分/拖后腿 ===================== */
.wins {{ display: grid; grid-template-columns: 1fr; gap: 12px; }}
.win {{
  background: var(--surface); border: 1px solid var(--line);
  border-radius: var(--radius); padding: 15px; box-shadow: var(--shadow);
}}
.win h4 {{ font-size: .92rem; }}
.win ul {{ list-style: none; margin: 0; padding: 0; }}
.win li {{
  display: grid; grid-template-columns: 1fr auto auto; gap: 11px; align-items: baseline;
  padding: 7px 0; border-bottom: 1px dashed var(--line); font-size: .83rem;
}}
.win li:last-child {{ border-bottom: none; }}
.win-score {{ font-weight: 690; font-variant-numeric: tabular-nums; }}

/* ===================== 分账表 ===================== */
.bds {{ display: grid; gap: 12px; }}
.bd {{
  background: var(--surface); border: 1px solid var(--line);
  border-radius: var(--radius); padding: 15px; box-shadow: var(--shadow);
}}
.bd h4 {{ display: flex; justify-content: space-between; align-items: baseline; font-size: .92rem; }}
.bd-total {{ font-variant-numeric: tabular-nums; color: var(--ink-soft); font-weight: 640; }}
.bd table {{ width: 100%; border-collapse: collapse; font-size: .795rem; }}
.bd th, .bd td {{ padding: 6px 4px; border-bottom: 1px solid var(--line-soft); }}
.bd th {{ color: var(--ink-faint); font-weight: 560; text-align: left; font-size: .71rem; }}
.bd th.num, .bd td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
.bd tbody tr:last-child td {{ border-bottom: none; }}

/* ===================== 时间线 ===================== */
.timeline {{ list-style: none; margin: 0; padding: 2px 0 0 6px; position: relative; }}
.timeline::before {{
  content: ""; position: absolute; left: 10px; top: 12px; bottom: 12px;
  width: 1.5px; background: var(--line); border-radius: 2px;
}}
.tl-node {{ position: relative; padding: 0 0 12px 30px; }}
.tl-node:last-child {{ padding-bottom: 0; }}
.tl-dot {{
  position: absolute; left: 4px; top: 13px; width: 13px; height: 13px;
  border-radius: 50%; border: 2.5px solid var(--bg); box-sizing: border-box;
}}
.tl-body {{
  background: var(--surface); border: 1px solid var(--line);
  border-radius: var(--radius-sm); padding: 11px 13px; box-shadow: var(--shadow);
}}
.tl-head {{ display: flex; flex-wrap: wrap; justify-content: space-between; gap: 8px; align-items: baseline; }}
.tl-title {{ font-weight: 645; font-size: .885rem; }}
.tl-when {{ font-size: .72rem; color: var(--ink-faint); font-variant-numeric: tabular-nums; }}
.tl-detail {{ margin: 4px 0 0; font-size: .84rem; color: var(--ink-soft); line-height: 1.66; }}

/* ===================== 注意项 ===================== */
.caveats {{ margin: 0; padding-left: 20px; font-size: .825rem; color: var(--ink-soft); line-height: 1.7; }}
.caveats li {{ margin-bottom: .35em; }}

/* ===================== 安慰 ===================== */
.comfort {{
  background: linear-gradient(168deg, #fdf8f5 0%, #f7f3f8 100%);
  border: 1px solid var(--line); border-radius: 20px; padding: 26px 20px;
  box-shadow: var(--shadow);
}}
.comfort-inner {{ max-width: 62ch; margin: 0 auto; }}
.comfort h2 {{ font-size: 1.16rem; color: #6b5f70; }}
.comfort p {{ color: #5b5464; font-size: .905rem; line-height: 1.82; }}

/* ===================== 页脚 ===================== */
footer {{
  margin-top: 30px; padding: 20px 16px 0; border-top: 1px solid var(--line);
  font-size: .735rem; color: var(--ink-faint); line-height: 1.72;
}}
footer p {{ margin: 0 0 .6em; }}
footer b {{ color: var(--ink-soft); }}

/* ===================== 桌面端 ===================== */
@media (min-width: 940px) {{
  :root {{ --fs-base: 15.5px; }}
  .layout {{ display: grid; grid-template-columns: var(--nav-w) minmax(0, 1fr); gap: 0; }}

  .nav {{
    position: sticky; top: 0; align-self: start; height: 100vh;
    background: var(--nav-bg); border-bottom: none; border-right: 1px solid rgba(255, 255, 255, .07);
    display: flex; flex-direction: column; overflow: hidden;
    box-shadow: var(--shadow-nav); z-index: 60;
  }}
  .nav-brand {{ padding: 22px 20px 16px; gap: 10px; }}
  .brand-mark {{ width: 30px; height: 30px; border-radius: 9px; font-size: 15px; }}
  .brand-title {{ color: #fff; font-size: .95rem; }}
  .brand-sub {{ color: rgba(255, 255, 255, .42); font-size: .7rem; }}

  .nav-list {{
    display: flex; flex-direction: column; gap: 1px;
    padding: 0 12px 16px; overflow-y: auto; overflow-x: hidden;
  }}
  .nav-list::-webkit-scrollbar {{ width: 4px; }}
  .nav-list::-webkit-scrollbar-thumb {{ background: rgba(255, 255, 255, .14); border-radius: 4px; }}
  .nav-link {{
    color: var(--nav-ink); font-size: .825rem; padding: 8px 11px;
    border-radius: 8px; border-left: 2px solid transparent; border-radius: 0 8px 8px 0;
    white-space: normal; line-height: 1.45;
  }}
  .nav-link:hover {{ background: rgba(255, 255, 255, .07); color: #fff; }}
  .nav-link.is-active {{
    background: rgba(255, 255, 255, .11); color: var(--nav-ink_active);
    border-left-color: var(--peer); font-weight: 620;
  }}
  .nav-foot {{
    display: block;
    margin-top: auto; padding: 14px 20px 18px; font-size: .68rem; line-height: 1.6;
    color: rgba(255, 255, 255, .34); border-top: 1px solid rgba(255, 255, 255, .07);
  }}

  .main {{ padding: 34px 40px 80px; max-width: 940px; }}
  .section {{ margin-bottom: 52px; scroll-margin-top: 24px; }}
  .section-head h2 {{ font-size: 1.4rem; }}
  .verdict-grid {{ display: grid; grid-template-columns: 300px minmax(0, 1fr); gap: 30px; align-items: center; }}
  .gauge-wrap {{ margin: 0; max-width: none; }}
  .verdict-text h1 {{ font-size: 2rem; }}
  .stats {{ grid-template-columns: repeat(auto-fit, minmax(158px, 1fr)); }}
  .chart-grid {{ grid-template-columns: 1fr 1fr; }}
  .dim-grid {{ grid-template-columns: 1fr 1fr; }}
  .wins {{ grid-template-columns: 1fr 1fr; }}
  .persona-grid {{ grid-template-columns: 1fr 1fr; }}
  .card {{ padding: 22px 20px; }}
}}
@media (min-width: 1180px) {{
  .main {{ max-width: 1020px; padding: 38px 48px 96px; }}
}}

@media print {{
  body {{ background: #fff; }}
  .nav {{ display: none; }}
  .layout {{ display: block; }}
  .main {{ padding: 0; max-width: none; }}
  .card, .dim-card, .chart-card, .ev, .stat, .q-item, .persona-card, .tl-body {{
    break-inside: avoid; box-shadow: none;
  }}
  .section {{ break-inside: avoid-page; }}
}}

/* =====================================================================
   动效系统
   ---------------------------------------------------------------------
   原则：入场有节制、交互有反馈、随时可关闭。

   隐藏内容靠 ``html.anim-ready`` 这个类，而不是默认状态：
   脚本启动时给 <html> 加 .anim-ready，CSS 才把元素藏起来等入场。
   脚本没跑或被拦掉时，页面就是一份「没有动画的正常文档」。
   JS 不可用会白屏，是这一层最贵的一类 bug，所以宁可少一个动画。
   ===================================================================== */

/* --- 入场：淡入 + 轻微上浮 ---
   关键设计：默认可见。
   脚本启动时给 ``<html>`` 加 ``.anim-ready``，下面第一条规则才生效、
   元素才被藏起来等着入场。这样脚本没跑、跑挂、或被拦掉时，
   页面永远是「没有动画的正常文档」，而不是一片空白——
   「内容因入场动画永久隐形」是这个项目里最贵的一类 bug。 */
html.anim-ready [data-reveal] {{
  opacity: 0;
  transform: translate3d(0, 16px, 0);
}}
[data-reveal] {{
  transition:
    opacity .6s cubic-bezier(.22, .7, .3, 1),
    transform .6s cubic-bezier(.22, .7, .3, 1);
  transition-delay: var(--d, 0ms);
}}
[data-reveal].is-in {{ opacity: 1; transform: none; }}
/* 减少动效时不做入场，元素始终可见 */
html.no-motion [data-reveal] {{ opacity: 1 !important; transform: none !important; transition: none !important; }}
html.no-motion [data-heat-cell] {{ animation: none; opacity: 1; }}

/* --- 进度条：宽度生长由脚本在揭示时驱动（见 _SPY_SCRIPT）。
       这里只给过渡曲线；没有脚本时宽度就是最终值，不会空白。 --- */
.bar-fill, .mini-fill, .persona-bar > div, .q-bar > div {{
  transform-origin: left center;
  transition: width .9s cubic-bezier(.22, .7, .3, 1);
}}

/* --- 卡片浮起 --- */
.card, .chart-card, .dim-card, .stat, .q-item, .persona-card,
.ev, .tl-body, .heat-wrap, .cloud-wrap, .win, .bd {{
  transition: box-shadow .3s ease, transform .3s cubic-bezier(.22, .7, .3, 1);
}}
@media (hover: hover) {{
  .stat:hover, .q-item:hover, .persona-card:hover, .ev:hover,
  .tl-body:hover, .win:hover, .dim-card:hover {{
    transform: translate3d(0, -3px, 0);
    box-shadow: 0 2px 4px rgba(36, 31, 43, .05),
                0 16px 34px -16px rgba(138, 79, 216, .28);
  }}
}}

/* --- 热力图格子：像涟漪一样铺开 --- */
@keyframes heatRise {{
  from {{ opacity: 0; transform: scale(.3); transform-box: fill-box; transform-origin: center; }}
  to   {{ opacity: 1; transform: scale(1); transform-box: fill-box; transform-origin: center; }}
}}
[data-heat-cell] {{
  animation: heatRise .5s cubic-bezier(.22, .7, .3, 1) both;
  animation-delay: var(--d, 0ms);
}}

/* --- 环形仪表盘：入场时轻微放大 --- */
@keyframes gaugeIn {{
  from {{ opacity: 0; transform: scale(.94); }}
  to   {{ opacity: 1; transform: scale(1); }}
}}
.gauge-wrap.is-in svg.gauge {{ animation: gaugeIn .7s cubic-bezier(.22, .7, .3, 1) both; }}

/* --- 雷达图：整块淡入 + 从中心展开 --- */
.radar {{
  transform-origin: center;
  transition: opacity .7s ease, transform .7s cubic-bezier(.22, .7, .3, 1);
}}
[data-reveal]:not(.is-in) .radar {{ opacity: 0; transform: scale(.9); }}

/* --- 折线：描边自左向右画出来 --- */
svg.linechart path {{
  stroke-dasharray: var(--len, 2400);
  stroke-dashoffset: var(--len, 2400);
}}
.is-in svg.linechart path {{ animation: drawLine 1.25s cubic-bezier(.35, .7, .3, 1) forwards; }}
@keyframes drawLine {{ to {{ stroke-dashoffset: 0; }} }}
svg.linechart circle {{ opacity: 0; transition: opacity .45s ease .5s; }}
.is-in svg.linechart circle {{ opacity: 1; }}

/* --- 时间线：节点依次亮起 --- */
.tl-node {{ transition: opacity .5s ease; transition-delay: var(--d, 0ms); }}
[data-reveal]:not(.is-in) .tl-node {{ opacity: 0; }}

/* --- 词云：词逐个浮现 --- */
svg.wordcloud text {{ transition: opacity .6s ease; transition-delay: var(--d, 0ms); }}
[data-reveal]:not(.is-in) svg.wordcloud text {{ opacity: 0; }}

/* --- 导航高亮平滑过渡 --- */
.nav-link {{ transition: background .25s ease, color .25s ease, border-color .25s ease; }}

/* --- 数字滚动标记 --- */
[data-count] {{ font-variant-numeric: tabular-nums; }}

/* --- 尊重「减少动态效果」 --- */
@media (prefers-reduced-motion: reduce) {{
  [data-reveal] {{ opacity: 1 !important; transform: none !important; transition: none !important; }}
  [data-heat-cell] {{ animation: none !important; opacity: 1 !important; }}
  .gauge-wrap.is-in svg.gauge {{ animation: none !important; }}
  svg.linechart path {{
    stroke-dasharray: none !important; stroke-dashoffset: 0 !important; animation: none !important;
  }}
  svg.linechart circle, svg.wordcloud text, .tl-node {{
    opacity: 1 !important; transition: none !important;
  }}
  .radar {{ opacity: 1 !important; transform: none !important; transition: none !important; }}
  .bar-fill, .mini-fill, .persona-bar > div, .q-bar > div {{ transition: none !important; }}
  .stat:hover, .q-item:hover, .persona-card:hover, .ev:hover,
  .tl-body:hover, .win:hover, .dim-card:hover {{ transform: none !important; }}
  html {{ scroll-behavior: auto; }}
}}

/* =====================================================================
   个性化收尾文案（insight）
   ===================================================================== */
.insight {{
  margin-top: 20px; padding: 18px 17px; border-radius: 16px;
  background: linear-gradient(140deg, #fdf4f8 0%, #f6f0fd 55%, #fdf1ea 100%);
  border: 1px solid #f0e4ee;
  box-shadow: 0 2px 6px rgba(138, 79, 216, .05),
              0 18px 40px -22px rgba(225, 72, 127, .35);
}}
.insight-head {{ display: flex; align-items: center; gap: 9px; margin-bottom: 11px; flex-wrap: wrap; }}
.insight-badge {{
  font-size: .68rem; font-weight: 700; letter-spacing: .05em;
  color: #fff; padding: 3px 11px; border-radius: 99px;
  background: linear-gradient(100deg, var(--me), var(--peer) 62%, var(--accent));
}}
.insight-from {{ color: #8b7f97; }}
.insight-lead {{
  font-size: 1rem; font-weight: 620; color: #3b3342; line-height: 1.72;
  margin: 0 0 5px; letter-spacing: -.01em;
}}
.insight-support {{ color: #8b7f97; margin: 0 0 11px; }}
.insight-closing {{
  font-size: .925rem; color: #4e4557; line-height: 1.85; margin: 0;
  padding-top: 11px; border-top: 1px dashed #ece0ee;
}}
.insight-based {{ color: #a396ae; margin: 9px 0 0; }}

/* --- 渐变文字（仅用于极少数强调，不用于正文） --- */
.grad-text {{
  background: linear-gradient(100deg, var(--me), var(--peer) 55%, var(--accent));
  -webkit-background-clip: text; background-clip: text;
  -webkit-text-fill-color: transparent; color: var(--peer);
}}
"""


# --------------------------------------------------------------------------- #
# 组装
# --------------------------------------------------------------------------- #

def _nav_items(analysis: Analysis) -> list[tuple[str, str, str, str]]:
    """导航项：``(锚点 id, 图标, 标题, 一句话说明)``。

    同时驱动侧栏导航、移动端标签栏与每个 section 的标题，
    避免「导航里叫这个名字、内容里叫那个名字」。
    """
    return [
        ("verdict", "🌼", "结论", "0–100 情感投入指数与等级判断"),
        ("quantifiers", "🎯", "量化指标", "回复速度 · 消息频率 · 深夜 · 破冰 · 收尾"),
        ("balance", "⚖️", "双方投入度与关键数据", "两个人的投入对比，以及一眼能看完的数字"),
        ("footprint", "🧭", "聊天足迹", "跨度、最佳时段、活跃月份、最长一次与最长沉默"),
        ("heatmap", "📅", "日历热力图", "一整年的对话热度，颜色深浅代表聊了多久"),
        ("topics", "💬", "话题词云", "你们聊得最多的是什么"),
        ("persona", "🪞", f"{analysis.peer} 是怎样的人", "基于可观测行为，不是性格判断"),
        ("timeline", "📍", "关键节点", "这段关系里值得记下来的时刻"),
        ("radar", "📊", "八维雷达图", "八个描述性维度的双方对比"),
        ("dimensions", "🔍", "维度明细", "每个维度的子指标数据"),
        ("trends", "📈", "互动趋势", "消息量 / 主动发起 / 情绪 / 回复速度"),
        ("evidence", "🗂", "原话与证据", "加分项、拖后腿项与真实片段"),
        ("score", "🧮", "分数是怎么算出来的", "完整权重与贡献分账"),
        ("notes", "📌", "说明与免责", "数据问题、来源与免责声明"),
    ]


def _nav_html(items: list[tuple[str, str, str, str]], analysis: Analysis,
              result: ScoreResult) -> str:
    links = "".join(
        f'<li><a class="nav-link" href="#{sid}">'
        f'<span class="nav-ico" aria-hidden="true">{ico}</span>{esc(title)}</a></li>'
        for sid, ico, title, _desc in items
    )
    return f"""
<nav class="nav" aria-label="报告章节导航">
  <div class="nav-brand">
    <span class="brand-mark" aria-hidden="true">🌼</span>
    <span class="brand-text">
      <span class="brand-title">TA 到底爱不爱我</span>
      <span class="brand-sub">{esc(analysis.peer)} × {esc(analysis.me)} · {result.total} 分</span>
    </span>
  </div>
  <ul class="nav-list">{links}</ul>
  <div class="nav-foot">
    数据只在本地处理<br>
    v{esc(__version__)}
  </div>
</nav>"""


def _section(sid: str, index: int, items: list[tuple[str, str, str, str]],
             body: str) -> str:
    """包一个带序号与锚点的区块。

    ``data-reveal`` 只打在**内容卡片**上，**不打在标题上**。
    标题一旦被藏起来而揭示又没触发，读者会看到「有内容没标题」的缺口，
    比整块没动画难看得多；而且标题本身也不适合做位移动画。
    """
    entry = next((it for it in items if it[0] == sid), None)
    title = entry[2] if entry else sid
    desc = entry[3] if entry else ""
    desc_html = f'<p class="section-desc">{esc(desc)}</p>' if desc else ""
    return f"""
<section class="section" id="{sid}">
  <div class="section-head">
    <h2><span class="section-num">{index:02d}</span>{esc(title)}</h2>
    {desc_html}
  </div>
  {body}
</section>"""


#: 页面脚本。两部分，都是纯渐进增强，**不含任何网络请求**：
#:
#: 1. **章节导航高亮**（scroll-spy）：普通锚点链接本来就能用，
#:    脚本只是让当前章节在侧栏/标签栏里显示为激活态。
#: 2. **入场动效**：滚动到视野内时淡入上浮、数字滚动、折线描边生长、
#:    进度条从 0 长出。任何一步失败都只是「没有动画」，不影响内容可读。
#:
#: 脚本第一件事就是检查 ``prefers-reduced-motion``：命中则给 ``<html>``
#: 加 ``.no-motion``，CSS 会把所有动效关掉；同时也兜住了「JS 被禁用」
#: 的情况——没有脚本时 CSS 里 ``[data-reveal]`` 的初始透明度为 0，
#: 所以由 ``<noscript>`` 里的样式负责显示（见 ``build_html``）。
_SPY_SCRIPT = """
<script>
(function () {
  var root = document.documentElement;
  root.classList.remove('no-motion');
  var reduce = window.matchMedia
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  if (reduce) { root.classList.add('no-motion'); }

  /* ---------- 1. 章节导航高亮 ---------- */
  var links = Array.prototype.slice.call(document.querySelectorAll('.nav-link'));
  var byId = {};
  links.forEach(function (a) {
    var href = a.getAttribute('href') || '';
    if (href.charAt(0) === '#') byId[href.slice(1)] = a;
  });
  var sections = Object.keys(byId)
    .map(function (id) { return document.getElementById(id); })
    .filter(Boolean);
  var visible = {};

  function highlight() {
    if (!sections.length) return;
    var best = null, bestTop = Infinity;
    sections.forEach(function (s) {
      if (!visible[s.id]) return;
      var top = Math.abs(s.getBoundingClientRect().top);
      if (top < bestTop) { bestTop = top; best = s.id; }
    });
    if (!best) {
      var y = window.scrollY + 140;
      sections.forEach(function (s) { if (s.offsetTop <= y) best = s.id; });
    }
    if (!best) return;
    links.forEach(function (a) { a.classList.toggle('is-active', a === byId[best]); });
    var active = byId[best];
    // 移动端把当前项滚进标签栏视野
    if (active && active.scrollIntoView && window.innerWidth < 940) {
      active.scrollIntoView({ block: 'nearest', inline: 'center' });
    }
  }

  if (sections.length && 'IntersectionObserver' in window) {
    var navIo = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) { visible[e.target.id] = e.isIntersecting; });
      highlight();
    }, { rootMargin: '-10% 0px -70% 0px', threshold: [0, 0.01, 0.5] });
    sections.forEach(function (s) { navIo.observe(s); });
    window.addEventListener('scroll', highlight, { passive: true });
    window.addEventListener('resize', highlight);
    highlight();
  }

  /* ---------- 2. 入场动效 ---------- */

  // 折线：先量出真实长度写进 --len，否则 dasharray 对不上会「画一半」
  function measureLines() {
    Array.prototype.forEach.call(document.querySelectorAll('svg.linechart path'), function (p) {
      try {
        var len = p.getTotalLength();
        if (len && isFinite(len)) p.style.setProperty('--len', len.toFixed(1));
      } catch (e) { /* 拿不到长度就不做描边动画 */ }
    });
  }

  // 数字滚动：只在元素首次进入视野时跑一次
  function countUp(el) {
    var target = parseFloat(el.getAttribute('data-count'));
    if (!isFinite(target)) return;
    var dur = 900, t0 = null;
    function step(ts) {
      if (t0 === null) t0 = ts;
      var p = Math.min(1, (ts - t0) / dur);
      // easeOutCubic：起步快、收尾稳
      var eased = 1 - Math.pow(1 - p, 3);
      el.textContent = String(Math.round(target * eased));
      if (p < 1) requestAnimationFrame(step);
      else el.textContent = String(Math.round(target));
    }
    requestAnimationFrame(step);
  }

  var revealTargets = Array.prototype.slice.call(document.querySelectorAll('[data-reveal]'));
  var gaugeEl = document.querySelector('.gauge-num');

  // 数字滚动：只跑一次；元素自身带 data-count，或它内部有 data-count
  function runCounts(el) {
    var list = [];
    if (el.hasAttribute && el.hasAttribute('data-count')) list.push(el);
    Array.prototype.forEach.call(el.querySelectorAll('[data-count]'), function (c) {
      list.push(c);
    });
    list.forEach(function (c) {
      if (c.dataset.done) return;
      c.dataset.done = '1';
      // 减少动效时数字直接落定，但仍然要执行——否则它会停在 0
      if (reduce) {
        c.textContent = String(Math.round(parseFloat(c.getAttribute('data-count'))));
      } else {
        countUp(c);
      }
    });
  }

  function showNow(el) {
    if (el.classList.contains('is-in')) return;
    // 先清掉内联 opacity（部分区块用它参与卡片错峰），再打标记触发过渡
    el.style.removeProperty('opacity');
    el.classList.add('is-in');
    // 进度条从 0 长出：先把真实宽度记下来、压到 0，
    // 下一帧再还原，这样浏览器才有机会做过渡。
    // 用显式调用而不是纯 CSS，是为了「没有 JS」时进度条依然是满的。
    var bars = el.querySelectorAll('.bar-fill, .mini-fill, .persona-bar > div, .q-bar > div');
    if (bars.length && !reduce) {
      Array.prototype.forEach.call(bars, function (b) {
        if (b.dataset.w === undefined) b.dataset.w = b.style.width || '0%';
        b.style.width = '0%';
      });
      window.requestAnimationFrame(function () {
        Array.prototype.forEach.call(bars, function (b) { b.style.width = b.dataset.w; });
      });
    }
    runCounts(el);
  }

  // 揭示逻辑刻意不依赖 IntersectionObserver：
  // 快速滚动、无头浏览器、部分内置浏览器里它会漏触发，
  // 而漏触发的后果是内容永久不可见——代价太大。
  // 这里用最朴素的「算一下在不在视口里」，滚动/缩放/加载后都跑一遍。
  var ticking = false;

  function inViewport(el) {
    var r = el.getBoundingClientRect();
    var vh = window.innerHeight || document.documentElement.clientHeight;
    return r.top < vh - 40 && r.bottom > -40;
  }

  function refresh() {
    ticking = false;
    for (var i = 0; i < revealTargets.length; i++) {
      var el = revealTargets[i];
      if (el.classList.contains('is-in')) continue;
      if (inViewport(el)) showNow(el);
    }
    measureLines();
  }

  function requestRefresh() {
    if (ticking) return;
    ticking = true;
    if (window.requestAnimationFrame) window.requestAnimationFrame(refresh);
    else window.setTimeout(refresh, 16);
  }

  measureLines();
  refresh();   // 首屏立刻处理

  window.addEventListener('scroll', requestRefresh, { passive: true });
  window.addEventListener('resize', requestRefresh);
  window.addEventListener('load', requestRefresh);

  // 仪表盘在首屏，一定可见：一进页面就滚动起来
  if (gaugeEl) runCounts(gaugeEl);

  // 兜底：布局变化后再补两次
  window.setTimeout(refresh, 180);
  window.setTimeout(refresh, 700);

  // 最后一道保险：无论前面发生什么，1.5 秒后把还没揭示的一律显示出来。
  // 宁可少一个动画，也不能让读者看到缺口。
  // （标题不参与入场，所以「有内容没标题」这种缺口不会出现。）
  window.setTimeout(function () {
    for (var i = 0; i < revealTargets.length; i++) {
      var el = revealTargets[i];
      if (!el.classList.contains('is-in')) el.classList.add('is-in');
    }
  }, 1500);
})();
</script>
"""


def build_html(analysis: Analysis, result: ScoreResult, conv_report=None,
               *, assumed: bool = False) -> str:
    """生成完整的单文件 HTML。

    结构：**左侧固定导航 + 右侧内容**（桌面端），移动端折叠为顶部可横滑的标签栏。
    导航与区块标题共用 :func:`_nav_items` 这一份数据，改标题只需改一处。

    ``assumed=True`` 表示「我」是自动推断的（用户没给 ``--me``），
    报告会显著提示这一点——搞反了会让结论整体颠倒。
    """
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")
    group_notice = ""
    if analysis.is_group:
        group_notice = (
            f"<p>这份记录里识别出 {len(analysis.speakers)} 个说话人，"
            f"报告只分析了 <b>{esc(analysis.me)}</b> 与 <b>{esc(analysis.peer)}</b>。</p>"
        )

    dims = list(analysis.dimensions.values())
    nav = _nav_items(analysis)

    # 区块序号按导航顺序生成，正文顺序与导航一致
    order = {sid: i + 1 for i, (sid, *_rest) in enumerate(nav)}

    def sec(sid: str, body: str) -> str:
        return _section(sid, order[sid], nav, body)

    quant_body = f"""
    <p class="tiny faint" style="margin-bottom:12px">
      这一组指标只看 <b>{esc(analysis.peer)}</b> 的行为信号：回得多快、一次能连发几条、
      深夜还在不在、冷战之后谁先开口、一段对话最后由谁收尾。
    </p>
    {render_quantifiers(analysis)}"""

    footprint_body = f"""
    <p class="tiny faint" style="margin-bottom:12px">
      从第一条消息一直算到今天。所有数字都来自消息时间戳，没有估算。
    </p>
    {render_footprint(analysis)}"""

    heatmap_body = f"""
    <p class="tiny faint" style="margin-bottom:12px">
      一整年的对话铺成一张图：每个格子是一天，<b>颜色越深，那天聊得越久</b>
      （算的是当天各段对话的时长总和，一段对话指消息间隔不超过 30 分钟的连续交谈）。
    </p>
    {render_heatmap_section(analysis)}"""

    topics_body = f"""
    <p class="tiny faint" style="margin-bottom:12px">
      取聊天记录里出现最多的话题关键词。颜色表示这个话题主要由谁说起。
    </p>
    {render_topics(analysis)}"""

    timeline_body = f"""
    <p class="tiny faint" style="margin-bottom:12px">
      这段关系里值得被记下来的时刻，按时间排列。
    </p>
    {render_timeline(analysis)}"""

    radar_body = f'<div class="card">{render_radar(dims, analysis.me, analysis.peer)}</div>'

    balance_body = f"""
    <div class="card">
      <h3 style="font-size:.95rem;margin-bottom:10px">双方的投入度</h3>
      {render_dual_bars(result, analysis)}
    </div>
    <h3 style="font-size:.95rem;margin:20px 0 10px">关键数据</h3>
    {render_cards(analysis)}"""

    evidence_body = f"""
    {render_winners(result)}
    <p class="tiny faint" style="margin:20px 0 12px">
      下面每一句都来自你导入的文件，没有改写、没有生成。自己复核一遍，比相信分数更重要。
    </p>
    {render_evidence(result, analysis)}"""

    score_body = f"""
    <details class="conf" open>
      <summary>展开 / 收起明细</summary>
      <p class="tiny faint">
        <b>总分由两个视角综合而成</b>：八维模型（覆盖面广）与五个量化指标
        （聚焦 TA 的行为信号）。两者先各自算成 0–100，再按权重综合：
        <b>{esc(result.blend_note)}</b>
      </p>
      <p class="tiny faint">
        每个维度先算出若干子指标，各自映射到 0–100 后加权得到维度分，维度分再加权得到总分。
        无法计算的维度会被剔除，权重重新归一化——<b>不会当成 0 分</b>。
        下面是 {esc(analysis.peer)} 视角的完整分账。
      </p>
      {render_breakdown(result, analysis)}
    </details>"""

    notes_body = f"""
    <p class="tiny faint">
      解析自：{esc(getattr(conv_report, 'path', '（未记录）') if conv_report else '（未记录）')}
    </p>
    {render_caveats(analysis, conv_report)}"""

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="light">
<meta name="robots" content="noindex, nofollow">
<title>TA 到底爱不爱我 · {esc(analysis.peer)} × {esc(analysis.me)} · {result.total} 分</title>
<style>{_css()}</style>
<noscript><style>
  /* 禁用 JS 时没有观察器来触发入场，必须让内容直接可见 */
  [data-reveal] {{ opacity: 1 !important; transform: none !important; transition: none !important; }}
  [data-heat-cell] {{ animation: none !important; opacity: 1 !important; }}
  svg.linechart path {{ stroke-dasharray: none !important; stroke-dashoffset: 0 !important; }}
  svg.linechart circle, svg.wordcloud text, .tl-node {{ opacity: 1 !important; }}
  .radar {{ opacity: 1 !important; transform: none !important; }}
</style></noscript>
<script>
  /* 启用入场动效之前先做两项判断，避免出现「先隐形再闪现」或白屏：
     1. 减少动效 / 没有必需 API  -> 加 .no-motion，不做任何动画；
     2. 否则加 .anim-ready，CSS 才把 [data-reveal] 藏起来等待入场。
     页面底部的主脚本与此处各管一段，互不依赖。 */
  (function () {{
    var d = document.documentElement;
    var ok = 'querySelectorAll' in document
      && window.addEventListener
      && window.getComputedStyle
      && (window.requestAnimationFrame || window.setTimeout);
    var noMotion = !ok
      || (window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
    if (noMotion) {{
      d.classList.add('no-motion');
      return;
    }}
    d.classList.add('anim-ready');
  }})();
</script>
</head>
<body>
<div class="layout">

  {_nav_html(nav, analysis, result)}

  <main class="main">

    {sec("verdict", render_verdict(result, analysis, assumed=assumed))}
    {sec("quantifiers", quant_body)}
    {sec("balance", balance_body)}
    {sec("footprint", footprint_body)}
    {sec("heatmap", heatmap_body)}
    {sec("topics", topics_body)}
    {sec("persona", render_persona_section(analysis))}
    {sec("timeline", timeline_body)}
    {sec("radar", radar_body)}
    {sec("dimensions", render_dimensions(analysis))}
    {sec("trends", render_trends(analysis, analysis.periods))}
    {sec("evidence", evidence_body)}
    {sec("score", score_body)}

    {sec("notes", notes_body)}

    {render_comfort(result, analysis)}

    <footer>
      <p><b>数据只在本地处理。</b>这份报告由 loves-me-not 在你的设备上离线生成，
      全程没有任何网络请求；你的聊天记录没有被上传到任何地方。</p>
      <p><b>结果仅供参考，不构成对真实关系的判断。</b>
      {esc(result.disclaimer)}</p>
      <p>工具只能看见文字，看不见人。沉默可能是冷淡，也可能是他那天加班到十一点、
      手机没电，或者他本来就不太会说话。</p>
      {group_notice}
      <p>生成时间：{esc(generated)} · loves-me-not v{esc(__version__)}</p>
    </footer>

  </main>
</div>
{_SPY_SCRIPT}
</body>
</html>
"""


def write_report(path: str | Path, analysis: Analysis, result: ScoreResult,
                 conv_report=None, *, do_redact: bool = False,
                 assumed: bool = False) -> Path:
    """把报告写到磁盘，返回实际路径。

    ``do_redact=True`` 时先对报告中所有原始文本做脱敏（手机号等），
    注意这会在传入的对象上就地生效。
    ``assumed=True`` 表示「我」的身份是自动推断的，报告会提示核对。
    """
    if do_redact:
        _apply_redaction(analysis, result)
    p = Path(path)
    if p.parent and not p.parent.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(build_html(analysis, result, conv_report, assumed=assumed), encoding="utf-8")
    return p


def build_json(analysis: Analysis, result: ScoreResult) -> str:
    """输出机器可读的结果（便于二次开发 / 自查）。"""
    payload = {
        "me": analysis.me,
        "peer": analysis.peer,
        "speakers": analysis.speakers,
        "is_group": analysis.is_group,
        "messages": {
            "total": analysis.total_messages,
            "real": analysis.total_real,
            "me": analysis.me_messages,
            "peer": analysis.peer_messages,
        },
        "span": {
            "first": analysis.first_at.isoformat(sep=" ") if analysis.first_at else None,
            "last": analysis.last_at.isoformat(sep=" ") if analysis.last_at else None,
            "days": analysis.days_span,
            "active_days": analysis.active_days,
        },
        "score": {
            "total": result.total,
            "raw_total": round(result.raw_total, 2),
            "tier": result.tier.key,
            "tier_title": result.tier.title,
            "insufficient_sample": result.insufficient,
            "confidence": {
                "level": result.confidence.level,
                "label": result.confidence.label,
                "score": round(result.confidence.score, 3),
                "reasons": result.confidence.reasons,
            },
            "parts": {
                name: {
                    "score": (None if part.score is None else round(part.score, 2)),
                    "weights": {k: round(v, 4) for k, v in part.used.items()},
                    "skipped": part.skipped,
                    "contributions": {k: round(v, 3) for k, v in part.contributions.items()},
                }
                for name, part in (("peer", result.peer), ("me", result.me), ("pair", result.pair))
            },
        },
        "dimensions": {
            key: {
                "label": d.label,
                "score_peer": None if d.score_peer is None else round(d.score_peer, 4),
                "score_me": None if d.score_me is None else round(d.score_me, 4),
                "available": d.available,
                "summary": d.summary,
                "subs": [
                    {
                        "key": s.key,
                        "label": s.label,
                        "value": None if s.value is None else round(s.value, 4),
                        "score": None if s.score is None else round(s.score, 4),
                        "display": s.display,
                        "note": s.note,
                    }
                    for s in d.subs
                ],
            }
            for key, d in analysis.dimensions.items()
        },
        "periods": [
            {
                "label": p.label,
                "start": p.start.isoformat(sep=" "),
                "total": p.total,
                "me": p.me_count,
                "peer": p.peer_count,
                "initiative_me": p.initiative_me,
                "initiative_peer": p.initiative_peer,
                "sentiment_me": None if p.sentiment_me is None else round(p.sentiment_me, 4),
                "sentiment_peer": None if p.sentiment_peer is None else round(p.sentiment_peer, 4),
                "reply_median_me_sec": p.reply_median_me_sec,
                "reply_median_peer_sec": p.reply_median_peer_sec,
            }
            for p in analysis.periods
        ],
        "evidence": [
            {
                "speaker": e.speaker,
                "time": e.timestamp.isoformat(sep=" ") if e.timestamp else None,
                "text": e.text,
                "reason": e.reason,
            }
            for e in result.evidence
        ],
        "caveats": analysis.caveats,
        "comfort": {"title": result.comfort_title, "body": result.comfort_body},
        "disclaimer": result.disclaimer,
        **_json_extras(analysis, result),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _json_extras(analysis: Analysis, result: ScoreResult) -> dict:
    """量化指标 / 足迹 / 时间线 / 话题 / 画像的 JSON 表示。"""
    out: dict = {
        "blend": {
            "base_score": None if result.base_score is None else round(result.base_score, 2),
            "quant_score": None if result.quant_score is None else round(result.quant_score, 2),
            "weights": result.blend,
            "note": result.blend_note,
        }
    }

    quant = analysis.quant
    if quant is not None:
        out["quantifiers"] = {
            "raw_total": None if quant.raw_total is None else round(quant.raw_total, 2),
            "summary": quant.summary,
            "weights": {k: round(v, 4) for k, v in quant.weights.items()},
            "contributions": {k: round(v, 3) for k, v in quant.contributions.items()},
            "skipped": quant.skipped,
            "metrics": {
                k: {
                    "label": m.label,
                    "value": None if m.value is None else round(m.value, 3),
                    "display": m.display,
                    "score": None if m.score is None else round(m.score, 4),
                    "formula": m.formula,
                    "source": m.source,
                    "note": m.note,
                }
                for k, m in quant.metrics.items()
            },
        }

    fp = analysis.footprint
    if fp is not None and fp.first_at is not None:
        out["footprint"] = {
            "first_at": fp.first_at.isoformat(sep=" "),
            "last_at": fp.last_at.isoformat(sep=" ") if fp.last_at else None,
            "span_to_now_days": fp.span_to_now_days,
            "span_days": fp.span_days,
            "active_days": fp.active_days,
            "total_messages": fp.total_messages,
            "total_sessions": fp.total_sessions,
            "total_chat_minutes": round(fp.total_chat_minutes, 1),
            "avg_session_minutes": round(fp.avg_session_minutes, 1),
            "best_hours": [h.label for h in fp.best_hours],
            "best_month": fp.best_month.label if fp.best_month else None,
            "warmest_month": fp.warmest_month.label if fp.warmest_month else None,
            "quietest_month": fp.quietest_month.label if fp.quietest_month else None,
            "best_weekday": fp.best_weekday,
            "longest_session": (
                {
                    "start": fp.longest_session.start.isoformat(sep=" "),
                    "end": fp.longest_session.end.isoformat(sep=" "),
                    "minutes": round(fp.longest_session.duration_minutes, 1),
                    "messages": fp.longest_session.count,
                }
                if fp.longest_session else None
            ),
            "longest_silence": (
                {
                    "start": fp.longest_silence.start.isoformat(sep=" "),
                    "end": fp.longest_silence.end.isoformat(sep=" "),
                    "days": round(fp.longest_silence.days, 2),
                    "broken_by": fp.longest_silence.broken_by,
                }
                if fp.longest_silence else None
            ),
            "hours": [
                {"hour": h.hour, "count": h.count,
                 "me": h.me_count, "peer": h.peer_count}
                for h in fp.hours
            ],
            "months": [
                {"label": m.label, "count": m.count, "me": m.me_count,
                 "peer": m.peer_count, "active_days": m.active_days,
                 "peer_share": round(m.peer_share, 4)}
                for m in fp.months
            ],
            "weekdays": fp.weekdays,
            "days": [
                {"date": d.date_key, "count": d.count,
                 "chat_minutes": round(d.chat_minutes, 1),
                 "span_minutes": round(d.span_minutes, 1),
                 "me": d.me_count, "peer": d.peer_count}
                for d in sorted(fp.days.values(), key=lambda x: x.date_key)
            ],
        }

    out["timeline"] = [
        {
            "kind": n.kind,
            "title": n.title,
            "when": n.when.isoformat(sep=" ") if n.when else None,
            "detail": n.detail,
        }
        for n in analysis.timeline_nodes
    ]
    out["topics"] = [
        {"word": t.word, "count": t.count, "weight": t.weight, "dominant": t.dominant}
        for t in analysis.topics
    ]
    out["persona"] = [
        {
            "key": p.key, "label": p.label, "strength": round(p.strength, 3),
            "evidence": p.evidence_text, "support": p.support, "tone": p.tone,
        }
        for p in analysis.persona
    ]
    return out