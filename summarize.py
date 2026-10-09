"""滚动摘要:把早期对话压缩成一段,塞回 LLM 上下文。

触发:每次写入消息后,如果 LLM 上下文窗口的"非摘要部分"超过 30 轮,
就把 1..N 轮让 LLM 总结成一段,删掉原文,只保留摘要 + 最近 30 轮。
"""
from __future__ import annotations

from .llm import _client
from .persona import build_system_prompt
from .storage import HistoryDB


# ponytail: 30 轮是经验值,大多数模型 8k-128k 上下文都扛得住;
# 真要更长,在 storage 层加 token 估算截断,这里不动。
CONTEXT_WINDOW_TURNS = 30


def maybe_summarize(db: HistoryDB, user_id: str, model: str | None) -> None:
    """检查并触发摘要折叠。"""
    recent = db.recent(user_id, limit=CONTEXT_WINDOW_TURNS + 50)
    if len(recent) <= CONTEXT_WINDOW_TURNS:
        return  # 还不到折叠阈值

    latest_sum = db.latest_summary(user_id)
    cutoff_id = latest_sum[0] if latest_sum else 0
    older = db.messages_before(user_id, before_id=recent[0]["id"] + 1)  # 不含窗口内
    if latest_sum:
        # 已有摘要:跳过摘要覆盖的那段
        older = [m for m in older if m["id"] > cutoff_id]
    if len(older) < 10:
        return  # 至少攒 10 条再折叠,避免每次都花 token

    # 把早期对话压缩成一段中文摘要
    text = "\n".join(f"{m['role']}: {m['content']}" for m in older)
    summary = _summarize(text, model)
    if not summary:
        return

    until_id = older[-1]["id"]
    db.save_summary(user_id, until_id, summary)


def build_context(db: HistoryDB, user_id: str, extra_system: str = "") -> list[dict]:
    """组装喂给 LLM 的 messages:system + 摘要 + 最近窗口。"""
    msgs: list[dict] = [{"role": "system", "content": build_system_prompt(extra_system)}]

    latest_sum = db.latest_summary(user_id)
    if latest_sum:
        _, content = latest_sum
        msgs.append({
            "role": "system",
            "content": f"# 早期对话摘要(用于保留长期记忆)\n{content}",
        })

    for m in db.recent(user_id, limit=CONTEXT_WINDOW_TURNS):
        msgs.append({"role": m["role"], "content": m["content"]})
    return msgs


def _summarize(text: str, model: str | None) -> str:
    """用 LLM 把一段对话压成中文摘要;失败返回空串。"""
    try:
        client = _client()
        resp = client.chat.completions.create(
            model=model or "gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是一个对话摘要助手。请把用户与恋爱关系 AI 导师的早期对话"
                        "压缩成一段简洁的中文摘要(200-400 字),保留:关键事件、用户情绪、"
                        "AI 给出的建议要点、未解决的核心问题。不要添加评价,不要省略细节。"
                    ),
                },
                {"role": "user", "content": text},
            ],
            temperature=0.2,
        )
        return (resp.choices[0].message.content or "").strip()
    except Exception:  # noqa: BLE001
        return ""


__all__ = ["maybe_summarize", "build_context", "CONTEXT_WINDOW_TURNS"]
