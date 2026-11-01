import asyncio
from ws_server import WebSocketServer
from threads.tts_thread import TTSGenerateThread
from threads.audio_send_thread import AudioSendThread
from threads.notify_thread import NotifyThread
from tools.logger import logger
from service_manager import ServiceManager
import sys
sys.path.append("..")


async def monitor_workers(service_manager, threads, interval_seconds=10.0):
    """Restart the service if a critical worker silently stops."""
    while not service_manager.stop_event.is_set():
        await asyncio.sleep(interval_seconds)

        for thread in threads:
            if service_manager.stop_event.is_set():
                return
            if not thread.is_alive():
                reason = f"{thread.name} 已停止"
                logger.error("%s，服务将退出并由 systemd 重启。", reason)
                service_manager.fail_fast(reason)
                return
            if hasattr(thread, "is_healthy") and not thread.is_healthy():
                reason = f"{thread.name} 心跳超时"
                logger.error("%s，服务将退出并由 systemd 重启。", reason)
                service_manager.fail_fast(reason)
                return


async def main():

    # 初始化vad asr chat intent tts服务
    service_manager = ServiceManager()

    # 启动 TTS 生成线程
    tts_generate_thread = TTSGenerateThread(service_manager)
    # tts_generate_thread.start()

    # 启动 audio 数据发送线程
    tts_send_thread = AudioSendThread(service_manager)
    tts_send_thread.start()

    # 启动主动播报线程（未启用时会空转等待）
    notify_thread = NotifyThread(service_manager)
    notify_thread.start()
    monitor_task = asyncio.create_task(
        monitor_workers(service_manager, [tts_send_thread, notify_thread])
    )

    # 启动 WebSocket 服务器
    server = WebSocketServer(host="0.0.0.0", port=8000, access_token="123456", service_manager=service_manager)
    try:
        await server.start_server()
    except KeyboardInterrupt:
        logger.info("\n服务器正在关闭...")
    finally:
        # 停止线程
        service_manager.stop_event.set()  # 设置停止事件
        monitor_task.cancel()
        try:
            await monitor_task
        except asyncio.CancelledError:
            pass
        # tts_generate_thread.join()
        notify_thread.join(timeout=5)
        tts_send_thread.join()
        logger.info("服务器已关闭。")

    fatal_reason = service_manager.fatal_reason()
    if fatal_reason:
        raise RuntimeError(f"关键工作线程异常：{fatal_reason}")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("程序已被用户中断")
    finally:
        # 确保事件循环关闭
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.stop()
        except RuntimeError:
            pass
        logger.info("事件循环已关闭")
