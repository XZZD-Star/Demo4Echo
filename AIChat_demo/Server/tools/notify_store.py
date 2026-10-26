"""主动播报通知队列的 SQLite 存储层。

与 feishu_bot / monitor 共用同一个数据库文件，只使用标准库 sqlite3，
不依赖任何虚拟环境内的第三方包。
"""

import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path

DB_PATH = Path("/var/lib/echomate/assistant.db")

LOCAL_TZ = timezone(timedelta(hours=8))

# 0 表示每次都读库：飞书改完静音/开关后下一轮轮询就生效。
# 单用户场景下每 3 秒几次 SQLite 查询，开销可以忽略。
SETTING_CACHE_TTL = 0.0
_setting_cache = {}
_setting_cache_at = 0.0

# 队列上限，超出后丢弃最低优先级的旧记录。
MAX_PENDING = 50
# 播报文本上限，超出截断，避免一口气念一分钟。
MAX_TEXT_LENGTH = 120


def now_utc():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def _connect():
    """连接用完必须关闭，否则 WAL 下会不断累积句柄并导致 database is locked。"""
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


def _shorten(text):
    text = (text or "").strip()
    if len(text) <= MAX_TEXT_LENGTH:
        return text
    return text[: MAX_TEXT_LENGTH - 6].rstrip() + "，详情见飞书"


def init_notify_tables():
    """建立通知队列表与运行期设置表。可重复调用。"""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    with _connect() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS notify_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kind TEXT NOT NULL,
                priority INTEGER NOT NULL DEFAULT 5,
                text TEXT NOT NULL,
                dedupe_key TEXT UNIQUE,
                created_at_utc TEXT NOT NULL,
                expires_at_utc TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                claimed_at_utc TEXT,
                sent_at_utc TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                last_error TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_notify_pending
                ON notify_outbox(status, priority, id);

            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at_utc TEXT NOT NULL
            );
            """
        )

        # P3 新增：客户端播放确认时间。老库需要补列。
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(notify_outbox)")
        }
        if "acked_at_utc" not in columns:
            conn.execute(
                "ALTER TABLE notify_outbox ADD COLUMN acked_at_utc TEXT"
            )


def _enforce_queue_cap(conn):
    """pending 超过上限时，丢弃优先级最低且最旧的记录。"""
    total = conn.execute(
        "SELECT COUNT(*) AS n FROM notify_outbox WHERE status = 'pending'"
    ).fetchone()["n"]

    excess = total - MAX_PENDING
    if excess <= 0:
        return 0

    conn.execute(
        """
        DELETE FROM notify_outbox
        WHERE id IN (
            SELECT id FROM notify_outbox
            WHERE status = 'pending'
            ORDER BY priority DESC, id ASC
            LIMIT ?
        )
        """,
        (excess,),
    )
    return excess


def enqueue(kind, text, dedupe_key=None, priority=5, ttl_minutes=60):
    """写入一条待播报通知。dedupe_key 重复时静默忽略。

    返回新记录的 id；若因去重被忽略则返回 None。
    """
    text = _shorten(text)
    if not text:
        return None

    created = now_utc()
    expires = None
    if ttl_minutes and ttl_minutes > 0:
        expires = (
            datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)
        ).isoformat(timespec="seconds")

    with _connect() as conn:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO notify_outbox
                (kind, priority, text, dedupe_key,
                 created_at_utc, expires_at_utc, status)
            VALUES (?, ?, ?, ?, ?, ?, 'pending')
            """,
            (kind, int(priority), text, dedupe_key, created, expires),
        )
        if cursor.rowcount == 0:
            return None

        _enforce_queue_cap(conn)
        return cursor.lastrowid


def expire_stale():
    """把已过期或重试次数过多的 pending 记录标记为 expired。"""
    current = now_utc()
    with _connect() as conn:
        conn.execute(
            """
            UPDATE notify_outbox
            SET status = 'expired'
            WHERE status IN ('pending', 'claimed')
              AND expires_at_utc IS NOT NULL
              AND expires_at_utc <= ?
            """,
            (current,),
        )
        conn.execute(
            """
            UPDATE notify_outbox
            SET status = 'failed'
            WHERE status IN ('pending', 'claimed')
              AND attempts >= 3
            """
        )


def recover_zombie(seconds=120):
    """把领取后长时间未结束的记录回滚为 pending，防止进程被杀后卡死。"""
    threshold = (
        datetime.now(timezone.utc) - timedelta(seconds=seconds)
    ).isoformat(timespec="seconds")

    with _connect() as conn:
        conn.execute(
            """
            UPDATE notify_outbox
            SET status = 'pending', claimed_at_utc = NULL
            WHERE status = 'claimed'
              AND (claimed_at_utc IS NULL OR claimed_at_utc <= ?)
            """,
            (threshold,),
        )


def enabled_kinds():
    """按 app_settings 的分类开关返回允许播报的 kind 列表。

    未配置时默认全部开启，保持与 P0 一致的行为。
    """
    kinds = []
    if get_setting("notify_todo_enabled", "1") == "1":
        kinds.append("todo")
    if get_setting("notify_monitor_enabled", "1") == "1":
        kinds.extend(["alert", "recovery"])
    kinds.append("manual")
    return kinds


def claim_one(kinds=None):
    """原子领取一条优先级最高的待播报通知。"""
    expire_stale()

    if kinds is None:
        kinds = enabled_kinds()

    if not kinds:
        return None

    placeholders = ",".join("?" for _ in kinds)
    current = now_utc()

    for _ in range(3):
        with _connect() as conn:
            row = conn.execute(
                f"""
                SELECT id, kind, priority, text
                FROM notify_outbox
                WHERE status = 'pending'
                  AND kind IN ({placeholders})
                  AND (expires_at_utc IS NULL OR expires_at_utc > ?)
                ORDER BY priority ASC, id ASC
                LIMIT 1
                """,
                (*kinds, current),
            ).fetchone()

            if row is None:
                return None

            cursor = conn.execute(
                """
                UPDATE notify_outbox
                SET status = 'claimed', claimed_at_utc = ?
                WHERE id = ? AND status = 'pending'
                """,
                (now_utc(), row["id"]),
            )

            if cursor.rowcount == 1:
                return {
                    "id": row["id"],
                    "kind": row["kind"],
                    "priority": row["priority"],
                    "text": row["text"],
                }

    return None


def claim_batch(max_items=3, kinds=None):
    """领取同一类型的一批通知，用于合并播报，避免连续叮咚。"""
    first = claim_one(kinds)
    if first is None:
        return []

    batch = [first]
    while len(batch) < max_items:
        row = claim_one([first["kind"]])
        if row is None:
            break
        batch.append(row)

    return batch


def mark_sent(notify_ids):
    if isinstance(notify_ids, int):
        notify_ids = [notify_ids]

    if not notify_ids:
        return

    placeholders = ",".join("?" for _ in notify_ids)
    with _connect() as conn:
        conn.execute(
            f"""
            UPDATE notify_outbox
            SET status = 'sent', sent_at_utc = ?, last_error = NULL
            WHERE id IN ({placeholders})
            """,
            (now_utc(), *notify_ids),
        )


def mark_failed(notify_ids, error):
    if isinstance(notify_ids, int):
        notify_ids = [notify_ids]

    if not notify_ids:
        return

    message = str(error)[:300]
    placeholders = ",".join("?" for _ in notify_ids)
    with _connect() as conn:
        conn.execute(
            f"""
            UPDATE notify_outbox
            SET status = 'pending',
                claimed_at_utc = NULL,
                attempts = attempts + 1,
                last_error = ?
            WHERE id IN ({placeholders})
            """,
            (message, *notify_ids),
        )
        conn.execute(
            f"""
            UPDATE notify_outbox
            SET status = 'failed'
            WHERE id IN ({placeholders}) AND attempts >= 3
            """,
            tuple(notify_ids),
        )


def mark_pending(notify_ids, reason=""):
    """Return claimed notifications to pending without consuming retries."""
    if isinstance(notify_ids, int):
        notify_ids = [notify_ids]

    if not notify_ids:
        return

    placeholders = ",".join("?" for _ in notify_ids)
    with _connect() as conn:
        conn.execute(
            f"""
            UPDATE notify_outbox
            SET status = 'pending',
                claimed_at_utc = NULL,
                last_error = ?
            WHERE id IN ({placeholders})
            """,
            (str(reason)[:300], *notify_ids),
        )


def mark_acked(notify_ids):
    """Record client ACK and close the matching shared reminder delivery."""
    if not notify_ids:
        return 0

    acked_at = now_utc()
    placeholders = ",".join("?" for _ in notify_ids)
    with _connect() as conn:
        # Delivery IDs are encoded in dedupe_key for todo reminders.
        rows = conn.execute(
            f"""
            SELECT dedupe_key FROM notify_outbox
            WHERE id IN ({placeholders})
              AND dedupe_key LIKE 'todo-delivery:%'
            """,
            tuple(notify_ids),
        ).fetchall()
        for row in rows:
            try:
                delivery_id = int(row["dedupe_key"].split(":", 1)[1])
            except (TypeError, ValueError):
                continue
            try:
                import echomate_store
                echomate_store.acknowledge_delivery(delivery_id)
            except Exception:
                # ACK bookkeeping must not break the deskbot playback flow.
                pass

        cursor = conn.execute(
            f"""
            UPDATE notify_outbox
            SET acked_at_utc = ?
            WHERE id IN ({placeholders}) AND acked_at_utc IS NULL
            """,
            (acked_at, *notify_ids),
        )
        return cursor.rowcount


def find_by_dedupe_key(dedupe_key):
    """Return a notification by dedupe key, or None."""
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT id, kind, text, status, sent_at_utc, acked_at_utc
            FROM notify_outbox WHERE dedupe_key = ?
            """,
            (dedupe_key,),
        ).fetchone()
    return dict(row) if row else None


def pending_count():
    with _connect() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM notify_outbox WHERE status = 'pending'"
        ).fetchone()
    return int(row["n"]) if row else 0


def recent_records(limit=10):
    """返回最近的播报记录，供飞书查询。"""
    with _connect() as conn:
        return conn.execute(
            """
            SELECT id, kind, text, status, attempts,
                   sent_at_utc, acked_at_utc, last_error
            FROM notify_outbox
            ORDER BY id DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()


def _load_settings():
    global _setting_cache, _setting_cache_at

    now = time.monotonic()
    if _setting_cache and now - _setting_cache_at < SETTING_CACHE_TTL:
        return _setting_cache

    values = {}
    try:
        with _connect() as conn:
            for row in conn.execute("SELECT key, value FROM app_settings"):
                values[row["key"]] = row["value"]
    except sqlite3.Error:
        values = dict(_setting_cache)

    _setting_cache = values
    _setting_cache_at = now
    return values


def get_setting(key, default=""):
    value = _load_settings().get(key)
    if value is None:
        return default
    return value


def set_setting(key, value):
    global _setting_cache_at

    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO app_settings (key, value, updated_at_utc)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at_utc = excluded.updated_at_utc
            """,
            (key, str(value), now_utc()),
        )

    _setting_cache_at = 0.0


def _parse_hhmm(text):
    if not text or ":" not in text:
        return None
    try:
        hour, minute = text.split(":", 1)
        hour = int(hour)
        minute = int(minute)
    except (TypeError, ValueError):
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


def is_quiet_now():
    """静音时段判断。任一端为空即视为未启用静音。"""
    start = _parse_hhmm(get_setting("notify_quiet_start", ""))
    end = _parse_hhmm(get_setting("notify_quiet_end", ""))

    if start is None or end is None or start == end:
        return False

    current = datetime.now(LOCAL_TZ)
    minute_of_day = current.hour * 60 + current.minute

    if start < end:
        return start <= minute_of_day < end
    return minute_of_day >= start or minute_of_day < end


def quiet_text():
    start = get_setting("notify_quiet_start", "")
    end = get_setting("notify_quiet_end", "")
    if _parse_hhmm(start) is None or _parse_hhmm(end) is None:
        return "未启用"
    return f"{start} - {end}（北京时间）"
