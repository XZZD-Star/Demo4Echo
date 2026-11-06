"""Echo-Mate shared business data.

Single SQLite-backed source of truth for deskbot and Feishu bot.
Only standard library dependencies are used.
"""

import hashlib
import os
import re
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .db import connect as sqlite_connect
from .db import retry_locked

DB_PATH = Path(
    os.environ.get("ECHOMATE_DB_PATH", "/var/lib/echomate/assistant.db")
)
LOCAL_TZ = ZoneInfo("Asia/Shanghai")
MAX_MEMORY_LENGTH = 2000
MAX_CHAT_MEMORY_LENGTH = 500


def now_utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect():
    return sqlite_connect(DB_PATH)


def write_connect():
    return sqlite_connect(DB_PATH, write=True)


def init_schema():
    with connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
    with write_connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_memories_owner_id
                ON memories(owner_id, id);

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

            CREATE TABLE IF NOT EXISTS chat_memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id TEXT NOT NULL,
                session_id TEXT NOT NULL DEFAULT '',
                source_turn_id TEXT NOT NULL DEFAULT '',
                source TEXT NOT NULL DEFAULT 'chat',
                kind TEXT NOT NULL DEFAULT 'chat',
                content TEXT NOT NULL,
                dedupe_key TEXT NOT NULL UNIQUE,
                created_at_utc TEXT NOT NULL,
                expires_at_utc TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_chat_memories_owner
                ON chat_memories(owner_id, id DESC);
            CREATE INDEX IF NOT EXISTS idx_chat_memories_session
                ON chat_memories(owner_id, session_id, id DESC);

            CREATE TABLE IF NOT EXISTS todos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner_id TEXT NOT NULL,
                title TEXT NOT NULL,
                due_at_utc TEXT NOT NULL,
                timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
                status TEXT NOT NULL DEFAULT 'pending',
                created_at_utc TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                reminded_at_utc TEXT,
                last_attempt_at_utc TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_todos_due
                ON todos(status, due_at_utc, reminded_at_utc);

            CREATE TABLE IF NOT EXISTS reminder_deliveries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                todo_id INTEGER NOT NULL,
                channel TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                last_attempt_at_utc TEXT,
                sent_at_utc TEXT,
                acked_at_utc TEXT,
                last_error TEXT,
                UNIQUE(todo_id, channel)
            );
            CREATE INDEX IF NOT EXISTS idx_delivery_pending
                ON reminder_deliveries(channel, status, id);
            """
        )

        memory_columns = {r["name"] for r in conn.execute("PRAGMA table_info(memories)")}
        if "category" not in memory_columns:
            conn.execute("ALTER TABLE memories ADD COLUMN category TEXT NOT NULL DEFAULT 'fact'")
        if "source" not in memory_columns:
            conn.execute("ALTER TABLE memories ADD COLUMN source TEXT NOT NULL DEFAULT 'feishu'")
        if "updated_at" not in memory_columns:
            conn.execute("ALTER TABLE memories ADD COLUMN updated_at TEXT")
        if "memory_key" not in memory_columns:
            conn.execute("ALTER TABLE memories ADD COLUMN memory_key TEXT")
        if "confidence" not in memory_columns:
            conn.execute(
                "ALTER TABLE memories ADD COLUMN confidence REAL NOT NULL DEFAULT 1.0"
            )
        if "evidence_count" not in memory_columns:
            conn.execute(
                "ALTER TABLE memories ADD COLUMN evidence_count INTEGER NOT NULL DEFAULT 1"
            )
        if "last_seen_at_utc" not in memory_columns:
            conn.execute("ALTER TABLE memories ADD COLUMN last_seen_at_utc TEXT")
        if "source_turn_id" not in memory_columns:
            conn.execute("ALTER TABLE memories ADD COLUMN source_turn_id TEXT")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_memories_key "
            "ON memories(owner_id, memory_key)"
        )

        todo_columns = {r["name"] for r in conn.execute("PRAGMA table_info(todos)")}
        additions = {
            "source": "TEXT NOT NULL DEFAULT 'feishu'",
            "dedupe_key": "TEXT",
            "notify_deskbot": "INTEGER NOT NULL DEFAULT 1",
            "notify_feishu": "INTEGER NOT NULL DEFAULT 1",
        }
        for name, definition in additions.items():
            if name not in todo_columns:
                conn.execute(f"ALTER TABLE todos ADD COLUMN {name} {definition}")
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_todos_dedupe "
            "ON todos(owner_id, dedupe_key) WHERE dedupe_key IS NOT NULL"
        )


def _normalize(text):
    return re.sub(r"[\s，。！？、,.!?：:；;]+", "", text or "").lower()


def detect_category(text):
    value = text or ""
    if any(word in value for word in ("我叫", "名字是", "居住", "住在", "家住", "生日", "职业")):
        return "profile"
    if any(word in value for word in ("喜欢", "讨厌", "偏好", "想听", "不要")):
        return "preference"
    if any(word in value for word in ("记住待办", "别忘了", "提醒")):
        return "task"
    return "fact"


def list_memories(owner_id, limit=50):
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, content, category, source
            FROM memories WHERE owner_id = ?
            ORDER BY id DESC LIMIT ?
            """,
            (owner_id, int(limit)),
        ).fetchall()
    return [dict(r) for r in rows]


def memory_context(owner_id, limit=50):
    seen = set()
    result = []
    for item in list_memories(owner_id, limit * 3):
        key = _normalize(item["content"])
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(item)
        if len(result) >= limit:
            break
    return result


def add_chat_memory(owner_id, content, session_id="", source_turn_id="",
                    source="chat", kind="chat", expires_days=90, conn=None):
    content = (content or "").strip()
    if not content:
        return None, "empty"
    if len(content) > MAX_CHAT_MEMORY_LENGTH:
        return None, "too_long"

    expires_at = None
    if expires_days:
        expires_at = (
            datetime.now(timezone.utc) + timedelta(days=int(expires_days))
        ).isoformat(timespec="seconds")
    dedupe_raw = (
        f"{owner_id}|{session_id}|{source_turn_id}|{_normalize(content)}"
    ).encode("utf-8")
    dedupe_key = "chat-memory:" + hashlib.sha256(dedupe_raw).hexdigest()
    if conn is not None:
        return _add_chat_memory(
            conn, owner_id, content, session_id, source_turn_id, source,
            kind, dedupe_key, expires_at
        )
    with write_connect() as owned_conn:
        return _add_chat_memory(
            owned_conn, owner_id, content, session_id, source_turn_id, source,
            kind, dedupe_key, expires_at
        )


def _add_chat_memory(conn, owner_id, content, session_id, source_turn_id,
                     source, kind, dedupe_key, expires_at):
    existing = conn.execute(
        "SELECT id FROM chat_memories WHERE dedupe_key = ?",
        (dedupe_key,),
    ).fetchone()
    if existing:
        return existing["id"], "duplicate"
    cursor = conn.execute(
        """
        INSERT INTO chat_memories
            (owner_id, session_id, source_turn_id, source, kind, content,
             dedupe_key, created_at_utc, expires_at_utc)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            owner_id, session_id or "", source_turn_id or "", source or "chat",
            kind or "chat", content, dedupe_key, now_utc(), expires_at,
        ),
    )
    return cursor.lastrowid, "created"


def list_chat_memories(owner_id, limit=50, offset=0, conn=None):
    sql = """
        SELECT id, session_id, source_turn_id, source, kind, content,
               created_at_utc, expires_at_utc
        FROM chat_memories
        WHERE owner_id = ?
          AND (expires_at_utc IS NULL OR expires_at_utc > ?)
        ORDER BY id DESC LIMIT ? OFFSET ?
    """
    params = (owner_id, now_utc(), int(limit), int(offset))
    if conn is not None:
        rows = conn.execute(sql, params).fetchall()
    else:
        with connect() as owned_conn:
            rows = owned_conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def _memory_query_terms(text):
    value = _normalize(text)
    if not value:
        return set()
    terms = {value[index:index + 2] for index in range(len(value) - 1)}
    terms.update(re.findall(r"[a-z0-9]{2,}", value))
    return terms


def search_chat_memories(owner_id, query, exclude_session_id="", limit=5):
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, session_id, source, kind, content, created_at_utc
            FROM chat_memories
            WHERE owner_id = ?
              AND (expires_at_utc IS NULL OR expires_at_utc > ?)
              AND session_id != ?
            ORDER BY id DESC LIMIT 200
            """,
            (owner_id, now_utc(), exclude_session_id or ""),
        ).fetchall()

    terms = _memory_query_terms(query)
    recall_requested = any(
        word in (query or "")
        for word in ("上次", "之前", "以前", "记得", "聊过", "提到")
    )
    scored = []
    for row in rows:
        item_terms = _memory_query_terms(row["content"])
        overlap = len(terms & item_terms)
        if recall_requested:
            overlap += 1
        if overlap <= 0:
            continue
        scored.append((overlap, row["id"], dict(row)))
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return [item[2] for item in scored[:max(1, int(limit))]]


def delete_chat_memory(owner_id, memory_id, conn=None):
    if conn is not None:
        return _delete_chat_memory(conn, owner_id, memory_id)
    with write_connect() as owned_conn:
        return _delete_chat_memory(owned_conn, owner_id, memory_id)


def _delete_chat_memory(conn, owner_id, memory_id):
    cursor = conn.execute(
        "DELETE FROM chat_memories WHERE id = ? AND owner_id = ?",
        (int(memory_id), owner_id),
    )
    return cursor.rowcount > 0


def clear_chat_memories(owner_id, conn=None):
    if conn is not None:
        return _clear_chat_memories(conn, owner_id)
    with write_connect() as owned_conn:
        return _clear_chat_memories(owned_conn, owner_id)


def _clear_chat_memories(conn, owner_id):
    cursor = conn.execute(
        "DELETE FROM chat_memories WHERE owner_id = ?",
        (owner_id,),
    )
    return cursor.rowcount


def upsert_preference_memory(owner_id, content, memory_key, source,
                             confidence=1.0, source_turn_id="", conn=None):
    content = (content or "").strip()
    memory_key = (memory_key or "").strip()
    if not content or not memory_key:
        return None, "empty"
    if len(content) > MAX_MEMORY_LENGTH:
        return None, "too_long"
    if conn is not None:
        return _upsert_preference_memory(
            conn, owner_id, content, memory_key, source, confidence,
            source_turn_id
        )
    with write_connect() as owned_conn:
        return _upsert_preference_memory(
            owned_conn, owner_id, content, memory_key, source, confidence,
            source_turn_id
        )


def _upsert_preference_memory(conn, owner_id, content, memory_key, source,
                              confidence, source_turn_id):
    row = conn.execute(
        """
        SELECT id FROM memories
        WHERE owner_id = ? AND memory_key = ?
        ORDER BY id DESC LIMIT 1
        """,
        (owner_id, memory_key),
    ).fetchone()
    now = now_utc()
    if row:
        conn.execute(
            """
            UPDATE memories
            SET content = ?, category = 'preference', source = ?,
                updated_at = ?, confidence = ?, last_seen_at_utc = ?,
                evidence_count = evidence_count + 1, source_turn_id = ?
            WHERE id = ?
            """,
            (
                content, source, now, float(confidence), now,
                source_turn_id or "", row["id"],
            ),
        )
        return row["id"], "updated"
    cursor = conn.execute(
        """
        INSERT INTO memories
            (owner_id, content, created_at, category, source, updated_at,
             memory_key, confidence, evidence_count, last_seen_at_utc,
             source_turn_id)
        VALUES (?, ?, ?, 'preference', ?, ?, ?, ?, 1, ?, ?)
        """,
        (
            owner_id, content, now, source, now, memory_key,
            float(confidence), now, source_turn_id or "",
        ),
    )
    return cursor.lastrowid, "created"


def enqueue_memory_candidate(owner_id, content, source="ai_chat", category=None,
                             conn=None):
    content = (content or "").strip()
    if not content:
        return None, "empty"
    if len(content) > MAX_MEMORY_LENGTH:
        return None, "too_long"

    category = category or detect_category(content)
    if conn is not None:
        return _enqueue_memory_candidate(conn, owner_id, content, source, category)
    with write_connect() as owned_conn:
        return _enqueue_memory_candidate(
            owned_conn, owner_id, content, source, category
        )


def _enqueue_memory_candidate(conn, owner_id, content, source, category):
    rows = conn.execute(
        "SELECT id, content FROM memory_candidates "
        "WHERE owner_id = ? AND status = 'pending'",
        (owner_id,),
    ).fetchall()
    key = _normalize(content)
    for row in rows:
        if _normalize(row["content"]) == key:
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


def list_memory_candidates(owner_id, limit=10, conn=None):
    if conn is not None:
        rows = _list_memory_candidates(conn, owner_id, limit)
    else:
        with connect() as owned_conn:
            rows = _list_memory_candidates(owned_conn, owner_id, limit)
    return [dict(r) for r in rows]


def _list_memory_candidates(conn, owner_id, limit):
    return conn.execute(
        """
        SELECT id, category, content, source, created_at_utc
        FROM memory_candidates
        WHERE owner_id = ? AND status = 'pending'
        ORDER BY id ASC LIMIT ?
        """,
        (owner_id, int(limit)),
    ).fetchall()


def decide_memory_candidate(owner_id, candidate_id, decision, conn=None):
    if decision not in ("accept", "reject"):
        return False
    if conn is not None:
        return _decide_memory_candidate(conn, owner_id, candidate_id, decision)
    with write_connect() as owned_conn:
        return _decide_memory_candidate(
            owned_conn, owner_id, candidate_id, decision
        )


def _decide_memory_candidate(conn, owner_id, candidate_id, decision):
    row = conn.execute(
        """
        SELECT id, category, content, source FROM memory_candidates
        WHERE id = ? AND owner_id = ? AND status = 'pending'
        """,
        (int(candidate_id), owner_id),
    ).fetchone()
    if not row:
        return False

    if decision == "accept":
        conn.execute(
            """
            INSERT INTO memories
                (owner_id, content, created_at, category, source, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (owner_id, row["content"], now_utc(), row["category"],
             row["source"], now_utc()),
        )
    conn.execute(
        "UPDATE memory_candidates SET status = ?, decided_at_utc = ? WHERE id = ?",
        ("accepted" if decision == "accept" else "rejected",
         now_utc(), int(candidate_id)),
    )
    return True


def delete_memory(owner_id, memory_id, conn=None):
    if conn is not None:
        return _delete_memory(conn, owner_id, memory_id)
    with write_connect() as owned_conn:
        return _delete_memory(owned_conn, owner_id, memory_id)


def _delete_memory(conn, owner_id, memory_id):
    cursor = conn.execute(
        "DELETE FROM memories WHERE id = ? AND owner_id = ?",
        (int(memory_id), owner_id),
    )
    return cursor.rowcount > 0


def clear_memories(owner_id, conn=None):
    if conn is not None:
        return _clear_memories(conn, owner_id)
    with write_connect() as owned_conn:
        return _clear_memories(owned_conn, owner_id)


def _clear_memories(conn, owner_id):
    cursor = conn.execute("DELETE FROM memories WHERE owner_id = ?", (owner_id,))
    return cursor.rowcount


def _parse_due(due_text):
    value = (due_text or "").strip()
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    return dt.astimezone(timezone.utc)


def create_todo(owner_id, title, due_at_utc, source="feishu",
                chat_id="", dedupe_key=None, notify_deskbot=True,
                notify_feishu=True, conn=None):
    title = (title or "").strip()
    if not title:
        return None, "empty"
    if len(title) > 200:
        return None, "too_long"

    due = _parse_due(due_at_utc)
    if due is None:
        return None, "bad_due"
    if due <= datetime.now(timezone.utc):
        return None, "past_due"

    due_text = due.isoformat(timespec="seconds")
    if not dedupe_key:
        raw = f"{owner_id}|{title}|{due_text}".encode("utf-8")
        dedupe_key = "todo:" + hashlib.sha256(raw).hexdigest()[:24]

    if conn is not None:
        return _create_todo(
            conn, owner_id, title, due_text, source, chat_id, dedupe_key,
            notify_deskbot, notify_feishu
        )
    with write_connect() as owned_conn:
        return _create_todo(
            owned_conn, owner_id, title, due_text, source, chat_id, dedupe_key,
            notify_deskbot, notify_feishu
        )


def _create_todo(conn, owner_id, title, due_text, source, chat_id, dedupe_key,
                 notify_deskbot, notify_feishu):
    row = conn.execute(
        """
        SELECT id FROM todos
        WHERE owner_id = ? AND dedupe_key = ?
          AND status = 'pending'
        """,
        (owner_id, dedupe_key),
    ).fetchone()
    if row:
        return row["id"], "duplicate"

    cursor = conn.execute(
        """
        INSERT INTO todos
            (owner_id, title, due_at_utc, timezone, status,
             created_at_utc, chat_id, source, dedupe_key,
             notify_deskbot, notify_feishu)
        VALUES (?, ?, ?, 'Asia/Shanghai', 'pending',
                ?, ?, ?, ?, ?, ?)
        """,
        (owner_id, title, due_text, now_utc(), chat_id or "",
         source, dedupe_key, int(bool(notify_deskbot)),
         int(bool(notify_feishu))),
    )
    return cursor.lastrowid, "created"


def list_todos(owner_id, include_done=False, limit=50, conn=None):
    where = "" if include_done else "AND status = 'pending'"
    if conn is not None:
        rows = _list_todos(conn, owner_id, where, limit)
    else:
        with connect() as owned_conn:
            rows = _list_todos(owned_conn, owner_id, where, limit)
    return [dict(r) for r in rows]


def _list_todos(conn, owner_id, where, limit):
    return conn.execute(
        f"""
        SELECT id, title, due_at_utc, status, source, chat_id,
               notify_deskbot, notify_feishu
        FROM todos WHERE owner_id = ? {where}
        ORDER BY due_at_utc ASC LIMIT ?
        """,
        (owner_id, int(limit)),
    ).fetchall()


def set_todo_status(owner_id, todo_id, status, conn=None):
    if status not in ("pending", "completed", "cancelled"):
        return False
    if conn is not None:
        return _set_todo_status(conn, owner_id, todo_id, status)
    with write_connect() as owned_conn:
        return _set_todo_status(owned_conn, owner_id, todo_id, status)


def _set_todo_status(conn, owner_id, todo_id, status):
    cursor = conn.execute(
        "UPDATE todos SET status = ? WHERE id = ? AND owner_id = ?",
        (status, int(todo_id), owner_id),
    )
    return cursor.rowcount > 0


REMINDER_GRACE_MINUTES = 5


@retry_locked
def materialize_due_reminders(grace_minutes=REMINDER_GRACE_MINUTES):
    """Create deliveries only for reminders that are still timely."""
    now = now_utc()
    grace_cutoff = (
        datetime.now(timezone.utc) - timedelta(minutes=grace_minutes)
    ).isoformat(timespec="seconds")
    with write_connect() as conn:
        conn.execute(
            """
            UPDATE reminder_deliveries
            SET status = 'skipped',
                last_error = 'reminder expired'
            WHERE status IN ('pending', 'claimed')
              AND todo_id IN (
                  SELECT id FROM todos
                  WHERE due_at_utc < ?
              )
            """,
            (grace_cutoff,),
        )
        conn.execute(
            """
            UPDATE todos SET reminded_at_utc = ?
            WHERE status = 'pending'
              AND reminded_at_utc IS NULL
              AND due_at_utc < ?
            """,
            (now, grace_cutoff),
        )
        rows = conn.execute(
            """
            SELECT id, title, notify_deskbot, notify_feishu
            FROM todos
            WHERE status = 'pending' AND due_at_utc <= ?
              AND reminded_at_utc IS NULL
              AND due_at_utc >= ?
            ORDER BY due_at_utc ASC LIMIT 50
            """,
            (now, grace_cutoff),
        ).fetchall()
        created = 0
        for row in rows:
            channels = []
            if row["notify_deskbot"]:
                channels.append("deskbot")
            if row["notify_feishu"]:
                channels.append("feishu")
            if not channels:
                conn.execute(
                    "UPDATE todos SET reminded_at_utc = ? WHERE id = ?",
                    (now, row["id"]),
                )
                continue
            for channel in channels:
                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO reminder_deliveries
                        (todo_id, channel, status)
                    VALUES (?, ?, 'pending')
                    """,
                    (row["id"], channel),
                )
                created += cursor.rowcount
        return created


def pending_deskbot_deliveries():
    """Return claimed/sent deskbot deliveries awaiting ACK reconciliation."""
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id AS delivery_id, todo_id, status
            FROM reminder_deliveries
            WHERE channel = 'deskbot'
              AND status IN ('claimed', 'sent')
              AND acked_at_utc IS NULL
            ORDER BY id ASC LIMIT 20
            """
        ).fetchall()
    return [dict(row) for row in rows]


@retry_locked
def claim_delivery(channel):
    grace_cutoff = (
        datetime.now(timezone.utc)
        - timedelta(minutes=REMINDER_GRACE_MINUTES)
    ).isoformat(timespec="seconds")
    with write_connect() as conn:
        row = conn.execute(
            """
            SELECT d.id AS delivery_id, d.todo_id, d.attempts,
                   t.title, t.due_at_utc, t.chat_id, t.owner_id
            FROM reminder_deliveries d
            JOIN todos t ON t.id = d.todo_id
            WHERE d.channel = ? AND d.status = 'pending'
              AND t.status = 'pending'
              AND t.reminded_at_utc IS NULL
              AND t.due_at_utc >= ?
            ORDER BY d.id ASC LIMIT 1
            """,
            (channel, grace_cutoff),
        ).fetchone()
        if not row:
            return None
        cursor = conn.execute(
            """
            UPDATE reminder_deliveries
            SET status = 'claimed', last_attempt_at_utc = ?, attempts = attempts + 1
            WHERE id = ? AND status = 'pending'
            """,
            (now_utc(), row["delivery_id"]),
        )
        if cursor.rowcount != 1:
            return None
        return dict(row)


@retry_locked
def mark_delivery(delivery_id, status, error=None):
    if status not in ("sent", "failed", "skipped"):
        return
    with write_connect() as conn:
        conn.execute(
            """
            UPDATE reminder_deliveries
            SET status = ?, sent_at_utc = CASE WHEN ? = 'sent' THEN ? ELSE sent_at_utc END,
                last_error = ?
            WHERE id = ?
            """,
            (status, status, now_utc(), (str(error)[:300] if error else None),
             int(delivery_id)),
        )
        if status in ("sent", "skipped"):
            row = conn.execute(
                "SELECT todo_id FROM reminder_deliveries WHERE id = ?",
                (int(delivery_id),),
            ).fetchone()
            if row:
                conn.execute(
                    """
                    UPDATE todos SET reminded_at_utc = ?
                    WHERE id = ? AND reminded_at_utc IS NULL
                    """,
                    (now_utc(), row["todo_id"]),
                )


def _acknowledge_delivery(conn, delivery_id):
    row = conn.execute(
        "SELECT todo_id FROM reminder_deliveries WHERE id = ?",
        (int(delivery_id),),
    ).fetchone()
    if not row:
        return False
    conn.execute(
        """
        UPDATE reminder_deliveries
        SET status = 'sent', acked_at_utc = ?
        WHERE id = ? AND acked_at_utc IS NULL
        """,
        (now_utc(), int(delivery_id)),
    )
    _finalize_todo_if_delivered(conn, row["todo_id"])
    return True


@retry_locked
def _acknowledge_delivery_transaction(delivery_id):
    with write_connect() as conn:
        return _acknowledge_delivery(conn, delivery_id)


def acknowledge_delivery(delivery_id, conn=None):
    """Mark the client ACK and finalize the todo when all channels finish."""
    if conn is not None:
        return _acknowledge_delivery(conn, delivery_id)
    return _acknowledge_delivery_transaction(delivery_id)


def _finalize_todo_if_delivered(conn, todo_id):
    row = conn.execute(
        """
        SELECT COUNT(*) AS n FROM reminder_deliveries
        WHERE todo_id = ? AND status IN ('pending', 'claimed', 'failed')
        """,
        (int(todo_id),),
    ).fetchone()
    if row and row["n"] == 0:
        conn.execute(
            "UPDATE todos SET reminded_at_utc = ? "
            "WHERE id = ? AND reminded_at_utc IS NULL",
            (now_utc(), int(todo_id)),
        )


@retry_locked
def _finalize_todo_if_delivered_transaction(todo_id):
    with write_connect() as conn:
        _finalize_todo_if_delivered(conn, todo_id)


def finalize_todo_if_delivered(todo_id, conn=None):
    if conn is not None:
        return _finalize_todo_if_delivered(conn, todo_id)
    return _finalize_todo_if_delivered_transaction(todo_id)
