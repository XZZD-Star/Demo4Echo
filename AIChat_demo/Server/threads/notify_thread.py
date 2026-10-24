"""主动播报调度线程。

职责：在客户端在线且空闲时，从 notify_outbox 领取一条通知，
先下发 notify JSON，再推送 TTS 音频，最后补一个 tts end 消息。
"""

import json
import threading
import time

from config.settings import global_settings
from service_manager import ServiceManager
from services.notify_service import NotifyService
from tools import notify_store
from tools.logger import logger


class NotifyThread(threading.Thread):
    def __init__(self, service_manager: ServiceManager):
        super().__init__(daemon=True, name="NotifyThread")
        self.service_manager = service_manager
        self.notify_service = NotifyService(
            on_data=self._on_tts_data
        )

        self._buffer = bytearray()
        self._frame_bytes = (
            int(
                service_manager.audio_processor.frame_duration_ms
                * service_manager.audio_processor.sample_rate
                / 1000
            )
            * 2
        )
        self._last_play_at = 0.0
        self._hour_window_start = 0.0
        self._hour_count = 0

    # ---------- 音频缓冲 ----------

    def _on_tts_data(self, data):
        """把 TTS 音频按 40ms 帧对齐后推入播放队列。"""
        if not data:
            return

        self._buffer.extend(data)

        while len(self._buffer) >= self._frame_bytes:
            chunk = bytes(self._buffer[: self._frame_bytes])
            del self._buffer[: self._frame_bytes]
            self.service_manager.audio_queue.put(chunk)

    def _flush_buffer(self):
        """把不足一帧的尾部补静音后推出去，避免最后半帧卡住。"""
        if not self._buffer:
            return

        remainder = bytes(self._buffer)
        self._buffer.clear()

        padding = self._frame_bytes - len(remainder)
        if padding > 0:
            remainder += b"\x00" * padding

        self.service_manager.audio_queue.put(remainder)

    # ---------- 播放条件 ----------

    def _rate_limited(self):
        now = time.monotonic()
        if now - self._hour_window_start >= 3600:
            self._hour_window_start = now
            self._hour_count = 0
            return False
        return self._hour_count >= global_settings.NOTIFY_MAX_PER_HOUR

    def _can_play(self):
        if not global_settings.NOTIFY_ENABLED:
            return False

        manager = self.service_manager

        if not manager.has_client():
            return False

        # 只在客户端处于 idle 时播报，避免打断用户说话。
        if manager.client_state != "idle":
            return False

        if manager.conversation_busy.is_set():
            return False

        if notify_store.is_quiet_now():
            return False

        if self._rate_limited():
            return False

        if (
            time.monotonic() - self._last_play_at
            < global_settings.NOTIFY_MIN_GAP_SECONDS
        ):
            return False

        return True

    # ---------- 播放流程 ----------

    def _wait_audio_idle(self, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.service_manager.audio_queue.empty():
                return True
            time.sleep(0.05)
        return False

    def _play(self, row):
        manager = self.service_manager
        notify_id = row["id"]

        if not self._wait_audio_idle():
            logger.warning("播放队列未清空，延后播报通知 #%s。", notify_id)
            notify_store.mark_failed(notify_id, "audio queue busy")
            return False

        self._buffer.clear()
        manager.conversation_busy.set()
        manager.notify_playing = True

        try:
            payload = {
                "type": "notify",
                "id": notify_id,
                "kind": row["kind"],
                "text": row["text"],
            }
            manager.ws_send_queue.put(
                json.dumps(payload, ensure_ascii=False)
            )
            logger.info(
                "开始播报通知 #%s（%s）：%s",
                notify_id,
                row["kind"],
                row["text"],
            )

            ok = self.notify_service.speak(
                row["text"],
                timeout_seconds=global_settings.NOTIFY_TTS_TIMEOUT,
            )

            self._flush_buffer()

            if not ok:
                notify_store.mark_failed(notify_id, "tts failed")
                return False

            # 等音频队列排空后再发结束标记，保证顺序。
            self._wait_audio_idle(timeout=10.0)
            manager.ws_send_queue.put(
                json.dumps({"type": "tts", "state": "end"})
            )

            notify_store.mark_sent(notify_id)
            self._hour_count += 1
            self._last_play_at = time.monotonic()
            logger.info("通知 #%s 播报完成。", notify_id)
            return True

        except Exception as exc:
            logger.exception("播报通知 #%s 异常：%s", notify_id, exc)
            notify_store.mark_failed(notify_id, exc)
            return False

        finally:
            manager.notify_playing = False
            manager.conversation_busy.clear()

    # ---------- 主循环 ----------

    def run(self):
        logger.info(
            "主动播报线程已启动（启用=%s，轮询=%s 秒）。",
            global_settings.NOTIFY_ENABLED,
            global_settings.NOTIFY_POLL_SECONDS,
        )

        try:
            notify_store.init_notify_tables()
            notify_store.recover_zombie()
        except Exception as exc:
            logger.error("初始化通知表失败：%s", exc)

        while not self.service_manager.stop_event.is_set():
            try:
                if not self._can_play():
                    time.sleep(global_settings.NOTIFY_POLL_SECONDS)
                    continue

                row = notify_store.claim_one()
                if row is None:
                    time.sleep(global_settings.NOTIFY_POLL_SECONDS)
                    continue

                self._play(row)

            except Exception as exc:
                logger.exception("播报线程异常：%s", exc)
                time.sleep(global_settings.NOTIFY_POLL_SECONDS)

        logger.info("主动播报线程已退出。")
