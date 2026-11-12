#ifndef __APP_CHATBOTPAGE_H
#define __APP_CHATBOTPAGE_H

#ifdef __cplusplus
extern "C" {
#endif

#include <stdbool.h>

extern uint8_t chat_bot_move_dir;

// 创建并初始化Application对象, 会开启一个线程
int start_ai_chat(const char* address, int port, const char* token, const char* deviceId, const char* aliyun_api_key, 
                    int protocolVersion, int sample_rate, int channels, int frame_duration);

int stop_ai_chat();

// 获取 AI Chat 状态
int get_ai_chat_state(void);
bool is_ai_chat_idle(void);

// 页面内语音交互开关；后台播报不受影响。
void set_ai_chat_interaction_enabled(bool enabled);
bool is_ai_chat_interaction_enabled(void);

// 将 PIR 触发时的室内温压数据交给现有 WebSocket/TTS 链路。
bool send_ai_chat_sensor_report(float temperature_c, float pressure_hpa);

// 专门处理Intent，目前只有运动
void chat_bot_get_intent_process(void);
void chat_bot_move(int dir);

#ifdef __cplusplus
} /*extern "C"*/
#endif

#endif
