import os
from pathlib import Path

from dotenv import load_dotenv
import dashscope

SERVER_DIR = Path(__file__).resolve().parents[1]
load_dotenv(SERVER_DIR / ".env", override=False)


class Settings:
    protocol_version = 2

    # 所有云端服务使用同一个服务端 API Key
    DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "").strip()

    # ASR 配置
    ECHOMATE_ASR_BACKEND = os.environ.get(
        "ECHOMATE_ASR_BACKEND", "local"
    ).strip().lower()
    DASHSCOPE_REALTIME_URL = os.environ.get(
        "DASHSCOPE_REALTIME_URL", ""
    ).strip()
    DASHSCOPE_WORKSPACE_ID = os.environ.get(
        "DASHSCOPE_WORKSPACE_ID", ""
    ).strip()
    DASHSCOPE_ASR_MODEL = os.environ.get(
        "DASHSCOPE_ASR_MODEL", "qwen3-asr-flash-realtime"
    ).strip()

    # TTS 原有工作空间地址，保持已经验证成功的配置
    DASHSCOPE_BASE_WEBSOCKET_API_URL = os.environ.get(
        "DASHSCOPE_BASE_WEBSOCKET_API_URL", ""
    ).strip()

    # 模型配置
    INTENT_MODEL = os.environ.get("INTENT_MODEL", "qwen-max").strip()
    CHAT_MODEL = os.environ.get("CHAT_MODEL", "qwen-max").strip()
    COSYVOICE_MODEL = os.environ.get(
        "COSYVOICE_MODEL", "cosyvoice-v2"
    ).strip()
    COSYVOICE_VOICE = os.environ.get(
        "COSYVOICE_VOICE", "longxiaochun_v2"
    ).strip()

    # 本地开发模式仍可使用这些设置
    ASR_DEVICE = os.environ.get("ASR_DEVICE", "cpu").strip()
    VAD_DEVICE = os.environ.get("VAD_DEVICE", "cpu").strip()
    API_TIMEOUT = int(os.environ.get("API_TIMEOUT", "10"))

    def Set_API_Key(self, aliyun_api_key):
        # 兼容旧调用：忽略空值，避免 None 覆盖已有配置。
        if aliyun_api_key:
            key = str(aliyun_api_key).strip()
            if key:
                self.DASHSCOPE_API_KEY = key
                dashscope.api_key = key


global_settings = Settings()

if global_settings.DASHSCOPE_API_KEY:
    dashscope.api_key = global_settings.DASHSCOPE_API_KEY

if global_settings.DASHSCOPE_BASE_WEBSOCKET_API_URL:
    dashscope.base_websocket_api_url = (
        global_settings.DASHSCOPE_BASE_WEBSOCKET_API_URL
    )
