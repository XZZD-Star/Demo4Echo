import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[1]
SHARED_ROOT = ROOT.parents[3] / "shared"
sys.path.insert(0, str(SHARED_ROOT))
sys.path.insert(0, str(ROOT))

import echomate_store
import memory_observer


class FakeCompletions:
    def __init__(self, payload):
        self.payload = payload

    def create(self, **kwargs):
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=json.dumps(self.payload, ensure_ascii=False)
                    )
                )
            ]
        )


class FakeClient:
    def __init__(self, payload):
        self.chat = SimpleNamespace(
            completions=FakeCompletions(payload)
        )


class MemoryObserverTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "assistant.db"
        echomate_store.DB_PATH = self.db_path
        echomate_store.init_schema()

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_chat_memory_and_preference_are_saved(self):
        client = FakeClient({
            "chat_memory": "用户最近在准备挑战杯比赛",
            "preference": {
                "key": "response_style",
                "content": "用户喜欢先听结论",
                "confidence": 0.95,
                "explicit": True,
            },
        })
        result = memory_observer.observe_turn_now(
            client,
            "fake-model",
            owner_id="owner",
            session_id="voice-session",
            turn_id="turn-1",
            source="voice",
            user_text="我最近在准备挑战杯，以后回答先给结论",
        )
        self.assertEqual(result["chat_memory"]["result"], "created")
        self.assertEqual(result["preference"], "created")
        self.assertEqual(len(echomate_store.list_chat_memories("owner")), 1)
        self.assertEqual(
            echomate_store.memory_context("owner")[0]["content"],
            "用户喜欢先听结论",
        )

    def test_sensitive_and_low_confidence_preference_is_skipped(self):
        client = FakeClient({
            "chat_memory": "用户银行卡密码是 123456",
            "preference": {
                "key": "response_style",
                "content": "用户可能喜欢长回答",
                "confidence": 0.4,
                "explicit": False,
            },
        })
        result = memory_observer.observe_turn_now(
            client,
            "fake-model",
            owner_id="owner",
            session_id="feishu",
            turn_id="turn-2",
            source="feishu",
            user_text="测试",
        )
        self.assertIsNone(result["chat_memory"])
        self.assertEqual(result["preference"], "skipped")
        self.assertEqual(echomate_store.list_chat_memories("owner"), [])
        self.assertEqual(echomate_store.memory_context("owner"), [])


if __name__ == "__main__":
    unittest.main()
