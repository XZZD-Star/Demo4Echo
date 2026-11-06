import json
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
CORE_ROOT = ROOT.parents[1] / "echomate_core"
sys.path.insert(0, str(CORE_ROOT))
sys.path.insert(0, str(ROOT))

from services.intent_service import IntentService


class FakeCompletions:
    def __init__(self, tool_name="", arguments=None):
        self.tool_name = tool_name
        self.arguments = arguments or {}
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        calls = []
        if self.tool_name:
            calls.append(
                SimpleNamespace(
                    function=SimpleNamespace(
                        name=self.tool_name,
                        arguments=json.dumps(self.arguments),
                    )
                )
            )
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content="",
                        tool_calls=calls,
                    )
                )
            ]
        )


class FakeClient:
    def __init__(self, tool_name="", arguments=None):
        self.chat = SimpleNamespace(
            completions=FakeCompletions(tool_name, arguments)
        )


class IntentServiceTest(unittest.TestCase):
    def _service(self, client):
        service = IntentService.__new__(IntentService)
        service.client = client
        service.model = "fake-model"
        service.registry = None
        return service

    def test_native_tool_call_is_normalized(self):
        client = FakeClient(
            "add_todo",
            {
                "title": "带书",
                "due_at": "2026-11-04T09:00:00+08:00",
            },
        )
        result = self._service(client).detect_intent(
            "明天九点提醒我带书"
        )
        self.assertEqual(result[0]["function_call"]["name"], "add_todo")
        self.assertEqual(
            result[0]["function_call"]["arguments"]["title"], "带书"
        )
        request = client.chat.completions.calls[0]
        self.assertNotEqual(request["tools"], [])

    def test_no_tool_falls_back_to_continue_chat(self):
        result = self._service(FakeClient()).detect_intent("你好")
        self.assertEqual(
            result[0]["function_call"]["name"], "continue_chat"
        )

    def test_unknown_tool_falls_back_to_continue_chat(self):
        result = self._service(FakeClient("unknown_tool")).detect_intent(
            "测试"
        )
        self.assertEqual(
            result[0]["function_call"]["name"], "continue_chat"
        )

    def test_current_session_recall_uses_working_memory(self):
        class FailIfCalled:
            class Chat:
                class Completions:
                    def create(self, **kwargs):
                        raise AssertionError("LLM should not be called")

                completions = Completions()

            chat = Chat()

        result = self._service(FailIfCalled()).detect_intent(
            "你知道我上一条问了你什么吗？"
        )
        self.assertEqual(
            result[0]["function_call"]["name"], "continue_chat"
        )


if __name__ == "__main__":
    unittest.main()
