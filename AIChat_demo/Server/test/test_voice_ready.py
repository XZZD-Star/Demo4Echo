import asyncio
import json
import queue
import tempfile
import threading
import unittest
from pathlib import Path

from handle.text_handler import TextHandler
from service_manager import ServiceManager
from services.cloud_asr_service import CloudASRService
from tools import flow_log


def make_service(on_ready=None):
    service = CloudASRService.__new__(CloudASRService)
    service._stop = threading.Event()
    service._input_finished = threading.Event()
    service._ready = threading.Event()
    service._audio_queue = queue.Queue(maxsize=2)
    service._terminal = False
    service._ready_notified = False
    service._speech_started = False
    service.on_ready = on_ready
    return service


class VoiceReadyTests(unittest.TestCase):
    def test_audio_is_dropped_before_session_ready(self):
        service = make_service()

        service.send_audio(b"\x00\x00" * 640)

        self.assertTrue(service._audio_queue.empty())

    def test_audio_is_queued_after_session_ready(self):
        service = make_service()
        service._ready.set()

        service.send_audio(b"\x00\x00" * 640)

        self.assertEqual(service._audio_queue.qsize(), 1)

    def test_ready_callback_runs_once(self):
        calls = []
        service = make_service(on_ready=lambda: calls.append("ready"))

        event = json.dumps({"type": "session.updated"})
        service._on_message(None, event)
        service._on_message(None, event)

        self.assertEqual(calls, ["ready"])

    def test_wake_event_prewarms_asr(self):
        class FakeManager:
            notify_playing = False

            def __init__(self):
                self.started = 0
                self.trace_id = "voice-test"

            def begin_voice_trace(self, turn_id=None):
                self.trace_id = turn_id or self.trace_id
                return self.trace_id

            def start_asr_session(self):
                self.started += 1

        manager = FakeManager()
        handler = TextHandler(manager)
        with tempfile.TemporaryDirectory() as temp_dir:
            old_path = flow_log.DEFAULT_PATH
            flow_log.DEFAULT_PATH = Path(temp_dir) / "flow.jsonl"
            try:
                asyncio.run(handler.handle_text_message({
                    "type": "wake_detected",
                    "turn_id": "voice-client-test",
                }))
                events = flow_log.read_events(limit=10)
            finally:
                flow_log.DEFAULT_PATH = old_path

        self.assertEqual(manager.started, 1)
        self.assertEqual(events[0]["step"], "wake")
        self.assertEqual(events[0]["trace_id"], "voice-client-test")

    def test_no_speech_sends_one_state_event(self):
        manager = ServiceManager.__new__(ServiceManager)
        manager.is_vad = False
        manager._asr_ready = threading.Event()
        manager._asr_ready.set()
        manager.conversation_busy = threading.Event()
        manager.conversation_busy.set()
        manager.ws_send_queue = queue.Queue()
        manager._voice_trace_lock = threading.Lock()
        manager._active_voice_trace_id = "voice-test"

        with tempfile.TemporaryDirectory() as temp_dir:
            old_path = flow_log.DEFAULT_PATH
            flow_log.DEFAULT_PATH = Path(temp_dir) / "flow.jsonl"
            try:
                manager._on_cloud_asr_no_speech("timeout")
            finally:
                flow_log.DEFAULT_PATH = old_path

        payload = json.loads(manager.ws_send_queue.get_nowait())
        self.assertEqual(payload["type"], "vad")
        self.assertEqual(payload["state"], "no_speech")
        self.assertTrue(manager.ws_send_queue.empty())
        self.assertFalse(manager._asr_ready.is_set())
        self.assertFalse(manager.conversation_busy.is_set())

    def test_inactive_tts_error_is_not_forwarded(self):
        manager = ServiceManager.__new__(ServiceManager)
        manager._tts_lock = threading.Lock()
        manager._tts_active = False
        manager._tts_active_epoch = None
        manager.ws_send_queue = queue.Queue()

        manager._tts_on_error("empty synthesis", 1)

        self.assertTrue(manager.ws_send_queue.empty())


if __name__ == "__main__":
    unittest.main()
