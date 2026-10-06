"""开发期自检：解析 → 维度 → 打分，打印完整结论。"""

import sys

sys.path.insert(0, r"E:\loves-me-not")
from loves_me_not import metrics, parser, scoring  # noqa: E402

CASES = [
    ("sample_wechat_warm.txt", "我", "阿澈"),
    ("sample_wechat_cooling.txt", "我", "阿澈"),
    ("sample_memotrace.csv", "我", "阿澈"),
    ("sample_tiny.txt", "我", "阿澈"),
]

for name, me, peer in CASES:
    conv = parser.parse_file(rf"E:\loves-me-not\samples\{name}")
    a = metrics.analyze(conv, me, peer)
    res = scoring.score(a)
    print("=" * 78)
    print(f"{name}  →  {res.total} 分  [{res.tier.title}]  样本不足={res.insufficient}")
    print(f"  raw={res.raw_total:.1f}  peer={res.peer.score and round(res.peer.score,1)}"
          f"  me={res.me.score and round(res.me.score,1)}"
          f"  pair={res.pair.score and round(res.pair.score,1)}")
    print(f"  可信度 {res.confidence.label} ({res.confidence.score:.2f})")
    print(f"  加分项: {[(l, round(s,2)) for l, s, _w in res.top_positive]}")
    print(f"  拖后腿: {[(l, round(s,2)) for l, s, _w in res.top_negative]}")
    print(f"  举证 {len(res.evidence)} 条:")
    for e in res.evidence[:4]:
        print(f"    - [{e.when()}] {e.speaker}: {e.text[:34]!r}  ({e.reason})")
    print(f"  安慰: 《{res.comfort_title}》")