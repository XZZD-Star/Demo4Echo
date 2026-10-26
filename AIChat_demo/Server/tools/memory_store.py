"""可控记忆存储层。

只负责候选记忆与确认写入，不做语义检索和自动修改。
"""

import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path

DB_PATH = Path("/var/lib/echomate/assistant.db")

MAX_CANDIDATES = 50
MAX_CONTENT_LENGTH = 2000
CANDIDATE_TTL_HOURS = 48

CATEGORIES = ("profile", "preference", "fact", "task")


def now_utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def _connect():
    conn = sqlite3.connect(str(DB_PATH), timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_memory_tables():
    """建立候选表，并给正式记忆补 category；可重复调用。"""
    with _connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS memory_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id TEXT NOT NULL,
                category TEXT NOT NULL DEFAULT 'fact',
                content TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'ai_chat',
                status TEXT NOT NULL DEFAULT 'pending',
                created_at_utc TEXT NOT NULL,
                decided_at_utc TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_memory_candidates_pending
                ON memory_candidates(status, owner_id, id);
            """
        )

        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(memories)")
        }
        if "category" not in columns:
            conn.execute(
                "ALTER TABLE memories ADD COLUMN category TEXT "
                "NOT NULL DEFAULT 'fact'"
            )


def _normalize(text):
    return re.sub(r"[\s，。！？、,.!?：:；;]+", "", text or "").lower()


def detect_category(text):
    """极简分类，宁可错放 fact，不做复杂模型推理。"""
    value = text or ""
    if any(word in value for word in ("我叫", "名字是", "居住", "住在", "家住", "生日", "职业")):
        return "profile"
    if any(word in value for word in ("喜欢", "讨厌", "偏好", "想听", "不要")):
        return "preference"
    if any(word in value for word in ("记住待办", "别忘了", "提醒")):
        return "task"
    return "fact"


def find_conflicts(owner_id, content, category):
    """按类别返回可能冲突的正式记忆；当前用简单关键词规则。"""
    key = _normalize(content)
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT id, content FROM memories
            WHERE owner_id = ? AND category = ?
            ORDER BY id DESC LIMIT 100
            """,
            (owner_id, category),
        ).fetchall()

    conflicts = []
    for row in rows:
        old = _normalize(row["content"])
        if old == key:
            continue
        if category == "profile":
            for prefix in ("居住", "住在", "家住", "生日", "职业"):
                if prefix in content and prefix in row["content"]:
                    conflicts.append(row)
                    break
    return conflicts


def enqueue_candidate(owner_id, content, source="ai_chat", category=None):
    """创建候选；重复候选直接更新时间。"""
    content = (content or "").strip()
    if not content:
        return None, "empty"
    if len(content) > MAX_CONTENT_LENGTH:
        return None, "too_long"

    category = category or detect_category(content)

    with _connect() as conn:
        existing = conn.execute(
            """
            SELECT id FROM memory_candidates
            WHERE owner_id = ? AND status = 'pending'
            """,
            (owner_id,),
        ).fetchall()

        key = _normalize(content)
        for row in existing:
            old = conn.execute(
                "SELECT content FROM memory_candidates WHERE id = ?",
                (row["id"],),
            ).fetchone()
            if old and _normalize(old["content"]) == key:
                return row["id"], "duplicate"

        cursor = conn.execute(
            """
            INSERT INTO memory_candidates
                (owner_id, category, content, source, created_at_utc)
            VALUES (?, ?, ?, ?, ?)
            """,
            (owner_id, category, content, source, now_utc()),
        )
        return cursor.lastrowid, "created"


def list_candidates(owner_id, limit=10):
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT id, category, content, created_at_utc
            FROM memory_candidates
            WHERE owner_id = ? AND status = 'pending'
            ORDER BY id ASC LIMIT ?
            """,
            (owner_id, limit),
        ).fetchall()
    return [dict(row) for row in rows]


def decide_candidate(owner_id, candidate_id, decision):
    """decision 只允许 accept / reject。"""
    if decision not in ("accept", "reject"):
        return False

    with _connect() as conn:
        row = conn.execute(
            """
            SELECT id, category, content
            FROM memory_candidates
            WHERE id = ? AND owner_id = ? AND status = 'pending'
            """,
            (candidate_id, owner_id),
        ).fetchone()

        if not row:
            return False

        if decision == "accept":
            conn.execute(
                """
                INSERT INTO memories (owner_id, content, created_at, category)
                VALUES (?, ?, ?, ?)
                """,
                (owner_id, row["content"], now_utc(), row["category"]),
            )

        conn.execute(
            """
            UPDATE memory_candidates
            SET status = ?, decided_at_utc = ?
            WHERE id = ?
            """,
            ("accepted" if decision == "accept" else "rejected",
             now_utc(), candidate_id),
        )
    return True


def reject_all_candidates(owner_id):
    with _connect() as conn:
        cursor = conn.execute(
            """
            UPDATE memory_candidates
            SET status = 'rejected', decided_at_utc = ?
            WHERE owner_id = ? AND status = 'pending'
            """,
            (now_utc(), owner_id),
        )
        return cursor.rowcount


def delete_memory(owner_id, memory_id):
    with _connect() as conn:
        cursor = conn.execute(
            "DELETE FROM memories WHERE id = ? AND owner_id = ?",
            (memory_id, owner_id),
        )
        return cursor.rowcount > 0


def clear_memories(owner_id):
    with _connect() as conn:
        cursor = conn.execute(
            "DELETE FROM memories WHERE owner_id = ?", (owner_id,)
        )
        return cursor.rowcount
