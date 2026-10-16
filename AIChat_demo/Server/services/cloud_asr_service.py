import os
import json
import time
import uuid
import queue
import base64
import threading

import websocket

from tools.logger import logger


MODEL = "qwen3-asr-flash-realtime"
SAMPLE_RATE = 16000
PCM_BYTES_PER_SECOND = SAMPLE_RATE * 2  # PCM16、单声道
NO_SPEECH_TIMEOUT_SECONDS = 3.0


class CloudASRService:
    """
    百炼实时 ASR + 服务端 VAD。

    输入：16kHz、单声道、PCM16 原始音频字节。
    输出：通过 on_result 回调返回最终识别文本。
    无语音或连接失败时调用 on_no_speech。
    """

    def __init__(self, on_result, on_no_speech):
        self.on_result = on_result
        self.on_no_speech = on_no_speech

        self.api_key = os.environ.get("DASHSCOPE_API_KEY", "").strip()
        if not self.api_key:
            raise RuntimeError(
                "缺少 DASHSCOPE_API_KEY 环境变量，"
                "请先在服务端安全配置百炼 API Key。"
            )

        self.url = self._build_url()

        self._ws = None
        self._thread = None
        self._sender_thread = None

        self._audio_queue = queue.Queue(maxsize=256)
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._input_finished = threading.Event()

        self._send_lock = threading.Lock()
        self._terminal_lock = threading.Lock()

        self._terminal = False
        self._speech_started = False
        self._sent_audio_seconds = 0.0
        self._started_at = time.monotonic()

    @staticmethod
    def _build_url():
        """
        支持直接配置完整实时 API 地址，或配置北京 Workspace ID。
        不读取、不输出源码中的 API Key。
        """
        url = os.environ.get("DASHSCOPE_REALTIME_URL", "").strip()

        if url:
            if not url.startswith("wss://"):
                raise RuntimeError(
                    "DASHSCOPE_REALTIME_URL 必须使用 wss:// 地址。"
                )

            if "model=" not in url:
                url += (
                    "&" if "?" in url else "?"
                ) + f"model={MODEL}"

            return url

        workspace_id = os.environ.get(
            "DASHSCOPE_WORKSPACE_ID", ""
        ).strip()

        if not workspace_id:
            raise RuntimeError(
                "缺少 DASHSCOPE_WORKSPACE_ID 或 "
                "DASHSCOPE_REALTIME_URL 环境变量。"
            )

        # 允许填写 Workspace ID 或北京地域的 API Host。
        workspace_id = workspace_id.removeprefix("https://")
        workspace_id = workspace_id.removeprefix("wss://")
        workspace_id = workspace_id.rstrip("/")

        suffix = ".cn-beijing.maas.aliyuncs.com"

        if workspace_id.endswith(suffix):
            host = workspace_id
        elif "." not in workspace_id:
            host = workspace_id + suffix
        else:
            raise RuntimeError(
                "Workspace 地址格式不正确，请检查地域和 API Host。"
            )

        return (
            f"wss://{host}/api-ws/v1/realtime"
            f"?model={MODEL}"
        )

    @staticmethod
    def _event(event_type, **kwargs):
        return {
            "event_id": "event_" + uuid.uuid4().hex,
            "type": event_type,
            **kwargs,
        }

    def _send_json(self, event):
        if self._ws is None:
            return False

        try:
            with self._send_lock:
                self._ws.send(json.dumps(event, ensure_ascii=False))
            return True
        except Exception as exc:
            logger.error("云端 ASR 发送事件失败: %s", exc)
            return False

    def start(self):
        """启动独立 WebSocket 接收线程和音频发送线程。"""
        if self._thread and self._thread.is_alive():
            return

        self._ws = websocket.WebSocketApp(
            self.url,
            header=[
                f"Authorization: Bearer {self.api_key}",
                "OpenAI-Beta: realtime=v1",
            ],
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )

        self._sender_thread = threading.Thread(
            target=self._sender_loop,
            name="CloudASRAudioSender",
            daemon=True,
        )
        self._thread = threading.Thread(
            target=self._run,
            name="CloudASRWebSocket",
            daemon=True,
        )

        self._sender_thread.start()
        self._thread.start()

        logger.info("云端 ASR 会话启动请求已提交。")

    def send_audio(self, pcm_data):
        """
        非阻塞地提交一个解码后的 PCM 音频块。
        允许客户端 40ms 音频帧进入队列，由发送线程串行发送。
        """
        if self._stop.is_set() or self._input_finished.is_set():
            return

        if not isinstance(pcm_data, (bytes, bytearray)):
            logger.warning("云端 ASR 收到非 bytes 音频数据，忽略。")
            return

        pcm_data = bytes(pcm_data)

        if not pcm_data:
            return

        # PCM16 每个采样点占两个字节。
        if len(pcm_data) % 2:
            logger.warning("PCM 数据长度为奇数，忽略不完整字节。")
            pcm_data = pcm_data[:-1]

        if not pcm_data:
            return

        try:
            self._audio_queue.put_nowait(pcm_data)
        except queue.Full:
            self._fail("云端 ASR 音频队列已满，可能发生网络拥塞。")

    def _on_open(self, ws):
        logger.info("云端 ASR WebSocket 已连接。")

        session = {
            "modalities": ["text"],
            "input_audio_format": "pcm",
            "sample_rate": SAMPLE_RATE,
            "turn_detection": {
                "type": "server_vad",
                "threshold": 0.2,
                "silence_duration_ms": 400,
            },
        }

        if not self._send_json(
            self._event("session.update", session=session)
        ):
            self._fail("无法发送云端 ASR 会话配置。")

    def _on_message(self, ws, message):
        try:
            event = json.loads(message)
        except (TypeError, json.JSONDecodeError):
            logger.warning("云端 ASR 返回了无法解析的事件。")
            return

        event_type = event.get("type", "")

        if event_type == "session.created":
            logger.info("云端 ASR 会话已创建。")

        elif event_type == "session.updated":
            logger.info("云端 ASR 会话配置成功，开始发送音频。")
            self._ready.set()

        elif event_type == "input_audio_buffer.speech_started":
            self._speech_started = True
            logger.info("云端 VAD 检测到语音开始。")

        elif event_type == "input_audio_buffer.speech_stopped":
            logger.info("云端 VAD 检测到语音结束。")

        elif event_type == (
            "conversation.item.input_audio_transcription.text"
        ):
            # 中间识别结果只记录，不触发 Qwen，避免重复回复。
            partial = event.get("text", "")
            if partial:
                logger.debug("云端 ASR 中间结果: %s", partial)

        elif event_type == (
            "conversation.item.input_audio_transcription.completed"
        ):
            transcript = (event.get("transcript") or "").strip()

            if transcript:
                self._finish_with_result(transcript)
            else:
                self._fail("云端 ASR 未返回有效识别文本。")

        elif event_type == "session.finished":
            logger.info("云端 ASR 会话正常结束。")
            self._stop.set()
            ws.close()

        elif event_type == "error":
            # 不输出鉴权请求头或 API Key。
            logger.error(
                "云端 ASR 错误事件: %s",
                json.dumps(event, ensure_ascii=False),
            )
            self._fail("云端 ASR 返回错误事件。")

    def _finish_with_result(self, transcript):
        with self._terminal_lock:
            if self._terminal:
                return
            self._terminal = True

        # 本轮识别已经结束，不再接受新的音频。
        self._input_finished.set()

        # 正常结束本轮云端会话，保留接收线程以读取结束事件。
        self._send_json(self._event("session.finish"))

        logger.info("云端 ASR 最终识别结果: %s", transcript)

        try:
            self.on_result(transcript)
        except Exception:
            logger.exception("处理云端 ASR 最终结果时发生异常。")

    def _notify_no_speech(self, reason):
        with self._terminal_lock:
            if self._terminal:
                return
            self._terminal = True

        logger.warning("云端 ASR 会话未完成: %s", reason)

        try:
            self.on_no_speech(reason)
        except Exception:
            logger.exception("处理云端 ASR 异常回调失败。")

    def _fail(self, reason):
        self._notify_no_speech(reason)
        self._input_finished.set()
        self._stop.set()

        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass

    def _sender_loop(self):
        while not self._stop.is_set():
            if self._input_finished.is_set():
                break

            if not self._ready.wait(timeout=0.1):
                if (
                    time.monotonic() - self._started_at > 15
                    and not self._stop.is_set()
                ):
                    self._fail("等待云端 ASR 会话配置超时。")
                    break
                continue

            try:
                pcm_data = self._audio_queue.get(timeout=0.1)
            except queue.Empty:
                continue

            if self._stop.is_set() or self._input_finished.is_set():
                break

            event = self._event(
                "input_audio_buffer.append",
                audio=base64.b64encode(pcm_data).decode("ascii"),
            )

            if not self._send_json(event):
                self._fail("发送云端 ASR 音频失败。")
                break

            self._sent_audio_seconds += (
                len(pcm_data) / PCM_BYTES_PER_SECOND
            )

            # 没有检测到任何语音时，避免会话永久等待。
            if (
                not self._speech_started
                and self._sent_audio_seconds >= NO_SPEECH_TIMEOUT_SECONDS
            ):
                self._fail("超过无语音等待时间。")
                break

    def _on_error(self, ws, error):
        logger.error("云端 ASR WebSocket 错误: %s", error)

        if not self._stop.is_set() and not self._terminal:
            self._notify_no_speech("WebSocket 连接或收发异常。")

        self._stop.set()
        self._ready.set()

    def _on_close(self, ws, status_code, message):
        logger.info(
            "云端 ASR WebSocket 已关闭: code=%s, reason=%s",
            status_code,
            message,
        )
        self._stop.set()
        self._ready.set()

    def _run(self):
        try:
            self._ws.run_forever(
                ping_interval=20,
                ping_timeout=10,
            )
        except Exception:
            logger.exception("运行云端 ASR WebSocket 时发生异常。")
        finally:
            self._stop.set()
            self._ready.set()

    def stop(self):
        """主动停止当前会话，不触发新的识别结果回调。"""
        self._stop.set()
        self._ready.set()

        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass

        current_thread = threading.current_thread()

        for thread in (self._sender_thread, self._thread):
            if (
                thread is not None
                and thread.is_alive()
                and thread is not current_thread
            ):
                thread.join(timeout=1.0)
