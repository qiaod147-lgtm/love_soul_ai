"""LLM 调用层。

设计目标:能跑就行。默认走 OpenAI 兼容协议,任何兼容端点
(DeepSeek、月之暗面、智谱、本地 Ollama 等)都能塞进来。
"""
from __future__ import annotations

import os
from typing import Iterable

from .memory import Session  # 保留,旧调用点不会断
from .persona import build_system_prompt
from .storage import get_db
# ponytail: 故意不在这里 from .summarize import ...,会与 summarize.py 的
# `from .llm import _client` 撞循环导入。改在 chat() 内部按需 import。


def _client():
    """懒加载 openai 客户端,缺包时给出明确错误。"""
    try:
        from openai import OpenAI
    except ImportError as e:  # pragma: no cover
        raise RuntimeError(
            "需要安装 openai SDK: pip install openai"
        ) from e
    return OpenAI(
        api_key=os.environ.get("OPENAI_API_KEY", ""),
        base_url=os.environ.get("OPENAI_BASE_URL") or None,
    )


def _model() -> str:
    return os.environ.get("LOVE_SOUL_MODEL", "gpt-4o-mini")


def chat(
    user_message: str,
    session: Session | None = None,  # 兼容旧签名
    *,
    user_id: str = "default",
    model: str | None = None,
    extra_system: str = "",
) -> str:
    """单轮对话。

    持久化走 SQLite:user/assistant 都落库,任何时候不丢;
    LLM 上下文走 summarize.build_context():system + 摘要 + 最近 30 轮。
    session 参数保留以兼容旧调用,实际不依赖。
    """
    if not user_message.strip():
        return "(你说的是空白消息,我没有接收到任何内容哦)"

    from .summarize import build_context, maybe_summarize  # 避免循环导入

    db = get_db()
    chosen = model or _model()

    # 1. 持久化用户消息
    db.append(user_id, "user", user_message)

    # 2. 触发摘要折叠(异步思维,但这里直接做;5000 轮场景下也不是热路径)
    maybe_summarize(db, user_id, chosen)

    # 3. 组装 LLM 上下文
    msgs = build_context(db, user_id, extra_system=extra_system)

    # 4. 调 LLM
    client = _client()
    resp = client.chat.completions.create(
        model=chosen,
        messages=msgs,
        temperature=0.7,
    )
    reply = (resp.choices[0].message.content or "").strip()

    # 5. 持久化助手消息
    db.append(user_id, "assistant", reply)
    return reply


def chat_with_context(
    messages: list[dict],
    *,
    model: str | None = None,
    extra_system: str = "",
) -> str:
    """给 /upload 用的:传一组完整 messages(已经带好系统提示),调一次 LLM。"""
    if not messages:
        return ""
    client = _client()
    chosen = model or _model()
    if extra_system:
        # 插到最前
        messages = [{"role": "system", "content": build_system_prompt(extra_system)}] + list(messages)
    resp = client.chat.completions.create(
        model=chosen,
        messages=messages,
        temperature=0.5,
    )
    return (resp.choices[0].message.content or "").strip()


def chat_with_images(
    user_message: str,
    images: list[tuple[bytes, str]],
    user_id: str = "default",
    extra_system: str = "",
) -> str:
    """普通聊天入口支持图片:文字 + N 张图混合,走多模态 LLM。

    images: [(bytes, content_type), ...]  — 字节 + MIME,大小校验由调用方负责。
    """
    from .summarize import build_context, maybe_summarize  # 避免循环导入

    db = get_db()
    chosen = _model()
    # 1. 持久化 user 消息(图存 data URI 入 images_json,content 留纯文字)
    import base64
    img_records: list[dict] = []
    for data, ct in images:
        b64 = base64.b64encode(data).decode("ascii")
        img_records.append({"data_uri": f"data:{ct};base64,{b64}", "ct": ct, "name": "pasted"})
    db.append(user_id, "user", user_message, img_records)

    # 2. 摘要折叠
    maybe_summarize(db, user_id, chosen)

    # 3. 构造 user content(图在前,文在后,符合 LLM 多模态惯例)
    import base64
    user_content: list[dict] = []
    if images:
        for data, ct in images:
            b64 = base64.b64encode(data).decode("ascii")
            user_content.append({
                "type": "image_url",
                "image_url": {"url": f"data:{ct};base64,{b64}"},
            })
    user_content.append({"type": "text", "text": user_message or "(仅图片)"})

    # 4. 组 LLM messages
    msgs = build_context(db, user_id, extra_system=extra_system)
    # 当前轮 user 消息替换为图文混合(LLM 看得到);历史轮的图存展示,不再回传
    if msgs and msgs[-1].get("role") == "user":
        msgs[-1] = {"role": "user", "content": user_content}

    # 5. 调 LLM
    client = _client()
    resp = client.chat.completions.create(
        model=chosen, messages=msgs, temperature=0.7,
    )
    reply = (resp.choices[0].message.content or "").strip()

    # 6. 持久化 assistant
    db.append(user_id, "assistant", reply)
    return reply


def history_summary(user_id: str = "default", limit: int = 30) -> str:
    """给 UI 展示用的历史摘要(最近 N 条),不要直接喂给 LLM。"""
    db = get_db()
    msgs = db.recent(user_id, limit=limit)
    return "\n".join(f"{m['role']}: {m['content']}" for m in msgs)


def list_models_hint() -> str:
    return (
        "可用环境变量:\n"
        "  OPENAI_API_KEY     必填,你的 API Key\n"
        "  OPENAI_BASE_URL    可选,兼容端点(如 https://api.deepseek.com/v1)\n"
        "  LOVE_SOUL_MODEL    可选,模型名(默认 gpt-4o-mini)\n"
    )


__all__ = ["chat", "chat_with_context", "history_summary", "list_models_hint", "Iterable"]
