"""Shared assistant actions for deskbot and Feishu AI chat."""

import json
import re
from datetime import datetime, timezone, timedelta

import echomate_store

LOCAL_TZ = echomate_store.LOCAL_TZ



def _tool_descriptor(name, description, properties, required):
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


TODO_TOOL = _tool_descriptor(
    "add_todo",
    "创建一条待办，到点会通过桌宠和飞书提醒。用户明确要求提醒或添加待办时调用。",
    {
        "title": {"type": "string", "description": "要做的事情"},
        "due_at": {
            "type": "string",
            "description": "ISO 8601 带时区时间，例如 2026-10-27T09:00:00+08:00",
        },
        "notify_deskbot": {"type": "boolean", "description": "是否通过桌宠播报"},
        "notify_feishu": {"type": "boolean", "description": "是否通过飞书提醒"},
    },
    ["title", "due_at"],
)

MEMORY_TOOL = _tool_descriptor(
    "propose_memory",
    "当用户表达长期偏好、个人资料或希望以后记住的信息时，创建一个待确认记忆候选。不要直接写入正式记忆。",
    {
        "content": {"type": "string", "description": "简洁完整的记忆内容"},
        "category": {
            "type": "string",
            "enum": ["profile", "preference", "fact", "task"],
            "description": "记忆分类",
        },
    },
    ["content"],
)

ASSISTANT_TOOLS = [TODO_TOOL, MEMORY_TOOL]


def _parse_local_datetime(text):
    value = (text or "").strip()
    if not value:
        return None

    normalized = value.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(normalized)
    except ValueError:
        return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=LOCAL_TZ)
    return dt.astimezone(LOCAL_TZ)


def format_due_speech(due, now=None):
    """Format a due datetime for Chinese speech without relying on the LLM."""
    due = due.astimezone(LOCAL_TZ)
    now = (now or datetime.now(LOCAL_TZ)).astimezone(LOCAL_TZ)
    days = (due.date() - now.date()).days
    day_text = {0: "今天", 1: "明天", 2: "后天"}.get(days)
    if day_text is None:
        day_text = f"{due.month}月{due.day}日"

    period = "凌晨"
    if 6 <= due.hour < 12:
        period = "上午"
    elif 12 <= due.hour < 14:
        period = "中午"
    elif 14 <= due.hour < 18:
        period = "下午"
    elif due.hour >= 18:
        period = "晚上"

    hour = due.hour % 12 or 12
    if due.minute:
        return f"{day_text}{period}{hour}点{due.minute}分"
    return f"{day_text}{period}{hour}点"


CHINESE_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3,
    "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}


def _small_number(value):
    value = (value or "").strip()
    if value.isdigit():
        return int(value)
    if not value or any(ch not in CHINESE_DIGITS and ch != "十" for ch in value):
        return None
    if "十" in value:
        left, right = value.split("十", 1)
        tens = CHINESE_DIGITS.get(left, 1) if left else 1
        ones = CHINESE_DIGITS.get(right, 0) if right else 0
        return tens * 10 + ones
    if len(value) == 1:
        return CHINESE_DIGITS[value]
    # Normalize spoken digits such as 零三 / 一五 / 二零.
    digits = "".join(str(CHINESE_DIGITS[ch]) for ch in value)
    return int(digits)


TIME_NUMBER = r"[0-9]{1,2}|[零〇一二两三四五六七八九十]{1,3}"


def _parse_clock(text):
    """Parse one clock expression and return (hour, minute, has_period)."""
    match = re.search(
        r"(?:(今晚|今早|今儿|早上|上午|中午|下午|晚上)\s*)?"
        rf"({TIME_NUMBER})\s*(?:点|时|[:：.．])\s*"
        rf"(?:(半)|({TIME_NUMBER})\s*分?)?",
        text or "",
    )
    if not match:
        return None
    period, hour_text, half, minute_text = match.groups()
    hour = _small_number(hour_text)
    if hour is None:
        return None
    if half:
        minute = 30
    elif minute_text:
        minute = _small_number(minute_text)
    else:
        minute = 0
    if minute is None or minute < 0 or minute > 59:
        return None
    if period in ("下午", "晚上", "今晚") and hour < 12:
        hour += 12
    elif period == "中午" and hour < 11:
        hour += 12
    elif period in ("早上", "今早") and hour == 12:
        hour = 0
    if hour > 23:
        return None
    return hour, minute, bool(period)


def infer_due_at(text, now=None):
    """Understand common Chinese dates and clock expressions.

    The LLM owns semantic interpretation. This helper only handles explicit
    date/time phrases and returns None when a bare clock is ambiguous.
    """
    raw = (text or "").strip()
    now = now or datetime.now(LOCAL_TZ)
    date_match = re.search(r"(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})日?", raw)
    if date_match:
        year, month, day = map(int, date_match.groups())
    else:
        date_match = re.search(r"(今天|明天|后天)", raw)
        if date_match:
            offset = {"今天": 0, "明天": 1, "后天": 2}[date_match.group(1)]
            date_value = now.date() + timedelta(days=offset)
            year, month, day = date_value.year, date_value.month, date_value.day
        else:
            year, month, day = now.year, now.month, now.day

    clock = _parse_clock(raw)
    if clock is None:
        return None
    hour, minute, has_period = clock
    try:
        due = datetime(year, month, day, hour, minute, tzinfo=LOCAL_TZ)
    except ValueError:
        return None

    # A bare clock already in the past is ambiguous, not automatically
    # tomorrow: ask the model/user to clarify instead of inventing a date.
    if not has_period and due <= now:
        return None
    return due

def _clean_title(text, due_at):
    raw = (text or "").strip()
    raw = re.sub(r"^(?:echo|Echo|小echo|小Echo)[，,：:\s]*", "", raw)

    # Match the full leading date/time prefix once. Using a fixed prefix is
    # less error-prone than trying several overlapping partial regexes.
    date_part = (
        r"(?:(?:今天|明天|后天)?\s*(?:早上|上午|中午|下午|晚上)?\s*"
        r"(?:[0-9]{1,2}|[零〇一二两三四五六七八九十]{1,3})\s*"
        r"(?:点|时|:|：)\s*(?:半|[0-9零〇一二两三四五六七八九十]{1,2}分?)?)"
    )
    reminder = re.search(r"(?:提醒我|提醒一下|记得提醒我|别忘了)", raw)
    if reminder:
        after = raw[reminder.end():].strip("，,：: \t\r\n")
        if after:
            # "提醒我 明天9点 交作业": strip a leading date/time.
            after = re.sub(rf"^{date_part}[，,：:\s]*", "", after)
            return after.strip("，,。.!！？?：:；; ") or "待办"
        # "明天9点 提醒我": remove the time and reminder words,
        # leaving the action that appeared before them.
        before = raw[:reminder.start()].strip()
        before = re.sub(rf"{date_part}[，,：:\s]*$", "", before)
        return before.strip("，,。.!！？?：:；; ") or "待办"

    match = re.fullmatch(rf"{date_part}\s*(.+)$", raw)
    if match:
        return match.group(1).strip("，,。.!！？?：:；; ") or "待办"

    raw = re.sub(date_part, "", raw)
    raw = raw.strip("，,。.!！？?：:；; ")
    return raw or "待办"


def create_todo_from_text(owner_id, text, source="deskbot", chat_id="",
                          notify_deskbot=True, notify_feishu=True):
    due = infer_due_at(text)
    if due is None:
        return {"ok": False, "error": "没有识别出具体日期和时间"}

    title = _clean_title(text, due)
    todo_id, result = echomate_store.create_todo(
        owner_id=owner_id,
        title=title,
        due_at_utc=due.astimezone(timezone.utc).isoformat(timespec="seconds"),
        source=source,
        chat_id=chat_id,
        notify_deskbot=notify_deskbot,
        notify_feishu=notify_feishu,
    )
    if result not in ("created", "duplicate"):
        return {"ok": False, "error": result}

    return {
        "ok": True,
        "duplicate": result == "duplicate",
        "todo_id": todo_id,
        "title": title,
        "due_local": due.isoformat(timespec="minutes"),
        "due_text": due.strftime("%Y-%m-%d %H:%M"),
        "due_speech": format_due_speech(due),
    }


def execute_tool_call(name, arguments, owner_id, source, chat_id=""):
    if name == "add_todo":
        try:
            args = json.loads(arguments or "{}")
        except (TypeError, json.JSONDecodeError):
            args = {}
        title = str(
            args.get("title")
            or args.get("content")
            or args.get("description")
            or ""
        ).strip()
        due_text = str(args.get("due_at") or "").strip()
        due = _parse_local_datetime(due_text)
        if not title:
            return {"ok": False, "error": "empty"}
        if due is None:
            return {"ok": False, "error": "bad_due"}

        notify_deskbot = args.get("notify_deskbot", True)
        notify_feishu = args.get("notify_feishu", True)
        if isinstance(notify_deskbot, str):
            notify_deskbot = notify_deskbot.lower() not in ("0", "false", "no")
        if isinstance(notify_feishu, str):
            notify_feishu = notify_feishu.lower() not in ("0", "false", "no")

        todo_id, result = echomate_store.create_todo(
            owner_id=owner_id,
            title=title,
            due_at_utc=due.astimezone(timezone.utc).isoformat(timespec="seconds"),
            source=source,
            chat_id=chat_id,
            notify_deskbot=bool(notify_deskbot),
            notify_feishu=bool(notify_feishu),
        )
        if result not in ("created", "duplicate"):
            return {"ok": False, "error": result}
        return {
            "ok": True,
            "duplicate": result == "duplicate",
            "todo_id": todo_id,
            "title": title,
            "due_text": due.strftime("%Y-%m-%d %H:%M"),
            "due_speech": format_due_speech(due),
        }

    if name == "propose_memory":
        try:
            args = json.loads(arguments or "{}")
        except (TypeError, json.JSONDecodeError):
            args = {}
        content = str(args.get("content") or "").strip()
        category = str(args.get("category") or "").strip() or None
        candidate_id, result = echomate_store.enqueue_memory_candidate(
            owner_id=owner_id,
            content=content,
            source=source,
            category=category,
        )
        return {
            "ok": result in ("created", "duplicate"),
            "candidate_id": candidate_id,
            "result": result,
        }

    return {"ok": False, "error": f"unknown tool: {name}"}


def memory_prompt_block(owner_id):
    items = echomate_store.memory_context(owner_id)
    if not items:
        return ""
    lines = "\n".join(f"- {item['content']}" for item in items)
    return (
        "\n\n以下是用户确认过的长期记忆，回答时优先使用；"
        "与当前事实冲突时先指出，不要编造：\n"
        f"<memory>\n{lines}\n</memory>"
    )
