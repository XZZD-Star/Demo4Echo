from config.settings import global_settings
from services.chat_service import ChatService
from services.tts_service import TTSService
from tools.registry import global_registry
from services.intent_service import IntentService
from services import assistant_actions
from tools.audio_processor import AudioProcessor
from threads.task_manager import TaskManager
from tools.logger import logger
import queue
import threading
import json
import os

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
        self.tts_service = TTSService()
        self.is_vad = False  # 防止VAD发生后还语音加入

        self.tts_text_queue = queue.Queue() # 用于存放 TTS 生成的文本
        self.audio_queue = queue.Queue()    # 用于存放生成的音频数据
        self.ws_send_queue = queue.Queue()  # 用于存储ws需要发送的数据

        self.stop_event = threading.Event() # 用于控制线程停止

        self.task_manager = TaskManager()   # 短生命周期的任务管理器

        # ===== 主动播报相关的运行期状态 =====
        self.client_ws = None               # 当前客户端 WebSocket 连接
        self.client_state = "unknown"       # 客户端上报的状态
        self.conversation_busy = threading.Event()  # 对话/识别占用中
        self.notify_playing = False         # 正在播报通知
        self.chat_turn_id = 0
        self.chat_turn_lock = threading.Lock()
        self.owner_id = "ou_1359f562d0a768f91232760499bc1cc0"  # Shared Feishu owner
        self.feishu_chat_id = global_settings.FEISHU_CHAT_ID

        def continue_chat():
            return "继续聊天..."

        def handle_exit_intent():
            return "再见！"

        # 默认的一些意图注册到系统
        global_registry.register_function("continue_chat", "继续聊天意图", {}, continue_chat)
        global_registry.register_function("exit_chat", "结束对话意图", {}, handle_exit_intent)

    def has_client(self):
        """是否有客户端在线。"""
        return self.client_ws is not None

    def set_client_state(self, state):
        """记录客户端上报的状态，用于判断能否主动播报。"""
        self.client_state = str(state or "unknown")

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

    def reset_services(self):
        """
        重置所有服务的状态
        """
        self.is_vad = False

        if self.asr_backend == "cloud":
            if self.cloud_asr_service is not None:
                self.cloud_asr_service.stop()
                self.cloud_asr_service = None
        else:
            self.vad_service.reset()
            self.asr_service.reset()

        self.chat_service.chat_clear()

        # 正在播报通知时不要关闭 TTS 流，避免打断。
        if self.notify_playing:
            return

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
            self.vad_service.reset()
            self.asr_service.reset()
            return

        # 防止重复创建云端会话。
        if self.cloud_asr_service is not None:
            self.cloud_asr_service.stop()
            self.cloud_asr_service = None

        self.conversation_busy.set()

        try:
            from services.cloud_asr_service import CloudASRService

            service = CloudASRService(
                on_result=self._on_cloud_asr_result,
                on_no_speech=self._on_cloud_asr_no_speech,
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
        }
        self.ws_send_queue.put(json.dumps(result, ensure_ascii=False))
        logger.info("云端 ASR 最终识别结果：%s", text)

        self.task_manager.submit_task(self.chat_start_task, text)

    def _on_cloud_asr_no_speech(self, reason):
        """通知客户端本轮识别未能得到有效文本。"""
        self.is_vad = True
        self.conversation_busy.clear()

        result = {
            "type": "vad",
            "state": "no_speech",
        }
        self.ws_send_queue.put(json.dumps(result, ensure_ascii=False))
        logger.warning("云端 ASR 未获得有效结果：%s", reason)

    def _tts_on_data(self, data):
        """
        TTS 生成回调函数
        :param data: 生成的音频数据
        """
        # 将生成的音频数据放入语音队列
        self.audio_queue.put(data)
        # logger.info(f"Received TTS data: {len(data)} bytes")

    def _tts_on_complete(self):
        msg = {
            "type": "tts",
            "state": "end",
        }
        self.ws_send_queue.put(json.dumps(msg))

    def _speak_reply(self, text, turn_id):
        """Send a final reply through the existing TTS stream."""
        if not self.is_chat_turn_active(turn_id):
            logger.warning("客户端已结束本轮，丢弃迟到回复。")
            return False
        text = (text or "").strip()
        if not text:
            text = "抱歉，这次没有生成有效回复，请再说一次。"
        try:
            self.tts_service.tts_speech_stream(text)
            self.tts_service.tts_close()
            return True
        except Exception:
            logger.exception("对话回复 TTS 失败")
            return False

    def _finish_chat_reply(self, turn_id):
        """Mark the end of this chat turn after TTS completion."""
        if not self.is_chat_turn_active(turn_id):
            return
        self.ws_send_queue.put(
            json.dumps({"type": "chat", "dialogue": "end"})
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
        self.ws_send_queue.put(
            json.dumps({"type": "chat", "dialogue": "processing"})
        )
        try:
            # 1.进行意图识别。语义理解交给模型，不再用关键词预判待办。
            function_calls = self.intent_service.detect_intent(text)
            if not self.is_chat_turn_active(turn_id):
                logger.warning("意图返回时本轮已结束，丢弃结果。")
                return
            history_list = []
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
                if name == "add_todo":
                    todo_result = result
                    if not result.get("ok"):
                        todo_error = result.get("error", "unknown")
                history_list.append({
                    "role": "user",
                    "content": f"工具 {name} 执行结果：{result}",
                })
            # 3.调用聊天服务生成文字
            answers = self.chat_service.generate_chat_response(
                text,
                history=history_list,
                is_stream=True,
                owner_id=self.owner_id,
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
                return
            reply = "".join(
                chunk for chunk in answers if isinstance(chunk, str)
            ).strip()
            logger.info("[回复]: %s", reply)
            self._speak_reply(reply, turn_id)
            self._finish_chat_reply(turn_id)
        finally:
            self.conversation_busy.clear()
