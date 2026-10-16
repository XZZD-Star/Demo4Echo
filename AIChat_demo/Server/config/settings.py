import dashscope
dashscope.base_websocket_api_url = (
    "wss://ws-j27i6oozvcb7v2k7.cn-beijing.maas.aliyuncs.com"
    "/api-ws/v1/inference"
)

class Settings:
    protocol_version = 2
    # LLM API 密钥
    dashscope.api_key = "sk-ws-H.PPXLLPM.Qjqc.MEUCICt3qG7muHjCiWDqzFlmv4QKTotB6AZQy2Na2fTYVXvyAiEA2mxzQqYuga3nJqRmHNsz1tofBcehOU-qsq47Ke_DhS0"

    # 模型选择
    INTENT_MODEL = "qwen-max"       # 专门用于意图识别
    CHAT_MODEL = "qwen-max"         # 用于常规对话

    # device
    ASR_DEVICE = "cpu"            # ASR 模型使用的设备
    # ASR_DEVICE = "cuda"            # ASR 模型使用的设备
    VAD_DEVICE = "cpu"            # ASR 模型使用的设备

    # 超时设置
    API_TIMEOUT = 10  # 秒

    def Set_API_Key(self, aliyun_api_key):
        dashscope.api_key = aliyun_api_key

global_settings = Settings()
