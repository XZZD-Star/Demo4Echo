import sys
import threading
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SHARED_ROOT = ROOT.parents[3] / "shared"
sys.path.insert(0, str(SHARED_ROOT))
sys.path.insert(0, str(ROOT))

from service_manager import ServiceManager
from models.llm_model import LLMModel


class FakeChatService:
    def __init__(self):
        self.clear_calls = 0

    def chat_clear(self):
        self.clear_calls += 1


class WorkingMemoryTest(unittest.TestCase):
    def _manager(self):
        manager = ServiceManager.__new__(ServiceManager)
        manager.asr_backend = "cloud"
        manager.cloud_asr_service = None
        manager.is_vad = False
        manager._asr_ready = threading.Event()
        manager.chat_service = FakeChatService()
        manager.notify_playing = True
        manager._conversation_session_id = None
        manager._conversation_session_generation = 0
        manager._conversation_session_lock = threading.Lock()
        return manager

    def test_idle_reset_keeps_working_memory(self):
        manager = self._manager()
        manager.reset_services()
        self.assertEqual(manager.chat_service.clear_calls, 0)

    def test_disconnect_clear_releases_working_memory(self):
        manager = self._manager()
        session_id = manager.ensure_conversation_session()
        manager.reset_services(clear_chat=True)
        self.assertEqual(manager.chat_service.clear_calls, 1)
        self.assertNotEqual(
            manager.ensure_conversation_session(),
            session_id,
        )

    def test_connection_session_is_stable_until_ended(self):
        manager = self._manager()
        first = manager.ensure_conversation_session()
        second = manager.ensure_conversation_session()
        self.assertEqual(first, second)
        manager.end_conversation()
        third = manager.ensure_conversation_session()
        self.assertNotEqual(first, third)

    def test_llm_working_memory_is_bounded(self):
        model = LLMModel()
        model.max_context_messages = 4
        for index in range(10):
            model.add_message("user", str(index))
        self.assertEqual(len(model.messages), 5)
        self.assertEqual(model.messages[0]["role"], "system")
        self.assertEqual(model.messages[-1]["content"], "9")


if __name__ == "__main__":
    unittest.main()
