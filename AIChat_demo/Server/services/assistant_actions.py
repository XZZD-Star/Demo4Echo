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


CHINESE_DIGITS = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3,
    "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}


def _small_number(value):
    value = (value or "").strip()
    if value.isdigit():
        return int(value)
    if value in CHINESE_DIGITS:
        return CHINESE_DIGITS[value]
    if value == "十":
        return 10
    if value.startswith("十") and len(value) == 2 and value[1] in CHINESE_DIGITS:
        return 10 + CHINESE_DIGITS[value[1]]
    if len(value) == 2 and value[0] in CHINESE_DIGITS and value[1] == "十":
        return CHINESE_DIGITS[value[0]] * 10
    if len(value) == 3 and value[0] in CHINESE_DIGITS and value[1] == "十" and value[2] in CHINESE_DIGITS:
        return CHINESE_DIGITS[value[0]] * 10 + CHINESE_DIGITS[value[2]]
    return None


def infer_due_at(text):
    """Small deterministic fallback for common Chinese date/time phrases."""
    raw = (text or "").strip()
    now = datetime.now(LOCAL_TZ)

    day = None
    if "后天" in raw:
        day = now.date() + timedelta(days=2)
    elif "明天" in raw:
        day = now.date() + timedelta(days=1)
    elif "今天" in raw:
        day = now.date()

    hour = minute = None
    match = re.search(
        r"(?:早上|上午|中午|下午|晚上)?\s*"
        r"([0-9]{1,2}|[零〇一二两三四五六七八九十]{1,3})\s*"
        r"(?:点|时|:|：)\s*(半|([0-9]{1,2})分?)?",
        raw,
    )
    if match:
        hour = _small_number(match.group(1))
        tail = match.group(2)
        if hour is None:
            return None
        if tail == "半":
            minute = 30
        elif match.group(3):
            minute = int(match.group(3))
        else:
            minute = 0
        if any(word in raw for word in ("下午", "晚上")) and hour < 12:
            hour += 12
        elif "中午" in raw and hour < 11:
            hour += 12

    if day is None:
        day = now.date()
    if hour is None:
        return None

    try:
        return datetime(day.year, day.month, day.day, hour, minute, tzinfo=LOCAL_TZ)
    except ValueError:
        return None


def _clean_title(text, due_at):
    raw = (text or "").strip()
    raw = re.sub(r"^(?:echo|Echo|小echo|小Echo)[，,：:\s]*", "", raw)

    # Match the full leading date/time prefix once. Using a fixed prefix is
    # less error-prone than trying several overlapping partial regexes.
    date_part = (
        r"(?:今天|明天|后天)?\s*(?:早上|上午|中午|下午|晚上)?\s*"
        r"(?:[0-9]{1,2}|[零〇一二两三四五六七八九十]{1,3})\s*"
        r"(?:点|时|:|：)\s*(?:半|[0-9]{1,2}分?)?"
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
    }


def execute_tool_call(name, arguments, owner_id, source, chat_id=""):
    if name == "add_todo":
        try:
            args = json.loads(arguments or "{}")
        except (TypeError, json.JSONDecodeError):
            args = {}
        title = str(args.get("title") or "").strip()
        due_text = str(args.get("due_at") or "").strip()
        due = _parse_local_datetime(due_text)
        if not title or due is None:
            return {"ok": False, "error": "待办标题或时间无效"}

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
