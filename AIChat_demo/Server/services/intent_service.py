"""Native tool routing for voice chat."""

import json
from datetime import datetime
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from openai import OpenAI

import assistant_core
from config.settings import global_settings
from tools.logger import logger


def _compatible_base_url():
    endpoint = (global_settings.DASHSCOPE_BASE_WEBSOCKET_API_URL or "").strip()
    host = urlparse(endpoint).hostname
    if host and host.endswith(".maas.aliyuncs.com"):
        return f"https://{host}/compatible-mode/v1"
    return "https://dashscope.aliyuncs.com/compatible-mode/v1"


class IntentService:
    def __init__(self, registry=None):
        self.registry = registry
        self.model = global_settings.INTENT_MODEL
        self.client = OpenAI(
            api_key=global_settings.DASHSCOPE_API_KEY,
            base_url=_compatible_base_url(),
            timeout=20.0,
            max_retries=0,
        )

    def _system_prompt(self):
        now = datetime.now(ZoneInfo("Asia/Shanghai")).strftime(
            "%Y-%m-%d %H:%M %A"
        )
        return (
            "你是 Echo-Mate 语音工具路由器。只调用一个最合适的工具，"
            "不要直接回答用户。\n"
            f"当前北京时间：{now}\n"
            "普通聊天、闲聊或无需工具时调用 continue_chat。"
            "用户明确结束对话时调用 exit_chat。"
            "天气、待办列表、过去记忆分别调用 get_weather、list_todos、"
            "search_memories。\n"
            "只有用户明确要求创建提醒/待办时才调用 add_todo；"
            "只有用户明确要求长期记住个人资料或偏好时才调用 propose_memory。\n"
            "add_todo 的 due_at 必须使用带时区的 ISO 8601 时间。"
            "时间不完整或无法确定时调用 continue_chat，让聊天模型向用户澄清。\n"
            "不要一次调用多个工具，不要返回 JSON 文本。"
        )

    def detect_intent(self, user_input):
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": self._system_prompt()},
                    {"role": "user", "content": user_input},
                ],
                tools=assistant_core.VOICE_ROUTER_TOOLS,
                tool_choice="auto",
                max_tokens=240,
                temperature=0,
            )
            message = response.choices[0].message
            tool_calls = getattr(message, "tool_calls", None) or []
            if not tool_calls:
                return [{"function_call": {"name": "continue_chat"}}]

            call = tool_calls[0]
            name = call.function.name
            allowed = {
                tool["function"]["name"]
                for tool in assistant_core.VOICE_ROUTER_TOOLS
            }
            if name not in allowed:
                logger.warning("语音路由返回未知工具：%s", name)
                return [{"function_call": {"name": "continue_chat"}}]
            try:
                arguments = json.loads(call.function.arguments or "{}")
            except (TypeError, json.JSONDecodeError):
                arguments = {}
            if not isinstance(arguments, dict):
                arguments = {}
            return [{
                "function_call": {
                    "name": name,
                    "arguments": arguments,
                }
            }]
        except Exception as exc:
            logger.warning("语音意图路由失败：%s", type(exc).__name__)
            return [{"function_call": {"name": "continue_chat"}}]
