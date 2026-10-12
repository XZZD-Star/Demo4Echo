import dashscope

class Settings:
    protocol_version = 2
    # LLM API 密钥
    dashscope.api_key = "sk-ws-H.PIYRDLE.F3VO.MEUCIQCOfKXDIVJWyHnNb85YEG4oqKAKrWLEZCX3_KdNZ-PG3gIgJZvZWQubaidmUbczPMZSG8Bk9dXZeNmh5Ll1tU3Gqj4"

    # 模型选择
    INTENT_MODEL = "qwen-plus-2025-07-28"       # 专门用于意图识别
    CHAT_MODEL = "qwen-plus-2025-07-28"         # 用于常规对话

    # device
    ASR_DEVICE = "cpu"            # ASR 模型使用的设备
    # ASR_DEVICE = "cuda"            # ASR 模型使用的设备
    VAD_DEVICE = "cpu"            # ASR 模型使用的设备

    # 超时设置
    API_TIMEOUT = 10  # 秒

    def Set_API_Key(self, aliyun_api_key):
        dashscope.api_key = aliyun_api_key

global_settings = Settings()
