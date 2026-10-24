"""主动播报用的独立 TTS 通道。

刻意不复用 TTSService：
1. text_handler 在 state=listening 时会 tts_set() 重建 synthesizer；
2. reset_services() 会 tts_close()。
两者都会打断正在播放的通知，所以这里持有独立的 SpeechSynthesizer。
"""

import threading

import dashscope
from dashscope.audio.tts_v2 import (
    AudioFormat,
    ResultCallback,
    SpeechSynthesizer,
)

from config.settings import global_settings
from tools.logger import logger


class _NotifyCallback(ResultCallback):
    def __init__(self, on_data, on_complete, on_error):
        self._on_data = on_data
        self._on_complete = on_complete
        self._on_error = on_error

    def on_open(self):
        logger.info("播报 TTS 连接已建立。")

    def on_data(self, data: bytes) -> None:
        if data:
            self._on_data(data)

    def on_complete(self):
        self._on_complete()

    def on_error(self, message: str):
        self._on_error(message)

    def on_close(self):
        logger.info("播报 TTS 连接已关闭。")


class NotifyService:
    """把一段文字合成 PCM 并交给调用方，同步阻塞直到结束或超时。"""

    def __init__(self, on_data):
        self._on_data = on_data
        self._lock = threading.Lock()

    def speak(self, text, timeout_seconds=30.0):
        """合成并推送音频。成功返回 True，失败或超时返回 False。

        streaming_complete() 在回调模式下会阻塞到任务结束，
        因此这里直接给它传超时，而不是自己再等一个 Event。
        """
        text = (text or "").strip()
        if not text:
            return False

        if not self._lock.acquire(timeout=5):
            logger.warning("播报通道正忙，本次跳过。")
            return False

        result = {"ok": False, "error": None}

        def _done():
            result["ok"] = True

        def _fail(message):
            result["error"] = message

        synthesizer = None
        try:
            callback = _NotifyCallback(
                on_data=self._on_data,
                on_complete=_done,
                on_error=_fail,
            )
            synthesizer = SpeechSynthesizer(
                model=global_settings.COSYVOICE_MODEL,
                voice=global_settings.COSYVOICE_VOICE,
                format=AudioFormat.PCM_16000HZ_MONO_16BIT,
                callback=callback,
            )

            synthesizer.streaming_call(text)
            synthesizer.streaming_complete(
                complete_timeout_millis=int(timeout_seconds * 1000)
            )

        except TimeoutError:
            logger.warning("播报 TTS 超时（%.1f 秒）。", timeout_seconds)
            result["error"] = "tts timeout"
        except Exception as exc:
            logger.exception("播报 TTS 异常：%s", exc)
            result["error"] = str(exc)
        finally:
            if result["error"] and synthesizer is not None:
                try:
                    synthesizer.streaming_cancel()
                except Exception:
                    pass
            self._lock.release()

        if result["error"]:
            logger.warning("播报 TTS 失败：%s", result["error"])
            return False

        if not result["ok"]:
            logger.warning("播报 TTS 结束但未收到完成回调，按失败处理。")
            return False

        return True
