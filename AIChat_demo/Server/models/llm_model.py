import dashscope
from tools.logger import logger
from config.settings import global_settings
from services import assistant_actions

class LLMModel:
    def __init__(self, model_name: str = "qwen-turbo"):
        self.model_name = model_name
        # 对话初始历史记录
        # 默认的是这个content
        self.messages = [
            {"role": "system", "content": "你是一个桌面机器人, 名为Echo, 快速地回复我."}
        ]

    def set_model_sys_content(self, content: str):
        """
        设置模型的系统描述内容

        :param content: 系统内容
        """
        self.messages[0]["content"] = content

    def add_message(self, role: str, content: str):
        """
        添加对话历史记录

        :param role: 消息角色 ("user" 或 "assistant")
        :param content: 消息内容
        """
        self.messages.append({"role": role, "content": content})

    def clear_messages(self):
        """
        清空对话历史记录
        """
        # 默认的是这个content
        self.messages = [
            {"role": "system", "content": "你是一个桌面机器人, 名为Echo, 全程请快速地回复我. 同时你还有函数执行的功能, 可以根据函数来回复我. "}
        ]

    def get_LLM_response(self, question: str) -> str:
        """
        非流式生成回答

        :param question: 用户问题
        :return: 完整的回答
        """
        if question:
            self.add_message("user", question)

        try:
            response = dashscope.Generation.call(
                api_key=global_settings.DASHSCOPE_API_KEY,
                model=self.model_name,
                messages=self.messages,
                tools=assistant_actions.ASSISTANT_TOOLS,
                tool_choice="auto",
                result_format='message',
                stream=False,
                incremental_output=False,
                enable_search=True
            )

            if response["status_code"] == 200:
                message = response["output"]["choices"][0]["message"]
                tool_calls = message.get("tool_calls") or []
                if tool_calls:
                    return self._run_tool_calls(
                        tool_calls,
                        owner_id="ou_1359f562d0a768f91232760499bc1cc0",
                        source="deskbot",
                    )
                content = message.get("content") or ""
                self.add_message("assistant", content)
                return content
            else:
                logger.error(f"非流式生成失败，状态码: {response['status_code']}")
                return "抱歉，我暂时无法处理您的请求。"
        except Exception as e:
            logger.error(f"非流式生成异常: {str(e)}")
            return "抱歉，我暂时无法处理您的请求。"

    def get_text_response(self, question: str) -> str:
        """纯文本推理，不注册也不执行任何工具。"""
        if question:
            self.add_message("user", question)

        try:
            response = dashscope.Generation.call(
                api_key=global_settings.DASHSCOPE_API_KEY,
                model=self.model_name,
                messages=self.messages,
                result_format="message",
                stream=False,
                incremental_output=False,
                enable_search=True,
            )

            if response["status_code"] == 200:
                message = response["output"]["choices"][0]["message"]
                content = message.get("content") or ""
                self.add_message("assistant", content)
                return content

            logger.error(
                "纯文本推理失败，状态码: %s",
                response["status_code"],
            )
        except Exception as exc:
            logger.error("纯文本推理异常: %s", exc)
        return ""


    def _run_tool_calls(self, tool_calls, owner_id, source, chat_id=""):
        results = []
        for call in tool_calls:
            function = call.get("function") or {}
            name = function.get("name", "")
            arguments = function.get("arguments", "{}")
            result = assistant_actions.execute_tool_call(
                name, arguments, owner_id=owner_id, source=source,
                chat_id=chat_id,
            )
            results.append(result)
        return results

    def get_LLM_response_stream(self, question):
        """获取百炼对话回答\r\n
        注意: 此处是yield生成器, 需要在外部循环中调用"""
        # 如果在此添加用户问题
        if(question):
            self.add_message('user', question)
        messages = self.messages

        try:
            responses = dashscope.Generation.call(
                api_key=global_settings.DASHSCOPE_API_KEY,
                model=self.model_name, # 模型列表：https://help.aliyun.com/zh/model-studio/getting-started/models
                messages=messages,
                result_format='message',
                stream=True,
                incremental_output=True,
                enable_search=True
            )
            full_text = ''
            for response in responses:
                if response["status_code"] == 200:
                    for choice in response["output"]["choices"]:
                        full_text += choice["message"]["content"]
                        yield choice["message"]["content"]
                else:
                    logger.error(f"Failed to get LLM response with status code {response['status_code']}")
                    yield -1
            # 最后记录回复的信息
            self.add_message('assistant', full_text)
        except Exception as e:
            logger.error(f"An exception occurred: {str(e)}")
            yield -1
