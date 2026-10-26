import os
from pathlib import Path

from dotenv import load_dotenv
import dashscope
from echomate_credentials import credential

SERVER_DIR = Path(__file__).resolve().parents[1]
load_dotenv(SERVER_DIR / ".env", override=False)


class Settings:
    protocol_version = 2

    # 所有云端服务使用同一个服务端 API Key
    DASHSCOPE_API_KEY = credential("dashscope_api_key")

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

    # ===== 主动语音播报 =====
    NOTIFY_ENABLED = (
        os.environ.get("ECHOMATE_NOTIFY_ENABLED", "0").strip() == "1"
    )
    NOTIFY_POLL_SECONDS = float(
        os.environ.get("ECHOMATE_NOTIFY_POLL_SECONDS", "3")
    )
    NOTIFY_MIN_GAP_SECONDS = int(
        os.environ.get("ECHOMATE_NOTIFY_MIN_GAP_SECONDS", "20")
    )
    NOTIFY_TTL_MINUTES = int(
        os.environ.get("ECHOMATE_NOTIFY_TTL_MINUTES", "60")
    )
    NOTIFY_MAX_PER_HOUR = int(
        os.environ.get("ECHOMATE_NOTIFY_MAX_PER_HOUR", "10")
    )
    NOTIFY_TTS_TIMEOUT = float(
        os.environ.get("ECHOMATE_NOTIFY_TTS_TIMEOUT", "30")
    )
    # 静音时段默认不启用；后续由飞书写入 app_settings 后热生效。
    NOTIFY_QUIET_START = os.environ.get(
        "ECHOMATE_NOTIFY_QUIET_START", ""
    ).strip()
    NOTIFY_QUIET_END = os.environ.get(
        "ECHOMATE_NOTIFY_QUIET_END", ""
    ).strip()

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
