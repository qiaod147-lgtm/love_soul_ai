"""聊天记录解析:支持粘贴文本(微信/QQ 导出格式),自动识别角色。

设计要点:
- 解析只做"格式 → 结构化消息",不做"哪边是我",那一步让用户确认
- 标定"我"后,所有分析(沟通建议/复盘/统计)都知道谁是谁
- 长记录自动分段:不切段直接喂 LLM 会撑爆上下文
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable


# ponytail: 支持三种最常见的导出格式,识别不到就给用户清晰提示;
# 别做格式猜测魔改,猜错比"不识别"更伤信任。

# 微信导出常见格式:
#   2024-01-15 10:23:45 张三
#   张三: hello
#   10:23 张三
#   [10:23] 张三:
# iMessage:
#   [2024-01-15 10:23:45] 张三: hello
# QQ:
#   2024-01-15 10:23:45 张三
#   hello

_PATTERNS = [
    # 带日期时间 + 名字 + 冒号
    re.compile(r"^\s*(?P<ts>\d{4}[-/]\d{1,2}[-/]\d{1,2}\s+\d{1,2}:\d{2}(?::\d{2})?)\s+"
               r"(?P<name>[^:\n]{1,30}?)\s*[:：]\s*(?P<text>.*)$"),
    # 仅时间 + 名字 + 冒号
    re.compile(r"^\s*(?P<ts>\d{1,2}:\d{2}(?::\d{2})?)\s+"
               r"(?P<name>[^:\n]{1,30}?)\s*[:：]\s*(?P<text>.*)$"),
    # 方括号时间戳 + 名字
    re.compile(r"^\s*\[(?P<ts>\d{1,2}:\d{2}(?::\d{2})?)\]\s*"
               r"(?P<name>[^:\n]{1,30}?)\s*[:：]\s*(?P<text>.*)$"),
    # 微信 HTML 导出常见: <span class="nickname">张三</span>
    re.compile(r"^\s*<[^>]*?class=[\"']nickname[\"'][^>]*>(?P<name>[^<]+)</[^>]+>\s*[:：]?\s*(?P<text>.*)$"),
    # 微信 HTML 导出纯文本回退: 2024-01-15 10:23:45张三 hello(名字和时间没分隔)
    re.compile(r"^\s*(?P<ts>\d{4}[-/]\d{1,2}[-/]\d{1,2}\s+\d{1,2}:\d{2}(?::\d{2})?)(?P<name>[^:\s\n]{1,30}?)\s+(?P<text>.*)$"),
]


@dataclass
class ParsedMessage:
    speaker: str
    text: str
    ts: str = ""
    is_system: bool = False   # 撤回/时间提示等系统消息
    is_recall: bool = False   # 撤回消息(关键事件)


@dataclass
class ParsedChat:
    messages: list[ParsedMessage] = field(default_factory=list)
    participants: list[str] = field(default_factory=list)  # 按出现频次
    me: str = ""   # 用户标定的"我"是谁
    you: str = ""  # 对方
    parse_warnings: list[str] = field(default_factory=list)

    def total(self) -> int:
        return len(self.messages)

    def by_speaker(self) -> dict[str, int]:
        from collections import Counter
        return dict(Counter(m.speaker for m in self.messages if not m.is_system))


def parse_chat_log(text: str) -> list[ParsedMessage]:
    """纯解析,返回消息列表。不参与"我 vs 对方"判定。"""
    out: list[ParsedMessage] = []
    warnings: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line.strip():
            continue

        # 撤回消息优先识别(独立通道)
        recall = _match_recall(line)
        if recall is not None:
            out.append(ParsedMessage(
                speaker=recall["name"] or "对方",
                text="[撤回了一条消息]",
                ts=recall.get("ts", ""),
                is_system=True,
                is_recall=True,
            ))
            continue

        matched = False
        for pat in _PATTERNS:
            m = pat.match(line)
            if m:
                # HTML 解析路径可能没拿到 ts,容错补 None
                ts = m.groupdict().get("ts") or ""
                name = m.group("name").strip()
                text_body = m.group("text").strip()
                # 微信 HTML 偶发 name 字段含标签残余,二次清洗
                name = re.sub(r"<[^>]+>", "", name).strip()
                if not name or not text_body:
                    continue
                out.append(ParsedMessage(speaker=name, text=text_body, ts=ts))
                matched = True
                break

        if not matched:
            if out:
                out[-1].text += "\n" + line.strip()
            # 没有任何已识别消息前出现的孤儿行:静默丢掉,避免误识别
    return out


def _match_recall(line: str) -> dict | None:
    """识别撤回消息。返回 {"name": str, "ts": str} 或 None。

    支持:
      - "你 撤回了一条消息"
      - "张三 撤回了一条消息"
      - "10:24 李四 撤回了一条消息"  (行首时间戳无方括号)
      - "[10:24] 李四 撤回了一条消息"
    """
    m = re.match(
        r"^\s*(?:\[(?P<ts1>\d{1,2}:\d{2}(?::\d{2})?)\]|(?P<ts2>\d{1,2}:\d{2}(?::\d{2})?))?\s*"
        r"(?P<name>[\u4e00-\u9fa5A-Za-z_·.]{1,30}?)\s*撤回了一条消息\s*$",
        line,
    )
    if m:
        ts = m.group("ts1") or m.group("ts2") or ""
        return {"name": (m.group("name") or "").strip(), "ts": ts.strip()}
    return None


def detect_participants(messages: Iterable[ParsedMessage]) -> list[str]:
    """提取出现过的说话人(按出现频次排),最多返回前 5 个。系统消息不算。"""
    from collections import Counter
    c = Counter(m.speaker for m in messages if not m.is_system)
    return [name for name, _ in c.most_common(5)]


def identify_me(parsed: list[ParsedMessage], me: str) -> ParsedChat:
    """用户标定"我"是谁,自动推断"对方",返回完整 ParsedChat。

    规则:
    - me 必须在 participants 里(否则标错比不标更糟,直接抛错)
    - you = 频次最高的"非我"说话人
    - 仅 2 人时,you 唯一;>2 人时(群聊)取频次最高的非我者,其他人归为"others"
    """
    participants = detect_participants(parsed)
    if me not in participants:
        raise ValueError(
            f"未找到标定的'我'={me!r},参与者只有: {participants}。"
            "请确认名字是否完全一致(含空格/标点)"
        )

    others = [p for p in participants if p != me]
    you = others[0] if others else ""

    return ParsedChat(
        messages=parsed,
        participants=participants,
        me=me,
        you=you,
    )


def chunk_by_messages(
    messages: list[ParsedMessage], max_messages: int = 80, overlap: int = 5
) -> list[list[ParsedMessage]]:
    """按消息数切段,带 overlap 保证上下文连贯。

    ponytail: 80 条是经验值;微信长对话里一个话题大约 30-60 轮来回,
    再大就超大多数模型的舒服区。overlap=5 是为了不丢"上一段最后一句 + 这段第一句"的关联。
    """
    if not messages:
        return []
    if len(messages) <= max_messages:
        return [messages]
    chunks: list[list[ParsedMessage]] = []
    step = max(1, max_messages - overlap)
    i = 0
    while i < len(messages):
        chunk = messages[i : i + max_messages]
        chunks.append(chunk)
        if i + max_messages >= len(messages):
            break
        i += step
    return chunks


def chunk_by_time(
    messages: list[ParsedMessage], gap_minutes: int = 30
) -> list[list[ParsedMessage]]:
    """按时间间隔切段:相邻消息间隔 > gap_minutes 视为新会话。

    ponytail: 用 timestamp 解析(支持 'YYYY-MM-DD HH:MM:SS' / 'HH:MM:SS'),
    解析失败的时间戳按"无间隔"处理,不强行切。
    """
    from datetime import datetime

    def _parse_ts(ts: str):
        if not ts:
            return None
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M",
                    "%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M",
                    "%H:%M:%S", "%H:%M"):
            try:
                return datetime.strptime(ts, fmt)
            except ValueError:
                continue
        return None

    if not messages:
        return []

    chunks: list[list[ParsedMessage]] = [[messages[0]]]
    last_ts = _parse_ts(messages[0].ts)
    for m in messages[1:]:
        ts = _parse_ts(m.ts)
        if last_ts and ts and (ts - last_ts).total_seconds() > gap_minutes * 60:
            chunks.append([])
        chunks[-1].append(m)
        if ts:
            last_ts = ts
    return chunks


def format_for_llm(messages: list[ParsedMessage], chat: ParsedChat | None = None) -> str:
    """把解析结果转成 LLM 友好的纯文本。

    如果传了 chat 且已标定 me,会在每行前加角色标签 (我/对方),让 LLM 不用猜。
    """
    if not messages:
        return ""
    role_map: dict[str, str] = {}
    if chat and chat.me:
        role_map[chat.me] = "我"
        if chat.you:
            role_map[chat.you] = "对方"
    lines = []
    for m in messages:
        if m.is_recall:
            lines.append(f"{m.speaker} 撤回了一条消息")
            continue
        prefix = f"[{m.ts}] " if m.ts else ""
        role = role_map.get(m.speaker, m.speaker)
        lines.append(f"{prefix}{role}: {m.text}")
    return "\n".join(lines)


__all__ = [
    "ParsedMessage",
    "ParsedChat",
    "parse_chat_log",
    "identify_me",
    "detect_participants",
    "chunk_by_messages",
    "chunk_by_time",
    "format_for_llm",
]
