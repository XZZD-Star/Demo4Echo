"""Shared SQLite connection and lock-retry helpers."""

import functools
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path


DEFAULT_TIMEOUT_SECONDS = 5.0
LOCK_RETRY_DELAYS = (0.1, 0.3, 0.6)


def is_locked_error(exc):
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    message = str(exc).lower()
    return "locked" in message or "busy" in message


def open_connection(db_path, timeout=None, row_factory=True):
    """Open a configured SQLite connection owned by the caller."""
    if timeout is None:
        timeout = DEFAULT_TIMEOUT_SECONDS
    conn = sqlite3.connect(str(Path(db_path)), timeout=float(timeout))
    if row_factory:
        conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={int(float(timeout) * 1000)}")
    return conn


@contextmanager
def connect(db_path, write=False, timeout=None, row_factory=True):
    """Open a short-lived SQLite transaction and always close it."""
    conn = open_connection(db_path, timeout=timeout, row_factory=row_factory)
    try:
        if write:
            conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def retry_locked(func):
    """Retry a complete SQLite operation when the database is temporarily locked."""
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        for attempt, delay in enumerate((0.0, *LOCK_RETRY_DELAYS)):
            if delay:
                time.sleep(delay)
            try:
                return func(*args, **kwargs)
            except sqlite3.OperationalError as exc:
                if not is_locked_error(exc) or attempt == len(LOCK_RETRY_DELAYS):
                    raise
        raise RuntimeError("unreachable")

    return wrapper
