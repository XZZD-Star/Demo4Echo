import threading
import time
import uuid

from dashscope.audio.tts_v2 import (
    AudioFormat,
    ResultCallback,
    SpeechSynthesizer,
)

from config.settings import global_settings
from tools.logger import logger


class _TTSResultCallback(ResultCallback):
    def __init__(self, session):
        self.session = session

    def on_open(self):
        self.session._handle_open()

    def on_data(self, data: bytes):
        self.session._handle_data(data)

    def on_complete(self):
        self.session._handle_complete()

    def on_error(self, message: str):
        self.session._handle_error(message)

    def on_close(self):
        self.session._handle_close()


class TTSStreamSession:
    """One real TTS WebSocket connection and its callback lifetime."""

    def __init__(self):
        self.session_id = uuid.uuid4().hex[:8]
        self.callback = None
        self.synthesizer = None
        self._opened = False
        self._closed = threading.Event()
        self._error = None
        self._text_sent = False
        self._on_complete = None
        self._on_error = None
        self._on_close = None
        self._on_data = None

    def open(
        self,
        on_open=None,
        on_complete=None,
        on_error=None,
        on_close=None,
        on_data=None,
    ):
        self._on_complete = on_complete
        self._on_error = on_error
        self._on_close = on_close
        self._on_data = on_data
        self.callback = _TTSResultCallback(self)
        self.synthesizer = SpeechSynthesizer(
            model=global_settings.COSYVOICE_MODEL,
            voice=global_settings.COSYVOICE_VOICE,
            format=AudioFormat.PCM_16000HZ_MONO_16BIT,
            callback=self.callback,
        )
        try:
            self.synthesizer.streaming_call("")
            time.sleep(0.1)
            if self._closed.is_set() or self._error:
                return False
            self._opened = True
            return True
        except Exception as exc:
            self._mark_error(exc)
            return False

    def send(self, text):
        if not text or not self.is_usable():
            return False
        try:
            self.synthesizer.streaming_call(text)
            self._text_sent = True
            return True
        except Exception as exc:
            self._mark_error(exc)
            return False

    def complete(self):
        if not self.is_usable():
            return False
        try:
            self.synthesizer.streaming_complete()
            if self._error:
                return False
            logger.info(
                "[TTS %s] Request ID: %s",
                self.session_id,
                self.synthesizer.get_last_request_id(),
            )
            return True
        except Exception as exc:
            self._mark_error(exc)
            return False
        finally:
            self._closed.set()

    def abort(self):
        if self._closed.is_set():
            return
        try:
            cancel = getattr(self.synthesizer, "streaming_cancel", None)
            if cancel:
                cancel()
        except Exception:
            pass
        finally:
            self._closed.set()

    def is_usable(self):
        return (
            self._opened
            and not self._closed.is_set()
            and self._error is None
        )

    def has_text(self):
        return self._text_sent

    def last_error(self):
        return self._error

    def _mark_error(self, exc):
        self._error = str(exc) or exc.__class__.__name__
        self._closed.set()
        logger.warning(
            "[TTS %s] connection failed: %s",
            self.session_id,
            self._error,
        )
        if self._on_error:
            self._on_error(self._error)

    def _handle_open(self):
        logger.info("[TTS %s] server-WS is open.", self.session_id)

    def _handle_data(self, data):
        if self._error is None and self._on_data:
            self._on_data(data)

    def _handle_complete(self):
        if self._on_complete:
            self._on_complete()

    def _handle_error(self, message):
        self._mark_error(message)

    def _handle_close(self):
        self._closed.set()
        logger.info("[TTS %s] server-WS is closed.", self.session_id)
        if self._on_close:
            self._on_close()


class TTSModel:
    """Compatibility facade that owns one replaceable TTS session."""

    def __init__(self):
        self.session = None

    def tts_stream_set(
        self,
        on_open=None,
        on_complete=None,
        on_error=None,
        on_close=None,
        on_data=None,
    ):
        if self.session is not None:
            self.session.abort()
        self.session = TTSStreamSession()
        return self.session.open(
            on_open=on_open,
            on_complete=on_complete,
            on_error=on_error,
            on_close=on_close,
            on_data=on_data,
        )

    def tts_stream_close(self):
        if self.session is None:
            return False
        return self.session.complete()

    def tts_stream_speech_synthesis(self, text_chunk):
        if self.session is None:
            return False
        return self.session.send(text_chunk)

    def is_ready(self):
        return self.session is not None and self.session.is_usable()

    def last_error(self):
        if self.session is None:
            return None
        return self.session.last_error()

    def session_id(self):
        if self.session is None:
            return "none"
        return self.session.session_id
