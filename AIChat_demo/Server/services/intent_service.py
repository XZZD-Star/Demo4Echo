from typing import Dict, Any, List
import json
from datetime import datetime
from zoneinfo import ZoneInfo
from config.settings import global_settings
from tools.registry import FunctionRegistry
from tools.logger import logger
from models.llm_model import LLMModel


class IntentService:
    def __init__(self, registry: FunctionRegistry):
        self.intent_llm_model = LLMModel(model_name=global_settings.INTENT_MODEL)
        self.registry = registry
        # dashscope.api_key = settings.DASHSCOPE_API_KEY

    def generate_prompt(self) -> str:
        """动态构建意图识别提示词"""
        tools_info = json.dumps(
            self.registry.get_registered_tools(),
            ensure_ascii=False,
            indent=2
        )
        current_time = datetime.now(
            ZoneInfo("Asia/Shanghai")
        ).strftime("%Y-%m-%d %H:%M %A")
        return (
            "你是一个带意图识别的语音助手。请分析用户的最后一句话，判断用户意图属于以下哪一类：\n"
            f"当前北京时间：{current_time}\n"
            "以下面这个注册的工具列表为准:\n"
            "<start>\n"
            "工具列表:\n"
            f"{tools_info}\n"
            "<end>\n"
            "注意:\n"
            "- 只返回纯JSON\n"
            "- 如果有多个意图， 请返回一个包含多个函数调用的JSON数组, \n"
            "- 注意不要重复意图了，例如说再见拜拜晚安这种重复的语句实际只有1个意图，但你不要返回3个exit_chat的function_call诸如此类\n"
            "- 如果没有明确意图，则返回{'function_call': {'name': 'continue_chat'}}\n"
            "- 你负责理解用户语义，不要依赖固定关键词；自然语言表达提醒、安排、待办时调用 add_todo\n"
            "- add_todo 的 due_at 必须是带时区的 ISO 8601 时间；结合当前北京时间解析今天、明天、下午、晚上等表达\n"
            "- 如果时间缺少上午/下午且无法唯一确定，不要猜，返回 continue_chat，让聊天模型向用户澄清\n"
            "- 只能返回 function_call，绝不能直接返回工具执行结果、ok 或 error 字段\n"
            "- 不要添加任何额外说明\n"
            "示例分析:\n"
            "```\n"
            "用户: 你好呀, 能帮帮我吗\n"
            '返回: {"function_call": {"name": "continue_chat"}}\n'
            "```\n"
            "```\n"
            "用户: 请先左转，然后后退\n"
            '[{"function_call": {"name": "robot_move", "arguments": {"direction": "left"}}}, {"function_call": {"name": "robot_move", "arguments": {"direction": "backward"}}}]\n'
            "```\n"
            "```\n"
            "用户: 我们明天再聊吧, 再见拜拜\n"
            '返回: {"function_call": {"name": "exit_chat"}}\n'
            "```\n"
            "```\n"
            "用户: xxx\n"
            '返回: {"function_call": {"name": "xxxx"}}\n'
            "```\n"
            "诸如此类"
        )

    def detect_intent(self, user_input: str) -> List[Dict[str, Any]]:
        """
        识别用户意图并返回函数调用列表
        :param user_input: 用户输入的字符串，用于意图识别。
        :return: 包含多个函数调用信息的列表，格式如下：
            [
                {
                    "function_call": {
                        "name": "function_name",
                        "arguments": {
                            "arg1": "value1",
                            "arg2": "value2"
                        }
                    }
                },
                ...
            ]
        """
        prompt = self.generate_prompt()
        self.intent_llm_model.clear_messages()
        self.intent_llm_model.set_model_sys_content(prompt)
        self.intent_llm_model.add_message("user", user_input)

        try:
            response = self.intent_llm_model.get_LLM_response(user_input)
            # 如果有 ```json ``` 包裹，去掉它
            if isinstance(response, str):
                if response.startswith("```json"):
                    response = response.strip("```json").strip()
                elif response.startswith("```"):
                    response = response.strip("```").strip()
                logger.info(f"[意图识别结果]: {response}")
                response = json.loads(response)  # 解析为字典或列表
            elif isinstance(response, (dict, list)):
                logger.info(f"[意图识别结果]: {response}")
            else:
                logger.error(f"意图识别返回了未知类型: {type(response)}")
                return [{"function_call": {"name": "continue_chat"}}]

            # 如果返回的是单个函数调用，包装成列表
            if isinstance(response, dict):
                response = [response]

            # 解析每个函数调用的参数
            for function_call in response:
                if "function_call" in function_call and "arguments" in function_call["function_call"]:
                    try:
                        if isinstance(function_call["function_call"]["arguments"], str):
                            function_call["function_call"]["arguments"] = json.loads(function_call["function_call"]["arguments"])
                        function_call["function_call"]["arguments"] = self._convert_numbers_to_strings(function_call["function_call"]["arguments"])
                    except json.JSONDecodeError as e:
                        logger.error(f"函数参数解析失败: {str(e)}")
                        function_call["function_call"]["arguments"] = {}

            return response
        except json.JSONDecodeError as e:
            logger.error(f"意图识别解析失败: {str(e)}")
            return [{"function_call": {"name": "continue_chat"}}]

    def _convert_numbers_to_strings(self, data):
        """
        递归地将字典或列表中的数字类型转换为字符串类型
        :param data: 输入数据（字典或列表）
        :return: 转换后的数据
        """
        if isinstance(data, dict):
            return {k: self._convert_numbers_to_strings(v) for k, v in data.items()}
        elif isinstance(data, list):
            return [self._convert_numbers_to_strings(item) for item in data]
        elif isinstance(data, (int, float)):
            return str(data)
        return data

