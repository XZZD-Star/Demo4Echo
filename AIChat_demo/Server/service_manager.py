from config.settings import global_settings
import assistant_core
from memory_observer import MemoryObserver
from services.chat_service import ChatService
from services.tts_service import TTSService
from tools.registry import global_registry
from services.intent_service import IntentService
from services import assistant_actions
from tools.audio_processor import AudioProcessor
from threads.task_manager import TaskManager
from tools.logger import logger
from tools import flow_log
import queue
import threading
import json
import os
import time

TTS_SENTENCE_ENDINGS = frozenset("。！？!?；;\n")
TTS_SOFT_ENDINGS = frozenset("，,、：:")
TTS_CHUNK_MAX_CHARS = 24


def split_tts_chunks(buffer, final=False):
    """Split streamed text into TTS-friendly sentence-sized chunks."""
    chunks = []
    rest = buffer or ""
    while rest:
        window = rest[:TTS_CHUNK_MAX_CHARS]
        boundary = next(
            (
                index + 1
                for index, char in enumerate(window)
                if char in TTS_SENTENCE_ENDINGS
            ),
            None,
        )
        if boundary is None and len(rest) >= TTS_CHUNK_MAX_CHARS:
            boundary = next(
                (
                    index + 1
                    for index in range(len(window) - 1, -1, -1)
                    if window[index] in TTS_SOFT_ENDINGS
                ),
                TTS_CHUNK_MAX_CHARS,
            )
        if boundary is None:
            break

        chunk = rest[:boundary].strip()
        if chunk:
            chunks.append(chunk)
        rest = rest[boundary:]

    if final:
        chunk = rest.strip()
        if chunk:
            chunks.append(chunk)
        rest = ""
    return chunks, rest


class ServiceManager:
    def __init__(self):
        # 初始化服务
        self.audio_processor = AudioProcessor()

        # 默认为本地模式；只有明确设置 cloud 才启用云端 ASR。
        self.asr_backend = os.environ.get(
            "ECHOMATE_ASR_BACKEND", "local"
        ).strip().lower()

        if self.asr_backend not in ("local", "cloud"):
            raise ValueError(
                "ECHOMATE_ASR_BACKEND 只能是 local 或 cloud"
            )

        self.cloud_asr_service = None
        self._asr_ready = threading.Event()

        if self.asr_backend == "cloud":
            # 云端模式不初始化本地 VAD 和 ASR 模型。
            self.vad_service = None
            self.asr_service = None
            logger.info("ASR 后端：云端实时 ASR + 服务端 VAD")
        else:
            # 延迟导入，保证云端模式不依赖本地模型初始化。
            from services.vad_service import VADService
            from services.asr_service import ASRService

            self.vad_service = VADService()
            self.asr_service = ASRService()
            logger.info("ASR 后端：本地 VAD + 本地 ASR")

        self.intent_service = IntentService(global_registry)
        self.chat_service = ChatService()
        self.memory_observer = MemoryObserver(
            self.intent_service.client,
            global_settings.MEMORY_MODEL,
            delay_seconds=3.0,
            can_process=self._memory_worker_can_run,
        )
        self.tts_service = TTSService()
        self.is_vad = False  # 防止VAD发生后还语音加入
        self._active_chat_trace_id = None
        self._active_voice_trace_id = None
        self._voice_trace_lock = threading.Lock()
        self._tts_first_data_seen = False
        self._tts_lock = threading.Lock()
        self._tts_epoch = 0
        self._tts_active = False
        self._tts_active_epoch = None

        self.tts_text_queue = queue.Queue() # 用于存放 TTS 生成的文本
        self.audio_queue = queue.Queue()    # 用于存放生成的音频数据
        self.ws_send_queue = queue.Queue()  # 用于存储ws需要发送的数据

        self.stop_event = threading.Event() # 用于控制线程停止
        self._fatal_lock = threading.Lock()
        self._fatal_reason = None

        self.task_manager = TaskManager()   # 短生命周期的任务管理器

        # ===== 主动播报相关的运行期状态 =====
        self.client_ws = None               # 当前客户端 WebSocket 连接
        self.client_state = "unknown"       # 客户端上报的状态
        self.conversation_busy = threading.Event()  # 对话/识别占用中
        self.notify_playing = False         # 正在播报通知
        self.chat_turn_id = 0
        self.chat_turn_lock = threading.Lock()
        self._conversation_session_id = None
        self._conversation_session_generation = 0
        self._conversation_session_lock = threading.Lock()
        self.owner_id = "ou_1359f562d0a768f91232760499bc1cc0"  # Shared Feishu owner
        self.feishu_chat_id = global_settings.FEISHU_CHAT_ID

        def continue_chat():
            return "继续聊天..."

        def handle_exit_intent():
            return "再见！"

        # 默认的一些意图注册到系统
        global_registry.register_function("continue_chat", "继续聊天意图", {}, continue_chat)
        global_registry.register_function("exit_chat", "结束对话意图", {}, handle_exit_intent)

    def _memory_worker_can_run(self):
        """Run memory extraction only when foreground voice work is idle."""
        return (
            not self.conversation_busy.is_set()
            and self.client_state in {"idle", "unknown"}
        )

    def has_client(self):
        """是否有客户端在线。"""
        return self.client_ws is not None

    def fail_fast(self, reason):
        """Request a non-zero service exit so systemd can restart it."""
        with self._fatal_lock:
            if self._fatal_reason is None:
                self._fatal_reason = str(reason)
        self.stop_event.set()

    def fatal_reason(self):
        with self._fatal_lock:
            return self._fatal_reason

    def set_client_state(self, state):
        """记录客户端上报的状态，用于判断能否主动播报。"""
        self.client_state = str(state or "unknown")

    def begin_voice_trace(self, trace_id=None):
        """Return one trace id for the full wake-to-idle interaction."""
        with self._voice_trace_lock:
            if self._active_voice_trace_id is None:
                candidate = (
                    str(trace_id).strip()
                    if trace_id is not None
                    else ""
                )
                if (
                    not candidate.startswith("voice-")
                    or len(candidate) > 80
                ):
                    candidate = f"voice-{int(time.time() * 1000)}"
                self._active_voice_trace_id = candidate
            return self._active_voice_trace_id

    def active_voice_trace_id(self):
        with self._voice_trace_lock:
            return self._active_voice_trace_id

    def clear_voice_trace(self, trace_id=None):
        with self._voice_trace_lock:
            if (
                trace_id is None
                or self._active_voice_trace_id == trace_id
            ):
                self._active_voice_trace_id = None

    def _notify_asr_ready(self):
        if self._asr_ready.is_set():
            return
        self._asr_ready.set()
        trace_id = self.active_voice_trace_id()
        self.ws_send_queue.put(json.dumps({
            "type": "asr",
            "state": "ready",
            "trace_id": trace_id,
            "turn_id": trace_id,
        }))
        flow_log.emit(
            "asr", "ready", "ok",
            trace_id=trace_id,
            message="session ready",
        )

    def _invalidate_tts(self):
        with self._tts_lock:
            self._tts_epoch += 1
            self._tts_active = False
            self._tts_active_epoch = None
            return self._tts_epoch

    def prepare_tts(self):
        """Open a TTS stream whose callbacks belong to one generation."""
        epoch = self._invalidate_tts()
        return self._set_tts_callbacks(epoch)

    def _set_tts_callbacks(self, epoch):
        opened = self.tts_service.tts_set(
            on_data=lambda data: self._tts_on_data(data, epoch),
            on_complete=lambda: self._tts_on_complete(epoch),
            on_error=lambda message: self._tts_on_error(message, epoch),
        )
        flow_log.emit(
            "tts",
            "session_open",
            "ok" if opened else "error",
            trace_id=self._active_chat_trace_id,
            message=f"session={self.tts_service.session_id()}",
        )
        return opened

    def _ensure_tts_ready(self, epoch):
        if self.tts_service.is_ready():
            return epoch
        epoch = self._invalidate_tts()
        with self._tts_lock:
            self._tts_active = True
            self._tts_active_epoch = epoch
        if not self._set_tts_callbacks(epoch):
            raise RuntimeError(
                self.tts_service.last_error()
                or "TTS 预打开失败"
            )
        return epoch

    def _tts_is_active(self, epoch):
        with self._tts_lock:
            return (
                self._tts_active
                and self._tts_active_epoch == epoch
            )

    def invalidate_chat_turn(self):
        with self.chat_turn_lock:
            self.chat_turn_id += 1
            return self.chat_turn_id

    def is_chat_turn_active(self, turn_id):
        with self.chat_turn_lock:
            return self.chat_turn_id == turn_id

    def drain_send_queues(self):
        """清空待发送队列，避免上一条连接的残留音频推给新连接。"""
        for q in (self.ws_send_queue, self.audio_queue):
            try:
                while True:
                    q.get_nowait()
            except queue.Empty:
                pass

    def ensure_conversation_session(self):
        """Return the working-memory session bound to this DeskBot connection."""
        with self._conversation_session_lock:
            if not self._conversation_session_id:
                self._conversation_session_generation += 1
                self._conversation_session_id = (
                    f"voice-session-{int(time.time() * 1000)}-"
                    f"{self._conversation_session_generation}"
                )
            return self._conversation_session_id

    def end_conversation(self):
        """Clear only working memory when the DeskBot connection ends."""
        with self._conversation_session_lock:
            self._conversation_session_id = None
        self.chat_service.chat_clear()

    def reset_services(self, clear_chat=False):
        """
        重置运行时服务；普通 Idle 默认保留工作记忆。
        """
        self.is_vad = False

        if self.asr_backend == "cloud":
            if self.cloud_asr_service is not None:
                self.cloud_asr_service.stop()
                self.cloud_asr_service = None
        else:
            self.vad_service.reset()
            self.asr_service.reset()
        self._asr_ready.clear()

        if clear_chat:
            self.end_conversation()

        # 正在播报通知时不要关闭 TTS 流，避免打断。
        if self.notify_playing:
            return

        self._invalidate_tts()

        # 主动 stop 不会触发 ASR 回调，必须在这里显式释放占用标记，
        # 否则主动播报会一直被 conversation_busy 挡住。
        self.conversation_busy.clear()

        try:
            self.tts_service.tts_close()
        except Exception as e:
            pass

    def start_asr_session(self):
        """根据当前后端启动一轮新的语音识别会话。"""
        self.is_vad = False

        if self.asr_backend == "local":
            if not self._asr_ready.is_set():
                self.vad_service.reset()
                self.asr_service.reset()
                self._notify_asr_ready()
            return

        # Wake prewarm and the following listening state may both request a
        # session. Keep the in-flight session instead of reconnecting.
        if self._asr_ready.is_set() or self.cloud_asr_service is not None:
            return

        self.conversation_busy.set()

        try:
            from services.cloud_asr_service import CloudASRService

            service = CloudASRService(
                on_result=self._on_cloud_asr_result,
                on_no_speech=self._on_cloud_asr_no_speech,
                on_ready=self._notify_asr_ready,
                on_speech_started=self._on_cloud_asr_speech_started,
            )
            self.cloud_asr_service = service
            service.start()
            logger.info("已启动新一轮云端 ASR 会话。")

        except Exception:
            logger.exception("启动云端 ASR 会话失败。")
            self.cloud_asr_service = None
            self.conversation_busy.clear()
            self._on_cloud_asr_no_speech("云端 ASR 启动失败。")

    def _on_cloud_asr_result(self, text):
        """只在收到最终识别文本后触发一次对话。"""
        text = (text or "").strip()
        if not text:
            self._on_cloud_asr_no_speech("最终识别文本为空。")
            return

        self.is_vad = True

        result = {
            "type": "asr",
            "text": text,
            "turn_id": self.active_voice_trace_id(),
        }
        self.ws_send_queue.put(json.dumps(result, ensure_ascii=False))
        self._asr_ready.clear()
        logger.info("云端 ASR 最终识别结果：%s", text)
        trace_id = (
            self.active_voice_trace_id() or self.begin_voice_trace()
        )
        flow_log.emit("asr", "final", "ok",
                      trace_id=trace_id,
                      message=f"chars={len(text)}")

        self.task_manager.submit_task(self.chat_start_task, text)

    def _on_cloud_asr_no_speech(self, reason):
        """通知客户端本轮识别未能得到有效文本。"""
        self.is_vad = True
        self._asr_ready.clear()
        self.conversation_busy.clear()

        result = {
            "type": "vad",
            "state": "no_speech",
            "reason": str(reason or "no_speech")[:200],
            "turn_id": self.active_voice_trace_id(),
        }
        self.ws_send_queue.put(json.dumps(result, ensure_ascii=False))
        trace_id = self.active_voice_trace_id()
        flow_log.emit(
            "asr", "no_speech", "warn",
            trace_id=trace_id,
            message=str(reason or "no_speech")[:200],
        )
        logger.warning("云端 ASR 未获得有效结果：%s", reason)

    def _on_cloud_asr_speech_started(self):
        trace_id = self.active_voice_trace_id()
        self.ws_send_queue.put(json.dumps({
            "type": "asr",
            "state": "speech_started",
            "turn_id": trace_id,
        }))
        flow_log.emit(
            "asr", "speech_started", "info",
            trace_id=trace_id,
        )

    def _tts_on_data(self, data, epoch):
        """
        TTS 生成回调函数
        :param data: 生成的音频数据
        """
        if not self._tts_is_active(epoch):
            return
        # 将生成的音频数据放入语音队列
        if not self._tts_first_data_seen:
            self._tts_first_data_seen = True
            flow_log.emit(
                "tts", "first_audio", "ok",
                trace_id=self._active_chat_trace_id,
                message=f"bytes={len(data)}",
            )
        self.audio_queue.put(data)
        # logger.info(f"Received TTS data: {len(data)} bytes")

    def _tts_on_complete(self, epoch):
        with self._tts_lock:
            if not (
                self._tts_active
                and self._tts_active_epoch == epoch
            ):
                logger.warning("忽略非活动 TTS 完成回调。")
                return
            self._tts_active = False
        msg = {
            "type": "tts",
            "state": "end",
            "turn_id": self._active_chat_trace_id,
        }
        self.ws_send_queue.put(json.dumps(msg))
        flow_log.emit("tts", "complete", "ok",
                      trace_id=self._active_chat_trace_id)

    def _tts_on_error(self, message, epoch):
        with self._tts_lock:
            if not (
                self._tts_active
                and self._tts_active_epoch == epoch
            ):
                logger.warning("忽略非活动 TTS 错误回调。")
                return
            self._tts_active = False
        reason = str(message or "tts failed")[:200]
        logger.error("对话 TTS 失败：%s", reason)
        flow_log.emit("tts", "error", "error",
                      trace_id=self._active_chat_trace_id,
                      message=reason)
        self.ws_send_queue.put(json.dumps({
            "type": "tts",
            "state": "error",
            "reason": "synthesis_failed",
            "turn_id": self._active_chat_trace_id,
        }))

    def _speak_reply(self, text, turn_id):
        """Send a final reply through the existing TTS stream."""
        if not self.is_chat_turn_active(turn_id):
            logger.warning("客户端已结束本轮，丢弃迟到回复。")
            return False
        text = (text or "").strip()
        if not text:
            text = "抱歉，这次没有生成有效回复，请再说一次。"
        try:
            flow_log.emit("llm", "reply_ready", "ok",
                          trace_id=self._active_chat_trace_id,
                          message=f"chars={len(text)}")
            with self._tts_lock:
                epoch = self._tts_epoch
                self._tts_active = True
                self._tts_active_epoch = epoch
            epoch = self._ensure_tts_ready(epoch)
            if not self.tts_service.tts_speech_stream(text):
                raise RuntimeError(
                    self.tts_service.last_error()
                    or "TTS 合成未启动"
                )
            if not self.tts_service.tts_close():
                raise RuntimeError(
                    self.tts_service.last_error()
                    or "TTS 关闭失败"
                )
            return True
        except Exception as exc:
            logger.exception("对话回复 TTS 失败")
            with self._tts_lock:
                error_epoch = self._tts_active_epoch or epoch
            self._tts_on_error(exc, error_epoch)
            return False

    def _stream_reply_to_tts(self, answer_stream, turn_id):
        """Stream ordinary chat text into TTS as soon as sentences form."""
        if not self.is_chat_turn_active(turn_id):
            logger.warning("客户端已结束本轮，丢弃迟到回复。")
            return None

        with self._tts_lock:
            epoch = self._tts_epoch
            self._tts_active = True
            self._tts_active_epoch = epoch

        try:
            epoch = self._ensure_tts_ready(epoch)
            collected = []
            pending = ""
            first_token_seen = False
            tts_text_seen = False

            for chunk in answer_stream:
                if not self.is_chat_turn_active(turn_id):
                    logger.warning("流式回复期间本轮已结束，停止 TTS。")
                    return None
                if chunk == -1:
                    logger.warning("LLM 流式生成返回错误。")
                    continue
                if not isinstance(chunk, str) or not chunk:
                    continue

                if not first_token_seen:
                    first_token_seen = True
                    flow_log.emit(
                        "llm", "first_token", "ok",
                        trace_id=self._active_chat_trace_id,
                    )
                collected.append(chunk)
                pending += chunk
                pieces, pending = split_tts_chunks(pending)
                for piece in pieces:
                    if not self.tts_service.tts_speech_stream(piece):
                        raise RuntimeError(
                            self.tts_service.last_error()
                            or "TTS 分段合成失败"
                        )
                    if not tts_text_seen:
                        tts_text_seen = True
                        flow_log.emit(
                            "tts", "text_ready", "ok",
                            trace_id=self._active_chat_trace_id,
                            message=f"chars={len(piece)}",
                        )

            pieces, _ = split_tts_chunks(pending, final=True)
            for piece in pieces:
                if not self.tts_service.tts_speech_stream(piece):
                    raise RuntimeError(
                        self.tts_service.last_error()
                        or "TTS 分段合成失败"
                    )
                if not tts_text_seen:
                    tts_text_seen = True
                    flow_log.emit(
                        "tts", "text_ready", "ok",
                        trace_id=self._active_chat_trace_id,
                        message=f"chars={len(piece)}",
                    )

            reply = "".join(collected).strip()
            if not reply:
                reply = "抱歉，这次没有生成有效回复，请再说一次。"
                if not self.tts_service.tts_speech_stream(reply):
                    raise RuntimeError(
                        self.tts_service.last_error()
                        or "TTS 兜底回复失败"
                    )

            flow_log.emit(
                "llm", "reply_ready", "ok",
                trace_id=self._active_chat_trace_id,
                message=f"chars={len(reply)}",
            )
            logger.info("[回复]: %s", reply)
            if not self.tts_service.tts_close():
                raise RuntimeError(
                    self.tts_service.last_error()
                    or "TTS 关闭失败"
                )
            return reply
        except Exception as exc:
            logger.exception("流式回复 TTS 失败")
            with self._tts_lock:
                error_epoch = self._tts_active_epoch or epoch
            self._tts_on_error(exc, error_epoch)
            return None

    def _finish_chat_reply(self, turn_id):
        """Mark the end of this chat turn after TTS completion."""
        if not self.is_chat_turn_active(turn_id):
            return
        self.ws_send_queue.put(
            json.dumps({
                "type": "chat",
                "dialogue": "end",
                "turn_id": self._active_chat_trace_id,
            })
        )
        flow_log.emit("llm", "turn_end", "ok",
                      trace_id=self._active_chat_trace_id)

    def _queue_memory_observation(self, user_text, reply, tool_runs, turn_id):
        observer = getattr(self, "memory_observer", None)
        if observer is None:
            return
        if any(run.get("tool") == "propose_memory" for run in tool_runs):
            return
        observer.enqueue_turn(
            owner_id=self.owner_id,
            session_id=self.ensure_conversation_session(),
            turn_id=self._active_chat_trace_id or str(turn_id),
            source="voice",
            user_text=user_text,
            tool_runs=tool_runs,
        )

    def chat_start_task(self, text):
        """
        处理识别到的文本，进行对话
        :param self: ServiceManager 实例
        :param text: 文本
        """
        # 对话期间标记占用，主动播报会让路。
        self.conversation_busy.set()
        turn_id = self.invalidate_chat_turn()
        self._active_chat_trace_id = (
            self.active_voice_trace_id() or self.begin_voice_trace()
        )
        session_id = self.ensure_conversation_session()
        self._tts_first_data_seen = False
        started_at = time.monotonic()
        flow_log.emit("llm", "start", "info",
                      trace_id=self._active_chat_trace_id)
        self.ws_send_queue.put(
            json.dumps({
                "type": "chat",
                "dialogue": "processing",
                "turn_id": self._active_chat_trace_id,
            })
        )
        try:
            # 1.进行意图识别。语义理解交给模型，不再用关键词预判待办。
            function_calls = self.intent_service.detect_intent(text)
            if not self.is_chat_turn_active(turn_id):
                logger.warning("意图返回时本轮已结束，丢弃结果。")
                return
            history_list = []
            tool_runs = []
            todo_result = None
            todo_error = None
            # 2.执行函数调用（如果有）
            for function_call in function_calls:
                if "function_call" not in function_call:
                    continue
                call = function_call["function_call"]
                if "name" not in call:
                    continue
                logger.info(f"[准备调用] {function_call}")
                # 执行函数调用
                if call["name"] == "continue_chat":
                    # 继续聊天意图
                    continue
                if call["name"] == "exit_chat":
                    continue
                # 共享工具由服务端执行；结果必须反馈给聊天模型。
                name = call["name"]
                arguments = call.get("arguments", {})
                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except json.JSONDecodeError:
                        arguments = {}
                result = assistant_actions.execute_tool_call(
                    name,
                    json.dumps(arguments, ensure_ascii=False),
                    owner_id=self.owner_id,
                    source="deskbot",
                    chat_id=self.feishu_chat_id,
                )
                tool_runs.append(
                    assistant_core.normalize_tool_run(name, result)
                )
                if name == "add_todo":
                    todo_result = result
                    if not result.get("ok"):
                        todo_error = result.get("error", "unknown")
                history_list.append({
                    "role": "user",
                    "content": f"工具 {name} 执行结果：{result}",
                })
            if todo_error:
                reply = {
                    "past_due": "这个时间已经过去了，我没有创建待办。请给我一个之后的时间。",
                    "bad_due": "我没有识别出有效时间，待办没有创建。",
                    "empty": "待办内容不能为空。",
                    "too_long": "待办内容太长了。",
                }.get(todo_error, "待办没有创建成功，请换个说法再试一次。")
                self._speak_reply(reply, turn_id)
                self._finish_chat_reply(turn_id)
                return
            if todo_result and todo_result.get("ok"):
                reply = (
                    f"好的，已添加待办：{todo_result['title']}，"
                    f"{todo_result.get('due_speech') or todo_result['due_text']} 提醒你。"
                )
                self._speak_reply(reply, turn_id)
                self._finish_chat_reply(turn_id)
                return
            # 3.调用聊天服务生成文字
            answers = self.chat_service.generate_chat_response(
                text,
                history=history_list,
                is_stream=True,
                owner_id=self.owner_id,
                session_id=session_id,
            )
            if not self.is_chat_turn_active(turn_id):
                logger.warning("聊天返回时本轮已结束，丢弃结果。")
                return
            if answers == -1:
                logger.error("LLM 生成失败")
                self._speak_reply(
                    "抱歉，我暂时无法处理这个请求，请稍后再试。",
                    turn_id,
                )
                self._finish_chat_reply(turn_id)
                return
            if isinstance(answers, list):
                # Tool-only response: confirm the resulting shared action.
                ok_items = [x for x in answers if x.get("ok")]
                if todo_result and todo_result.get("ok"):
                    reply = (
                        f"好的，已添加待办：{todo_result['title']}，"
                        f"{todo_result.get('due_speech') or todo_result['due_text']} 提醒你。"
                    )
                elif ok_items:
                    reply = "已经处理好了。"
                else:
                    reply = "这件事没有处理成功，请再说一次。"
                self._speak_reply(reply, turn_id)
                self._finish_chat_reply(turn_id)
                self._queue_memory_observation(
                    text, reply, tool_runs, turn_id
                )
                return

            final_reply = None
            if history_list:
                final_reply = "".join(
                    chunk for chunk in answers
                    if isinstance(chunk, str)
                ).strip()
                logger.info("[回复]: %s", final_reply)
                self._speak_reply(final_reply, turn_id)
            else:
                if isinstance(answers, str):
                    answers = iter([answers])
                final_reply = self._stream_reply_to_tts(answers, turn_id)
            self._finish_chat_reply(turn_id)
            self._queue_memory_observation(
                text, final_reply, tool_runs, turn_id
            )
        finally:
            flow_log.emit(
                "llm", "turn_complete", "ok",
                trace_id=self._active_chat_trace_id,
                duration_ms=int((time.monotonic() - started_at) * 1000),
            )
            self.conversation_busy.clear()
