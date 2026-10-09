"""自检:不调 LLM,验证 storage/upload/summarize 的纯逻辑。
运行: python -m love_soul_ai._selftest
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from .storage import HistoryDB
from .upload import detect_participants, format_for_llm, parse_chat_log


def assert_eq(name: str, got, want) -> None:
    if got != want:
        raise AssertionError(f"[{name}] got={got!r} want={want!r}")
    print(f"  ok  {name}")


def test_upload_parsing():
    print("[upload] parse_chat_log")
    sample = """\
2024-01-15 10:23:45 小明: 今天有点累
10:24 小红: 怎么啦?需要我陪你聊聊吗
[10:25] 小明: 嗯 有点想分手
10:26 小红: 为什么呀?
"""
    parsed = parse_chat_log(sample)
    assert_eq("count", len(parsed), 4)
    assert_eq("first_speaker", parsed[0].speaker, "小明")
    assert_eq("first_text_startswith", parsed[0].text.startswith("今天有点累"), True)
    participants = detect_participants(parsed)
    assert_eq("participants_top1", participants[0], "小明")
    formatted = format_for_llm(parsed)
    assert_eq("formatted_contains_ts", "[2024-01-15 10:23:45]" in formatted, True)


def test_upload_continuation():
    print("[upload] 续行合并")
    sample = """\
10:23 张三: 第一行
这是第二行续行
10:24 李四: 收到
"""
    parsed = parse_chat_log(sample)
    # 第一行匹配后,第二行未匹配,被并到上一条
    assert_eq("continuation_count", len(parsed), 2)
    assert_eq("continuation_merged", "\n" in parsed[0].text or "续行" in parsed[0].text, True)
    assert_eq("continuation_count", len(parsed), 2)
    assert_eq("continuation_text_has_newline", "\n" in parsed[0].text, True)
    assert_eq("second_speaker", parsed[1].speaker, "李四")


def test_storage_roundtrip():
    print("[storage] append/search/count/clear")
    with tempfile.TemporaryDirectory() as td:
        db = HistoryDB(Path(td) / "t.db")
        try:
            db.append("u1", "user", "今天他突然不回消息了")
            db.append("u1", "assistant", "先别急,可能他在忙")
            db.append("u1", "user", "但是已经 3 个小时了")
            assert_eq("count", db.count("u1"), 3)
            recent = db.recent("u1", limit=10)
            assert_eq("recent_len", len(recent), 3)
            hits = db.search("u1", "突然")
            assert_eq("search_count", len(hits) >= 1, True)
            assert_eq("search_first_content", "突然" in hits[0]["content"], True)
            db.clear("u1")
            assert_eq("after_clear_count", db.count("u1"), 0)
        finally:
            # ponytail: WAL 模式下不显式 close,WAL/SHM 文件被锁,Windows 删不了临时目录
            db._conn.close()


def test_identify_me():
    print("[upload] identify_me 标定身份")
    from .upload import identify_me
    sample = """\
10:23 张三: 今天好累
10:24 李四: 怎么啦
10:25 张三: 工作压力大
10:26 李四: 要不要我陪你
"""
    parsed = parse_chat_log(sample)
    chat = identify_me(parsed, "张三")
    assert_eq("me", chat.me, "张三")
    assert_eq("you", chat.you, "李四")
    formatted = chat.messages  # 复用 messages
    # 错误名字应抛错
    try:
        identify_me(parsed, "王五")
        raise AssertionError("应该抛错")
    except ValueError:
        print("  ok  wrong_me_raises")


def test_recall_parsing():
    print("[upload] 撤回消息识别")
    sample = """\
10:23 张三: 你好
10:24 李四 撤回了一条消息
张三 撤回了一条消息
10:25 李四: 嗯
"""
    parsed = parse_chat_log(sample)
    recalls = [m for m in parsed if m.is_recall]
    assert_eq("recall_count", len(recalls), 2)
    assert_eq("first_recall_speaker", recalls[0].speaker, "李四")


def test_chunk_by_messages():
    print("[upload] 按消息数切段")
    from .upload import chunk_by_messages
    msgs = [
        __import__("love_soul_ai.upload", fromlist=["ParsedMessage"]).ParsedMessage(
            speaker="A", text=str(i), ts=""
        )
        for i in range(150)
    ]
    chunks = chunk_by_messages(msgs, max_messages=80, overlap=5)
    # 步长 = 75, 段边界: [0:80], [75:150] 末尾不足 80 但已覆盖,期望 2 段
    assert_eq("chunk_count_short", len(chunks), 2)
    assert_eq("first_chunk_len", len(chunks[0]), 80)


def test_chunk_by_time():
    print("[upload] 按时间切段")
    from .upload import chunk_by_time
    msgs = [
        __import__("love_soul_ai.upload", fromlist=["ParsedMessage"]).ParsedMessage(
            speaker="A", text=str(i), ts=t
        )
        for i, t in enumerate([
            "10:00", "10:05", "10:10",       # 段1
            "11:00", "11:05",                # 段2 (>30 分钟)
            "11:30",                         # 段2 续
        ])
    ]
    chunks = chunk_by_time(msgs, gap_minutes=30)
    assert_eq("time_chunk_count", len(chunks), 2)
    assert_eq("seg1_len", len(chunks[0]), 3)
    assert_eq("seg2_len", len(chunks[1]), 3)


def test_summarize_window():
    print("[summarize] CONTEXT_WINDOW_TURNS 常量")
    from .summarize import CONTEXT_WINDOW_TURNS
    # 5000 轮场景:摘要触发阈值是窗口 + 50,确保窗口内消息数不超 LLM 上下文
    assert_eq("window_is_int", isinstance(CONTEXT_WINDOW_TURNS, int), True)
    assert_eq("window_positive", CONTEXT_WINDOW_TURNS > 0, True)


def main() -> int:
    test_upload_parsing()
    test_upload_continuation()
    test_identify_me()
    test_recall_parsing()
    test_chunk_by_messages()
    test_chunk_by_time()
    test_storage_roundtrip()
    test_summarize_window()
    print("\nALL OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
