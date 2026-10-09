"""对话历史管理(进程内,够用即可)。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable


# ponytail: 200 轮是经验值,普通长聊天(求复合、复盘整段关系)够用;
# 真要再长,要么按 token 估算截断,要么上摘要 + 向量库。先按"轮数"走,简单可解释。
DEFAULT_MAX_TURNS = 200


@dataclass
class Session:
    """一次会话:用户级共享,内存里就够。"""
    messages: list[dict] = field(default_factory=list)
    max_turns: int = DEFAULT_MAX_TURNS

    def add(self, role: str, content: str) -> None:
        # 系统提示永远在最前面,不参与截断,避免人格中途漂移
        if role == "system":
            # 去重:同 role 的 system 只保留最后一条
            self.messages = [m for m in self.messages if m["role"] != "system"]
            self.messages.insert(0, {"role": "system", "content": content})
            return
        self.messages.append({"role": role, "content": content})
        self._truncate()

    def _truncate(self) -> None:
        """保留 system + 最近 N*2 条(user/assistant 配对)。"""
        system = [m for m in self.messages if m["role"] == "system"]
        rest = [m for m in self.messages if m["role"] != "system"]
        cap = self.max_turns * 2
        if len(rest) > cap:
            rest = rest[-cap:]
        self.messages = system + rest

    def to_openai(self) -> list[dict]:
        return list(self.messages)

    def clear(self) -> None:
        self.messages.clear()


# ponytail: 进程内 dict,单用户 CLI 够用。多用户/重启恢复时再上 SQLite。
SESSIONS: dict[str, Session] = {}


def get_session(user_id: str = "default", max_turns: int = DEFAULT_MAX_TURNS) -> Session:
    s = SESSIONS.get(user_id)
    if s is None:
        s = Session(max_turns=max_turns)
        SESSIONS[user_id] = s
    return s


def export_messages(messages: Iterable[dict]) -> list[dict]:
    """纯函数,方便测试与导出。"""
    return [dict(m) for m in messages]
