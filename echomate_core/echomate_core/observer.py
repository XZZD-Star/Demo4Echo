"""Best-effort asynchronous memory extraction for chat turns."""

import json
import logging
import queue
import re
import threading
import time

from . import store as echomate_store

logger = logging.getLogger("echomate.memory")

_SENSITIVE_PATTERN = re.compile(
    r"(?:密码|验证码|银行卡|身份证|护照号|私钥|密钥|完整住址|家庭住址)"
)
_PREFERENCE_KEY_PATTERN = re.compile(r"^[a-z_]{2,32}$")


def _strip_json_fence(text):
    value = (text or "").strip()
    if value.startswith("```json"):
        value = value[7:]
    elif value.startswith("```"):
        value = value[3:]
    if value.endswith("```"):
        value = value[:-3]
    return value.strip()


def _safe_text(value, limit):
    text = str(value or "").strip()
    if not text or _SENSITIVE_PATTERN.search(text):
        return ""
    return text[:limit]


def observe_turn_now(client, model, *, owner_id, session_id, turn_id,
                     source, user_text, tool_runs=None):
    """Extract at most one chat memory and one durable preference."""
    user_text = (user_text or "").strip()
    if not user_text:
        return {"chat_memory": None, "preference": None}

    safe_tools = [
        {
            "tool": run.get("tool"),
            "ok": bool(run.get("ok")),
        }
        for run in (tool_runs or [])
        if isinstance(run, dict)
    ]
    prompt = {
        "user_text": user_text,
        "verified_tool_status": safe_tools,
        "current_time": echomate_store.now_utc(),
    }
    response = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": (
                    "你是 Echo-Mate 的后台记忆观察器。只根据 user_text "
                    "和已验证工具状态提取记忆，不得根据助手回答推测事实。"
                    "只返回 JSON 对象，不要 Markdown。格式："
                    '{"chat_memory": string|null,'
                    '"preference":{"key":string,"content":string,'
                    '"confidence":number,"explicit":boolean}|null}。'
                    "chat_memory 只记录用户本人的稳定资料、经历或近期事项，"
                    "一句中文，最多80字；没有值得跨会话保留的信息返回 null。"
                    "preference 只记录回答风格、称呼、兴趣、作息等长期偏好；"
                    "一次性情绪、临时请求、敏感信息、猜测和助手说过的内容不得提取。"
                    "key 只能使用英文小写和下划线，例如 response_style、"
                    "address_name、interest、routine、tone。"
                ),
            },
            {
                "role": "user",
                "content": json.dumps(prompt, ensure_ascii=False),
            },
        ],
        max_tokens=260,
        temperature=0,
    )
    content = response.choices[0].message.content or "{}"
    try:
        data = json.loads(_strip_json_fence(content))
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        logger.warning("Memory observer returned invalid JSON: %s", type(exc).__name__)
        return {"chat_memory": None, "preference": None}

    chat_memory = _safe_text(data.get("chat_memory"), 300)
    if chat_memory:
        memory_id, result = echomate_store.add_chat_memory(
            owner_id,
            chat_memory,
            session_id=session_id,
            source_turn_id=turn_id,
            source=source,
            kind="chat",
            expires_days=90,
        )
    else:
        memory_id, result = None, "skipped"

    preference = data.get("preference")
    preference_result = "skipped"
    if isinstance(preference, dict):
        key = str(preference.get("key") or "").strip()
        content = _safe_text(preference.get("content"), 240)
        try:
            confidence = float(preference.get("confidence") or 0)
        except (TypeError, ValueError):
            confidence = 0
        explicit = bool(preference.get("explicit"))
        if (
            content
            and _PREFERENCE_KEY_PATTERN.fullmatch(key)
            and (explicit and confidence >= 0.8 or confidence >= 0.92)
        ):
            _, preference_result = echomate_store.upsert_preference_memory(
                owner_id,
                content,
                key,
                source=f"{source}_auto",
                confidence=confidence,
                source_turn_id=turn_id,
            )

    return {
        "chat_memory": {
            "id": memory_id,
            "result": result,
        } if memory_id else None,
        "preference": preference_result,
    }


class MemoryObserver:
    """One bounded background worker per service process."""

    def __init__(self, client, model, *, max_queue=8, delay_seconds=2.0,
                 can_process=None, poll_seconds=0.5):
        self.client = client
        self.model = model
        self.delay_seconds = max(0.0, float(delay_seconds))
        self.poll_seconds = max(0.1, float(poll_seconds))
        self._can_process = can_process or (lambda: True)
        self.queue = queue.Queue(maxsize=max(1, int(max_queue)))
        self._thread = threading.Thread(
            target=self._run,
            name="memory-observer",
            daemon=True,
        )
        self._thread.start()

    def enqueue_turn(self, *, owner_id, session_id, turn_id, source,
                     user_text, tool_runs=None):
        job = {
            "owner_id": owner_id,
            "session_id": session_id or "",
            "turn_id": turn_id or "",
            "source": source or "chat",
            "user_text": user_text or "",
            "tool_runs": list(tool_runs or []),
        }
        try:
            self.queue.put_nowait(job)
            return True
        except queue.Full:
            logger.warning("Memory observer queue full; dropping turn")
            return False

    def _run(self):
        while True:
            job = self.queue.get()
            try:
                while not self._wait_for_quiet_window():
                    time.sleep(self.poll_seconds)
                started = time.monotonic()
                observe_turn_now(
                    self.client,
                    self.model,
                    **job,
                )
                logger.info(
                    "Memory observer completed: model=%s elapsed_ms=%d",
                    self.model,
                    int((time.monotonic() - started) * 1000),
                )
            except Exception as exc:
                logger.warning(
                    "Memory observer failed: %s", type(exc).__name__
                )
            finally:
                self.queue.task_done()

    def _wait_for_quiet_window(self):
        if not self._can_process():
            return False
        deadline = time.monotonic() + self.delay_seconds
        while time.monotonic() < deadline:
            time.sleep(
                min(self.poll_seconds, max(0.0, deadline - time.monotonic()))
            )
            if not self._can_process():
                return False
        return True
