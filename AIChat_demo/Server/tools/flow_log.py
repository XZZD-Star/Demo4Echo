"""Append-only structured event log used by the local flow viewer."""

import json
import os
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows development fallback
    fcntl = None

LOCAL_TZ = timezone(timedelta(hours=8))
DEFAULT_PATH = Path(
    os.environ.get("ECHOMATE_FLOW_LOG", "/var/log/echomate/flow.jsonl")
)
MAX_EVENT_LINE = 16 * 1024
MAX_SCAN_LINES = 10_000
_lock = threading.Lock()
_sequence = 0
_pid = os.getpid()
BASE_FIELDS = {
    "schema", "id", "ts", "component", "step", "status", "trace_id",
    "notify_id", "duration_ms", "message",
}


def _redact(key, value):
    lowered = str(key).lower()
    if any(token in lowered for token in (
        "token", "secret", "password", "api_key", "authorization",
    )):
        return "<redacted>"
    if isinstance(value, dict):
        return {
            str(item_key): _redact(item_key, item_value)
            for item_key, item_value in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact("", item) for item in value]
    if isinstance(value, str) and len(value) > 500:
        return value[:500] + "..."
    return value


def emit(component, step, status="info", trace_id=None, notify_id=None,
         duration_ms=None, message=None, **fields):
    """Append one concise flow event without blocking the caller on errors."""
    global _sequence
    with _lock:
        _sequence += 1
        event = {
            "schema": 1,
            "id": f"{int(time.time() * 1000)}-{_pid}-{_sequence}",
            "ts": datetime.now(LOCAL_TZ).isoformat(timespec="milliseconds"),
            "component": str(component),
            "step": str(step),
            "status": str(status),
            "trace_id": str(trace_id) if trace_id else None,
            "notify_id": int(notify_id) if notify_id is not None else None,
            "duration_ms": duration_ms,
            "message": _redact("message", message),
        }
        for key, value in fields.items():
            event[str(key)] = _redact(key, value)

        try:
            path = Path(DEFAULT_PATH)
            path.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(path.parent, 0o700)
            line = json.dumps(
                event, ensure_ascii=False, separators=(",", ":")
            )
            if len(line.encode("utf-8")) > MAX_EVENT_LINE:
                event = {
                    key: value for key, value in event.items()
                    if key in BASE_FIELDS
                }
                event["fields_truncated"] = True
                line = json.dumps(
                    event, ensure_ascii=False, separators=(",", ":")
                )
            lock_path = path.with_name(path.name + ".lock")
            lock_fd = os.open(
                lock_path, os.O_WRONLY | os.O_CREAT, 0o600
            )
            try:
                if fcntl is not None:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX)
                fd = os.open(
                    path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600
                )
                try:
                    os.write(fd, (line + "\n").encode("utf-8"))
                finally:
                    os.close(fd)
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
        except (OSError, TypeError, ValueError):
            # Observability must never interrupt voice/notification flow.
            return False
    return True


def read_events(limit=200, trace_id=None, notify_id=None, component=None,
                status=None, after_id=None):
    """Read recent events, optionally returning only events after an id."""
    limit = max(1, min(int(limit), 5000))
    path = Path(DEFAULT_PATH)
    if not path.exists():
        return []
    with _lock:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            lines = deque(handle, maxlen=MAX_SCAN_LINES)

    parsed = []
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        parsed.append(event)

    start_index = 0
    if after_id:
        for index, event in enumerate(parsed):
            if event.get("id") == after_id:
                start_index = index + 1
                break

    result = []
    for event in parsed[start_index:]:
        if trace_id and event.get("trace_id") != trace_id:
            continue
        if notify_id is not None and event.get("notify_id") != int(notify_id):
            continue
        if component and event.get("component") != component:
            continue
        if status and event.get("status") != status:
            continue
        result.append(event)
    return result[-limit:]


def health():
    path = Path(DEFAULT_PATH)
    try:
        stat = path.stat()
        return {
            "ok": True,
            "path": str(path),
            "size": stat.st_size,
            "modified_at": datetime.fromtimestamp(
                stat.st_mtime, LOCAL_TZ
            ).isoformat(timespec="seconds"),
        }
    except OSError as exc:
        return {"ok": False, "path": str(path), "error": str(exc)}
