"""FastAPI Web UI:同一套 storage/summarize/llm,前后端分离最小版。

跑法:
    pip install fastapi uvicorn
    uvicorn love_soul_ai.web:app --reload

打开 http://127.0.0.1:8000
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

# ponytail: 图片大小上限,免得有人拖个 50MB 截图把上下文撑爆。
_MAX_IMG_BYTES = 4 * 1024 * 1024
_ALLOWED_IMG = {"image/jpeg", "image/png", "image/gif", "image/webp"}

from .llm import chat, chat_with_context
from .persona import build_system_prompt
from .storage import get_db
from .summarize import build_context
from .upload import (
    detect_participants,
    format_for_llm,
    identify_me,
    parse_chat_log,
)


_HERE = Path(__file__).resolve().parent
_STATIC = _HERE / "web_static"


app = FastAPI(title="Love Soul AI", version="0.2.0")


# ponytail: 静态文件先确保目录存在,没建过也别让启动挂。
_STATIC.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=_STATIC), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(_STATIC / "index.html")


class ChatReq(BaseModel):
    message: str = Field(..., min_length=1, max_length=8000)
    user_id: str = "default"
    extra_system: str = ""


@app.post("/api/chat")
async def api_chat(
    message: str = Form(""),
    user_id: str = Form("default"),
    extra_system: str = Form(""),
    images: list[UploadFile] = File(default=[]),
) -> JSONResponse:
    """普通聊天:支持文字 + 图片(图文混合,多模态)。

    历史始终落库(普通聊天就该有历史)。
    """
    if not message.strip() and not images:
        raise HTTPException(400, "message 和 images 至少需要一个")
    try:
        # ponytail: 走 chat_with_images,无图时它也工作(空 list)
        img_bytes: list[tuple[bytes, str]] = []
        for f in images or []:
            ct = (f.content_type or "").lower()
            if ct not in _ALLOWED_IMG:
                raise HTTPException(400, f"不支持的图片类型: {ct}")
            data = await f.read()
            if not data:
                continue
            if len(data) > _MAX_IMG_BYTES:
                raise HTTPException(400, f"图片 {f.filename or '?'} 超过 {_MAX_IMG_BYTES // 1024}KB 限制")
            img_bytes.append((data, ct))
        from .llm import chat_with_images
        reply = chat_with_images(
            message.strip(),
            img_bytes,
            user_id=user_id or "default",
            extra_system=extra_system or "",
        )
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, str(e)) from e
    return JSONResponse({"reply": reply, "images_used": len(img_bytes)})


class HistoryReq(BaseModel):
    user_id: str = "default"
    limit: int = Field(50, ge=1, le=500)


@app.post("/api/history")
def api_history(req: HistoryReq) -> JSONResponse:
    db = get_db()
    msgs = db.recent(req.user_id, limit=req.limit)
    return JSONResponse({
        "messages": msgs,
        "total": db.count(req.user_id),
        "summary_until": db.latest_summary(req.user_id)[0] if db.latest_summary(req.user_id) else 0,
    })


class SearchReq(BaseModel):
    keyword: str = Field(..., min_length=1)
    user_id: str = "default"
    limit: int = Field(20, ge=1, le=100)


@app.post("/api/search")
def api_search(req: SearchReq) -> JSONResponse:
    db = get_db()
    hits = db.search(req.user_id, req.keyword, limit=req.limit)
    return JSONResponse({"hits": hits})


class ClearReq(BaseModel):
    user_id: str = "default"


@app.post("/api/clear")
def api_clear(req: ClearReq) -> JSONResponse:
    db = get_db()
    db.clear(req.user_id)
    return JSONResponse({"ok": True})


class UploadReq(BaseModel):
    text: str = Field(..., min_length=1)
    mode: str = Field("1")  # 1=沟通建议 2=关系复盘 3=自定义
    custom: str = ""
    note: str = ""
    me: str = ""            # 用户标定的"我"是谁
    user_id: str = "default"
    extra_system: str = ""
    save_to_history: bool = False
    chunk_strategy: str = "none"  # none | message | time
    chunk_size: int = 80
    chunk_gap_minutes: int = 30


@app.post("/api/upload")
def api_upload(req: UploadReq) -> JSONResponse:
    parsed = parse_chat_log(req.text)
    if not parsed:
        raise HTTPException(400, "未能解析出任何消息;请确认格式形如 '10:23 张三: 内容'")
    participants = detect_participants(parsed)

    # 标定"我"和"对方"
    chat_obj = None
    if req.me:
        if req.me not in participants:
            raise HTTPException(400, f"未找到标定的'我'={req.me!r},参与者: {participants}")
        chat_obj = identify_me(parsed, req.me)

    # 切段策略
    from .upload import chunk_by_messages, chunk_by_time
    chunks: list[list]
    if req.chunk_strategy == "message":
        chunks = chunk_by_messages(parsed, max_messages=req.chunk_size)
    elif req.chunk_strategy == "time":
        chunks = chunk_by_time(parsed, gap_minutes=req.chunk_gap_minutes)
    else:
        chunks = [parsed]

    # 每段独立分析,最后再合成总报告
    chunk_replies: list[str] = []
    for idx, chunk in enumerate(chunks, start=1):
        formatted = format_for_llm(chunk, chat=chat_obj)
        if req.mode == "1":
            task = (
                "请基于以下这段聊天记录,给出沟通建议。结构:\n"
                "1) 现状判断(1-2 句,这段对话处于什么状态)\n"
                "2) 给'我'的回复建议(具体到下一句怎么说,2-3 句)\n"
                "3) 对方可能的反应与我的应对(1-2 句)\n"
                "4) 需要警惕的边界问题(如有)\n"
                "请基于人格设定中'先共情后建议、拒绝 PUA'的原则回答。"
            )
        elif req.mode == "2":
            task = (
                "请对这段对话做关系复盘。结构:\n"
                "1) 客观发生了什么(2-3 句,不带评判)\n"
                "2) 双方的情绪与潜在需求(各 1-2 句)\n"
                "3) 沟通模式(出现了哪些模式:逃避/指责/讨好/超理智/一致性)\n"
                "4) 关键转折点(具体到第几句话)\n"
                "5) 改进建议(下次类似场景,具体怎么做)\n"
                "请基于人格设定中'平等、尊重、真诚'的原则回答。"
            )
        elif req.mode == "3":
            if not req.custom.strip():
                raise HTTPException(400, "自定义模式需要提供 custom 字段")
            task = req.custom.strip()
        else:
            raise HTTPException(400, f"未知 mode: {req.mode}")

        seg_header = (
            f"参与者:{', '.join(participants)}\n"
            f"{'我 = ' + chat_obj.me + (', 对方 = ' + chat_obj.you if chat_obj and chat_obj.you else '')}\n"
            f"用户补充说明:{req.note or '(无)'}\n"
            f"{f'(分段 {idx}/{len(chunks)})' if len(chunks) > 1 else ''}\n\n"
            f"聊天记录:\n{formatted}"
        )
        msgs = [
            {"role": "system", "content": build_system_prompt(req.extra_system) + "\n\n# 任务\n" + task},
            {"role": "user", "content": seg_header},
        ]
        try:
            r = chat_with_context(msgs, extra_system=req.extra_system)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(500, str(e)) from e
        chunk_replies.append(f"## 段 {idx}\n\n{r}" if len(chunks) > 1 else r)

    # 多段时再做一次总报告(把段摘要合成)
    if len(chunks) > 1:
        synth_prompt = (
            "以下是按时间段/消息数切分的多段独立分析,请合成一份整体结论,保留关键洞察但不重复细节:\n\n"
            + "\n\n---\n\n".join(chunk_replies)
        )
        try:
            final = chat_with_context([
                {"role": "system", "content": build_system_prompt(req.extra_system) +
                 "\n\n# 任务\n你是关系复盘助手,请把多段分析合成为一份简洁的总报告(800 字内),"
                 "保留最关键的模式、转折、改进建议。"},
                {"role": "user", "content": synth_prompt},
            ], extra_system=req.extra_system)
        except Exception as e:  # noqa: BLE001
            final = "[合成失败] " + str(e) + "\n\n" + "\n\n---\n\n".join(chunk_replies)
    else:
        final = chunk_replies[0]

    if req.save_to_history:
        db = get_db()
        task_used = req.custom.strip() if req.mode == "3" else f"mode={req.mode}"
        db.append(req.user_id, "user", f"[上传分析] {task_used}\n\n{req.text[:2000]}")
        db.append(req.user_id, "assistant", final)

    return JSONResponse({
        "reply": final,
        "chunk_replies": chunk_replies if len(chunks) > 1 else [],
        "parsed_count": len(parsed),
        "participants": participants,
        "me": chat_obj.me if chat_obj else "",
        "you": chat_obj.you if chat_obj else "",
        "chunks": len(chunks),
    })


# ponytail: 图文混合上传。和 /api/upload 共用同样的切段/任务模板,
# 区别只在 user 消息里把图片作为 image_url 一并发给 LLM(多模态)。
def _build_upload_task(mode: str, custom: str) -> str:
    if mode == "1":
        return (
            "请基于以下这段聊天记录(可能附带截图),给出沟通建议。结构:\n"
            "1) 现状判断(1-2 句,这段对话处于什么状态)\n"
            "2) 给'我'的回复建议(具体到下一句怎么说,2-3 句)\n"
            "3) 对方可能的反应与我的应对(1-2 句)\n"
            "4) 需要警惕的边界问题(如有)\n"
            "请基于人格设定中'先共情后建议、拒绝 PUA'的原则回答。"
        )
    if mode == "2":
        return (
            "请对这段对话做关系复盘。结构:\n"
            "1) 客观发生了什么(2-3 句,不带评判)\n"
            "2) 双方的情绪与潜在需求(各 1-2 句)\n"
            "3) 沟通模式(出现了哪些模式:逃避/指责/讨好/超理智/一致性)\n"
            "4) 关键转折点(具体到第几句话)\n"
            "5) 改进建议(下次类似场景,具体怎么做)\n"
            "请基于人格设定中'平等、尊重、真诚'的原则回答。"
        )
    if mode == "3":
        if not custom.strip():
            raise HTTPException(400, "自定义模式需要提供 custom 字段")
        return custom.strip()
    raise HTTPException(400, f"未知 mode: {mode}")


@app.post("/api/upload/chat")
async def api_upload_chat(
    text: str = Form(""),
    mode: str = Form("1"),
    custom: str = Form(""),
    note: str = Form(""),
    me: str = Form(""),
    user_id: str = Form("default"),
    extra_system: str = Form(""),
    save_to_history: str = Form("true"),  # ponytail: 默认存,用户显式 false 才不存
    images: list[UploadFile] = File(default=[]),
) -> JSONResponse:
    """图文混合上传:text 必填,images 可选(0-N 张)。"""
    text = (text or "").strip()
    if not text and not images:
        raise HTTPException(400, "text 和 images 至少需要一个")

    save_flag = str(save_to_history).lower() in ("1", "true", "yes")

    # 读图片 -> data URI(尺寸已限)
    image_parts: list[dict] = []
    for f in images or []:
        ct = (f.content_type or "").lower()
        if ct not in _ALLOWED_IMG:
            raise HTTPException(400, f"不支持的图片类型: {ct}(允许: jpg/png/gif/webp)")
        data = await f.read()
        if not data:
            continue
        if len(data) > _MAX_IMG_BYTES:
            raise HTTPException(400, f"图片 {f.filename or '?'} 超过 {_MAX_IMG_BYTES // 1024}KB 限制")
        import base64
        b64 = base64.b64encode(data).decode("ascii")
        image_parts.append({
            "type": "image_url",
            "image_url": {"url": f"data:{ct};base64,{b64}"},
        })

    parsed = parse_chat_log(text) if text else []
    participants = detect_participants(parsed) if parsed else []
    chat_obj = None
    if me and parsed:
        if me not in participants:
            raise HTTPException(400, f"未找到标定的'我'={me!r},参与者: {participants}")
        chat_obj = identify_me(parsed, me)

    task = _build_upload_task(mode, custom)
    who_line = (
        f"我 = {chat_obj.me}"
        + (f", 对方 = {chat_obj.you}" if chat_obj.you else "")
        if chat_obj else ""
    )
    seg_hdr = (
        f"参与者:{', '.join(participants)}\n"
        f"{who_line}\n"
        f"用户补充说明:{note or '(无)'}\n\n"
        f"聊天记录:\n"
    )
    user_content: list[dict] = []
    if image_parts:
        user_content.append({
            "type": "text",
            "text": "以下是用户上传的聊天截图,请结合截图内容理解后再回答。\n\n",
        })
        user_content.extend(image_parts)
    user_content.append({
        "type": "text",
        "text": seg_hdr + (text or "(无文字聊天记录,仅图片)"),
    })

    msgs = [
        {"role": "system", "content": build_system_prompt(extra_system) + "\n\n# 任务\n" + task},
        {"role": "user", "content": user_content},
    ]
    try:
        final = chat_with_context(msgs, extra_system=extra_system)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, str(e)) from e

    if save_flag:
        db = get_db()
        task_used = custom.strip() if mode == "3" else f"mode={mode}"
        # ponytail: 图存 user 那条(images_json),content 只放文字 + 任务标记
        db.append(
            user_id, "user",
            f"[聊天分析] {task_used}\n\n{text[:2000]}",
            [{"data_uri": ip["image_url"]["url"], "ct": "image", "name": "uploaded"} for ip in image_parts],
        )
        db.append(user_id, "assistant", final)

    return JSONResponse({
        "reply": final,
        "parsed_count": len(parsed),
        "participants": participants,
        "me": chat_obj.me if chat_obj else "",
        "you": chat_obj.you if chat_obj else "",
        "images_used": len(image_parts),
    })


# ponytail: 这个接口主要给前端调试用,生产前再加鉴权。
@app.get("/api/health")
def health() -> JSONResponse:
    return JSONResponse({"ok": True, "version": app.version})


__all__ = ["app"]
