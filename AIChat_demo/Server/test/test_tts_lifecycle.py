import unittest
import queue
import threading
from unittest.mock import patch

from models.tts_model import TTSModel


class FakeSynthesizer:
    instances = []
    fail_preopen = False

    def __init__(self, *args, **kwargs):
        self.calls = []
        self.complete_calls = 0
        self.__class__.instances.append(self)

    def streaming_call(self, text):
        self.calls.append(text)
        if self.fail_preopen and text == "":
            raise RuntimeError("preopen boom")

    def streaming_complete(self):
        self.complete_calls += 1

    def streaming_cancel(self):
        pass

    def get_last_request_id(self):
        return "fake-request"


class TTSLifecycleTest(unittest.TestCase):
    def setUp(self):
        FakeSynthesizer.instances = []
        FakeSynthesizer.fail_preopen = False

    def _model(self):
        with patch("models.tts_model.SpeechSynthesizer", FakeSynthesizer):
            return TTSModel()

    def test_success_moves_through_ready_and_closed(self):
        model = self._model()
        with patch("models.tts_model.SpeechSynthesizer", FakeSynthesizer):
            self.assertTrue(model.tts_stream_set())
            self.assertTrue(model.tts_stream_speech_synthesis("你好"))
            self.assertTrue(model.tts_stream_close())

        synth = model.session.synthesizer
        self.assertEqual(synth.calls, ["", "你好"])
        self.assertEqual(synth.complete_calls, 1)
        self.assertFalse(model.is_ready())

    def test_preopen_failure_is_not_reused_or_closed(self):
        FakeSynthesizer.fail_preopen = True
        model = self._model()
        with patch("models.tts_model.SpeechSynthesizer", FakeSynthesizer):
            self.assertFalse(model.tts_stream_set())
            self.assertIn("preopen boom", model.last_error())
            self.assertFalse(model.tts_stream_speech_synthesis("你好"))
            self.assertFalse(model.tts_stream_close())

        synth = model.session.synthesizer
        self.assertEqual(synth.calls, [""])
        self.assertEqual(synth.complete_calls, 0)

    def test_close_callback_invalidates_current_session(self):
        model = self._model()
        with patch("models.tts_model.SpeechSynthesizer", FakeSynthesizer):
            self.assertTrue(model.tts_stream_set())
            self.assertTrue(model.is_ready())
            model.session.callback.on_close()
            self.assertFalse(model.is_ready())

    def test_error_callback_invalidates_current_session(self):
        model = self._model()
        with patch("models.tts_model.SpeechSynthesizer", FakeSynthesizer):
            self.assertTrue(model.tts_stream_set())
            model.session.callback.on_error("idle timeout")
            self.assertFalse(model.is_ready())
            self.assertIn("idle timeout", model.last_error())

    def test_stale_close_callback_does_not_invalidate_new_session(self):
        model = self._model()
        with patch("models.tts_model.SpeechSynthesizer", FakeSynthesizer):
            self.assertTrue(model.tts_stream_set())
            old_session = model.session
            self.assertTrue(model.tts_stream_set())
            old_session.callback.on_close()
            self.assertTrue(model.is_ready())

    def test_service_returns_failure_and_creates_fresh_object(self):
        from services.tts_service import TTSService

        FakeSynthesizer.fail_preopen = True
        with patch("models.tts_model.SpeechSynthesizer", FakeSynthesizer):
            service = TTSService()
            self.assertFalse(service.tts_set())
            self.assertFalse(service.tts_speech_stream("你好"))
            self.assertIn("preopen boom", service.last_error())

        FakeSynthesizer.fail_preopen = False
        with patch("models.tts_model.SpeechSynthesizer", FakeSynthesizer):
            self.assertTrue(service.tts_set())
            self.assertTrue(service.tts_speech_stream("你好"))
            self.assertTrue(service.tts_close())

    def test_service_reopens_after_idle_close(self):
        from services.tts_service import TTSService

        with patch("models.tts_model.SpeechSynthesizer", FakeSynthesizer):
            service = TTSService()
            self.assertTrue(service.tts_set())
            old_session = service.tts_model.session
            old_session.callback.on_close()
            self.assertFalse(service.is_ready())
            self.assertTrue(service.tts_set())
            self.assertIsNot(service.tts_model.session, old_session)
            self.assertTrue(service.tts_speech_stream("你好"))
            self.assertTrue(service.tts_close())

    def test_speak_reply_rebuilds_a_failed_preopen(self):
        from service_manager import ServiceManager

        class FakeTTSService:
            def __init__(self):
                self.ready = False
                self.set_calls = 0
                self.speech_calls = 0
                self.close_calls = 0

            def is_ready(self):
                return self.ready

            def tts_set(self, **kwargs):
                self.set_calls += 1
                self.ready = True
                return True

            def tts_speech_stream(self, text):
                self.speech_calls += 1
                return True

            def tts_close(self):
                self.close_calls += 1
                return True

            def last_error(self):
                return None

            def session_id(self):
                return "fake"

        manager = ServiceManager.__new__(ServiceManager)
        manager._tts_lock = threading.Lock()
        manager._tts_epoch = 7
        manager._tts_active = False
        manager._tts_active_epoch = None
        manager.chat_turn_lock = threading.Lock()
        manager.chat_turn_id = 3
        manager._active_chat_trace_id = "voice-test"
        manager.ws_send_queue = queue.Queue()
        manager.tts_service = FakeTTSService()

        with patch("service_manager.flow_log.emit"):
            self.assertTrue(manager._speak_reply("hello", 3))

        self.assertEqual(manager.tts_service.set_calls, 1)
        self.assertEqual(manager.tts_service.speech_calls, 1)
        self.assertEqual(manager.tts_service.close_calls, 1)

    def test_split_tts_chunks_prefers_sentence_boundaries(self):
        from service_manager import split_tts_chunks

        chunks, pending = split_tts_chunks(
            "你好。今天天气不错，适合出门。"
        )
        self.assertEqual(
            chunks,
            ["你好。", "今天天气不错，适合出门。"],
        )
        self.assertEqual(pending, "")

        chunks, pending = split_tts_chunks("一" * 30)
        self.assertEqual(len(chunks[0]), 24)
        self.assertEqual(len(pending), 6)

    def test_stream_reply_sends_each_sentence_once(self):
        from service_manager import ServiceManager

        class FakeTTSService:
            def __init__(self):
                self.texts = []
                self.close_calls = 0

            def is_ready(self):
                return True

            def tts_speech_stream(self, text):
                self.texts.append(text)
                return True

            def tts_close(self):
                self.close_calls += 1
                return True

            def last_error(self):
                return None

        manager = ServiceManager.__new__(ServiceManager)
        manager._tts_lock = threading.Lock()
        manager._tts_epoch = 9
        manager._tts_active = False
        manager._tts_active_epoch = None
        manager.chat_turn_lock = threading.Lock()
        manager.chat_turn_id = 4
        manager._active_chat_trace_id = "voice-stream"
        manager.tts_service = FakeTTSService()

        with patch("service_manager.flow_log.emit"):
            reply = manager._stream_reply_to_tts(
                iter(["你好。", "今天天气不错，适合出门。"]),
                4,
            )

        self.assertEqual(reply, "你好。今天天气不错，适合出门。")
        self.assertEqual(
            manager.tts_service.texts,
            ["你好。", "今天天气不错，适合出门。"],
        )
        self.assertEqual(manager.tts_service.close_calls, 1)


if __name__ == "__main__":
    unittest.main()
