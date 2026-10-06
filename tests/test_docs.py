"""文档一致性护栏：中英文成对、互相有切换按钮、多语言结构对齐。

这些断言存在的理由很实际：文档是最容易「改了一半」的地方——
中文加了一节、英文忘了跟；或者切换按钮指向了自己，读者点进去原地踏步。
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: 中英文成对的文档（相对仓库根）
PAIRS = (
    ("README.md", "README.en.md"),
    ("SKILL.md", "SKILL.en.md"),
    ("demo/README.md", "demo/README.en.md"),
    ("samples/README.md", "samples/README.en.md"),
)

FENCE = "`" * 3


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _count(text: str, pattern: str) -> int:
    return len(re.findall(pattern, text, re.M))


class TestBilingualDocs(unittest.TestCase):
    def test_every_pair_exists(self):
        for zh, en in PAIRS:
            with self.subTest(pair=f"{zh} / {en}"):
                self.assertTrue((ROOT / zh).is_file(), f"缺少 {zh}")
                self.assertTrue((ROOT / en).is_file(), f"缺少 {en}")

    def test_language_toggle_points_at_the_counterpart(self):
        """每个文档都要有指向另一语言版本的切换链接，且不能指向自己。"""
        for zh, en in PAIRS:
            for rel, target in ((zh, en), (en, zh)):
                with self.subTest(doc=rel):
                    body = _read(rel)
                    name = Path(target).name
                    self.assertIn(f"]({name})", body,
                                  f"{rel} 缺少指向 {name} 的切换链接")
                    # 指向自己说明写错了，读者点进去原地不动
                    self.assertNotIn(f"]({Path(rel).name})", body,
                                     f"{rel} 的切换链接指向了自己")

    def test_structure_is_aligned(self):
        """标题、代码块、表格、图片、折叠块数量必须一致，防止只改一边。"""
        for zh, en in PAIRS:
            a, b = _read(zh), _read(en)
            for label, pattern in (
                ("二级标题", r"^## "),
                ("三级标题", r"^### "),
                ("表格", r"^\|\s*-{2,}"),
            ):
                with self.subTest(pair=zh, kind=label):
                    self.assertEqual(_count(a, pattern), _count(b, pattern),
                                     f"{zh} 与 {en} 的{label}数量不一致")
            for label, counter in (
                ("代码块", lambda t: t.count(FENCE) // 2),
                ("图片", lambda t: t.count("<img")),
                ("折叠块", lambda t: t.count("<details")),
            ):
                with self.subTest(pair=zh, kind=label):
                    self.assertEqual(counter(a), counter(b),
                                     f"{zh} 与 {en} 的{label}数量不一致")

    def test_code_fences_are_balanced(self):
        for zh, en in PAIRS:
            for rel in (zh, en):
                with self.subTest(doc=rel):
                    self.assertEqual(_read(rel).count(FENCE) % 2, 0,
                                     f"{rel} 的代码围栏没有配平")

    def test_details_tags_are_balanced(self):
        for zh, en in PAIRS:
            for rel in (zh, en):
                with self.subTest(doc=rel):
                    body = _read(rel)
                    self.assertEqual(body.count("<details"), body.count("</details>"),
                                     f"{rel} 的 <details> 没有配平")

    def test_local_references_resolve(self):
        """本地图片与链接必须真实存在；跨文件锚点要能在目标文档里找到。"""
        def slug(text: str) -> str:
            text = re.sub(r"[^\w\s\u4e00-\u9fff-]", "", text.strip(), flags=re.UNICODE)
            return text.replace(" ", "-").lower()

        headings = {
            p.resolve(): {slug(l[3:]) for l in p.read_text(encoding="utf-8").splitlines()
                          if l.startswith("## ")}
            for p in ROOT.rglob("*.md") if ".git" not in p.parts
        }

        for zh, en in PAIRS:
            for rel in (zh, en):
                doc = ROOT / rel
                body = _read(rel)
                with self.subTest(doc=rel):
                    for m in re.finditer(r'src="([^"]+)"', body):
                        u = m.group(1)
                        if u.startswith(("http", "data:")):
                            continue
                        self.assertTrue((doc.parent / u).resolve().exists(),
                                        f"{rel} 引用了不存在的图片 {u}")
                    for m in re.finditer(r"\]\(([^)]+)\)", body):
                        u = m.group(1)
                        if u.startswith(("http", "mailto:")):
                            continue
                        path_part, _, anchor = u.partition("#")
                        if path_part:
                            target = (doc.parent / path_part).resolve()
                            self.assertTrue(target.exists(),
                                            f"{rel} 链接到不存在的 {path_part}")
                            if anchor and target.suffix == ".md":
                                self.assertIn(anchor, headings.get(target, set()),
                                              f"{rel} -> {path_part}#{anchor} 锚点不存在")
                        elif anchor:
                            self.assertIn(anchor, headings[doc.resolve()],
                                          f"{rel} 的自身锚点 #{anchor} 不存在")

    def test_english_docs_are_mostly_english(self):
        """英文文档里不该夹带大段中文（代码示例里的数据值除外）。"""
        for _, en in PAIRS:
            body = _read(en)
            # 去掉代码块后统计中文字符
            stripped = re.sub(r"```.*?```", "", body, flags=re.S)
            han = len(re.findall(r"[\u4e00-\u9fff]", stripped))
            with self.subTest(doc=en):
                self.assertLess(han, 120,
                                f"{en} 里残留了 {han} 个中文字符，疑似漏译")


if __name__ == "__main__":
    unittest.main()
