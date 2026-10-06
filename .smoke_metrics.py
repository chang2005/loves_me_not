"""开发期自检脚本：对 samples 跑一遍 metrics，打印各维度分数。"""

import sys

sys.path.insert(0, r"E:\loves-me-not")
from loves_me_not import metrics, parser  # noqa: E402

CASES = [
    ("sample_wechat_warm.txt", "我", "阿澈"),
    ("sample_wechat_cooling.txt", "我", "阿澈"),
    ("sample_memotrace.csv", "我", "阿澈"),
    ("sample_tiny.txt", "我", "阿澈"),
]

for name, me, peer in CASES:
    conv = parser.parse_file(rf"E:\loves-me-not\samples\{name}")
    a = metrics.analyze(conv, me, peer)
    print("==", name, "| 有效消息", a.total_real, "| 跨度", a.days_span, "天")
    for _k, d in a.dimensions.items():
        sp = "NA" if d.score_peer is None else f"{d.score_peer:.2f}"
        sm = "NA" if d.score_me is None else f"{d.score_me:.2f}"
        print(f"   {d.label:16} peer={sp:>4} me={sm:>4}  {d.summary[:64]}")
    print("   periods:", len(a.periods), "| caveats:", len(a.caveats))
    for c in a.caveats:
        print("     -", c)