import sys
import time
import threading
from pathlib import Path

from services.cloud_asr_service import CloudASRService

audio_path = Path(
    sys.argv[1] if len(sys.argv) > 1
    else "test/cloud_asr_input.pcm"
)

if not audio_path.is_file():
    raise SystemExit(f"测试音频不存在：{audio_path}")

pcm = audio_path.read_bytes()

if not pcm or len(pcm) % 2:
    raise SystemExit("PCM 文件为空或格式不正确。")

# 补充 1.5 秒静音，供云端 VAD 检测句尾。
pcm += b"\x00" * (16000 * 2 * 15 // 10)

done = threading.Event()
result = {}

def on_result(text):
    result["text"] = text
    done.set()

def on_no_speech(reason):
    result["error"] = reason
    done.set()

service = CloudASRService(
    on_result=on_result,
    on_no_speech=on_no_speech,
)

try:
    print("开始测试 CloudASRService。")
    print("音频格式：PCM16 / 16000 Hz / Mono")

    service.start()

    # 按 100ms 音频块发送，并模拟实时传输速度。
    chunk_size = 3200

    for offset in range(0, len(pcm), chunk_size):
        if done.is_set():
            break

        service.send_audio(pcm[offset:offset + chunk_size])
        time.sleep(0.1)

    if not done.wait(timeout=25):
        print("❌ 等待云端识别超时。")
        sys.exit(2)

    if "error" in result:
        print("❌ 云端服务返回错误：", result["error"])
        sys.exit(2)

    print("\n✅ 云端服务最终识别成功：")
    print(result["text"])

    # 等待 session.finished，避免过早关闭连接。
    service._stop.wait(timeout=5)

finally:
    service.stop()
