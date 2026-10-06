"""三种新可视化：聊天足迹热力图、话题词云、人物画像卡。

全部输出**内联 SVG / HTML**，不依赖任何前端库，报告保持单文件自包含。

1. :func:`render_heatmap` —— 仿 GitHub Contribution 的日历热力图。
   颜色深浅代表**当次聊天时长**（当天首尾消息的时间差），
   而不是消息条数——用户明确要求用时长来编码深浅。
2. :func:`render_wordcloud` —— 话题词云。用「螺旋放置 + 碰撞检测」在
   Python 里算好每个词的位置，输出绝对定位的 HTML span，
   移动端自动换行（宽度不足时降级为普通标签流）。
3. :func:`render_persona` —— 「TA 是一个怎样的人」卡片墙。
"""

from __future__ import annotations

import html
import math
import random
from datetime import datetime, timedelta
from typing import Sequence

from .insights import DayStat, Persona, TopicWord
from .timeline import Footprint, HourBucket, MonthStat

# --------------------------------------------------------------------------- #
# 调色板：**这里是全项目颜色的唯一来源**
# --------------------------------------------------------------------------- #

PALETTE = {
    # 底色与层次
    "bg": "#f6f4f1",
    "surface": "#ffffff",
    "surface_alt": "#faf8f6",
    "ink": "#2a2731",
    "ink_soft": "#5f5966",
    "ink_faint": "#928c98",
    "line": "#e7e2df",
    "line_soft": "#f0ece8",
    # 语义色
    "peer": "#b4576f",        # 深玫瑰——代表 TA
    "peer_soft": "#f3e4e8",
    "me": "#4f7fa0",          # 雾蓝——代表我
    "me_soft": "#e5edf4",
    "accent": "#b07d34",
    "accent_soft": "#f6eedd",
    "good": "#4f8a6b",
    "warn": "#b5793a",
    # 结构（侧栏导航）
    "nav_bg": "#221f28",
    "nav_ink": "#cfc9d4",
    "nav_ink_active": "#ffffff",
    # 兼容旧键名（HTML 侧仍在用 card）
    "card": "#ffffff",
}

#: 热力图色阶：由浅到深（时长 0 → 最长）
HEAT_SCALE = ("#f0ecea", "#f0dde3", "#e2b9c5", "#d08ea2", "#b4576f", "#8e3d55")

WEEKDAY_LABELS = ("一", "二", "三", "四", "五", "六", "日")
MONTH_LABELS = ("1月", "2月", "3月", "4月", "5月", "6月",
                "7月", "8月", "9月", "10月", "11月", "12月")


def esc(text: object) -> str:
    return html.escape(str(text if text is not None else ""), quote=True)


# --------------------------------------------------------------------------- #
# 1. 模仿 GitHub Contribution 的日历热力图
# --------------------------------------------------------------------------- #

def _heat_color(minutes: float, scale_max: float) -> str:
    """按聊天时长选颜色。分档而不是线性插值，便于阅读。"""
    if minutes <= 0:
        return HEAT_SCALE[0]
    if scale_max <= 0:
        return HEAT_SCALE[1]
    ratio = min(1.0, minutes / scale_max)
    idx = int(ratio * (len(HEAT_SCALE) - 1)) + 1
    return HEAT_SCALE[min(idx, len(HEAT_SCALE) - 1)]


def render_heatmap(footprint: Footprint, weeks_limit: int | None = None) -> str:
    """仿 GitHub 贡献图的日历热力图。

    * 每个格子 = 一天；
    * 列 = 周（周一在上），行 = 星期；
    * **格子颜色的深浅 = 当次聊天时长**（当天最后一条 − 第一条消息）；
    * 时长存疑时（样本不足）仍然画，但图例里写清楚口径。

    ``weeks_limit`` 用于截断过长的历史（默认全部画出，靠横向滚动查看）。
    """
    if not footprint.days:
        return '<p class="faint">没有可用于绘制热力图的数据。</p>'

    days = footprint.days
    keys = sorted(days)
    first = datetime.strptime(keys[0], "%Y-%m-%d")
    last = datetime.strptime(keys[-1], "%Y-%m-%d")

    # 对齐到周一开头
    start = first - timedelta(days=first.weekday())
    end = last + timedelta(days=6 - last.weekday())
    total_days = (end - start).days + 1
    total_weeks = total_days // 7

    if weeks_limit and total_weeks > weeks_limit:
        start = end - timedelta(days=weeks_limit * 7 - 1)
        total_weeks = weeks_limit

    cell, gap = 11, 3
    pad_left, pad_top = 26, 16
    grid_w = total_weeks * (cell + gap)
    grid_h = 7 * (cell + gap)

    scale_max = max((d.chat_minutes for d in days.values()), default=1.0)
    scale_max = max(scale_max, 20.0)   # 下限，避免「都是 1 分钟」时对比失真

    # 日期 → (周索引, 星期索引)
    def coord(day: datetime) -> tuple[int, int]:
        delta = (day - start).days
        return delta // 7, day.weekday()

    cells: list[str] = []
    for key in keys:
        d = days[key]
        day = datetime.strptime(key, "%Y-%m-%d")
        if day < start or day > end:
            continue
        wi, di = coord(day)
        if wi < 0 or wi >= total_weeks:
            continue
        x = pad_left + wi * (cell + gap)
        y = pad_top + di * (cell + gap)
        color = _heat_color(d.chat_minutes, scale_max)
        tip = (f"{key} · {d.count} 条消息 · 聊了 {_hm(d.chat_minutes)}"
               f"（TA {d.peer_count} / 我 {d.me_count}）")
        cells.append(
            f'<rect x="{x}" y="{y}" width="{cell}" height="{cell}" rx="2.5" '
            f'fill="{color}"><title>{esc(tip)}</title></rect>'
        )

    # 月份标签：每列若跨月则在顶部标注
    month_marks: list[str] = []
    seen_months: set[str] = set()
    for wi in range(total_weeks):
        col_start = start + timedelta(days=wi * 7)
        mk = col_start.strftime("%Y-%m")
        if mk in seen_months:
            continue
        seen_months.add(mk)
        x = pad_left + wi * (cell + gap)
        month_marks.append(
            f'<text x="{x}" y="{pad_top - 5}" font-size="9" '
            f'fill="{PALETTE["ink_faint"]}">{esc(MONTH_LABELS[col_start.month - 1])}</text>'
        )

    # 星期标签
    weekday_marks = []
    for di, name in enumerate(WEEKDAY_LABELS):
        if di % 2:          # 隔行标注，避免拥挤
            continue
        y = pad_top + di * (cell + gap) + cell / 2 + 3
        weekday_marks.append(
            f'<text x="{pad_left - 6}" y="{y}" font-size="9" text-anchor="end" '
            f'fill="{PALETTE["ink_faint"]}">{esc(name)}</text>'
        )

    svg_w = pad_left + grid_w + 6
    svg_h = pad_top + grid_h + 6

    # 色阶图例
    legend_cells = "".join(
        f'<rect x="{i * 15}" y="0" width="11" height="11" rx="2.5" fill="{c}"/>'
        for i, c in enumerate(HEAT_SCALE)
    )

    busiest = max(days.values(), key=lambda d: d.count)
    longest = max(days.values(), key=lambda d: d.chat_minutes)

    return f"""
<div class="heat-wrap">
  <div class="heat-scroll">
    <svg viewBox="0 0 {svg_w} {svg_h}" width="{svg_w}" height="{svg_h}"
         class="heatmap" role="img"
         aria-label="聊天足迹热力图，共 {len(days)} 天有对话">
      {''.join(month_marks)}
      {''.join(weekday_marks)}
      {''.join(cells)}
    </svg>
  </div>
  <div class="heat-legend">
    <span class="tiny faint">颜色越深 = 当天聊得越久</span>
    <svg width="{len(HEAT_SCALE) * 15 - 4}" height="11" class="heat-scale" aria-hidden="true">
      {legend_cells}
    </svg>
    <span class="tiny faint">0 → {_hm(scale_max)}</span>
  </div>
  <div class="heat-facts tiny">
    <span>有对话的天数：<b>{len(days)}</b></span>
    <span>消息最多：<b>{busiest.date_key}</b>（{busiest.count} 条）</span>
    <span>聊得最久：<b>{longest.date_key}</b>（{_hm(longest.chat_minutes)}）</span>
  </div>
  <p class="tiny faint heat-note">
    口径：每个格子是一天，颜色深浅取<b>当天各段对话的时长之和</b>——
    一段对话指相邻消息间隔不超过 30 分钟的连续聊天。
    所以「发了很多条但集中在 5 分钟内」的一天颜色会很浅；
    而「早上说一句、晚上说一句」也<b>不会</b>被算成聊了一整天。
    悬停（手机上点按）可以看到当天明细。
  </p>
</div>"""


def _hm(minutes: float) -> str:
    """分钟 → 人话（用于热力图图例与悬浮提示）。"""
    if minutes < 1:
        return f"{minutes * 60:.0f} 秒"
    if minutes < 60:
        return f"{minutes:.0f} 分钟"
    return f"{minutes / 60:.1f} 小时"


# --------------------------------------------------------------------------- #
# 2. 话题词云
# --------------------------------------------------------------------------- #

#: 词云画布（逻辑坐标）；在页面上按容器宽度等比缩放
CLOUD_W = 720.0
CLOUD_H = 340.0
MIN_FONT = 12.0
MAX_FONT = 40.0


def _estimate_box(word: str, font: float) -> tuple[float, float]:
    """估算一个词的包围盒（中文字符按 1.0 em，ASCII 按 0.55 em）。"""
    w = 0.0
    for ch in word:
        w += font * (0.55 if ord(ch) < 0x2E80 else 1.0)
    return w, font * 1.22


def _layout_cloud(words: Sequence[TopicWord]) -> list[tuple[TopicWord, float, float, float]]:
    """螺旋放置 + 矩形碰撞检测，返回 ``[(词, 字号, x, y)]``。

    用固定随机种子（按词序）保证同一个输入每次生成的图一样——
    报告要可复现、可比对。
    """
    if not words:
        return []

    placed: list[tuple[float, float, float, float]] = []   # x0,y0,x1,y1
    out: list[tuple[TopicWord, float, float, float]] = []

    cx, cy = CLOUD_W / 2, CLOUD_H / 2
    rng = random.Random(20240401)

    for i, tw in enumerate(words):
        # weight 1.0 → 最大字号；0 → 最小
        font = MIN_FONT + (MAX_FONT - MIN_FONT) * (tw.weight ** 0.75)
        bw, bh = _estimate_box(tw.word, font)

        # 螺旋搜索：角度递增、半径随需要的面积增长
        found = None
        step = 0
        # 起始角按索引错开，避免所有词都往同一个方向挤
        angle0 = rng.uniform(0, math.tau) if i else -math.pi / 2
        while step < 900:
            t = step / 900.0
            radius = (max(bw, bh) * 0.5 + 6) + t * (min(CLOUD_W, CLOUD_H) * 0.62)
            angle = angle0 + step * 0.42
            x = cx + radius * math.cos(angle)
            y = cy + radius * math.sin(angle) * 0.72     # 压扁一点，更像词云
            box = (x - bw / 2, y - bh / 2, x + bw / 2, y + bh / 2)
            if (box[0] >= 4 and box[1] >= 4
                    and box[2] <= CLOUD_W - 4 and box[3] <= CLOUD_H - 4
                    and not any(_overlaps(box, p) for p in placed)):
                found = (x, y, box, bw, bh)
                break
            step += 1

        if found is None:
            continue    # 放不下就丢弃：宁可少画几个词，也不要叠在一起看不清
        x, y, box, _bw, _bh = found
        placed.append(box)
        out.append((tw, font, x, y))

    return out


def _overlaps(a: tuple[float, float, float, float],
              b: tuple[float, float, float, float]) -> bool:
    pad = 2.0
    return not (a[2] + pad <= b[0] or b[2] + pad <= a[0]
                or a[3] + pad <= b[1] or b[3] + pad <= a[1])


def _word_color(tw: TopicWord, me: str, peer: str) -> str:
    if tw.dominant == peer:
        return PALETTE["peer"]
    if tw.dominant == me:
        return PALETTE["me"]
    return PALETTE["ink_soft"]


def render_wordcloud(words: Sequence[TopicWord], me: str, peer: str,
                     total_messages: int) -> str:
    """话题词云（内联 SVG）。

    * 字号 = 词的加权频次（种子话题词有加权）；
    * 颜色 = 这个词主要由谁说的（TA 玫瑰色 / 我雾蓝色 / 双方相当灰色）；
    * 位置在 Python 里算好，输出 ``<text>`` 绝对坐标，配合 ``viewBox``
      **自适应缩放、零 JS 依赖**——手机上也不会糊或溢出。
    """
    if not words:
        return '<p class="faint">这份记录里没有提取出足够的话题关键词。</p>'

    laid = _layout_cloud(words)
    if not laid:
        return '<p class="faint">话题词太少，无法排版词云。</p>'

    texts = []
    for tw, font, x, y in laid:
        color = _word_color(tw, me, peer)
        opacity = 0.62 + 0.38 * tw.weight
        who = tw.dominant or "双方"
        texts.append(
            f'<text x="{x:.1f}" y="{y:.1f}" font-size="{font:.1f}" fill="{color}" '
            f'opacity="{opacity:.2f}" text-anchor="middle" dominant-baseline="middle" '
            f'font-weight="{600 if tw.weight > 0.6 else 500}">'
            f'<title>{esc(tw.word)}：出现 {tw.count} 次，主要由{esc(who)}说起</title>'
            f'{esc(tw.word)}</text>'
        )

    top5 = "、".join(tw.word for tw in words[:5])
    return f"""
<div class="cloud-wrap">
  <svg viewBox="0 0 {CLOUD_W:.0f} {CLOUD_H:.0f}" class="wordcloud" role="img"
       aria-label="话题词云，高频词：{esc(top5)}">
    {''.join(texts)}
  </svg>
  <div class="legend">
    <span><i style="background:{PALETTE['peer']}"></i>{esc(peer)}说得更多</span>
    <span><i style="background:{PALETTE['me']}"></i>{esc(me)}说得更多</span>
    <span><i style="background:{PALETTE['ink_soft']}"></i>双方相当</span>
  </div>
  <p class="tiny faint">
    字号 = 该词在 {total_messages} 条消息里的加权出现频次（话题种子词加权更高）；
    颜色 = 这个词主要由谁说起。做法是本地 n-gram 统计，没有分词器，
    所以偶尔会出现切歪的片段——只当参考，不当结论。
  </p>
</div>"""


# --------------------------------------------------------------------------- #
# 3. 「TA 是一个怎样的人」
# --------------------------------------------------------------------------- #

_TONE_COLOR = {
    "warm": PALETTE["peer"],
    "cool": PALETTE["me"],
    "neutral": PALETTE["ink_soft"],
}

_TONE_LABEL = {
    "warm": "偏积极",
    "cool": "偏疏离",
    "neutral": "中性",
}


def render_persona(persona: Sequence[Persona], peer: str) -> str:
    """人物画像卡片墙。

    每一张卡只陈述**可观测的行为**并附上支撑数字，
    页面顶部有一句明确的免责：这不是性格判断。
    """
    if not persona:
        return ('<p class="faint">能读到的行为样本太少，画不出可靠的画像。'
                "（这不代表 TA 没有特点，只代表这份记录不够长。）</p>")

    cards = []
    for p in persona:
        color = _TONE_COLOR.get(p.tone, PALETTE["ink_soft"])
        bar = max(6.0, min(100.0, p.strength * 100))
        cards.append(f"""
    <article class="persona-card">
      <div class="persona-head">
        <span class="persona-label" style="color:{color}">{esc(p.label)}</span>
        <span class="persona-tone tiny" style="background:{color}1f;color:{color}">
          {esc(_TONE_LABEL.get(p.tone, '中性'))}</span>
      </div>
      <p class="persona-text">{esc(p.evidence_text)}</p>
      <div class="persona-bar"><div style="width:{bar:.0f}%;background:{color}"></div></div>
      <span class="persona-support tiny faint">{esc(p.support)}</span>
    </article>""")

    return f"""
<div class="persona-wrap">
  <p class="persona-note tiny">
    下面每一条都只描述 <b>{esc(peer)}</b> 在这份记录里的<b>可观测行为</b>，
    并附上支撑数字。它<strong>不是性格判断</strong>，也不代表 TA 在别的关系里也这样——
    一个人回得慢，可能只是不擅长用文字说话。
  </p>
  <div class="persona-grid">{''.join(cards)}</div>
</div>"""


# --------------------------------------------------------------------------- #
# 辅助小图：24 小时活跃度、月度柱状
# --------------------------------------------------------------------------- #

def render_hour_bars(hours: Sequence[HourBucket], me: str, peer: str) -> str:
    """24 小时活跃度柱状图（堆叠：TA / 我）。"""
    if not hours or sum(h.count for h in hours) == 0:
        return '<p class="faint">没有可用于统计时段的数据。</p>'

    w, h = 720, 190
    pad_l, pad_r, pad_t, pad_b = 34, 10, 14, 30
    plot_w = w - pad_l - pad_r
    plot_h = h - pad_t - pad_b
    n = 24
    bw = plot_w / n
    peak = max(x.count for x in hours) or 1

    bars = []
    for i, hb in enumerate(hours):
        x = pad_l + i * bw
        total_h = plot_h * (hb.count / peak)
        peer_h = plot_h * (hb.peer_count / peak)
        me_h = plot_h * (hb.me_count / peak)
        y_top = pad_t + plot_h - total_h
        if peer_h > 0:
            bars.append(
                f'<rect x="{x + 1:.1f}" y="{pad_t + plot_h - peer_h:.1f}" '
                f'width="{bw - 2:.1f}" height="{peer_h:.1f}" fill="{PALETTE["peer"]}" '
                f'opacity="0.85"><title>{i:02d}:00 · {esc(peer)} {hb.peer_count} 条</title></rect>'
            )
        if me_h > 0:
            bars.append(
                f'<rect x="{x + 1:.1f}" y="{y_top:.1f}" '
                f'width="{bw - 2:.1f}" height="{me_h:.1f}" fill="{PALETTE["me"]}" '
                f'opacity="0.85"><title>{i:02d}:00 · {esc(me)} {hb.me_count} 条</title></rect>'
            )

    labels = []
    for i in range(0, 24, 3):
        x = pad_l + i * bw + bw / 2
        labels.append(
            f'<text x="{x:.1f}" y="{h - pad_b + 15}" font-size="9" text-anchor="middle" '
            f'fill="{PALETTE["ink_faint"]}">{i:02d}</text>'
        )

    # 深夜时段底色
    shade = []
    for i in range(24):
        if i >= 23 or i < 3:
            shade.append(
                f'<rect x="{pad_l + i * bw:.1f}" y="{pad_t}" width="{bw:.1f}" '
                f'height="{plot_h}" fill="{PALETTE["peer"]}" opacity="0.05"/>'
            )

    grid = []
    for k in range(4):
        y = pad_t + plot_h * k / 3
        grid.append(f'<line x1="{pad_l}" y1="{y:.1f}" x2="{w - pad_r}" y2="{y:.1f}" '
                    f'stroke="{PALETTE["line"]}" stroke-width="1"/>')

    return f"""
<svg viewBox="0 0 {w} {h}" class="hourbars" role="img" aria-label="24 小时活跃度">
  {''.join(shade)}{''.join(grid)}{''.join(bars)}{''.join(labels)}
</svg>
<div class="legend">
  <span><i style="background:{PALETTE['peer']}"></i>{esc(peer)}</span>
  <span><i style="background:{PALETTE['me']}"></i>{esc(me)}</span>
  <span class="faint">淡色区域 = 深夜 23:00–03:00</span>
</div>"""


def render_month_bars(months: Sequence[MonthStat], me: str, peer: str) -> str:
    """月度消息量柱状图（堆叠：TA / 我），标注最多与最少。"""
    if not months:
        return '<p class="faint">没有可用于统计月份的数据。</p>'

    w = 720
    pad_l, pad_r, pad_t, pad_b = 34, 10, 16, 34
    plot_w = w - pad_l - pad_r
    # 月份多时增高，保证柱子不被压扁
    plot_h = 150
    h = plot_h + pad_t + pad_b
    n = len(months)
    bw = plot_w / n
    peak = max(m.count for m in months) or 1
    peak_label = max(months, key=lambda m: m.count).label
    low_label = min(months, key=lambda m: m.count).label

    bars = []
    for i, m in enumerate(months):
        x = pad_l + i * bw
        total_h = plot_h * (m.count / peak)
        peer_h = plot_h * (m.peer_count / peak)
        me_h = plot_h * (m.me_count / peak)
        bars.append(
            f'<rect x="{x + 1.5:.1f}" y="{pad_t + plot_h - peer_h:.1f}" '
            f'width="{bw - 3:.1f}" height="{peer_h:.1f}" fill="{PALETTE["peer"]}" '
            f'opacity="0.85"><title>{esc(m.label)} · {esc(peer)} {m.peer_count} 条</title></rect>'
        )
        bars.append(
            f'<rect x="{x + 1.5:.1f}" y="{pad_t + plot_h - total_h:.1f}" '
            f'width="{bw - 3:.1f}" height="{me_h:.1f}" fill="{PALETTE["me"]}" '
            f'opacity="0.85"><title>{esc(m.label)} · {esc(me)} {m.me_count} 条</title></rect>'
        )

    labels = []
    for i, m in enumerate(months):
        # 月份多时隔几个标一次，避免重叠
        step = max(1, n // 12)
        if i % step and i != n - 1:
            continue
        x = pad_l + i * bw + bw / 2
        y, mm = m.label.split("-")
        labels.append(
            f'<text x="{x:.1f}" y="{h - pad_b + 16}" font-size="9" text-anchor="middle" '
            f'fill="{PALETTE["ink_faint"]}">{int(mm)}月</text>'
        )

    # 标出最多/最少的月份
    marks = []
    for i, m in enumerate(months):
        x = pad_l + i * bw + bw / 2
        if m.label == peak_label:
            marks.append(f'<text x="{x:.1f}" y="{pad_t - 4}" font-size="9" '
                         f'text-anchor="middle" fill="{PALETTE["accent"]}">最多</text>')
        elif m.label == low_label and n > 2:
            marks.append(f'<text x="{x:.1f}" y="{pad_t - 4}" font-size="9" '
                         f'text-anchor="middle" fill="{PALETTE["ink_faint"]}">最少</text>')

    return f"""
<svg viewBox="0 0 {w} {h}" class="monthbars" role="img" aria-label="月度消息量">
  {''.join(bars)}{''.join(labels)}{''.join(marks)}
</svg>
<div class="legend">
  <span><i style="background:{PALETTE['peer']}"></i>{esc(peer)}</span>
  <span><i style="background:{PALETTE['me']}"></i>{esc(me)}</span>
</div>"""