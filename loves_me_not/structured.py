"""结构化导出格式（JSON / HTML / weflow）的解析。

除了纯文本与 CSV，还有一批导出工具会把聊天记录写成**结构化文件**：

* ``.json`` —— 直接是数据文件；
* ``.html`` —— 单文件网页，数据以 ``window.XXX = [...]`` 的形式内嵌在
  ``<script>`` 里（这类文件无法当文本行解析，必须先把数据抠出来）。

本模块负责把这两类统一成 :class:`~loves_me_not.parser.Message`，
让后续所有分析逻辑对输入格式完全无感。

**设计原则**（与 parser 一致）：

1. 认不出来就明确报错，绝不猜、不编；
2. 字段名容错（同一含义可能有多种写法），但**不做语义假设**；
3. 时间戳支持 Unix 秒/毫秒、ISO 字符串、``YYYY-MM-DD HH:MM:SS``。

不依赖任何第三方库。
"""

from __future__ import annotations

import html as _html
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterator, Sequence

from .parser import Message, ParseError, is_system_text, media_tag_of

# --------------------------------------------------------------------------- #
# 字段名映射：同一含义在不同导出器里叫法不同
# --------------------------------------------------------------------------- #

#: 时间字段候选（按优先级）
TIME_KEYS = (
    "createTime", "create_time", "timestamp", "time", "ts", "date",
    "formattedTime", "formatted_time", "datetime", "sendTime", "msgTime",
    "t", "createDate", "CreateTime", "Time",
)

#: 「是否我发出」字段候选
IS_SEND_KEYS = ("isSend", "is_send", "isMe", "is_me", "isSelf", "self", "s")

#: 发送人字段候选（名字）
SPEAKER_KEYS = (
    "senderDisplayName", "sender_display_name", "senderName", "sender_name",
    "sender", "speaker", "from", "nickname", "talker", "userName", "name",
    "displayName", "SendCardNickName",
)

#: 发送人字段候选（稳定 ID，名字缺失时用）
SPEAKER_ID_KEYS = ("senderUsername", "sender_username", "wxid", "username", "uid", "fromId")

#: 正文字段候选
TEXT_KEYS = (
    "content", "text", "msg", "message", "body", "Content", "messageText",
    "b", "value", "chatContent",
)

#: 消息类型字段候选
TYPE_KEYS = ("type", "msgType", "msg_type", "localType", "messageType", "contentType")

#: 有些导出（例如网页版单文件）只给正文 HTML，没有类型字段。
#: 这时按正文里的媒体容器类名反推类型。
_BODY_TYPE_HINTS: tuple[tuple[str, str], ...] = (
    ("message-media", "图片"),
    ("message-image", "图片"),
    ("inline-emoji", "表情"),
    ("message-emoji", "表情"),
    ("message-voice", "语音"),
    ("message-video", "视频"),
    ("message-file", "文件"),
    ("message-link-card", "链接"),
    ("message-transfer", "转账"),
)


def _classify_body_html(raw: str) -> tuple[bool, bool, str]:
    """没有类型字段时，从正文 HTML 的类名反推 ``(系统, 媒体, 标记)``。"""
    if "<" not in raw:
        return False, False, ""
    for hint, tag in _BODY_TYPE_HINTS:
        if hint in raw:
            return False, True, f"[{tag}]"
    return False, False, ""

# --------------------------------------------------------------------------- #
# 时间解析
# --------------------------------------------------------------------------- #

#: 大于这个值的整数时间戳按毫秒处理（约等于 2001-09-09 之后的秒级时间戳）
_MILLIS_THRESHOLD = 10_000_000_000


def parse_time_value(value: Any) -> datetime | None:
    """把各种时间写法统一成 ``datetime``。

    支持：Unix 秒 / 毫秒（int、float、数字字符串）、
    ISO 8601、``YYYY-MM-DD HH:MM:SS``、``YYYY/MM/DD HH:MM``。
    认不出来返回 ``None``（由调用方决定是报错还是留空）。
    """
    if value is None or value == "":
        return None

    if isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        ts = float(value)
        if ts <= 0:
            return None
        if ts > _MILLIS_THRESHOLD:
            ts /= 1000.0
        try:
            # 导出工具给的都是本地时间，所以用本地时区还原
            return datetime.fromtimestamp(ts)
        except (OSError, OverflowError, ValueError):
            return None

    if isinstance(value, str):
        s = value.strip()
        if not s:
            return None
        # 纯数字字符串
        if re.fullmatch(r"\d{9,14}", s):
            return parse_time_value(int(s))
        # ISO 8601（含 Z / 时区偏移）
        iso = s.replace("Z", "+00:00")
        try:
            dt = datetime.fromisoformat(iso)
            if dt.tzinfo is not None:
                dt = dt.astimezone().replace(tzinfo=None)
            return dt
        except ValueError:
            pass
        # 交给 parser 的通用时间戳解析（它认很多中文/斜杠写法）
        from .parser import parse_timestamp
        return parse_timestamp(s)

    return None


# --------------------------------------------------------------------------- #
# 从 HTML 里抠出内嵌数据
# --------------------------------------------------------------------------- #

def extract_embedded_json(text: str) -> Any | None:
    """从单文件网页里把内嵌的 JSON 数据抠出来。

    导出工具常见的写法有几种：

    * ``window.WEFLOW_DATA = [ ... ];``
    * ``var data = { ... };``
    * ``<script type="application/json">{ ... }</script>``

    做法：找到赋值号或 script 标签后，用**括号配平**扫描到与之匹配的收尾，
    再交给 ``json.loads``。不用正则去匹配整个对象——
    那样遇到正文里含花括号的消息就会截断。

    JavaScript 字面量里可能出现单引号字符串（例如正文含撇号），
    这时按「只有双引号才是字符串边界」扫会提前跑偏，
    所以要再按 JS 规则（单双引号都算）重扫一遍。
    """
    # 1) <script type="application/json">…</script>
    for m in re.finditer(
        r'<script[^>]*type\s*=\s*["\']application/(?:ld\+)?json["\'][^>]*>(.*?)</script>',
        text, re.S | re.I,
    ):
        parsed = _try_json(m.group(1).strip())
        if parsed is not None:
            return parsed

    # 2) window.X = [...] / var X = {...} / const X = [...]
    for m in re.finditer(
        r'(?:window\.|var\s+|let\s+|const\s+)?([A-Za-z_$][\w$]*)\s*=\s*(?=[\[{])',
        text,
    ):
        start = m.end()
        for single_quotes in (False, True):
            literal = _scan_balanced(text, start, single_quotes=single_quotes)
            if not literal:
                continue
            parsed = _try_json(literal)
            if parsed is not None:
                return parsed

    return None


def _try_json(raw: str) -> Any | None:
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return None


def _scan_balanced(text: str, start: int, *, single_quotes: bool = False) -> str | None:
    """从 ``start`` 处的 ``[`` 或 ``{`` 开始，按括号配平取出一段完整字面量。

    ``single_quotes=True`` 时把 ``'`` 也当作字符串边界（JavaScript 规则）。
    """
    if start >= len(text) or text[start] not in "[{":
        return None
    depth = 0
    i = start
    in_str = False
    quote = ""
    esc = False
    while i < len(text):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == quote:
                in_str = False
        else:
            if ch == '"' or (single_quotes and ch == "'"):
                in_str = True
                quote = ch
            elif ch in "[{":
                depth += 1
            elif ch in "]}":
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
        i += 1
    return None


# --------------------------------------------------------------------------- #
# 把一条结构化记录转成 Message
# --------------------------------------------------------------------------- #

@dataclass
class _Schema:
    """识别出来的字段映射。"""

    time_key: str | None
    text_key: str
    speaker_key: str | None
    speaker_id_key: str | None
    is_send_key: str | None
    type_key: str | None


def _first_key(record: dict, candidates: Sequence[str]) -> str | None:
    for k in candidates:
        if k in record and record[k] is not None:
            return k
    # 再试一次大小写不敏感匹配
    lower = {k.lower(): k for k in record}
    for k in candidates:
        if k.lower() in lower:
            return lower[k.lower()]
    return None


#: 只在「确定是消息记录」时才认的弱字段名。
#: ``b`` 这类单字母键很容易在别的 JSON 里表示别的东西，
#: 所以必须同时出现真正的消息特征才采用。
_WEAK_TEXT_KEYS = ("b",)

#: 足以证明「这确实是一批消息记录」的字段（强证据）
_MESSAGE_EVIDENCE_KEYS = (
    "createTime", "create_time", "timestamp", "formattedTime", "isSend",
    "is_send", "senderUsername", "senderDisplayName", "sender", "speaker",
    "localId", "localType", "platformMessageId",
)

#: 紧凑导出的特征组合：只有这几个单字母键**同时**出现，
#: 才认为是在描述消息（时间 + 发送方向）。
_COMPACT_EVIDENCE_COMBOS = (
    ("t", "s"),          # 网页内嵌数据：时间 + 是否我发
    ("t", "i", "b"),     # 同上，带序号与正文
)


def _detect_schema(records: Sequence[dict]) -> _Schema:
    """从样本记录里推断字段映射。"""
    if not records:
        raise ParseError("数组里没有记录。")
    sample = dict(records[0])
    # 用前若干条求并集，避免第一条缺字段就识别失败
    for r in records[1: min(len(records), 20)]:
        for k, v in r.items():
            sample.setdefault(k, v)

    evidence = any(k in sample for k in _MESSAGE_EVIDENCE_KEYS) or any(
        all(k in sample for k in combo) for combo in _COMPACT_EVIDENCE_COMBOS
    )

    text_key = _first_key(sample, TEXT_KEYS)
    if text_key is None:
        raise ParseError(
            "认不出正文列。看到的字段有："
            + "、".join(list(records[0].keys())[:14])
        )
    # 单字母弱字段：必须有别的消息特征傍身才认
    if text_key in _WEAK_TEXT_KEYS and not evidence:
        raise ParseError(
            f"字段 {text_key!r} 太含糊，无法确认它是消息正文。"
            "请确认这是聊天记录导出文件；"
            "字段有：" + "、".join(list(records[0].keys())[:14])
        )

    return _Schema(
        time_key=_first_key(sample, TIME_KEYS),
        text_key=text_key,
        speaker_key=_first_key(sample, SPEAKER_KEYS),
        speaker_id_key=_first_key(sample, SPEAKER_ID_KEYS),
        is_send_key=_first_key(sample, IS_SEND_KEYS),
        type_key=_first_key(sample, TYPE_KEYS),
    )


#: 结构化导出里表示「非文本内容」的类型关键词
_MEDIA_TYPE_HINTS: tuple[tuple[str, str], ...] = (
    ("图片", "图片"), ("image", "图片"), ("photo", "图片"), ("img", "图片"),
    ("动画表情", "表情"), ("表情", "表情"), ("sticker", "表情"), ("emoji", "表情"),
    ("语音", "语音"), ("voice", "语音"), ("audio", "语音"),
    ("视频", "视频"), ("video", "视频"),
    ("文件", "文件"), ("file", "文件"), ("doc", "文件"),
    ("链接", "链接"), ("link", "链接"), ("url", "链接"), ("小程序", "链接"),
    ("转账", "转账"), ("红包", "红包"), ("名片", "名片"),
    ("位置", "位置"), ("location", "位置"),
    ("系统", "系统"), ("system", "系统"), ("撤回", "系统"),
    ("聊天记录", "聊天记录"), ("引用", "引用"),
)


def _classify_type(raw_type: Any) -> tuple[bool, bool, str]:
    """返回 ``(是否系统消息, 是否媒体, 媒体标记)``。"""
    if raw_type is None:
        return False, False, ""
    text = str(raw_type).lower()
    for hint, tag in _MEDIA_TYPE_HINTS:
        if hint in text:
            is_system = tag == "系统" or "系统" in text or "system" in text
            return is_system, (tag != "系统"), f"[{tag}]"
    # 微信 localType 的整数含义：10000 = 系统消息
    if re.fullmatch(r"\d+", text):
        if text == "10000":
            return True, False, ""
    return False, False, ""


def _html_to_text(raw: str) -> str:
    """把消息正文里的 HTML 片段转成纯文本。

    结构化导出常把正文写成一小段 HTML：外层是气泡，里面还嵌着一个
    ``message-time`` 时间标签。时间已经由时间戳字段提供，
    这里必须把它剥掉，否则每条消息的正文都会以「2025-11-03 21:01:02」
    开头，字数、话题、情感统计全部被污染。

    其余处理：媒体转成 ``[图片]`` 之类的标记、保留引用文字、
    剥标签、解码实体、压缩空白。
    """
    if "<" not in raw:
        return _html.unescape(raw).strip()

    s = raw
    # 先整体去掉时间标签（含内容），它不属于消息正文
    s = re.sub(
        r'<[^>]*class\s*=\s*["\'][^"\']*\bmessage-time\b[^"\']*["\'][^>]*>.*?</[^>]+>',
        "", s, flags=re.S | re.I,
    )
    s = re.sub(
        r'<[^>]*class\s*=\s*["\'][^"\']*\b(?:msg-time|chat-time|timestamp)\b'
        r'[^"\']*["\'][^>]*>.*?</[^>]+>',
        "", s, flags=re.S | re.I,
    )
    # 媒体/表情占位：alt 常见「图片消息」，统一成一个字
    s = re.sub(r"<img[^>]*alt\s*=\s*[\"']([^\"']*)[\"'][^>]*>",
               lambda m: f"[{_normalize_media_label(m.group(1))}]", s, flags=re.I)
    s = re.sub(r"<img[^>]*>", "[图片]", s, flags=re.I)
    # 换行
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.I)
    # 引用块：保留其文字，前面加个标记
    s = re.sub(r'<div[^>]*class\s*=\s*["\'][^"\']*quoted-message[^"\']*["\'][^>]*>',
               "\n[引用] ", s, flags=re.I)
    # 段落/块级元素之间补换行
    s = re.sub(r"</(?:div|p|li|h[1-6])>", "\n", s, flags=re.I)
    # 剥掉剩下的标签
    s = re.sub(r"<[^>]+>", "", s)
    s = _html.unescape(s)
    # 压缩空行与首尾空白
    s = re.sub(r"[ \t\u00a0]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n", s)
    lines = [ln.strip() for ln in s.split("\n")]
    return "\n".join(ln for ln in lines if ln).strip()


#: 把「图片消息」这类占位文字压成「图片」
_MEDIA_LABEL_NOISE = ("消息", "msg", "message")


def _normalize_media_label(label: str) -> str:
    """``图片消息`` → ``图片``；``图片`` 原样返回。"""
    text = (label or "").strip()
    if not text:
        return "图片"
    for noise in _MEDIA_LABEL_NOISE:
        if text.lower().endswith(noise) and len(text) > len(noise):
            text = text[: -len(noise)].strip()
    return text or "图片"


def records_to_messages(records: Sequence[dict], *,
                        schema: _Schema | None = None,
                        me_label: str = "我",
                        peer_label: str = "对方",
                        peer_id: str | None = None
                        ) -> tuple[list[Message], list[str]]:
    """把一批结构化记录转成 :class:`Message` 列表。

    说话人判定（私聊只有两个人，所以判不出「对方」的一律算系统消息，
    而不是硬塞给某一方——宁可少几条，也不能把关系算反）：

    1. 有「是否我发出」字段且为真 → ``me_label``；
    2. ``isSend`` 为假时：
       * 有发送人稳定 ID 且与会话里的对方 ID 一致 → ``peer_label``；
       * 否则若发送人名字与会话名一致 → ``peer_label``；
       * 再否则视为系统消息（例如自己另一台设备产生的回执）。
    3. 完全没有「是否我发出」字段时，退回「按发送人名字区分」。
    """
    schema = schema or _detect_schema(records)
    messages: list[Message] = []
    seen_names: list[str] = []
    idx = 0

    for rec in records:
        if not isinstance(rec, dict):
            continue

        # ---- 时间 ----
        ts = parse_time_value(rec.get(schema.time_key)) if schema.time_key else None
        if ts is None and schema.time_key:
            # 有些导出把可读时间放在另一个字段里
            for alt in ("formattedTime", "formatted_time", "time", "date"):
                if alt in rec:
                    ts = parse_time_value(rec.get(alt))
                    if ts is not None:
                        break

        # ---- 类型 ----
        raw_type = rec.get(schema.type_key) if schema.type_key else None
        is_system, is_media, media_tag = _classify_type(raw_type)
        # 没有类型字段（或没认出来）时，按正文里的媒体容器反推
        if not is_media and not is_system:
            raw_body = rec.get(schema.text_key)
            if isinstance(raw_body, str):
                b_sys, b_media, b_tag = _classify_body_html(raw_body)
                if b_media:
                    is_media, media_tag = True, b_tag

        # ---- 正文 ----
        raw_text = rec.get(schema.text_key)
        if raw_text is None:
            text = ""
        elif isinstance(raw_text, str):
            text = _html_to_text(raw_text)
            if is_media and not text:
                text = media_tag
        else:
            # content 是对象/数组：能取到文本就取，取不到就当媒体
            text = ""
            if isinstance(raw_text, dict):
                for k in ("text", "content", "title", "desc", "url"):
                    v = raw_text.get(k)
                    if isinstance(v, str) and v.strip():
                        text = v.strip()
                        break
            elif isinstance(raw_text, list):
                text = " ".join(str(x) for x in raw_text if isinstance(x, str)).strip()
            if not text:
                is_media = True
                text = media_tag or "[非文本消息]"

        if not text and not is_system:
            # 完全空白的记录没有分析价值，跳过（不伪造内容）
            continue
        # 正文本身是系统文案时也算系统消息（类型字段可能标错）
        if not is_system and is_system_text(text):
            is_system = True
        # 补媒体标记：正文已经以标记开头就不重复加
        if media_tag and not is_system and not text.startswith("["):
            text = f"{media_tag}{text}"
        # 媒体占位文字统一成简短标记，避免 [图片消息]/[图片] 混用
        if media_tag and not is_system:
            text = re.sub(r"^\[[^\[\]]{0,12}(?:消息|图片|表情|语音|视频|文件)\]",
                          media_tag, text)

        # ---- 说话人 ----
        speaker = ""
        if schema.is_send_key:
            raw_send = rec.get(schema.is_send_key)
            truthy = raw_send in (1, "1", True, "true", "True", "yes")
            if truthy:
                speaker = me_label
            else:
                name = ""
                if schema.speaker_key:
                    v = rec.get(schema.speaker_key)
                    if isinstance(v, str) and v.strip():
                        name = v.strip()
                sid = ""
                if schema.speaker_id_key:
                    v = rec.get(schema.speaker_id_key)
                    if isinstance(v, str) and v.strip():
                        sid = v.strip()

                if peer_id and sid and sid == peer_id:
                    speaker = peer_label
                elif peer_id and sid:
                    # 知道对方 ID，而这条的发送人不是对方：
                    # 说明是「我」的另一台设备产生的记录，不该算进任何一方
                    is_system = True
                    speaker = peer_label
                elif peer_id and name and name == peer_label:
                    speaker = peer_label
                elif not peer_id and not sid:
                    # 没有可用的对方标识（例如网页导出没有会话对象）时，
                    # 只能相信「不是我就算对方」——私聊本来只有两个人。
                    speaker = name or peer_label
                elif name and name != me_label:
                    speaker = name
                else:
                    # 判不出来源 → 当作系统消息，不计入任何一方
                    is_system = True
                    speaker = peer_label
        else:
            for key in (schema.speaker_key, schema.speaker_id_key):
                if key:
                    v = rec.get(key)
                    if isinstance(v, str) and v.strip():
                        speaker = v.strip()
                        break
        if not speaker:
            speaker = peer_label

        if not is_system and speaker not in seen_names:
            seen_names.append(speaker)

        messages.append(Message(
            index=idx,
            speaker=speaker,
            timestamp=ts,
            text=text,
            line_no=idx + 1,
            is_system=is_system,
            is_media=is_media and media_tag_of(text) is not None,
        ))
        idx += 1

    if not messages:
        raise ParseError("结构化数据里没有解析出任何一条消息。")
    return messages, seen_names


# --------------------------------------------------------------------------- #
# 顶层入口
# --------------------------------------------------------------------------- #

def looks_like_structured(text: str) -> bool:
    """粗判这份内容是不是结构化导出（而不是逐行文本）。"""
    head = text.lstrip()[:400]
    if head.startswith(("{", "[")):
        return True
    if "<html" in head.lower() or "<!doctype" in head.lower():
        return True
    if "application/json" in head.lower():
        return True
    return False


#: 从网页标题里取会话名的常见后缀
_TITLE_SUFFIXES = (
    " - 聊天记录", "-聊天记录", "_聊天记录", " - 微信聊天记录", " 聊天记录",
    " - Chat", " - WhatsApp Chat", " - Telegram",
)


def _title_contact_name(text: str) -> str | None:
    """从 ``<title>某某 - 聊天记录</title>`` 里取出联系人名。

    结构化 HTML 导出通常把对方的名字放在标题里；
    消息数组本身只有「是不是我发的」这一个布尔量，没有名字。
    """
    m = re.search(r"<title[^>]*>(.*?)</title>", text, re.S | re.I)
    if not m:
        return None
    title = _html.unescape(m.group(1)).strip()
    if not title:
        return None
    for suffix in _TITLE_SUFFIXES:
        if title.endswith(suffix):
            title = title[: -len(suffix)].strip()
            break
    return title or None


def parse_structured(text: str, *, name: str = "<string>",
                     me_label: str = "我",
                     peer_label: str = "对方") -> tuple[list[Message], list[str], str]:
    """解析 JSON / 内嵌数据的 HTML。

    返回 ``(messages, speakers, 说明)``；说明用于告诉用户
    「这是怎么认出来的」，比如 ``JSON 数组 3584 条``。
    """
    # ---- 1. 整体就是 JSON ----
    stripped = text.lstrip()
    if stripped.startswith(("{", "[")):
        data = _try_json(text)
        if data is None:
            raise ParseError("看起来是 JSON，但解析失败（可能被截断或含非法字符）。")
        return _from_data(data, me_label=me_label, peer_label=peer_label,
                          how="JSON 文档")

    # ---- 2. 单文件网页：内嵌数据 ----
    data = extract_embedded_json(text)
    if data is None:
        raise ParseError(
            "这像是一个网页，但里面没有找到可用的聊天数据。"
            "请确认它是导出工具生成的单文件记录（数据内嵌在页面里），"
            "而不是普通网页。"
        )
    # 网页导出把联系人名放在标题里，用它给「对方」一个真实名字
    contact = _title_contact_name(text)
    return _from_data(data, me_label=me_label,
                      peer_label=contact or peer_label,
                      how="网页内嵌数据")


def _from_data(data: Any, *, me_label: str, peer_label: str,
               how: str) -> tuple[list[Message], list[str], str]:
    """把已解析的 JSON 结构转成消息列表。"""
    records: Sequence[dict] | None = None
    session_name: str | None = None
    session_peer_id: str | None = None

    if isinstance(data, list):
        records = [x for x in data if isinstance(x, dict)]
    elif isinstance(data, dict):
        # 常见包裹结构：{messages: [...], session: {...}}
        for key in ("messages", "message", "data", "list", "records", "chatRecords",
                    "msgList", "items"):
            v = data.get(key)
            if isinstance(v, list) and v and isinstance(v[0], dict):
                records = [x for x in v if isinstance(x, dict)]
                break
        # 会话信息：用来给「对方」起个像样的名字，并拿到对方的稳定 ID。
        # 名字优先级：备注 > 显示名 > 昵称 —— 备注是用户自己起的，
        # 最接近他在聊天列表里看到的那个人。
        for key in ("session", "contact", "info", "chat", "conversation"):
            v = data.get(key)
            if isinstance(v, dict):
                for nk in ("remark", "displayName", "nickname", "name", "NickName"):
                    nv = v.get(nk)
                    if isinstance(nv, str) and nv.strip():
                        session_name = nv.strip()
                        break
                for ik in ("wxid", "username", "uid", "id", "peerId"):
                    iv = v.get(ik)
                    if isinstance(iv, str) and iv.strip():
                        session_peer_id = iv.strip()
                        break
            if session_name or session_peer_id:
                break
        if records is None:
            # 单个对象就是一条记录
            if any(k in data for k in TEXT_KEYS):
                records = [data]

    if not records:
        raise ParseError(
            "在结构化数据里没有找到消息数组。"
            "期望的形式是「数组」或「含 messages / data 等字段的对象」。"
        )

    peer = session_name or peer_label
    msgs, speakers = records_to_messages(
        records, me_label=me_label, peer_label=peer, peer_id=session_peer_id
    )

    with_time = sum(1 for m in msgs if m.timestamp is not None)
    how_detail = f"{how}，{len(msgs)} 条消息"
    if with_time < len(msgs):
        how_detail += f"（其中 {len(msgs) - with_time} 条没有可用时间戳）"
    if session_name:
        how_detail += f"，会话对象：{session_name}"
    return msgs, speakers, how_detail
