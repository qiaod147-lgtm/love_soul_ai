"""SQLite 持久化:全量消息不丢,5000 轮 + 长期记忆。

设计:
- messages 表存每条 user/assistant 消息
- summaries 表存早期对话的滚动摘要(LLM 上下文折叠用)
- 进程内 Session 仍负责"当前窗口",DB 负责"完整历史"
- 纯 sqlite3,无依赖;FTS5 做关键词检索(检索不到时降级 LIKE)
"""
from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator


# ponytail: 数据放用户目录,跨平台别用相对路径。
DEFAULT_DB_PATH = Path.home() / ".love_soul_ai" / "history.db"


SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT    NOT NULL,
    role        TEXT    NOT NULL CHECK (role IN ('user', 'assistant', 'system')),
    content     TEXT    NOT NULL,
    images_json TEXT    NOT NULL DEFAULT '[]',
    created_at  REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_user_time
    ON messages(user_id, created_at);

CREATE TABLE IF NOT EXISTS summaries (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT    NOT NULL,
    until_id    INTEGER NOT NULL,   -- 摘要覆盖到 messages.id = until_id
    content     TEXT    NOT NULL,
    created_at  REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_summaries_user ON summaries(user_id);

-- FTS5 虚拟表,关键词检索用;独立存储,避免 external content 模式 + WAL 下的 delete 边角
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    content,
    tokenize='unicode61'
);

CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    DELETE FROM messages_fts WHERE rowid = old.id;
END;
CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
    DELETE FROM messages_fts WHERE rowid = old.id;
    INSERT INTO messages_fts(rowid, content) VALUES (new.id, new.content);
END;
"""


class HistoryDB:
    """单用户/多用户都能用,自带连接锁(sqlite3 单连接 + check_same_thread)。"""

    def __init__(self, path: Path | str = DEFAULT_DB_PATH):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            self.path, check_same_thread=False, isolation_level=None
        )
        self._migrate()
        self._conn.executescript(SCHEMA)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")

    def _migrate(self) -> None:
        """检测旧版 FTS(external content) → drop 旧 trigger/fts 重建。

        ponytail: 旧 db 文件的 schema 已落盘,CREATE TABLE IF NOT EXISTS 不会覆盖;
        首次跑新代码必须显式 drop 旧的 messages_fts 与相关 trigger。
        """
        # ponytail: 老 db 没 images_json 列,补上
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(messages)").fetchall()}
        if "images_json" not in cols:
            self._conn.execute("ALTER TABLE messages ADD COLUMN images_json TEXT NOT NULL DEFAULT '[]'")
        cur = self._conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='messages_fts'"
        ).fetchone()
        if cur and cur[0] and "content_rowid" in cur[0]:
            self._conn.executescript("""
                DROP TRIGGER IF EXISTS messages_ai;
                DROP TRIGGER IF EXISTS messages_ad;
                DROP TABLE IF EXISTS messages_fts;
            """)

    @contextmanager
    def _cur(self) -> Iterator[sqlite3.Cursor]:
        with self._lock:
            cur = self._conn.cursor()
            try:
                yield cur
            finally:
                cur.close()

    def append(self, user_id: str, role: str, content: str, images: list[dict] | None = None) -> int:
        # ponytail: images 是 list[{"data_uri": "...", "ct": "image/png", "name": "..."}]
        import json as _json
        imgs_json = _json.dumps(images or [], ensure_ascii=False)
        with self._cur() as c:
            c.execute(
                "INSERT INTO messages(user_id, role, content, images_json, created_at) VALUES (?,?,?,?,?)",
                (user_id, role, content, imgs_json, time.time()),
            )
            return c.lastrowid

    def recent(self, user_id: str, limit: int = 30) -> list[dict]:
        import json as _json
        with self._cur() as c:
            rows = c.execute(
                "SELECT id, role, content, images_json FROM messages WHERE user_id=? "
                "AND role IN ('user','assistant') "
                "ORDER BY id DESC LIMIT ?",
                (user_id, limit),
            ).fetchall()
        return [
            {"id": r[0], "role": r[1], "content": r[2], "images": _json.loads(r[3] or "[]")}
            for r in reversed(rows)
        ]

    def messages_before(self, user_id: str, before_id: int) -> list[dict]:
        """取早于某 id 的全部消息,用于摘要折叠。"""
        import json as _json
        with self._cur() as c:
            rows = c.execute(
                "SELECT id, role, content, images_json FROM messages WHERE user_id=? "
                "AND role IN ('user','assistant') AND id < ? ORDER BY id ASC",
                (user_id, before_id),
            ).fetchall()
        return [
            {"id": r[0], "role": r[1], "content": r[2], "images": _json.loads(r[3] or "[]")}
            for r in rows
        ]

    def save_summary(self, user_id: str, until_id: int, content: str) -> None:
        with self._cur() as c:
            c.execute(
                "INSERT INTO summaries(user_id, until_id, content, created_at) VALUES (?,?,?,?)",
                (user_id, until_id, content, time.time()),
            )

    def latest_summary(self, user_id: str) -> tuple[int, str] | None:
        """返回 (until_id, content);没摘要返回 None。"""
        with self._cur() as c:
            row = c.execute(
                "SELECT until_id, content FROM summaries WHERE user_id=? "
                "ORDER BY id DESC LIMIT 1",
                (user_id,),
            ).fetchone()
        if not row:
            return None
        return (row[0], row[1])

    def search(self, user_id: str, keyword: str, limit: int = 10) -> list[dict]:
        """FTS5 优先;中文 unicode61 分词常丢命中,所以叠加 LIKE 兜底,再合并去重。"""
        if not keyword.strip():
            return []
        keyword = keyword.strip()
        # ponytail: FTS5 unicode61 对中文短语常不命中(分词粒度问题),
        # 简单 fallback:关键词按"整段"和"拆字"都试一次 LIKE,合并去重。
        like_patterns = [f"%{keyword}%"]
        if len(keyword) <= 4:
            like_patterns.extend(f"%{c}%" for c in keyword)

        with self._cur() as c:
            seen: set[int] = set()
            out: list[dict] = []
            # 1) FTS5 整段匹配
            try:
                rows = c.execute(
                    "SELECT m.id, m.role, m.content, m.created_at FROM messages_fts f "
                    "JOIN messages m ON m.id = f.rowid "
                    "WHERE messages_fts MATCH ? AND m.user_id=? "
                    "ORDER BY rank LIMIT ?",
                    (keyword, user_id, limit),
                ).fetchall()
                for r in rows:
                    if r[0] in seen:
                        continue
                    seen.add(r[0])
                    out.append({"id": r[0], "role": r[1], "content": r[2], "created_at": r[3]})
            except sqlite3.OperationalError:
                pass
            # 2) LIKE 兜底(对中文短关键词最稳)
            for pat in like_patterns:
                if len(out) >= limit:
                    break
                rows = c.execute(
                    "SELECT id, role, content, created_at FROM messages "
                    "WHERE user_id=? AND content LIKE ? ORDER BY id DESC LIMIT ?",
                    (user_id, pat, limit * 2),
                ).fetchall()
                for r in rows:
                    if r[0] in seen:
                        continue
                    seen.add(r[0])
                    out.append({"id": r[0], "role": r[1], "content": r[2], "created_at": r[3]})
                    if len(out) >= limit:
                        break
        return out[:limit]

    def count(self, user_id: str) -> int:
        with self._cur() as c:
            return c.execute(
                "SELECT COUNT(*) FROM messages WHERE user_id=?",
                (user_id,),
            ).fetchone()[0]

    def clear(self, user_id: str) -> None:
        # ponytail: 直接 DELETE messages 触发的 messages_ad trigger 在 WAL+autocommit 模式下
        # 偶发 "SQL logic error"(FTS5 rowid 同步问题)。最稳的修法是显式逐行删,
        # 让每条 delete 单独触发 trigger,避免批量 delete 的边角。
        with self._cur() as c:
            ids = [r[0] for r in c.execute(
                "SELECT id FROM messages WHERE user_id=?", (user_id,)
            ).fetchall()]
            for mid in ids:
                c.execute("DELETE FROM messages WHERE id=?", (mid,))
            c.execute("DELETE FROM summaries WHERE user_id=?", (user_id,))


# ponytail: 单例就够,跑 CLI 不需要连接池。
_DB: HistoryDB | None = None


def get_db() -> HistoryDB:
    global _DB
    if _DB is None:
        _DB = HistoryDB()
    return _DB


__all__ = ["HistoryDB", "get_db", "DEFAULT_DB_PATH", "Iterable"]
