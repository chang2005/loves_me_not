"""命令行入口：``python -m loves_me_not <命令>``。

子命令：

* ``analyze`` —— 解析 → 分析 → 打分 → 生成单文件 HTML 报告
* ``inspect`` —— 只做解析，把识别到的说话人、消息数、时间范围打印出来
  （**强烈建议先跑这个**：输入错了，分数就没有意义）
* ``balance`` —— 独立的投入对称性诊断（不参与总分）
* ``context`` —— 关键窗口采样与有预算的上下文检索
* ``memory`` —— 可选、需显式同意的本地关系记忆（默认不建库）

设计约定：出错时返回非零退出码并打印可操作的提示，绝不悄悄生成一份
基于错误输入的「看起来正常」的报告。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__, balance, context, metrics, parser, report, scoring


# --------------------------------------------------------------------------- #
# 输出辅助
# --------------------------------------------------------------------------- #

def _say(msg: str = "") -> None:
    print(msg)


def _fail(msg: str, code: int = 2) -> int:
    print(f"错误：{msg}", file=sys.stderr)
    return code


# --------------------------------------------------------------------------- #
# inspect
# --------------------------------------------------------------------------- #

def cmd_inspect(args: argparse.Namespace) -> int:
    try:
        conv = parser.parse_file(args.input, fmt=args.format)
    except parser.ParseError as exc:
        return _fail(str(exc))

    _say(parser.describe(conv))
    _say("")
    _say("前几条消息（请核对发言人识别是否正确）：")
    for m in conv.messages[:8]:
        ts = m.timestamp.strftime("%Y-%m-%d %H:%M:%S") if m.timestamp else "（无时间）"
        tag = "[系统] " if m.is_system else ("[媒体] " if m.is_media else "")
        text = m.text.replace("\n", " ⏎ ")
        _say(f"  {ts}  {m.speaker}  {tag}{text[:60]}")

    speakers = conv.speakers
    if len(speakers) >= 2:
        _say("")
        _say(f"下一步：python -m loves_me_not analyze \"{args.input}\" "
             f"--me \"{speakers[0]}\" --peer \"{speakers[1]}\"")
        _say("（如果「我」不是第一个说话人，把 --me/--peer 换成正确的昵称）")
    return 0


# --------------------------------------------------------------------------- #
# analyze
# --------------------------------------------------------------------------- #

def cmd_analyze(args: argparse.Namespace) -> int:
    # 1. 解析
    try:
        conv = parser.parse_file(args.input, fmt=args.format)
    except parser.ParseError as exc:
        return _fail(str(exc))

    _say(parser.describe(conv))
    _say("")

    # 2. 定人
    assumed_me = not args.me
    if assumed_me and not args.peer:
        _say("提示：你没有指定 --me，下面按「首次出现的说话人」分配，"
             "如果反了请用 --me/--peer 明确指定。报告顶部也会提示这一点。")
    try:
        me, peer = metrics.choose_pair(conv, args.me, args.peer)
    except ValueError as exc:
        return _fail(str(exc))

    if me == peer:
        return _fail("「我」和「TA」不能是同一个人，请检查 --me/--peer。")

    _say(f"分析对象：「我」= {me}   「TA」= {peer}")
    _say("")

    # 3. 分析 + 打分（样本不足会在这里明确降级）
    try:
        analysis = metrics.analyze(conv, me, peer)
    except ValueError as exc:
        return _fail(str(exc))

    result = scoring.score(analysis)

    # 3.5 可选的对称性诊断（旁路指标，绝不改总分）
    balance_result = None
    if args.balance:
        balance_result = balance.compute_balance(conv, analysis, me, peer)

    # 4. 报告
    out_path = Path(args.output) if args.output else Path("out") / "report.html"
    try:
        written = report.write_report(
            out_path, analysis, result, conv.report,
            do_redact=args.redact, assumed=assumed_me,
        )
    except OSError as exc:
        return _fail(f"写入报告失败：{exc}")

    if args.redact:
        _say("已按 --redact 对报告中的手机号、身份证、卡号、邮箱、账号、地址做打码。")
        _say("（这只能降低风险，不能保证匿名——聊天内容本身仍可能暴露身份。）")

    if args.json:
        try:
            Path(args.json).write_text(
                report.build_json(analysis, result, balance_obj=balance_result),
                encoding="utf-8",
            )
        except OSError as exc:
            return _fail(f"写入 JSON 失败：{exc}")

    # 5. 控制台摘要
    _say("=" * 66)
    if result.insufficient:
        _say("⚠ 样本不足：以下分数已降级，仅供参考，不构成结论。")
    _say(f"情感投入指数（{peer} 对你的投入度）：{result.total} / 100")
    _say(f"等级结论：{result.tier.title} —— {result.tier.one_liner}")
    _say(f"可信度：{result.confidence.label}（{result.confidence.score:.2f}）")
    if result.peer.score is not None:
        _say(f"  · {peer} 的投入度：{result.peer.score:.1f}")
    if result.me.score is not None:
        _say(f"  · {me} 的投入度：{result.me.score:.1f}")
    if result.pair.score is not None:
        _say(f"  · 双向互动综合：{result.pair.score:.1f}")

    if result.top_positive:
        _say("  主要加分项：" + "、".join(
            f"{l}({s * 100:.0f})" for l, s, _w in result.top_positive))
    if result.top_negative:
        _say("  主要拖后腿：" + "、".join(
            f"{l}({s * 100:.0f})" for l, s, _w in result.top_negative))

    if balance_result is not None:
        _say("")
        _say(balance.format_text(balance_result, me, peer))

    if analysis.caveats:
        _say("")
        _say("数据说明：")
        for c in analysis.caveats:
            _say(f"  - {c}")

    _say("=" * 66)
    _say(f"报告已生成：{written.resolve()}")
    _say("（单文件 HTML，双击即可打开；全程本地处理，没有任何数据离开这台设备。）")
    if args.json:
        _say(f"JSON 结果：{Path(args.json).resolve()}")
    return 0


# --------------------------------------------------------------------------- #
# balance
# --------------------------------------------------------------------------- #

def _resolve_pair(conv, me: str | None, peer: str | None) -> tuple[str, str]:
    try:
        pair = metrics.choose_pair(conv, me, peer)
    except ValueError as exc:
        raise ValueError(str(exc)) from exc
    if pair[0] == pair[1]:
        raise ValueError("「我」和「TA」不能是同一个人，请检查 --me/--peer。")
    return pair


def cmd_balance(args: argparse.Namespace) -> int:
    try:
        conv = parser.parse_file(args.input, fmt=args.format)
    except parser.ParseError as exc:
        return _fail(str(exc))
    try:
        me, peer = _resolve_pair(conv, args.me, args.peer)
    except ValueError as exc:
        return _fail(str(exc))
    try:
        analysis = metrics.analyze(conv, me, peer)
    except ValueError as exc:
        return _fail(str(exc))

    result = balance.compute_balance(conv, analysis, me, peer)
    _say(parser.describe(conv))
    _say("")
    _say(balance.format_text(result, me, peer))
    if args.json:
        try:
            Path(args.json).write_text(
                json.dumps(result.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            return _fail(f"写入 JSON 失败：{exc}")
        _say(f"JSON 结果：{Path(args.json).resolve()}")
    return 0


# --------------------------------------------------------------------------- #
# context
# --------------------------------------------------------------------------- #

def cmd_context(args: argparse.Namespace) -> int:
    try:
        conv = parser.parse_file(args.input, fmt=args.format)
    except parser.ParseError as exc:
        return _fail(str(exc))

    msgs = conv.messages
    try:
        if args.mode == "digest":
            payload = {
                "digest": context.messages_digest(msgs),
                "messages": len(msgs),
                "note": "把这个 digest 传给 query 模式的 --expected-digest，可防止输入被替换。",
            }
        elif args.mode == "conflict":
            win = context.find_conflict_window(msgs, window=args.window)
            payload = context.describe_windows(
                [win] if win else [], messages=msgs, include_messages=True
            )
        elif args.mode == "repair":
            wins = context.find_repair_moments(msgs)
            payload = context.describe_windows(
                wins[:args.limit], messages=msgs, include_messages=True
            )
        else:
            payload = context.query_context(
                msgs,
                keyword=args.keyword,
                speaker=args.speaker,
                since=args.since,
                until=args.until,
                before=args.before,
                after=args.after,
                max_matches=args.max_matches,
                max_messages=args.max_messages,
                max_chars=args.max_chars,
                expected_digest=args.expected_digest,
            )
    except context.ContextError as exc:
        return _fail(str(exc))

    text = json.dumps(payload, ensure_ascii=False, indent=2)
    _say(text[:6000])
    if len(text) > 6000:
        _say(f"...（已截断显示，共 {len(text)} 字符）")
    if args.json:
        try:
            Path(args.json).write_text(text, encoding="utf-8")
        except OSError as exc:
            return _fail(f"写入 JSON 失败：{exc}")
        _say(f"JSON 结果：{Path(args.json).resolve()}")
    return 0


# --------------------------------------------------------------------------- #
# memory（可选旁路，默认不建库）
# --------------------------------------------------------------------------- #

def cmd_memory(args: argparse.Namespace) -> int:
    from . import memory  # 延迟导入：不用记忆功能时连 sqlite3 都不碰
    return memory.main(args.memory_args or ["status"])


# --------------------------------------------------------------------------- #
# 参数
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="loves-me-not",
        description="TA 到底爱不爱我 —— 在本地分析聊天记录，生成单文件 HTML 报告。",
        epilog="数据全程只在本地处理，不上传、不联网。结果仅供参考与自我反思。",
    )
    p.add_argument("-V", "--version", action="version", version=f"loves-me-not {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    ins = sub.add_parser("inspect", help="只解析输入文件，核对发言人识别是否正确")
    ins.add_argument("input", help="聊天记录文件（txt / csv / log）")
    ins.add_argument("--format", choices=("auto", "text", "csv", "json", "html"), default="auto",
                     help="强制指定格式（默认按扩展名与内容自动判断；json/html 走结构化解析）")
    ins.set_defaults(func=cmd_inspect)

    ana = sub.add_parser("analyze", help="分析聊天记录并生成 HTML 报告")
    ana.add_argument("input", help="聊天记录文件（txt / csv / log）")
    ana.add_argument("--me", help="「我」的昵称（必须与文件里出现的写法一致）")
    ana.add_argument("--peer", help="「TA」的昵称")
    ana.add_argument("-o", "--output", default="out/report.html",
                     help="报告输出路径（默认 out/report.html）")
    ana.add_argument("--json", help="同时输出机器可读的 JSON 结果")
    ana.add_argument("--format", choices=("auto", "text", "csv", "json", "html"), default="auto",
                     help="强制指定格式（默认自动判断；json/html 走结构化解析）")
    ana.add_argument("--redact", action="store_true",
                     help="对报告中的手机号、身份证号等敏感串做打码（昵称保留）")
    ana.add_argument("--balance", action="store_true",
                     help="附带输出独立的投入对称性诊断（不改总分、不改报告版式）")
    ana.set_defaults(func=cmd_analyze)

    bal = sub.add_parser("balance", help="独立诊断双方投入是否对等（不影响总分）")
    bal.add_argument("input", help="聊天记录文件")
    bal.add_argument("--me", help="「我」的昵称")
    bal.add_argument("--peer", help="「TA」的昵称")
    bal.add_argument("--format", choices=("auto", "text", "csv", "json", "html"), default="auto")
    bal.add_argument("--json", help="把对称性诊断写入 JSON 文件")
    bal.set_defaults(func=cmd_balance)

    ctx = sub.add_parser("context", help="查关键窗口 / 按关键词取有预算的上下文")
    ctx.add_argument("input", help="聊天记录文件")
    ctx.add_argument("mode", nargs="?", default="query",
                     choices=("query", "conflict", "repair", "digest"),
                     help="query（默认）/ conflict / repair / digest")
    ctx.add_argument("--keyword", help="query 模式：关键词（不区分大小写）")
    ctx.add_argument("--speaker", help="query 模式：只看某位说话人")
    ctx.add_argument("--since", help="query 模式：起始时间（ISO 8601）")
    ctx.add_argument("--until", help="query 模式：结束时间（ISO 8601）")
    ctx.add_argument("--before", type=int, default=10, help="锚点前取几条（默认 10）")
    ctx.add_argument("--after", type=int, default=10, help="锚点后取几条（默认 10）")
    ctx.add_argument("--max-matches", type=int, default=5, help="最多取几个锚点（默认 5）")
    ctx.add_argument("--max-messages", type=int, default=100, help="最多返回多少条（默认 100）")
    ctx.add_argument("--max-chars", type=int, default=20000, help="最多返回多少字符（默认 20000）")
    ctx.add_argument("--expected-digest", help="期望的数据摘要，用于防止输入已被替换")
    ctx.add_argument("--window", type=int, default=100, help="conflict 模式的滑窗宽度")
    ctx.add_argument("--limit", type=int, default=10, help="repair 模式最多显示几段")
    ctx.add_argument("--format", choices=("auto", "text", "csv", "json", "html"), default="auto")
    ctx.add_argument("--json", help="把查询结果写入 JSON 文件")
    ctx.set_defaults(func=cmd_context)

    mem = sub.add_parser("memory", help="可选的本地关系记忆（默认不启用、不建库）")
    mem.add_argument("memory_args", nargs=argparse.REMAINDER,
                     help="子命令与参数原样透传，例如：memory status、"
                          "memory enable --confirm、memory apply --json '{...}'")
    mem.set_defaults(func=cmd_memory)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())