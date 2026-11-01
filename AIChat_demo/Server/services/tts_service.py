from config.settings import global_settings
from models.tts_model import TTSModel
from tools.logger import logger

class TTSService:
    def __init__(self):
        self.tts_model = TTSModel()

    def tts_set(self, on_open=None, on_complete=None, on_error=None, on_close=None, on_data=None):
        '''设置TTS回调函数, 提前打开ws连接

        :param: on_open: 连接打开时的回调函数
        :param: on_complete: 合成完成时的回调函数
        :param: on_error: 合成错误时的回调函数
        :param: on_close: 连接关闭时的回调函数
        :param: on_data: 接收到数据时的回调函数(PCM-16bit 音频数据), 可以查看tts_test.py看如何使用
        '''
        return self.tts_model.tts_stream_set(
            on_open, on_complete, on_error, on_close, on_data
        )

    def tts_close(self):
        '''关闭TTS流式合成'''
        return self.tts_model.tts_stream_close()

    def tts_speech_stream(self, text_chunk):
        return self.tts_model.tts_stream_speech_synthesis(text_chunk)

    def is_ready(self):
        return self.tts_model.is_ready()

    def last_error(self):
        return self.tts_model.last_error()
