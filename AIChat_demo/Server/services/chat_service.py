from typing import Dict, Optional

from config.settings import global_settings
from models.llm_model import LLMModel
from services import assistant_actions


class ChatService:
    def __init__(self):
        self.chat_llm_model = LLMModel(model_name=global_settings.CHAT_MODEL)
        self.chat_llm_model.clear_messages()
        self.chat_llm_model.set_model_sys_content(
            "你是一个桌面机器人，名为 Echo，全程快速、自然地回复。"
            "不要输出 JSON 或代码片段。"
        )

    def chat_clear(self):
        self.chat_llm_model.clear_messages()

    def generate_chat_response(
        self,
        user_input: str,
        history: Optional[Dict] = None,
        is_stream: bool = False,
        owner_id: str = "",
    ) -> str:
        if history:
            for item in history:
                if "role" in item and "content" in item:
                    self.chat_llm_model.add_message(
                        item["role"], item["content"]
                    )

        system = (
            "你是 Echo-Mate 桌面助手。用简体中文自然回答，简洁但有信息量。"
            "你已连接共享待办和记忆，不能声称无法创建待办。"
            "明确要求提醒时调用 add_todo；"
            "长期偏好或个人信息只调用 propose_memory 生成候选，"
            "不要声称已永久保存。"
        )
        if owner_id:
            system += assistant_actions.memory_prompt_block(owner_id)
        self.chat_llm_model.set_model_sys_content(system)

        if is_stream:
            return self.chat_llm_model.get_LLM_response_stream(user_input)
        return self.chat_llm_model.get_LLM_response(user_input)
