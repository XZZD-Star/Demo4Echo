import os
import re
import sys
import json
import time
import uuid
import base64
import threading
from pathlib import Path

import websocket


MODEL = "qwen3-asr-flash-realtime"
SAMPLE_RATE = 16000
CHUNK_BYTES = 3200          # 100ms，PCM16 单声道
SILENCE_SECONDS = 1.2       # 在末尾补静音，便于服务端 VAD 断句


def get_endpoint():
    # 优先使用显式配置的完整实时 API 地址
    url = os.environ.get("DASHSCOPE_REALTIME_URL")
    if url:
        if "model=" not in url:
            url += ("&" if "?" in url else "?") + f"model={MODEL}"
        return url

    # 也允许直接提供 Workspace ID
    workspace_id = os.environ.get("DASHSCOPE_WORKSPACE_ID")
    if workspace_id:
        if ".cn-beijing.maas.aliyuncs.com" in workspace_id:
            host = workspace_id
        else:
            host = f"{workspace_id}.cn-beijing.maas.aliyuncs.com"
        return f"wss://{host}/api-ws/v1/realtime?model={MODEL}"

    # 尝试从现有 TTS 配置中提取北京 Workspace 主机名。
    # 只提取地址，不读取或打印 API Key。
    pattern = re.compile(
        r"wss://([A-Za-z0-9.-]+\.cn-beijing\.maas\.aliyuncs\.com)"
        r"/api-ws/v1/inference"
    )

    for filename in ("config/settings.py", "models/tts_model.py"):
        path = Path(filename)
        if not path.exists():
            continue

        content = path.read_text(encoding="utf-8", errors="ignore")
        match = pattern.search(content)
        if match:
            host = match.group(1)
            return f"wss://{host}/api-ws/v1/realtime?model={MODEL}"

    raise RuntimeError(
        "未能自动找到 Workspace 地址。请设置 "
        "DASHSCOPE_WORKSPACE_ID 或 DASHSCOPE_REALTIME_URL。"
    )


def main():
    api_key = os.environ.get("DASHSCOPE_API_KEY")
    if not api_key:
        raise RuntimeError("请先设置 DASHSCOPE_API_KEY 环境变量。")

    audio_path = Path(
        sys.argv[1] if len(sys.argv) > 1
        else "test/cloud_asr_input.pcm"
    )

    if not audio_path.is_file():
        raise FileNotFoundError(f"音频文件不存在：{audio_path}")

    pcm = audio_path.read_bytes()

    if not pcm:
        raise ValueError("音频文件为空。")
    if len(pcm) % 2:
        raise ValueError("PCM 文件字节数为奇数，请检查音频格式。")

    # 添加 1.2 秒静音，帮助 VAD 检测句尾。
    pcm += b"\x00" * int(SAMPLE_RATE * 2 * SILENCE_SECONDS)

    url = get_endpoint()
    print(f"模型：{MODEL}")
    print(f"音频：{audio_path}")
    print(f"原始 PCM 大小：{audio_path.stat().st_size} bytes")
    print("音频格式：PCM16 / 16000 Hz / Mono")
    print("服务端 VAD：开启")
    print("正在连接实时 ASR ...")

    finished = threading.Event()
    audio_started = False
    state_lock = threading.Lock()
    final_transcripts = []

    def send_audio(ws):
        try:
            print("会话配置完成，开始发送音频。")

            for offset in range(0, len(pcm), CHUNK_BYTES):
                if not ws.sock or not ws.sock.connected:
                    print("WebSocket 已断开，停止发送音频。")
                    return

                chunk = pcm[offset:offset + CHUNK_BYTES]
                event = {
                    "event_id": f"event_{uuid.uuid4().hex}",
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(chunk).decode("ascii"),
                }
                ws.send(json.dumps(event))
                time.sleep(0.1)

            # VAD 模式下无需手动 commit。
            ws.send(json.dumps({
                "event_id": f"event_{uuid.uuid4().hex}",
                "type": "session.finish",
            }))
            print("音频发送完成，等待最终识别结果。")

        except Exception as exc:
            print(f"音频发送失败：{exc}")
            finished.set()
            ws.close()

    def on_open(ws):
        print("WebSocket 连接成功。")
        session_update = {
            "event_id": f"event_{uuid.uuid4().hex}",
            "type": "session.update",
            "session": {
                "modalities": ["text"],
                "input_audio_format": "pcm",
                "sample_rate": SAMPLE_RATE,
                "turn_detection": {
                    "type": "server_vad",
                    "threshold": 0.2,
                    "silence_duration_ms": 600,
                },
            },
        }
        ws.send(json.dumps(session_update))

    def on_message(ws, message):
        nonlocal audio_started

        try:
            event = json.loads(message)
        except json.JSONDecodeError:
            print("收到无法解析的消息。")
            return

        event_type = event.get("type", "")
        print(f"[事件] {event_type}")

        if event_type == "session.updated":
            with state_lock:
                if not audio_started:
                    audio_started = True
                    threading.Thread(
                        target=send_audio,
                        args=(ws,),
                        daemon=True,
                    ).start()

        elif event_type == (
            "conversation.item.input_audio_transcription.text"
        ):
            text = event.get("text", "")
            if text:
                print(f"[中间识别] {text}")

        elif event_type == (
            "conversation.item.input_audio_transcription.completed"
        ):
            transcript = event.get("transcript", "")
            final_transcripts.append(transcript)
            print(f"[最终识别] {transcript}")

        elif event_type == "error":
            print("[云端错误]")
            print(json.dumps(event, ensure_ascii=False, indent=2))

        elif event_type == "session.finished":
            print("服务端确认会话结束。")
            finished.set()
            ws.close()

    def on_error(ws, error):
        print(f"[WebSocket 错误] {error}")
        finished.set()

    def on_close(ws, status_code, message):
        print(f"连接关闭：code={status_code}, reason={message}")
        finished.set()

    app = websocket.WebSocketApp(
        url,
        header=[
            f"Authorization: Bearer {api_key}",
            "OpenAI-Beta: realtime=v1",
        ],
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close,
    )

    def watchdog():
        if not finished.wait(45):
            print("等待超时：45 秒内未正常完成，请检查日志。")
            app.close()

    threading.Thread(target=watchdog, daemon=True).start()
    app.run_forever(ping_interval=20, ping_timeout=10)

    if final_transcripts:
        print("\n========== 最终识别汇总 ==========")
        for index, transcript in enumerate(final_transcripts, start=1):
            print(f"{index}. {transcript}")
    else:
        print(
            "\n没有收到最终识别文本。"
            "请结合上述事件检查鉴权、模型权限、音频格式和 VAD 状态。"
        )
        sys.exit(2)


if __name__ == "__main__":
    main()
