"""CLI 入口:文字对话,输入即回复,exit 退出。"""
from __future__ import annotations

import argparse
import sys

from .llm import chat, chat_with_context, history_summary, list_models_hint
from .persona import build_system_prompt
from .storage import get_db
from .summarize import build_context
from .upload import (
    detect_participants,
    format_for_llm,
    identify_me,
    parse_chat_log,
    chunk_by_messages,
    chunk_by_time,
)


BANNER = """\
================================================
   Love Soul AI · 你的恋爱关系导师
   (输入文字开始对话,输入 /help 查看指令,
    输入 exit 或 Ctrl+C 退出)
================================================
"""


HELP = """\
指令:
  /help           显示本帮助
  /clear          清空当前会话历史(全量,不可恢复)
  /history [N]    查看最近 N 条历史(默认 30)
  /search <关键词> 跨历史检索(FTS5)
  /upload         进入聊天记录上传模式(粘完按 EOF/Ctrl+D 结束)
  /system         显示当前系统提示词概要
  /stats          查看消息总数与摘要状态
  exit            退出程序
"""


def repl(user_id: str = "default", extra_system: str = "") -> int:
    print(BANNER)
    if not _has_key():
        print("[提示] 未检测到 OPENAI_API_KEY,先设置环境变量再开始对话。")
        print(list_models_hint())

    db = get_db()
    while True:
        try:
            line = input("\n你: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见,愿你拥有健康、真诚的关系。")
            return 0

        if not line:
            continue
        if line in ("exit", "quit", "q"):
            print("再见,愿你拥有健康、真诚的关系。")
            return 0

        if line.startswith("/"):
            _handle_command(line, user_id, extra_system, db)
            continue

        try:
            reply = chat(line, user_id=user_id, extra_system=extra_system)
        except Exception as e:  # noqa: BLE001
            print(f"\n[出错了] {e}\n你可以重试,或检查 API Key/网络。")
            continue

        print(f"\nLove Soul AI:\n{reply}")

    return 0


def _handle_command(line: str, user_id: str, extra_system: str, db) -> None:
    parts = line.split(maxsplit=1)
    cmd = parts[0]
    arg = parts[1] if len(parts) > 1 else ""

    if cmd == "/help":
        print(HELP)
    elif cmd == "/clear":
        confirm = input("确认清空所有历史?(yes/no): ").strip().lower()
        if confirm == "yes":
            db.clear(user_id)
            print("[已清空全部历史]")
        else:
            print("[已取消]")
    elif cmd == "/history":
        n = int(arg) if arg.isdigit() else 30
        print(history_summary(user_id, limit=n) or "(暂无历史)")
    elif cmd == "/search":
        if not arg:
            print("用法: /search <关键词>")
            return
        hits = db.search(user_id, arg)
        if not hits:
            print("(没找到相关历史)")
            return
        print(f"找到 {len(hits)} 条:")
        for h in hits:
            print(f"  [{h['id']}] {h['role']}: {h['content'][:120]}")
    elif cmd == "/upload":
        _upload_flow(user_id, extra_system)
    elif cmd == "/system":
        from .persona import build_system_prompt as _bsp
        print(_bsp(extra_system)[:400] + "...")
    elif cmd == "/stats":
        latest = db.latest_summary(user_id)
        print(f"消息总数: {db.count(user_id)}")
        print(f"摘要覆盖至 id: {latest[0] if latest else '无'}")
    else:
        print(f"未知指令: {cmd},输入 /help 查看帮助")


def _upload_flow(user_id: str, extra_system: str) -> None:
    """聊天记录上传:粘文本 → 解析 → 选"我" → 选模式 → AI 沟通建议/关系复盘。"""
    print("进入聊天记录上传模式。")
    print("请粘贴聊天记录(支持微信/QQ/iMessage 常见导出格式),")
    print("单独一行输入 ::: 结束输入,或按 Ctrl+D / Ctrl+C 取消。")
    print("说明(可选,直接回车跳过):")
    note = input("  > ").strip()

    print("粘贴聊天记录:")
    lines: list[str] = []
    try:
        while True:
            buf = input()
            if buf.strip() == ":::":
                break
            lines.append(buf)
    except (EOFError, KeyboardInterrupt):
        print("\n[已取消]")

    text = "\n".join(lines).strip()
    if not text:
        print("[未检测到聊天记录,已取消]")
        return

    parsed = parse_chat_log(text)
    if not parsed:
        print("[未能解析出任何消息]")
        print("提示:每行格式形如  '10:23 张三: 你好'  或  '[2024-01-15 10:23] 张三: 你好'")
        return

    participants = detect_participants(parsed)
    print(f"解析到 {len(parsed)} 条消息,参与者: {', '.join(participants)}")

    # 标定"我"
    print("请选择'我'是谁(直接回车跳过,让 AI 自己判断):")
    for i, name in enumerate(participants, 1):
        print(f"  {i}. {name}")
    pick = input("选(数字,默认跳过): ").strip()
    me = ""
    if pick.isdigit() and 1 <= int(pick) <= len(participants):
        me = participants[int(pick) - 1]
    if me:
        print(f"已标定: 我={me}")

    print("\n请选择分析模式:")
    print("  1. 沟通建议 —— 给你/对方各自的下一步回复建议")
    print("  2. 关系复盘 —— 分析这段对话里发生了什么、模式、关键点")
    print("  3. 自定义问题(输入后回车)")
    mode = input("选择(1/2/3,默认 1): ").strip() or "1"

    if mode == "1":
        task = (
            "请基于以上聊天记录,给出沟通建议。结构:\n"
            "1) 现状判断(1-2 句,这段对话处于什么状态)\n"
            "2) 给'我'的回复建议(具体到下一句怎么说,2-3 句)\n"
            "3) 对方可能的反应与我的应对(1-2 句)\n"
            "4) 需要警惕的边界问题(如有)\n"
            "请基于人格设定中'先共情后建议、拒绝 PUA'的原则回答。"
        )
    elif mode == "2":
        task = (
            "请对这段对话做关系复盘。结构:\n"
            "1) 客观发生了什么(2-3 句,不带评判)\n"
            "2) 双方的情绪与潜在需求(各 1-2 句)\n"
            "3) 沟通模式(出现了哪些模式:逃避/指责/讨好/超理智/一致性)\n"
            "4) 关键转折点(具体到第几句话)\n"
            "5) 改进建议(下次类似场景,具体怎么做)\n"
            "请基于人格设定中'平等、尊重、真诚'的原则回答。"
        )
    else:
        custom = input("你的问题: ").strip()
        if not custom:
            print("[未提供问题,已取消]")
            return
        task = custom

    # 标定 chat_obj(供 LLM 输出 我/对方 角色标签)
    chat_obj = None
    if me:
        try:
            chat_obj = identify_me(parsed, me)
        except ValueError as e:
            print(f"[警告] {e} — AI 会自己猜双方身份")

    # 切段
    print("切段策略:")
    print("  1. 不切段(默认,适合 < 80 条)")
    print("  2. 按消息数切(每 80 条一段)")
    print("  3. 按时间间隔切(>30 分钟视为新会话)")
    chunk_pick = input("选择(1/2/3,默认 1): ").strip() or "1"
    if chunk_pick == "2":
        chunks = chunk_by_messages(parsed, max_messages=80)
    elif chunk_pick == "3":
        chunks = chunk_by_time(parsed, gap_minutes=30)
    else:
        chunks = [parsed]
    if len(chunks) > 1:
        print(f"将分为 {len(chunks)} 段分析")

    def _build_segment_msgs(seg_idx: int, seg_msgs: list) -> list[dict]:
        who_line = f"我 = {me}" + (f", 对方 = {chat_obj.you}" if chat_obj and chat_obj.you else "")
        return [
            {"role": "system", "content": build_system_prompt(extra_system) +
             "\n\n# 任务\n" + task},
            {"role": "user", "content": (
                f"参与者:{', '.join(participants)}\n"
                f"{who_line}\n"
                f"用户补充说明:{note or '(无)'}\n"
                f"{f'(分段 {seg_idx + 1}/{len(chunks)})' if len(chunks) > 1 else ''}\n\n"
                f"聊天记录:\n{format_for_llm(seg_msgs, chat=chat_obj)}"
            )},
        ]

    print("\n正在分析...")
    chunk_replies: list[str] = []
    try:
        for i, seg in enumerate(chunks):
            seg_msgs = _build_segment_msgs(i, seg)
            if len(chunks) > 1:
                print(f"\n--- 段 {i + 1}/{len(chunks)} ---")
            r = chat_with_context(seg_msgs, extra_system=extra_system)
            chunk_replies.append(r)
            if len(chunks) == 1:
                print(f"\nLove Soul AI:\n{r}")
    except Exception as e:  # noqa: BLE001
        print(f"\n[出错了] {e}")
        return

    reply = "\n\n---\n\n".join(chunk_replies) if len(chunks) > 1 else chunk_replies[0]
    # ponytail: 不自动落库;上传分析是临时任务,不污染用户历史。
    save = input("\n是否保存这轮分析到历史?(yes/no,默认 no): ").strip().lower()
    if save == "yes":
        db = get_db()
        db.append(user_id, "user", f"[上传分析] {task}\n\n{text[:2000]}")
        db.append(user_id, "assistant", reply)
        print("[已保存]")


def _has_key() -> bool:
    import os
    return bool(os.environ.get("OPENAI_API_KEY"))


def main() -> int:
    p = argparse.ArgumentParser(prog="love-soul-ai", description="恋爱关系咨询 AI 导师")
    p.add_argument("--user", default="default", help="会话用户标识,默认 default")
    p.add_argument("--system", default="", help="追加到系统提示词的自定义约束")
    args = p.parse_args()
    return repl(user_id=args.user, extra_system=args.system)


if __name__ == "__main__":
    sys.exit(main())
