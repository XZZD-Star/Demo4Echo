from service_manager import ServiceManager
from tools.logger import logger
from tools.registry import global_registry
from tools import flow_log
from echomate_core import notify as notify_store

class TextHandler:
    def __init__(self, service_manager: ServiceManager):
        self.service_manager = service_manager

    async def handle_text_message(self, data: dict):
        """
        处理 JSON 文本消息
        :param data: JSON 数据
        :return: 响应消息
        """
        if data.get('type') == 'hello':
            audio_params = data.get('audio_params', {})
            logger.info(f"Received hello message with audio params: {audio_params}")
            # API Key 仅由服务端配置，忽略客户端 hello 中的密钥。
            # 暂时没设定可变的音频参数列表, 所以client发送过来的音频参数不会被使用
            # sample_rate = audio_params.get('sample_rate', AudioProcessor.sample_rate)
            # channels = audio_params.get('channels', AudioProcessor.CHANNELS)
            # frame_duration_ms = audio_params.get('frame_duration', AudioProcessor.frame_duration_ms)
            # logger.info(f"Set audio parameters: sample_rate={sample_rate}, channels={channels}, frame_duration_ms={frame_duration_ms}")
            # self.audio_processor.set_audio_params(sample_rate, channels, frame_duration_ms)

        elif data.get('type') == 'functions_register':
            # 获取要注册的函数列表
            functions = data.get('functions', [])
            if not isinstance(functions, list):
                logger.error("functions 字段必须是一个列表")
                return {"type": "error", "message": "Invalid functions format"}
            self.handle_register_functions(functions)

        elif data.get('type') == 'wake_detected':
            trace_id = self.service_manager.begin_voice_trace(
                data.get('turn_id')
            )
            flow_log.emit(
                "client", "wake", "info",
                trace_id=trace_id,
                message="wake detected",
                stage="wake",
            )
            # Use the prompt playback time to prewarm cloud ASR.
            self.service_manager.start_asr_session()

        elif data.get('type') == 'notify_ack':
            ids = data.get('ids')
            if not isinstance(ids, list):
                single = data.get('id')
                ids = [single] if isinstance(single, int) else []

            clean_ids = [i for i in ids if isinstance(i, int)]
            if clean_ids:
                ack_status = str(data.get('status') or 'played').lower()
                flow_log.emit(
                    "ack",
                    "notify_ack",
                    ack_status,
                    trace_id=f"notify-{clean_ids[0]}",
                    notify_id=clean_ids[0],
                    message=f"ack status={ack_status}",
                    ids=clean_ids,
                )
                try:
                    if ack_status == 'played':
                        updated = notify_store.mark_acked(clean_ids)
                        logger.info(
                            "收到播放确认 %s，更新 %d 条。",
                            clean_ids, updated,
                        )
                    elif ack_status in ("timeout", "error"):
                        updated = notify_store.mark_playback_failed(
                            clean_ids, f"client playback {ack_status}"
                        )
                        logger.warning(
                            "客户端播放失败 %s（%s），更新 %d 条。",
                            clean_ids, ack_status, updated,
                        )
                    else:
                        logger.warning(
                            "忽略未知播放确认状态：%s", ack_status
                        )
                except Exception as exc:
                    logger.warning("记录播放确认失败：%s", exc)

        elif data.get('type') == 'state':
            # client 端 idle 信息
            client_state = str(data.get('state') or "unknown")
            trace_id = None
            if client_state in ("speaking", "listening", "thinking"):
                trace_id = (
                    self.service_manager.active_voice_trace_id()
                    if self.service_manager.notify_playing
                    else self.service_manager.begin_voice_trace()
                )
            elif client_state == "idle":
                trace_id = self.service_manager.active_voice_trace_id()
            self.service_manager.set_client_state(client_state)
            flow_log.emit(
                "client",
                "state",
                "ok" if client_state == "idle" else "info",
                trace_id=trace_id,
                message=f"state={client_state}",
                stage=client_state,
            )

            if client_state == 'idle':
                self.service_manager.invalidate_chat_turn()
                self.service_manager.reset_services()
                self.service_manager.clear_voice_trace(trace_id)
                logger.info("Client is idle, resetting services")

            elif client_state == 'listening':
                # 根据配置启动本地或云端 ASR 会话。
                self.service_manager.start_asr_session()
                # 提前打开tts流
                self.service_manager.prepare_tts()

            elif client_state == 'thinking':
                logger.info("Client is thinking")

            elif client_state == 'speaking':
                logger.info("Client is speaking")

        else:
            logger.warning(f"Unknown JSON message type: {data.get('type')}")
            logger.info(f"Received unknown message: {data}")
            return {"type": "error", "message": "Unknown message type"}


    def handle_register_functions(self, functions: list):
        """
        处理函数注册请求
        :param functions: 函数列表
        """
        for func in functions:
            try:
                # 提取函数信息
                function_name = func.get('name')
                description = func.get('description', '')
                arguments = func.get('arguments', {})

                # 检查必要字段
                if not function_name or not isinstance(arguments, dict):
                    logger.error(f"函数注册失败，缺少必要字段: {func}")
                    continue

                # 注册函数，使用通用回调函数
                global_registry.register_function(
                    function_name=function_name,
                    description=description,
                    parameters=arguments,
                    impl=self._generic_function_callback
                )
                logger.info(f"成功注册函数: {function_name}, 描述: {description}, 参数: {arguments}")

            except Exception as e:
                logger.error(f"注册函数时发生错误: {e}")


    def _generic_function_callback(self, function_name: str, *args, **kwargs):
        """
        通用intent函数回调处理, server端只打印
        :param function_name: 函数名称
        :param args: 函数参数
        :param kwargs: 函数关键字参数
        :return: 函数执行结果
        """
        logger.info(f"Function '{function_name}' called with args: {args}, kwargs: {kwargs}")
        # 这里可以根据需要执行相应的操作
        # 例如，调用其他服务或处理数据
        return "Function executed successfully"
