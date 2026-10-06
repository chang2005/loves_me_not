"""命令行入口：``python -m loves_me_not <命令>``。

子命令：

* ``analyze`` —— 解析 → 分析 → 打分 → 生成单文件 HTML 报告
* ``inspect`` —— 只做解析，把识别到的说话人、消息数、时间范围打印出来
  （**强烈建议先跑这个**：输入错了，分数就没有意义）

设计约定：出错时返回非零退出码并打印可操作的提示，绝不悄悄生成一份
基于错误输入的「看起来正常」的报告。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__, metrics, parser, report, scoring


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
    if not args.me and not args.peer:
        _say("提示：你没有指定 --me，下面按「首次出现的说话人」分配，"
             "如果反了请用 --me/--peer 明确指定。")
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

    # 4. 报告
    out_path = Path(args.output) if args.output else Path("out") / "report.html"
    try:
        written = report.write_report(
            out_path, analysis, result, conv.report, do_redact=args.redact
        )
    except OSError as exc:
        return _fail(f"写入报告失败：{exc}")

    if args.redact:
        _say("已按 --redact 对报告中的手机号、身份证、卡号、邮箱、账号、地址做打码。")
        _say("（这只能降低风险，不能保证匿名——聊天内容本身仍可能暴露身份。）")

    if args.json:
        try:
            Path(args.json).write_text(
                report.build_json(analysis, result), encoding="utf-8"
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
    ins.add_argument("--format", choices=("auto", "text", "csv"), default="auto",
                     help="强制指定格式（默认按扩展名与内容自动判断）")
    ins.set_defaults(func=cmd_inspect)

    ana = sub.add_parser("analyze", help="分析聊天记录并生成 HTML 报告")
    ana.add_argument("input", help="聊天记录文件（txt / csv / log）")
    ana.add_argument("--me", help="「我」的昵称（必须与文件里出现的写法一致）")
    ana.add_argument("--peer", help="「TA」的昵称")
    ana.add_argument("-o", "--output", default="out/report.html",
                     help="报告输出路径（默认 out/report.html）")
    ana.add_argument("--json", help="同时输出机器可读的 JSON 结果")
    ana.add_argument("--format", choices=("auto", "text", "csv"), default="auto",
                     help="强制指定格式（默认自动判断）")
    ana.add_argument("--redact", action="store_true",
                     help="对报告中的手机号、身份证号等敏感串做打码（昵称保留）")
    ana.set_defaults(func=cmd_analyze)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())