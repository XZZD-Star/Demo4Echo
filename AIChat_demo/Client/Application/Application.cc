#include "Application.h"
#include "../Utils/user_log.h"
#include "StateConfig.h"
#include "WS_Handler.h"

#include <cmath>
#include <cstdio>

WSHandler app_handler;

Application::Application(const std::string& address, int port, const std::string& token, const std::string& deviceId, const std::string& aliyun_api_key, 
                         int protocolVersion, int sample_rate, int channels, int frame_duration)
    : ws_client_(address, port, token, deviceId, protocolVersion),
      aliyun_api_key_(aliyun_api_key),
      ws_protocolVersion_(protocolVersion),
      client_state_(static_cast<int>(AppState::startup)),
      audio_processor_(sample_rate, channels, frame_duration) {
        // 设置接收到消息的回调函数
        ws_client_.SetMessageCallback([this](const std::string& message, bool is_binary) {
            app_handler.ws_msg_handle(message, is_binary, this);
        });
        ws_client_.SetCloseCallback([this]() {
            // 断开连接时的回调
            eventQueue_.Enqueue(static_cast<int>(AppEvent::fault_happen));
        });
}

Application::~Application() {
    threads_stop_flag_.store(true);
    if (ws_msg_thread_.joinable()) ws_msg_thread_.join();
    if (state_trans_thread_.joinable()) state_trans_thread_.join();
    USER_LOG_WARN("Application destruct.");
}

std::string Application::begin_voice_turn() {
    auto now = std::chrono::system_clock::now().time_since_epoch();
    auto millis = std::chrono::duration_cast<std::chrono::milliseconds>(now)
                      .count();
    std::lock_guard<std::mutex> lock(turn_mutex_);
    ++turn_generation_;
    active_turn_id_ = "voice-" + std::to_string(millis)
                    + "-" + std::to_string(turn_generation_);
    asr_phase_ = AsrPhase::preparing;
    asr_terminal_ = false;
    asr_ready_.store(false);
    return active_turn_id_;
}

std::string Application::get_active_turn_id() {
    std::lock_guard<std::mutex> lock(turn_mutex_);
    return active_turn_id_;
}

bool Application::is_current_turn(const std::string& turn_id) {
    std::lock_guard<std::mutex> lock(turn_mutex_);
    return !turn_id.empty() && turn_id == active_turn_id_;
}

bool Application::set_asr_phase(const std::string& turn_id, AsrPhase phase) {
    std::lock_guard<std::mutex> lock(turn_mutex_);
    if (turn_id.empty() || turn_id != active_turn_id_ || asr_terminal_) {
        return false;
    }
    asr_phase_ = phase;
    if (phase == AsrPhase::terminal) {
        asr_terminal_ = true;
    }
    return true;
}

bool Application::mark_asr_terminal(const std::string& turn_id) {
    return set_asr_phase(turn_id, AsrPhase::terminal);
}

AsrPhase Application::get_asr_phase(const std::string& turn_id) {
    std::lock_guard<std::mutex> lock(turn_mutex_);
    if (turn_id.empty() || turn_id != active_turn_id_) {
        return AsrPhase::none;
    }
    return asr_phase_;
}

bool Application::get_asr_terminal(const std::string& turn_id) {
    std::lock_guard<std::mutex> lock(turn_mutex_);
    return turn_id == active_turn_id_ && asr_terminal_;
}

void Application::Run() {

    // 启动状态机事件处理线程
    state_trans_thread_ = std::thread([this]() {
        // initialize state machine and user states
        StateConfig::Configure(client_state_, this);
        // 主要执行state事件处理, 状态切换
        while(threads_stop_flag_.load() == false) {
            std::this_thread::sleep_for(std::chrono::milliseconds(100));
            if(eventQueue_.IsEmpty() == false) {
                // 事件queue处理
                if (auto event_opt = eventQueue_.Dequeue(); event_opt) {
                    client_state_.HandleEvent(event_opt.value());
                }
            } 
        }
    });

    // 等待 state 切换事件线程结束
    state_trans_thread_.join();
    if(ws_client_.IsConnected()) {
        ws_client_.Close();
        std::this_thread::sleep_for(std::chrono::milliseconds(100));
    }
    USER_LOG_WARN("ai chat app run end");
}

bool Application::SendSensorReport(float temperature_c, float humidity_percent,
                                   float pressure_hpa) {
    if (!ws_client_.IsConnected()
        || !std::isfinite(temperature_c)
        || !std::isfinite(humidity_percent)
        || !std::isfinite(pressure_hpa)
        || temperature_c < -40.0f || temperature_c > 85.0f
        || humidity_percent < 0.0f || humidity_percent > 100.0f
        || pressure_hpa < 300.0f || pressure_hpa > 1100.0f) {
        return false;
    }

    char message[192];
    int written = std::snprintf(
        message, sizeof(message),
        R"({"type":"sensor_report","temperature_c":%.1f,"humidity_percent":%.1f,"pressure_hpa":%.1f})",
        temperature_c, humidity_percent, pressure_hpa);
    if (written <= 0 || written >= (int)sizeof(message)) {
        return false;
    }

    ws_client_.SendText(message);
    return true;
}
