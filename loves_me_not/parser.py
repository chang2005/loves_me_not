"""聊天记录容错解析器。

目标：把「微信 / QQ 导出的纯文本（txt / csv / log）」这类格式各异、脏得很真实的
文件，统一解析成 ``list[Message]``，并且**不猜、不编**——解析不出来就明确报告，
而不是伪造内容。

支持的典型形态（同一文件里混着出现也能处理）::

    # A. 微信 PC 端复制 / 多数导出工具的 txt
    2023-04-01 21:33:02 阿澈
    到家了跟我说一声

    # B. 带 QQ 号
    2023-04-01 21:33:02 阿澈(12345678)
    到家了跟我说一声

    # C. 方括号时间戳
    [2023-04-01 21:33:02] 阿澈
    到家了跟我说一声

    # D. 时间戳与昵称同一行、正文跟在后面
    阿澈 2023-04-01 21:33:02
    到家了跟我说一声

    # E. 只有时间的 txt（同一天内的导出）
    [21:33:02] 阿澈
    到家了跟我说一声

    # F. 昵称: 正文（很多工具转出来的形式）
    阿澈: 到家了跟我说一声

    # G. CSV（留痕 MemoTrace / 微信导出 / 自建脚本都可能）
    时间,发送者,内容
    2023-04-01 21:33:02,阿澈,到家了跟我说一声
"""

from __future__ import annotations

import codecs
import csv
import io
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Iterator, Sequence

# --------------------------------------------------------------------------- #
# 常量
# --------------------------------------------------------------------------- #

#: 媒体 / 非文本内容的方括号标记（微信、QQ 通用）
MEDIA_TAGS = {
    "图片", "照片", "图像", "image", "photo", "pic",
    "表情", "动画表情", "emoji", "sticker",
    "视频", "video", "语音", "voice", "音频", "audio",
    "文件", "file", "文档", "doc",
    "链接", "link", "网页", "分享", "音乐", "歌曲",
    "位置", "location", "地图", "名片", "card", "转账", "红包",
    "聊天记录", "合并转发", "引用", "回复", "动画",
}

#: 系统提示的典型模式（命中即视为系统消息，不计入情绪与字数统计）
SYSTEM_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(p) for p in (
        r"撤回了一条消息",
        r"撤回了一條訊息",
        r"^你撤回",
        r"^.*?\s*邀请.*?加入了群聊",
        r"加入了群聊",
        r"加入了本群",
        r"退出了群聊",
        r"移出了群聊",
        r"邀请你加入了群聊",
        r"^\s*以上是打招呼的内容",
        r"^\s*我通过了你的朋友验证请求",
        r"^\s*你已添加了.*现在可以开始聊天了",
        r"^\s*对方已开启朋友验证",
        r"^\s*消息已发出，但被对方拒收了",
        r"^\s*「.*」撤回了一条消息",
        r"^\s*红包已被领取",
        r"^\s*与.*的语音通话",
        r"^\s*语音通话.*已结束",
        r"^\s*-{2,}\s*$",
    )
)

#: 疑问句特征：问号、常见疑问词、语气助词结尾
QUESTION_MARKS = "?？"
QUESTION_WORDS: tuple[str, ...] = (
    "吗", "呢", "嘛", "吧", "么",
    "什么", "怎么", "怎样", "咋", "如何", "为啥", "为什么",
    "哪", "哪儿", "哪里", "哪个", "哪些",
    "谁", "几点", "多久", "多少", "几个",
    "是不是", "有没有", "能不能", "可不可以", "要不要", "行不行",
    "好不好", "对不对", "好不好呀", "好不好嘛",
    "觉得", "陪我", "愿不愿意", "想不想",
)

# 时间戳正则片段（具名引用，避免将来插入新模式时下标错位）
_DATE_FULL = re.compile(
    r"(?<![\d])(?P<y>\d{4})\s*[-/.年]\s*(?P<m>\d{1,2})\s*[-/.月]\s*(?P<d>\d{1,2})\s*日?"
)
_DATE_CJK = re.compile(
    r"(?<![\d])(?P<y>\d{4})\s*年\s*(?P<m>\d{1,2})\s*月\s*(?P<d>\d{1,2})\s*日?"
)
#: 无年份：04-01 / 4/1。分隔符不含 : ，避免把 21:33 误认成 21 月 33 日。
_DATE_SHORT = re.compile(
    r"(?<![\d:.])(?P<m>\d{1,2})\s*[-/.]\s*(?P<d>\d{1,2})(?![\d:])"
)

#: 全部日期模式，**按可靠性降序**（越靠前越具体）
_DATE_PATTERNS: tuple[re.Pattern[str], ...] = (_DATE_FULL, _DATE_CJK, _DATE_SHORT)

_TIME_RE = re.compile(
    r"(?<![\d:：])(?P<h>[01]?\d|2[0-3])\s*[:：]\s*(?P<mi>[0-5]\d)(?:\s*[:：]\s*(?P<s>[0-5]\d))?(?![\d:：])"
)

#: 上午 / 下午 / 晚上 之类的前缀
_AMPM_RE = re.compile(r"(?P<ap>上午|下午|中午|凌晨|早上|晚上|夜里|傍晚)\s*$")


class ParseError(Exception):
    """无法解析输入文件时抛出。绝不返回伪造结果。"""


# --------------------------------------------------------------------------- #
# 数据结构
# --------------------------------------------------------------------------- #

@dataclass
class Message:
    """一条聊天消息。"""

    index: int
    speaker: str
    timestamp: datetime | None
    text: str
    #: 原始行号（便于用户回到原文件核对）
    line_no: int = 0
    #: 系统消息 / 撤回提示等
    is_system: bool = False
    #: 纯媒体消息（图片、语音…），text 保留标记但内容为空
    is_media: bool = False
    #: 由续行拼接而成的多行消息
    multiline: bool = False

    @property
    def char_count(self) -> int:
        """消息长度（字符数，去掉首尾空白）。"""
        return len(self.text.strip())

    @property
    def is_empty(self) -> bool:
        return self.is_media or not self.text.strip()

    @property
    def is_question(self) -> bool:
        return looks_like_question(self.text)

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        ts = self.timestamp.strftime("%Y-%m-%d %H:%M:%S") if self.timestamp else "??"
        return f"<Message #{self.index} {ts} {self.speaker!r} {self.text[:18]!r}>"


@dataclass
class ParseReport:
    """解析过程的自我报告——用来向用户交代「我到底读到了什么」。"""

    path: str = ""
    encoding: str = ""
    file_kind: str = ""
    total_lines: int = 0
    parsed_messages: int = 0
    skipped_lines: int = 0
    inferred_timestamps: int = 0
    merged_continuations: int = 0
    system_messages: int = 0
    media_messages: int = 0
    warnings: list[str] = field(default_factory=list)
    speakers: list[str] = field(default_factory=list)
    first_line_examples: list[str] = field(default_factory=list)

    def warn(self, text: str) -> None:
        if text not in self.warnings:
            self.warnings.append(text)


@dataclass
class Conversation:
    """一份解析完成的对话。"""

    messages: list[Message]
    speakers: list[str]
    report: ParseReport = field(default_factory=ParseReport)

    def __len__(self) -> int:
        return len(self.messages)

    @property
    def real_messages(self) -> list[Message]:
        """去掉系统消息后的消息。"""
        return [m for m in self.messages if not m.is_system]

    @property
    def by_speaker(self) -> dict[str, list[Message]]:
        out: dict[str, list[Message]] = {s: [] for s in self.speakers}
        for m in self.messages:
            out.setdefault(m.speaker, []).append(m)
        return out

    @property
    def span(self) -> tuple[datetime, datetime] | None:
        stamps = [m.timestamp for m in self.messages if m.timestamp]
        if not stamps:
            return None
        return min(stamps), max(stamps)


# --------------------------------------------------------------------------- #
# 文本判定辅助
# --------------------------------------------------------------------------- #

def looks_like_question(text: str) -> bool:
    """粗判一句话是不是提问。宁可宽松，因为它只是众多指标之一。"""
    t = text.strip()
    if not t:
        return False
    if any(ch in t for ch in QUESTION_MARKS):
        return True
    # 去掉结尾的标点与表情后看疑问词
    core = t.rstrip("。.!！~～…、,， \t")
    if core.endswith(QUESTION_WORDS):
        return True
    # 「你几点到」「今天吃什么」这类无疑问词的
    for w in ("什么", "怎么", "咋", "为什么", "为啥", "哪", "谁", "多少", "几点", "多久"):
        if w in core:
            return True
    for w in ("是不是", "有没有", "能不能", "可不可以", "要不要", "行不行"):
        if w in core:
            return True
    return False


def is_system_text(text: str) -> bool:
    t = text.strip()
    if not t:
        return True
    return any(p.search(t) for p in SYSTEM_PATTERNS)


def media_tag_of(text: str) -> str | None:
    """若整条消息就是一个 ``[图片]`` 之类的标记，返回标记名。"""
    m = re.fullmatch(r"[\[【（(]\s*([^\[\]【】（）()]{1,12}?)\s*[\]】）)]", text.strip())
    if not m:
        return None
    tag = m.group(1).strip().lower()
    for known in MEDIA_TAGS:
        if known in tag or tag in known:
            return known
    return None


def has_emoji(text: str) -> bool:
    """检测 unicode emoji、颜文字、QQ/微信表情标记。"""
    if not text:
        return False
    # 方括号表情：[微笑] [Facepalm]
    if re.search(r"[\[【][^\[\]【】]{1,10}[\]】]", text):
        return True
    for ch in text:
        cp = ord(ch)
        if 0x1F300 <= cp <= 0x1FAFF:      # 主流 emoji 区
            return True
        if 0x2600 <= cp <= 0x27BF:        # 杂项符号与装饰
            return True
        if 0x1F000 <= cp <= 0x1F2FF:      # 麻将、扑克等
            return True
        if cp in (0xFE0F, 0x200D):        # 变体选择符 / 零宽连接
            return True
    # 颜文字：(╯°□°）╯  T_T  orz
    if re.search(r"[（(][^）)]{0,6}[・ω・´`ﾟ▽TД╥╯╰°□][^）)]{0,6}[）)]", text):
        return True
    if re.search(r"(?i)\b(orz|T_T|QAQ|qwq|>_<|\._\.)\b", text):
        return True
    return False


# --------------------------------------------------------------------------- #
# 时间戳解析
# --------------------------------------------------------------------------- #

def parse_timestamp(raw: str, *, fallback_date: datetime | None = None) -> datetime | None:
    """从一段文本里尽力抽出时间戳。抽不到返回 ``None``。

    ``fallback_date`` 用于「只有时间没有日期」的导出文件：把日期补上。
    """
    if not raw or not raw.strip():
        return None
    s = raw.strip()
    # 归一化全角数字与空白
    s = s.translate(str.maketrans("０１２３４５６７８９", "0123456789"))
    s = re.sub(r"[\u3000\u00a0]", " ", s)

    year = month = day = None
    hour = minute = second = None

    m = _DATE_FULL.search(s) or _DATE_CJK.search(s)
    if m:
        year, month, day = int(m.group("y")), int(m.group("m")), int(m.group("d"))
    else:
        m2 = _DATE_SHORT.search(s)
        if m2:
            month, day = int(m2.group("m")), int(m2.group("d"))
            # 月份/日期越界说明这不是日期（例如 21-33 其实是时间）
            if month > 12 or day > 31 or month == 0 or day == 0:
                month = day = None

    tm = _TIME_RE.search(s)
    if tm:
        hour = int(tm.group("h"))
        minute = int(tm.group("mi"))
        second = int(tm.group("s") or 0)
        # 上午/下午 修正
        pre = s[: tm.start()]
        ap = _AMPM_RE.search(pre)
        if ap:
            word = ap.group("ap")
            if word in ("下午", "晚上", "傍晚") and hour < 12:
                hour += 12
            elif word == "中午" and hour < 12:
                hour += 12
            elif word in ("上午", "早上", "凌晨") and hour == 12:
                hour = 0

    if hour is None and month is None:
        return None

    if year is None:
        base = fallback_date or datetime(1970, 1, 1)
        year = base.year
        if month is None:                      # 只有时间 → 用回退日期
            month, day = base.month, base.day
    if month is None or day is None:
        base = fallback_date or datetime(year, 1, 1)
        month = month or base.month
        day = day or base.day
    if hour is None:
        hour = minute = second = 0
        minute = minute or 0

    try:
        return datetime(year, month, day, hour or 0, minute or 0, second or 0)
    except ValueError:
        return None


# --------------------------------------------------------------------------- #
# 编码探测
# --------------------------------------------------------------------------- #

_BOMS: tuple[tuple[bytes, str], ...] = (
    (codecs.BOM_UTF8, "utf-8-sig"),
    (codecs.BOM_UTF16_LE, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16"),
    (codecs.BOM_UTF32_LE, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32"),
)

_CANDIDATE_ENCODINGS = ("utf-8", "gb18030", "big5", "shift_jis", "cp1252", "latin-1")


def decode_bytes(data: bytes) -> tuple[str, str]:
    """把字节解成文本，返回 ``(text, encoding)``。"""
    for bom, enc in _BOMS:
        if data.startswith(bom):
            try:
                return data.decode(enc), enc
            except UnicodeDecodeError:
                break

    # 用 utf-8 / gb18030 的「成功解码比例」来选，取第一个能完整解码的
    for enc in _CANDIDATE_ENCODINGS:
        try:
            return data.decode(enc), enc
        except (UnicodeDecodeError, LookupError):
            continue

    # 最后兜底：忽略错误，但明确标注
    return data.decode("utf-8", errors="replace"), "utf-8(replace)"


def read_text(path: str | Path) -> tuple[str, str]:
    p = Path(path)
    if not p.exists():
        raise ParseError(f"文件不存在：{p}")
    if p.is_dir():
        raise ParseError(f"这是一个目录，不是文件：{p}")
    data = p.read_bytes()
    if not data.strip():
        raise ParseError(f"文件是空的：{p}")
    return decode_bytes(data)


# --------------------------------------------------------------------------- #
# 昵称「像不像昵称」的启发式
# --------------------------------------------------------------------------- #

_HEADER_NOISE = re.compile(
    r"^(消息记录|聊天记录|消息对象|消息分组|日期|时间|发送者|发送人|内容|昵称|备注|"
    r"=+|-{2,}|导出的聊天记录|以下为|微信|QQ|腾讯|第\s*\d+\s*页)",
    re.IGNORECASE,
)

#: 昵称里几乎不可能出现的字符：句末标点、网址、邮箱特征
_NAME_FORBIDDEN = re.compile(r"[。！？!?；;，,、\"'“”‘’<>@#%^*+=~|\\/]|://|\s{2,}")
_NAME_ASCII_HINT = re.compile(r"[A-Za-z0-9]")


def looks_like_speaker_name(token: str) -> bool:
    """判断一个 token 是否像说话人昵称。

    刻意保守：宁可漏认、让用户用 ``--me/--peer`` 指定，也不要误把**正文**
    当成昵称——那会静默地毁掉整份报告的可信度。
    """
    t = token.strip().strip(":：").strip()
    if not t or len(t) > 40:
        return False
    if _HEADER_NOISE.match(t):
        return False
    if _NAME_FORBIDDEN.search(t):
        return False
    if parse_timestamp(t) is not None:
        return False
    if _TIME_RE.search(t):
        return False
    if t.isdigit():
        return False
    # 含数字加冒号（21:30、33:02）的，是时间碎片而不是昵称
    if re.search(r"\d\s*[:：]\s*\d", t):
        return False

    # 纯 ASCII 的 token：只有「用户名式」的短标识才算昵称（例如 EnglishName、user_01）
    if not re.search(r"[\u4e00-\u9fff]", t):
        if not _NAME_ASCII_HINT.search(t):
            return False
        if len(t) > 24:
            return False
        # 像句子（含空格、多个单词）就不认
        if t.count(" ") > 0 and len(t.split()) > 2:
            return False
        return True

    # 含中文：昵称通常很短，且不会是一句完整的话
    if len(t) > 20:
        return False
    # 中文昵称里不该含「的吗呢吧了啊呀嘛」这类成句助词结尾 + 过长
    if len(t) >= 8 and re.search(r"[的吗呢吧了啊呀嘛哦噢]$", t):
        return False
    return True


# --------------------------------------------------------------------------- #
# 文本解析（核心状态机）
# --------------------------------------------------------------------------- #

@dataclass
class _RawEntry:
    speaker: str
    ts_raw: str
    body: list[str]
    line_no: int


#: 形如  ``<时间戳> <昵称>: <正文>``  或  ``<昵称>: <正文>``
_INLINE_COLON_RE = re.compile(r"^(?P<head>.{1,80}?)\s*[:：]\s*(?P<body>.*)$")

#: URL / 协议前缀——这种行里的冒号不是「昵称: 正文」分隔符
_URL_LIKE_RE = re.compile(r"(?:^|\s)[a-zA-Z][a-zA-Z0-9+.\-]*://|^www\.", re.IGNORECASE)

#: 昵称在行首、时间戳跟在后面（``阿澈 2023-04-01 21:33:02``）
_NAME_TS_RE = re.compile(r"^(?P<name>.{1,24}?)\s+(?P<ts>\S.*)$")

#: 「时间戳字符」——数字、日期/时间分隔符、以及时间戳内部可能出现的空格。
#: 用它从行首连续吃掉时间戳，遇到第一个非时间戳字符就停（那通常是昵称的开头）。
_TS_RUN_RE = re.compile(r"(?:[0-9\s\-/.年月日:T:：]|上午|下午|中午|凌晨|早上|晚上|夜里|傍晚)+")

#: 纯标点（不是昵称）
_PUNCT_ONLY_RE = re.compile(r"^[\s\-–—|·=:/\[\]【】()（）]*$")

_TS_EXTRA_CHARS = " \t-–—|·=:/.[]【】()（）"

_TIMESTAMP_RESIDUE = re.compile(r"[\d:：]")


def _validate_timestamp_text(s: str) -> str | None:
    """判断**整段文本**是否恰好就是一个时间戳。

    这是唯一的权威校验点。``_split_header`` 先按结构切出候选片段，
    再交给这里判定——避免「正则说像、解析器说不是」两套标准打架。
    """
    if not s:
        return None
    raw = s.strip().strip("[]【】()（）").strip()
    if not raw:
        return None
    if parse_timestamp(raw) is None:
        return None
    # 摘掉时间与日期成分后不允许还剩数字或冒号（排除 127.0.0.1:8080 这类）
    stripped = raw
    for p in (*_DATE_PATTERNS, _TIME_RE):
        stripped = p.sub(" ", stripped)
    stripped = stripped.strip(_TS_EXTRA_CHARS)
    if _TIMESTAMP_RESIDUE.search(stripped):
        return None
    return raw


def _locate_timestamp(s: str) -> str | None:
    """在字符串里定位时间戳片段并返回原文；找不到返回 ``None``。"""
    if not s or not s.strip():
        return None
    if parse_timestamp(s) is None:
        return None

    m = _TIME_RE.search(s)
    if m:
        start = m.start()
        prefix = s[:start]
        dm = None
        for p in _DATE_PATTERNS:
            for cand in p.finditer(prefix):
                dm = cand
        if dm is not None and prefix[dm.end():].strip(_TS_EXTRA_CHARS) == "":
            start = dm.start()
        end = m.end()
        while end < len(s) and s[end] in ":：":
            end += 1
        return _validate_timestamp_text(s[start:end])

    # 只有日期没有时间
    for p in _DATE_PATTERNS:
        dm = p.search(s)
        if dm:
            got = _validate_timestamp_text(s[dm.start():])
            if got:
                return got
    return None


def _split_header(line: str) -> tuple[str, str] | None:
    """把一行 header 拆成 ``(时间戳原文, 昵称)``；不像 header 就返回 ``None``。

    支持三种排列：

    1. ``2023-04-01 21:33 阿澈`` / ``[2023-04-01 21:33] 阿澈`` —— 时间戳在前
    2. ``2023-04-01 21:33`` —— 只有时间戳，昵称在下一行（返回空昵称）
    3. ``阿澈 2023-04-01 21:33`` —— 昵称在前

    **不使用「一个万能正则」**：先把可以包住时间戳的括号剥掉，再从行首
    连续吃掉「时间戳字符」，把这一整段交给 :func:`_validate_timestamp_text`
    判定。剩下的一律当作昵称候选，过不了 :func:`looks_like_speaker_name`
    就说明这一行根本不是 header——宁可漏判，也不要伪造发言人。
    """
    s = line.strip()
    if not s:
        return None

    # 剥掉可能包住时间戳的括号
    if s[:1] in "[【(（":
        closer = {"[": "]", "【": "】", "(": ")", "（": "）"}[s[0]]
        body = s[1:].lstrip()
    else:
        closer = "]"
        body = s

    # ---- 1 / 2：时间戳在行首 ----
    m = _TS_RUN_RE.match(body)
    if m and m.end() > 0:
        run = m.group(0)
        run = run.rstrip()
        # 末尾若紧跟闭括号，说明时间戳被括号包着，一并纳入候选
        consumed = m.end()
        if consumed < len(body) and body[consumed] == closer:
            run = body[: consumed + 1]
            consumed += 1

        ts_raw = _validate_timestamp_text(run)
        if ts_raw:
            tail = body[consumed:].strip().lstrip("-–—|·=:").strip()
            if not tail:
                return ts_raw, ""
            # 昵称取「最短可用前缀」：``阿澈: 到家了`` 里昵称是 ``阿澈``，
            # 冒号后面的是正文。所以不能简单地把整段 tail 当昵称。
            sep = min(
                (i for i, ch in enumerate(tail) if ch in ":："),
                default=len(tail),
            )
            head = tail[:sep].strip()
            if head and looks_like_speaker_name(head):
                return ts_raw, _clean_name(head)
            name = _clean_name(tail)
            if name and looks_like_speaker_name(name):
                return ts_raw, name
            # 首部确实是时间戳，但后面跟的不像昵称 → 整行不是 header
            return None

    # ---- 3：昵称在行首，时间戳跟在后面 ----
    m2 = _NAME_TS_RE.match(s)
    if m2:
        name = _clean_name(m2.group("name"))
        ts_raw = _validate_timestamp_text(m2.group("ts"))
        if name and ts_raw and looks_like_speaker_name(name):
            return ts_raw, name

    return None


def _extract_timestamp(s: str) -> str | None:
    """兼容旧调用点的薄封装（时间戳定位见 :func:`_locate_timestamp`）。"""
    return _locate_timestamp(s)


_QQ_ID_SUFFIX = re.compile(r"\s*[\(（][^()（）]{1,24}[\)）]\s*$")


def _clean_name(s: str) -> str:
    """清洗昵称：去掉尾部括号里的 QQ 号 / 备注、微信别名等。"""
    t = s.strip().strip(":：").strip()
    t = _QQ_ID_SUFFIX.sub("", t).strip()
    return t


def _iter_entries(text: str, report: ParseReport) -> Iterator[_RawEntry]:
    """把文本切成一条条「原始条目」（header + 续行）。"""
    lines = text.splitlines()
    report.total_lines = len(lines)

    current: _RawEntry | None = None
    pending_ts_only: str | None = None

    for i, line in enumerate(lines, start=1):
        raw = line.rstrip("\r\n")
        stripped = raw.strip()

        if not stripped:
            # 空行是消息之间的分隔，不并入正文
            continue

        if len(report.first_line_examples) < 12:
            report.first_line_examples.append(stripped[:80])

        # ---- 0. 上一行是「只有时间戳」的行，这一行是昵称 ----
        if pending_ts_only is not None:
            ts_for_name = pending_ts_only
            pending_ts_only = None
            # 时间戳单独占了一行，下一行按约定就是昵称：这里放宽校验，
            # 只排除明显的表头噪声和系统提示。
            if (
                stripped
                and len(stripped) <= 40
                and not _HEADER_NOISE.match(stripped)
                and parse_timestamp(stripped) is None
                and not is_system_text(stripped)
            ):
                if current is not None:
                    yield current
                current = _RawEntry(
                    speaker=stripped.strip(":："), ts_raw=ts_for_name, body=[], line_no=i
                )
                continue
            # 不是昵称：把那个孤零零的时间戳丢掉，继续按普通行处理
            report.warn(f"第 {i - 1} 行的独立时间戳后面不是昵称，已忽略。")

        # ---- 0.5 URL 行：绝不可能是「昵称: 正文」，当正文处理 ----
        if _URL_LIKE_RE.search(stripped):
            if current is not None:
                current.body.append(stripped)
            else:
                report.skipped_lines += 1
                report.warn(f"第 {i} 行像网址且前面没有归属消息，已跳过：{stripped[:60]}")
            continue

        # ---- 1. 标准 header：时间戳 [+ 昵称] ----
        head = _split_header(raw)
        if head is not None:
            ts_raw, name = head
            if not name:
                # 时间戳单独一行，昵称在下一行
                if current is not None:
                    yield current
                    current = None
                pending_ts_only = ts_raw
                continue
            if current is not None:
                yield current
            # ``2023-04-01 21:33 阿澈: 到家了`` —— 冒号后面就是正文
            body_lines: list[str] = []
            name_stripped = name.strip()
            raw_wo_ws = raw.strip()
            if raw_wo_ws.endswith(name_stripped) or f"{name_stripped}:" in raw_wo_ws or f"{name_stripped}：" in raw_wo_ws:
                pos = max(raw_wo_ws.rfind(f"{name_stripped}:"), raw_wo_ws.rfind(f"{name_stripped}："))
                if pos >= 0:
                    after = raw_wo_ws[pos + len(name_stripped) + 1:].strip()
                    if after:
                        body_lines.append(after)
            current = _RawEntry(speaker=name, ts_raw=ts_raw, body=body_lines, line_no=i)
            continue

        # ---- 2. 纯时间戳行（后面跟昵称 + 正文）----
        if parse_timestamp(stripped) is not None and len(stripped) <= 40:
            if current is not None:
                yield current
                current = None
            pending_ts_only = stripped
            continue

        # ---- 3. 昵称: 正文 ----
        mc = _INLINE_COLON_RE.match(stripped)
        if mc:
            head_text = mc.group("head").strip()
            body_text = mc.group("body")
            if not _URL_LIKE_RE.search(head_text):
                # head 里可能同时含时间戳；把它摘掉后再看剩下的是不是昵称
                ts_raw = ""
                name = head_text
                h2 = _split_header(head_text)
                if h2 is not None:
                    ts_raw, nm = h2
                    if nm:
                        name = nm
                else:
                    name = _clean_name(head_text)
                if (name or ts_raw) and looks_like_speaker_name(name):
                    if current is not None:
                        yield current
                    current = _RawEntry(
                        speaker=name.strip(), ts_raw=ts_raw, body=[body_text], line_no=i
                    )
                    continue

        # ---- 4. 续行：并入当前消息 ----
        if current is not None:
            current.body.append(stripped)
            continue

        # ---- 5. 完全无法归类的行 ----
        report.skipped_lines += 1
        if len(report.warnings) < 200 and report.skipped_lines <= 5:
            report.warn(f"第 {i} 行无法识别，已跳过：{stripped[:60]}")

    if current is not None:
        yield current


def _resolve_timestamps(entries: Sequence[_RawEntry], report: ParseReport) -> list[Message]:
    """把原始条目的时间戳补全（缺日期的按顺序推断），生成 Message 列表。"""
    messages: list[Message] = []
    last_dt: datetime | None = None

    for idx, e in enumerate(entries):
        ts_raw = e.ts_raw or ""
        ts = parse_timestamp(ts_raw, fallback_date=last_dt)

        has_date = bool(re.search(r"\d{4}\s*[-/.年]", ts_raw)) or bool(
            _DATE_PATTERNS[1].search(ts_raw)
        )

        if ts is not None:
            if not has_date and last_dt is not None:
                # 只有时间：补上最近一次的日期；若时间倒着走，说明跨天了
                ts = ts.replace(year=last_dt.year, month=last_dt.month, day=last_dt.day)
                if ts < last_dt - timedelta(hours=6):
                    ts += timedelta(days=1)
                report.inferred_timestamps += 1
            elif has_date and last_dt is not None and ts < last_dt - timedelta(days=180):
                # 年份写错（例如导出工具把年份写死）→ 不动，只提示
                report.warn("检测到时间戳顺序异常（可能是导出工具的年份问题），时间趋势仅供参考。")
            last_dt = ts
        else:
            report.inferred_timestamps += 1

        body = "\n".join(l for l in e.body if l.strip()).strip()
        media = media_tag_of(body)
        sysmsg = is_system_text(body)

        messages.append(
            Message(
                index=idx,
                speaker=e.speaker,
                timestamp=ts,
                text=body,
                line_no=e.line_no,
                is_system=sysmsg,
                is_media=media is not None,
                multiline=len(e.body) > 1,
            )
        )
        if len(e.body) > 1:
            report.merged_continuations += 1
        if media is not None:
            report.media_messages += 1
        if sysmsg:
            report.system_messages += 1

    return messages


# --------------------------------------------------------------------------- #
# CSV 解析
# --------------------------------------------------------------------------- #

_TS_HEADER_HINTS = ("time", "date", "时间", "日期", "timestamp", "stamp", "datetime")
_SPEAKER_HEADER_HINTS = ("sender", "speaker", "from", "name", "昵称", "发送", "说话", "用户", "who", "author", "talker")
_TEXT_HEADER_HINTS = ("content", "message", "msg", "text", "body", "内容", "消息", "正文", "chat")
_TYPE_HEADER_HINTS = ("type", "类型", "msgtype", "is_sender", "方向")


def _pick_delimiter(text: str) -> str:
    sample_lines = [l for l in text.splitlines()[:40] if l.strip()]
    if not sample_lines:
        return ","
    best, best_score = ",", -1.0
    for delim in (",", "\t", ";", "|"):
        counts = [l.count(delim) for l in sample_lines]
        if not counts:
            continue
        non_zero = [c for c in counts if c > 0]
        if not non_zero:
            continue
        # 希望「大多数行都有相同的列数」
        mode = max(set(non_zero), key=non_zero.count)
        consistency = non_zero.count(mode) / len(sample_lines)
        score = mode * consistency
        if score > best_score:
            best, best_score = delim, score
    return best


def _looks_like_header(row: Sequence[str]) -> bool:
    joined = " ".join(c.lower() for c in row if c)
    has_ts = any(h in joined for h in _TS_HEADER_HINTS)
    has_spk = any(h in joined for h in _SPEAKER_HEADER_HINTS)
    has_txt = any(h in joined for h in _TEXT_HEADER_HINTS)
    # 表头里不应该已经有可解析的时间戳
    if any(parse_timestamp(c) for c in row if c):
        return False
    return (has_ts and has_spk) or (has_spk and has_txt) or (has_ts and has_txt)


def _detect_columns(rows: Sequence[Sequence[str]]) -> tuple[int | None, int, int]:
    """返回 ``(时间列, 说话人列, 正文列)``，时间列可能为 ``None``。"""
    sample = [r for r in rows[:80] if any(c.strip() for c in r)]
    if not sample:
        raise ParseError("CSV 里没有可用的数据行。")
    ncols = max(len(r) for r in sample)

    ts_scores: list[float] = []
    spk_scores: list[float] = []
    txt_scores: list[float] = []
    for c in range(ncols):
        vals = [r[c].strip() for r in sample if c < len(r) and r[c].strip()]
        if not vals:
            ts_scores.append(0.0)
            spk_scores.append(0.0)
            txt_scores.append(0.0)
            continue
        ts_hit = sum(1 for v in vals if parse_timestamp(v) is not None) / len(vals)
        avg_len = sum(len(v) for v in vals) / len(vals)
        uniq = len(set(vals)) / len(vals)
        short = sum(1 for v in vals if len(v) <= 24) / len(vals)

        ts_scores.append(ts_hit)
        # 说话人：唯一性高、通常很短
        spk_scores.append(uniq * short * (1.0 if avg_len <= 24 else 0.4))
        # 正文：长度大、唯一性高
        txt_scores.append(min(avg_len / 30.0, 1.0) * uniq)

    ts_candidates = [i for i in range(ncols) if ts_scores[i] > 0.6]
    ts_col: int | None = max(ts_candidates, key=lambda i: ts_scores[i]) if ts_candidates else None

    txt_candidates = [i for i in range(ncols) if i != ts_col]
    if not txt_candidates:
        raise ParseError("CSV 只有一列，无法区分说话人与正文。")
    txt_col = max(txt_candidates, key=lambda i: txt_scores[i])

    spk_candidates = [i for i in range(ncols) if i not in (ts_col, txt_col)]
    if not spk_candidates:
        raise ParseError("CSV 只有两列，无法区分时间、说话人与正文，请用 --format text 或检查文件。")
    spk_col = max(spk_candidates, key=lambda i: spk_scores[i])
    return ts_col, spk_col, txt_col


def parse_csv(text: str, report: ParseReport) -> tuple[list[Message], list[str]]:
    delim = _pick_delimiter(text)
    report.file_kind = f"csv (分隔符 {delim!r})"

    reader = csv.reader(io.StringIO(text), delimiter=delim)
    rows = [r for r in reader]
    rows = [r for r in rows if any(c.strip() for c in r)]
    if not rows:
        raise ParseError("CSV 里没有可用的数据行。")

    has_header = _looks_like_header(rows[0])
    header = [c.strip().lower() for c in rows[0]] if has_header else []
    data = rows[1:] if has_header else rows

    ts_col: int | None = None
    spk_col: int
    txt_col: int

    if has_header:
        def find(hints: Sequence[str]) -> int | None:
            for i, h in enumerate(header):
                if any(hint in h for hint in hints):
                    return i
            return None

        ts_col = find(_TS_HEADER_HINTS)
        spk_col_i = find(_SPEAKER_HEADER_HINTS)
        txt_col_i = find(_TEXT_HEADER_HINTS)
        if spk_col_i is None or txt_col_i is None:
            auto_ts, auto_spk, auto_txt = _detect_columns(data)
            ts_col = ts_col if ts_col is not None else auto_ts
            spk_col = spk_col_i if spk_col_i is not None else auto_spk
            txt_col = txt_col_i if txt_col_i is not None else auto_txt
        else:
            spk_col, txt_col = spk_col_i, txt_col_i
    else:
        ts_col, spk_col, txt_col = _detect_columns(data)

    entries: list[_RawEntry] = []
    for n, row in enumerate(data):
        def cell(i: int | None) -> str:
            if i is None or i >= len(row):
                return ""
            return row[i].strip()

        speaker = cell(spk_col)
        body = cell(txt_col)
        ts_raw = cell(ts_col)
        if not speaker and not body:
            report.skipped_lines += 1
            continue
        if not speaker:
            speaker = "未知"
        entries.append(_RawEntry(speaker=speaker, ts_raw=ts_raw, body=[body], line_no=n + (2 if has_header else 1)))

    messages = _resolve_timestamps(entries, report)
    # 去掉被 CSV 引号包成空壳的行
    messages = [m for m in messages if m.speaker or m.text]
    messages = _reindex(messages)
    return messages, _speaker_order(messages)


# --------------------------------------------------------------------------- #
# 文本解析入口
# --------------------------------------------------------------------------- #

def parse_text(text: str, report: ParseReport) -> tuple[list[Message], list[str]]:
    report.file_kind = "text"

    entries = list(_iter_entries(text, report))
    if not entries:
        raise ParseError(
            "没能从文件里认出任何一条消息。请先用 `inspect` 子命令查看文件头部，"
            "确认它是聊天记录导出（而不是加密的数据库或二进制文件）。"
        )
    messages = _resolve_timestamps(entries, report)
    messages = _reindex(messages)
    return messages, _speaker_order(messages)


def _reindex(messages: list[Message]) -> list[Message]:
    for i, m in enumerate(messages):
        m.index = i
    return messages


def _speaker_order(messages: list[Message]) -> list[str]:
    """按「首次出现顺序」返回说话人列表。

    刻意不用消息条数排序：那样同一份文件在不同切片下顺序会变，
    报告里的「A vs B」就会忽左忽右。
    """
    seen: list[str] = []
    for m in messages:
        if m.speaker not in seen:
            seen.append(m.speaker)
    return seen


# --------------------------------------------------------------------------- #
# 公共 API
# --------------------------------------------------------------------------- #

def _detect_format(path: Path, text: str) -> str:
    """判断该用哪种解析方式。

    顺序：扩展名 → 内容特征 → 兜底。
    ``structured`` 指的是 JSON / 网页内嵌数据这类**结构化导出**，
    它们不能按文本行解析。
    """
    suffix = path.suffix.lower()
    if suffix == ".json":
        return "structured"
    if suffix in (".html", ".htm", ".xhtml"):
        return "structured"
    if suffix == ".csv":
        return "csv"
    if suffix in (".txt", ".log", ".md", ".text"):
        return "text"
    # 其它扩展名（含无扩展名）继续往下按内容判断

    # 没有可用的扩展名线索时看内容
    head = text.lstrip()[:2000].lower()
    if head.startswith(("{", "[")):
        return "structured"
    if "<!doctype html" in head or "<html" in head:
        return "structured"
    if "application/json" in head:
        return "structured"
    # 内嵌数据的网页（数据在 <script> 里，开头可能是一堆 HTML）
    if "weflow_data" in head or re.search(r"\b\w+\s*=\s*\[\s*\{", head):
        return "structured"
    return "csv" if _pick_delimiter(text) != "," else "text"


def parse_file(path: str | Path, *, fmt: str = "auto") -> Conversation:
    """解析一份聊天记录文件，返回 :class:`Conversation`。

    支持的格式（``fmt="auto"`` 时自动判断）：

    ============  ====================================================
    扩展名         解析方式
    ============  ====================================================
    ``.json``      结构化导出（字段名容错）
    ``.html/.htm`` 单文件网页，从内嵌的 ``window.X = [...]`` 取数据
    ``.csv``       表格（自动分隔符与列识别）
    ``.txt/.log``  逐行文本（多种时间戳与说话人写法）
    ============  ====================================================
    """
    p = Path(path)
    text, encoding = read_text(p)
    report = ParseReport(path=str(p), encoding=encoding)

    fmt = (fmt or "auto").lower()
    if fmt == "auto":
        fmt = _detect_format(p, text)

    if fmt in ("structured", "json", "html"):
        from .structured import parse_structured
        try:
            messages, speakers, how = parse_structured(text, name=str(p))
        except ParseError:
            raise
        except Exception as exc:  # pragma: no cover - 防御性
            raise ParseError(f"结构化解析失败：{exc}") from exc
        report.file_kind = how
    elif fmt == "csv":
        try:
            messages, speakers = parse_csv(text, report)
        except ParseError:
            raise
        except Exception as exc:  # pragma: no cover - 防御性
            raise ParseError(f"CSV 解析失败：{exc}") from exc
    else:
        messages, speakers = parse_text(text, report)

    if not messages:
        raise ParseError("解析完成但没有任何消息，已中止（不会生成虚构报告）。")

    report.parsed_messages = len(messages)
    report.speakers = speakers

    if len(speakers) > 2:
        report.warn(
            f"检测到 {len(speakers)} 个说话人（可能是群聊）。"
            "报告只会分析你和其中互动最多的那一位，建议用 --peer 指定。"
        )

    conv = Conversation(messages=messages, speakers=speakers, report=report)
    return conv


def parse_string(text: str, *, fmt: str = "text", name: str = "<string>") -> Conversation:
    """解析一段字符串（测试与嵌入式使用）。"""
    report = ParseReport(path=name, encoding="utf-8")
    if fmt == "csv":
        messages, speakers = parse_csv(text, report)
    else:
        messages, speakers = parse_text(text, report)
    report.parsed_messages = len(messages)
    report.speakers = speakers
    return Conversation(messages=messages, speakers=speakers, report=report)


def infer_subject(conv: Conversation) -> str | None:
    """猜「我」是谁。

    策略（按可靠性排序）：

    1. 常见的自我昵称（我、本人、自己、me…）
    2. 在 CSV 有方向列的情况下不适用，这里只看名字
    3. 看起来像真名的、出现次数较少的那个（保守起见返回 ``None``，交给用户指定）
    """
    self_names = ("我", "本人", "自己", "me", "myself", "自己本人")
    for s in conv.speakers:
        if s.strip() in self_names:
            return s
    return None


def describe(conv: Conversation) -> str:
    """生成人类可读的解析摘要。"""
    r = conv.report
    lines = [
        f"文件        : {r.path}",
        f"编码 / 格式 : {r.encoding} / {r.file_kind}",
        f"总行数      : {r.total_lines}",
        f"解析消息数  : {r.parsed_messages}",
        f"说话人      : {', '.join(conv.speakers) if conv.speakers else '（无）'}",
    ]
    if r.system_messages:
        lines.append(f"系统消息    : {r.system_messages}（已排除出统计）")
    if r.media_messages:
        lines.append(f"媒体消息    : {r.media_messages}（图片/表情等，不计字数）")
    if r.merged_continuations:
        lines.append(f"多行消息    : {r.merged_continuations}")
    if r.inferred_timestamps:
        lines.append(f"时间戳推断  : {r.inferred_timestamps} 条（原始数据缺日期或时间）")
    if r.skipped_lines:
        lines.append(f"跳过行      : {r.skipped_lines}")
    span = conv.span
    if span:
        lines.append(f"时间范围    : {span[0]:%Y-%m-%d %H:%M} → {span[1]:%Y-%m-%d %H:%M}")
    if r.warnings:
        lines.append("提示        :")
        for w in r.warnings:
            lines.append(f"  - {w}")
    return "\n".join(lines)


def to_records(messages: Iterable[Message]) -> list[dict]:
    """导出为可 JSON 序列化的字典列表。"""
    out = []
    for m in messages:
        out.append({
            "index": m.index,
            "speaker": m.speaker,
            "timestamp": m.timestamp.isoformat(sep=" ") if m.timestamp else None,
            "text": m.text,
            "line_no": m.line_no,
            "is_system": m.is_system,
            "is_media": m.is_media,
            "multiline": m.multiline,
            "char_count": m.char_count,
            "is_question": m.is_question,
        })
    return out