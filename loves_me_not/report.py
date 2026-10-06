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
from .scoring import ScoreResult
from . import __version__

# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #

def esc(text: object) -> str:
    """HTML 转义（用户聊天内容必须转义，否则会把报告结构冲掉）。"""
    return html.escape(str(text if text is not None else ""), quote=True)


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
    # 微信号 / QQ 号声明
    (re.compile(r"(?:微信|weixin|wechat|vx|VX|qq|QQ)\s*[:：]?\s*[A-Za-z0-9_-]{5,20}"), "[账号已打码]"),
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
    """就地脱敏：报告里所有会展示原始文本的地方都过一遍。"""
    for dim in analysis.dimensions.values():
        for ev in dim.evidence:
            ev.text = redact(ev.text)
    for ev in result.evidence:
        ev.text = redact(ev.text)
    for group in analysis.highlights.values():
        for ev in group:
            ev.text = redact(ev.text)


# --------------------------------------------------------------------------- #
# 调色板：柔和、低饱和，投影和手机上都不刺眼
# --------------------------------------------------------------------------- #

PALETTE = {
    "bg": "#fbf8f6",
    "card": "#ffffff",
    "ink": "#3d3a3f",
    "ink_soft": "#7b7480",
    "ink_faint": "#a9a2ae",
    "line": "#ece6e8",
    "peer": "#c96a8a",     # 薰衣草玫瑰——代表 TA
    "me": "#7fa8c9",       # 雾蓝——代表我
    "accent": "#d9a441",   # 暖金——强调
    "good": "#7fb99a",
    "warn": "#e0a06a",
}


# --------------------------------------------------------------------------- #
# SVG：仪表盘
# --------------------------------------------------------------------------- #

_GAUGE_ANGLE = 250.0        # 弧线总张角（度）
_GAUGE_START = 145.0        # 起始角度（从 12 点顺时针计）


def _polar(cx: float, cy: float, r: float, angle_deg: float) -> tuple[float, float]:
    """角度以「12 点方向为 0，顺时针为正」计。"""
    rad = math.radians(angle_deg - 90.0)
    return cx + r * math.cos(rad), cy + r * math.sin(rad)


def _arc_path(cx: float, cy: float, r: float, a0: float, a1: float) -> str:
    """画圆弧。

    角度按「12 点方向为 0、顺时针为正」定义，而 SVG 的 y 轴向下，
    所以「屏幕上顺时针」对应 ``sweep-flag = 0``。这里固定用 0，
    否则弧线会朝反方向画，和指针指向对不上。
    """
    if a1 < a0:
        a0, a1 = a1, a0
    x0, y0 = _polar(cx, cy, r, a0)
    x1, y1 = _polar(cx, cy, r, a1)
    large = 1 if abs(a1 - a0) > 180 else 0
    return f"M {x0:.2f} {y0:.2f} A {r:.2f} {r:.2f} 0 {large} 0 {x1:.2f} {y1:.2f}"


def render_gauge(score: int, tier_color: str, label: str) -> str:
    """总分仪表盘。"""
    cx, cy, r = 160.0, 150.0, 108.0
    frac = max(0.0, min(1.0, score / 100.0))
    end_angle = _GAUGE_START + _GAUGE_ANGLE * frac

    ticks = []
    for v in range(0, 101, 10):
        a = _GAUGE_START + _GAUGE_ANGLE * (v / 100.0)
        x0, y0 = _polar(cx, cy, r + 8, a)
        x1, y1 = _polar(cx, cy, r + (16 if v % 20 == 0 else 12), a)
        ticks.append(
            f'<line x1="{x0:.1f}" y1="{y0:.1f}" x2="{x1:.1f}" y2="{y1:.1f}" '
            f'stroke="{PALETTE["ink_faint"]}" stroke-width="1.5" stroke-linecap="round"/>'
        )
        if v % 20 == 0:
            tx, ty = _polar(cx, cy, r + 28, a)
            ticks.append(
                f'<text x="{tx:.1f}" y="{ty:.1f}" fill="{PALETTE["ink_faint"]}" '
                f'font-size="10" text-anchor="middle" dominant-baseline="middle">{v}</text>'
            )

    nx, ny = _polar(cx, cy, r - 26, end_angle)
    return f"""
<svg viewBox="0 0 320 250" class="gauge" role="img" aria-label="情感投入指数 {score} 分">
  <path d="{_arc_path(cx, cy, r, _GAUGE_START, _GAUGE_START + _GAUGE_ANGLE)}"
        fill="none" stroke="{PALETTE['line']}" stroke-width="18" stroke-linecap="round"/>
  <path d="{_arc_path(cx, cy, r, _GAUGE_START, end_angle)}"
        fill="none" stroke="{tier_color}" stroke-width="18" stroke-linecap="round"/>
  {''.join(ticks)}
  <line x1="{cx}" y1="{cy}" x2="{nx:.1f}" y2="{ny:.1f}"
        stroke="{PALETTE['ink']}" stroke-width="3.5" stroke-linecap="round"/>
  <circle cx="{cx}" cy="{cy}" r="7" fill="{PALETTE['ink']}"/>
  <text x="{cx}" y="{cy - 34}" text-anchor="middle" fill="{PALETTE['ink']}"
        font-size="52" font-weight="700">{score}</text>
  <text x="{cx}" y="{cy - 8}" text-anchor="middle" fill="{PALETTE['ink_soft']}"
        font-size="13">/ 100</text>
  <text x="{cx}" y="{cy + 44}" text-anchor="middle" fill="{tier_color}"
        font-size="16" font-weight="600">{esc(label)}</text>
</svg>"""


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
<div class="chart-card">
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

def render_verdict(result: ScoreResult, analysis: Analysis) -> str:
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
    for d in analysis.dimensions.values():
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
  <article class="dim-card{'' if d.available else ' dim-na'}">
    <h3>{esc(d.label)}</h3>
    {na_note}
    <p class="dim-summary">{esc(d.summary)}</p>
    <div class="mini-bars">{bars}</div>
    <div class="chips">{''.join(chips)}</div>
  </article>""")
    return f'<div class="dim-grid">{"".join(cards)}</div>'


def render_cards(analysis: Analysis) -> str:
    out = []
    for title, value, sub in analysis.cards:
        out.append(
            f'<div class="stat"><span class="stat-title">{esc(title)}</span>'
            f'<span class="stat-value">{esc(value)}</span>'
            f'<span class="stat-sub">{esc(sub)}</span></div>'
        )
    return f'<div class="stats">{"".join(out)}</div>'


def render_evidence(result: ScoreResult, analysis: Analysis) -> str:
    if not result.evidence:
        return ('<p class="faint">这份记录里没有挑出足够清晰的原话片段。'
                "结论只基于统计量，请谨慎参考。</p>")
    items = []
    for ev in result.evidence:
        color = PALETTE["peer"] if ev.speaker == analysis.peer else PALETTE["me"]
        when = ev.when()
        items.append(f"""
    <li class="ev">
      <div class="ev-head">
        <span class="ev-who" style="color:{color}">{esc(ev.speaker)}</span>
        <span class="ev-when">{esc(when)}</span>
      </div>
      <blockquote>{esc(ev.text)}</blockquote>
      <span class="ev-reason">{esc(ev.reason)}</span>
    </li>""")
    return f'<ul class="ev-list">{"".join(items)}</ul>'


def render_winners(result: ScoreResult) -> str:
    def block(title: str, rows: Sequence[tuple[str, float, float]], color: str, empty: str) -> str:
        if not rows:
            return f'<div class="win"><h4>{esc(title)}</h4><p class="faint tiny">{esc(empty)}</p></div>'
        lis = "".join(
            f'<li><span>{esc(label)}</span>'
            f'<span class="win-score" style="color:{color}">{score * 100:.0f}</span>'
            f'<span class="tiny faint">权重 {_pct(w)}</span></li>'
            for label, score, w in rows
        )
        return f'<div class="win"><h4>{esc(title)}</h4><ul>{lis}</ul></div>'

    return (
        '<div class="wins">'
        + block("主要加分项", result.top_positive, PALETTE["good"], "没有明显高于基准的维度。")
        + block("主要拖后腿", result.top_negative, PALETTE["warn"], "没有明显低于基准的维度。")
        + "</div>"
    )


def render_breakdown(result: ScoreResult, analysis: Analysis) -> str:
    """把「分数怎么来的」摊开给人看。"""
    labels = {k: d.label for k, d in analysis.dimensions.items()}

    def table(part, title: str) -> str:
        if part.score is None:
            return f'<div class="bd"><h4>{esc(title)}</h4><p class="faint tiny">无可用维度。</p></div>'
        rows = []
        for k, w in sorted(part.used.items(), key=lambda kv: -kv[1]):
            dim = analysis.dimensions[k]
            dim_score = dim.score_peer if part.name != "me" else dim.score_me
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
    <div class="bd">
      <h4>{esc(title)} <span class="bd-total">{part.score:.1f}</span></h4>
      <table>
        <thead><tr><th>维度</th><th class="num">维度分</th><th class="num">权重</th><th class="num">贡献</th></tr></thead>
        <tbody>{''.join(rows)}</tbody>
      </table>
      {skipped}
    </div>"""

    return (
        '<div class="bds">'
        + table(result.peer, f"{analysis.peer} 的投入度（决定上面的总分）")
        + table(result.me, f"{analysis.me} 的投入度")
        + table(result.pair, "双向互动综合")
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


def render_comfort(result: ScoreResult) -> str:
    paragraphs = "".join(
        f"<p>{esc(p.strip())}</p>" for p in result.comfort_body.split("\n\n") if p.strip()
    )
    return f"""
<section class="comfort">
  <div class="comfort-inner">
    <p class="kicker">写在这里的话</p>
    <h2>{esc(result.comfort_title)}</h2>
    {paragraphs}
  </div>
</section>"""


# --------------------------------------------------------------------------- #
# 样式
# --------------------------------------------------------------------------- #

def _css() -> str:
    p = PALETTE
    return f"""
:root {{
  --bg: {p['bg']}; --card: {p['card']};
  --ink: {p['ink']}; --ink-soft: {p['ink_soft']}; --ink-faint: {p['ink_faint']};
  --line: {p['line']}; --peer: {p['peer']}; --me: {p['me']}; --accent: {p['accent']};
}}
* {{ box-sizing: border-box; }}
html {{ -webkit-text-size-adjust: 100%; }}
body {{
  margin: 0; background: var(--bg); color: var(--ink);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC",
               "Hiragino Sans GB", "Microsoft YaHei", "Noto Sans SC", sans-serif;
  font-size: 15px; line-height: 1.72;
  -webkit-font-smoothing: antialiased;
}}
.wrap {{ max-width: 760px; margin: 0 auto; padding: 18px 16px 56px; }}
h1, h2, h3, h4 {{ line-height: 1.35; margin: 0 0 .5em; font-weight: 650; }}
h2 {{ font-size: 1.28rem; }}
h3 {{ font-size: 1.02rem; }}
h4 {{ font-size: .92rem; }}
p {{ margin: 0 0 .8em; }}
.tiny {{ font-size: .78rem; }}
.faint {{ color: var(--ink-faint); }}
.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
.kicker {{
  font-size: .72rem; letter-spacing: .16em; text-transform: uppercase;
  color: var(--ink-faint); margin: 0 0 .35em;
}}
section {{ margin: 0 0 26px; }}
.card {{
  background: var(--card); border: 1px solid var(--line); border-radius: 16px;
  padding: 18px 16px;
}}
.banner {{
  display: flex; flex-direction: column; gap: 6px;
  background: #fdf6e9; border: 1px solid #f0dcb8; border-radius: 14px;
  padding: 12px 14px; margin-bottom: 16px; font-size: .86rem; color: #8a6a2f;
}}
.banner strong {{ font-size: .95rem; }}
.verdict .verdict-grid {{ display: block; }}
.gauge-wrap {{ max-width: 340px; margin: 0 auto; }}
svg.gauge {{ width: 100%; height: auto; display: block; }}
.verdict-text h1 {{ font-size: 1.7rem; margin: .1em 0 .2em; }}
.one-liner {{ color: var(--ink-soft); margin: 0 0 .6em; }}
.advice {{
  background: var(--card); border: 1px solid var(--line); border-left-width: 4px;
  border-radius: 12px; padding: 12px 14px; margin: 14px 0 0;
}}
.advice p {{ margin: 0; color: var(--ink-soft); font-size: .9rem; }}
.disclaimer-top {{
  margin-top: 14px; padding: 10px 12px; border-radius: 10px;
  background: #f4f1f5; color: var(--ink-soft); font-size: .76rem;
}}
details.conf {{ margin: .6em 0 0; }}
details.conf summary {{
  cursor: pointer; font-size: .82rem; color: var(--ink-soft);
  padding: 4px 0; list-style: none;
}}
details.conf summary::-webkit-details-marker {{ display: none; }}
details.conf summary::before {{ content: "▸ "; color: var(--ink-faint); }}
details.conf[open] summary::before {{ content: "▾ "; }}
details.conf ul {{ margin: 6px 0; padding-left: 20px; font-size: .82rem; color: var(--ink-soft); }}
.bars {{ margin-top: 6px; }}
.bar-row {{ margin-bottom: 12px; }}
.bar-head {{ display: flex; justify-content: space-between; font-size: .85rem; margin-bottom: 5px; }}
.bar-head .who {{ font-weight: 600; }}
.bar-val {{ font-variant-numeric: tabular-nums; font-weight: 650; }}
.bar-track {{ height: 10px; background: var(--line); border-radius: 99px; overflow: hidden; }}
.bar-fill {{ height: 100%; border-radius: 99px; }}
.stats {{
  display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px;
}}
.stat {{
  background: var(--card); border: 1px solid var(--line); border-radius: 14px;
  padding: 12px 13px; display: flex; flex-direction: column; gap: 2px;
}}
.stat-title {{ font-size: .74rem; color: var(--ink-faint); }}
.stat-value {{ font-size: 1.18rem; font-weight: 680; font-variant-numeric: tabular-nums; }}
.stat-sub {{ font-size: .72rem; color: var(--ink-faint); }}
.dim-grid {{ display: grid; grid-template-columns: 1fr; gap: 12px; }}
.dim-card {{
  background: var(--card); border: 1px solid var(--line); border-radius: 16px; padding: 15px 14px;
}}
.dim-card.dim-na {{ background: #fafafa; border-style: dashed; }}
.dim-summary {{ font-size: .88rem; color: var(--ink-soft); margin: 0 0 .7em; }}
.mini-bars {{ margin: 0 0 .7em; }}
.mini {{ display: grid; grid-template-columns: 68px 1fr 34px; align-items: center; gap: 8px;
        font-size: .76rem; margin-bottom: 5px; }}
.mini span {{ color: var(--ink-soft); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.mini em {{ font-style: normal; text-align: right; font-variant-numeric: tabular-nums; font-weight: 640; }}
.mini-track {{ height: 7px; background: var(--line); border-radius: 99px; overflow: hidden; }}
.mini-fill {{ height: 100%; border-radius: 99px; }}
.chips {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(132px, 1fr)); gap: 6px; }}
.chip {{
  background: #f7f4f6; border-radius: 9px; padding: 7px 9px;
  display: flex; flex-direction: column; gap: 1px;
}}
.chip-na {{ background: #f2f2f4; opacity: .72; }}
.chip-label {{ font-size: .7rem; color: var(--ink-faint); line-height: 1.4; }}
.chip-val {{ font-size: .86rem; font-weight: 620; font-variant-numeric: tabular-nums; }}
.chip-note {{ font-size: .66rem; color: var(--ink-faint); line-height: 1.35; }}
.radar {{ width: 100%; max-width: 460px; height: auto; display: block; margin: 0 auto; }}
.legend {{
  display: flex; flex-wrap: wrap; gap: 10px; font-size: .74rem;
  color: var(--ink-soft); margin-top: 6px;
}}
.legend span {{ display: inline-flex; align-items: center; gap: 5px; }}
.legend i {{ width: 10px; height: 10px; border-radius: 3px; display: inline-block; }}
.chart-grid {{ display: grid; grid-template-columns: 1fr; gap: 12px; }}
.chart-card {{
  background: var(--card); border: 1px solid var(--line); border-radius: 16px; padding: 14px;
}}
.chart-card h4 {{ margin: 0 0 .1em; }}
svg.linechart {{ width: 100%; height: auto; display: block; margin: 6px 0 2px; }}
.ev-list {{ list-style: none; padding: 0; margin: 0; display: grid; gap: 10px; }}
.ev {{
  background: var(--card); border: 1px solid var(--line); border-radius: 14px; padding: 12px 13px;
}}
.ev-head {{ display: flex; justify-content: space-between; font-size: .78rem; margin-bottom: 6px; }}
.ev-who {{ font-weight: 650; }}
.ev-when {{ color: var(--ink-faint); font-variant-numeric: tabular-nums; }}
.ev blockquote {{
  margin: 0 0 6px; padding: 8px 11px; background: #f8f5f7; border-radius: 9px;
  font-size: .88rem; white-space: pre-wrap; word-break: break-word;
}}
.ev-reason {{ font-size: .72rem; color: var(--ink-faint); }}
.wins {{ display: grid; grid-template-columns: 1fr; gap: 12px; }}
.win {{ background: var(--card); border: 1px solid var(--line); border-radius: 16px; padding: 14px; }}
.win ul {{ list-style: none; margin: 0; padding: 0; }}
.win li {{
  display: grid; grid-template-columns: 1fr auto auto; gap: 10px; align-items: baseline;
  padding: 6px 0; border-bottom: 1px dashed var(--line); font-size: .84rem;
}}
.win li:last-child {{ border-bottom: none; }}
.win-score {{ font-weight: 680; font-variant-numeric: tabular-nums; }}
.bds {{ display: grid; gap: 12px; }}
.bd {{ background: var(--card); border: 1px solid var(--line); border-radius: 16px; padding: 14px; }}
.bd h4 {{ display: flex; justify-content: space-between; align-items: baseline; }}
.bd-total {{ font-variant-numeric: tabular-nums; color: var(--ink-soft); font-weight: 600; }}
.bd table {{ width: 100%; border-collapse: collapse; font-size: .8rem; }}
.bd th, .bd td {{ padding: 5px 4px; border-bottom: 1px solid var(--line); }}
.bd th {{ color: var(--ink-faint); font-weight: 500; text-align: left; font-size: .72rem; }}
.bd th.num, .bd td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
.caveats {{ margin: 0; padding-left: 20px; font-size: .84rem; color: var(--ink-soft); }}
.caveats li {{ margin-bottom: .35em; }}
.comfort {{
  background: linear-gradient(170deg, #fdf7f4 0%, #f6f2f7 100%);
  border: 1px solid var(--line); border-radius: 18px; padding: 22px 18px;
}}
.comfort-inner {{ max-width: 62ch; margin: 0 auto; }}
.comfort h2 {{ font-size: 1.2rem; color: #6f6273; }}
.comfort p {{ color: #5f5766; font-size: .92rem; }}
footer {{
  margin-top: 26px; padding-top: 16px; border-top: 1px solid var(--line);
  font-size: .74rem; color: var(--ink-faint);
}}
footer p {{ margin: 0 0 .5em; }}
@media (min-width: 620px) {{
  body {{ font-size: 15.5px; }}
  .wrap {{ padding: 26px 22px 72px; }}
  .verdict .verdict-grid {{ display: grid; grid-template-columns: 300px 1fr; gap: 20px; align-items: center; }}
  .gauge-wrap {{ max-width: none; }}
  .verdict-text h1 {{ font-size: 2rem; }}
  .dim-grid {{ grid-template-columns: 1fr 1fr; }}
  .chart-grid {{ grid-template-columns: 1fr 1fr; }}
  .wins {{ grid-template-columns: 1fr 1fr; }}
  .bds {{ grid-template-columns: 1fr; }}
}}
@media print {{
  body {{ background: #fff; }}
  .card, .dim-card, .chart-card, .ev, .stat {{ break-inside: avoid; }}
}}
"""


# --------------------------------------------------------------------------- #
# 组装
# --------------------------------------------------------------------------- #

def build_html(analysis: Analysis, result: ScoreResult, conv_report=None) -> str:
    """生成完整的单文件 HTML。"""
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")
    group_notice = ""
    if analysis.is_group:
        group_notice = (
            f"<p>这份记录里识别出 {len(analysis.speakers)} 个说话人，"
            f"报告只分析了 <b>{esc(analysis.me)}</b> 与 <b>{esc(analysis.peer)}</b>。</p>"
        )

    dims = list(analysis.dimensions.values())

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="color-scheme" content="light">
<meta name="robots" content="noindex, nofollow">
<title>TA 到底爱不爱我 · {esc(analysis.peer)} × {esc(analysis.me)} · {result.total} 分</title>
<style>{_css()}</style>
</head>
<body>
<div class="wrap">

  {render_verdict(result, analysis)}

  <section>
    <h2>双方投入度</h2>
    <div class="card">
      {render_dual_bars(result, analysis)}
    </div>
  </section>

  <section>
    <h2>关键数据</h2>
    {render_cards(analysis)}
  </section>

  <section>
    <h2>八维雷达图</h2>
    <div class="card">
      {render_radar(dims, analysis.me, analysis.peer)}
    </div>
  </section>

  <section>
    <h2>维度明细</h2>
    {render_dimensions(analysis)}
  </section>

  <section>
    <h2>互动趋势</h2>
    {render_trends(analysis, analysis.periods)}
  </section>

  <section>
    <h2>主要加分项与拖后腿项</h2>
    {render_winners(result)}
  </section>

  <section>
    <h2>支撑结论的原话</h2>
    <p class="tiny faint">
      下面每一句都来自你导入的文件，没有改写、没有生成。自己复核一遍，比相信分数更重要。
    </p>
    {render_evidence(result, analysis)}
  </section>

  <section>
    <h2>分数是怎么算出来的</h2>
    <details class="conf" open>
      <summary>展开 / 收起明细</summary>
      <p class="tiny faint">
        每个维度先算出若干子指标，各自映射到 0–100 后加权得到维度分，维度分再加权得到总分。
        无法计算的维度会被剔除，权重重新归一化——<b>不会当成 0 分</b>。
        下面是 {esc(analysis.peer)} 视角（也就是顶部总分）的完整分账。
      </p>
      {render_breakdown(result, analysis)}
    </details>
  </section>

  {render_comfort(result)}

  <section>
    <h2>数据说明与免责</h2>
    <p class="tiny faint">
      解析自：{esc(getattr(conv_report, 'path', '（未记录）') if conv_report else '（未记录）')}
    </p>
    {render_caveats(analysis, conv_report)}
  </section>

  <footer>
    <p><b>数据只在本地处理。</b>这份报告由 loves-me-not 在你的设备上离线生成，
    全程没有任何网络请求；你的聊天记录没有被上传到任何地方。</p>
    <p><b>结果仅供参考，不构成对真实关系的判断。</b>
    {esc(result.disclaimer)}</p>
    <p>工具只能看见文字，看不见人。沉默可能是冷淡，也可能是他那天加班到十一点、
    手机没电，或者他本来就不太会说话。</p>
    <p>{group_notice}</p>
    <p>生成时间：{esc(generated)} · loves-me-not v{esc(__version__)}</p>
  </footer>

</div>
</body>
</html>
"""


def write_report(path: str | Path, analysis: Analysis, result: ScoreResult,
                 conv_report=None, *, do_redact: bool = False) -> Path:
    """把报告写到磁盘，返回实际路径。

    ``do_redact=True`` 时先对报告中所有原始文本做脱敏（手机号等），
    注意这会在传入的对象上就地生效。
    """
    if do_redact:
        _apply_redaction(analysis, result)
    p = Path(path)
    if p.parent and not p.parent.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(build_html(analysis, result, conv_report), encoding="utf-8")
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
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)