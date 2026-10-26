#ifndef APPLICATION_H
#define APPLICATION_H

#include "../Audio/AudioProcess.h"
#include "../StateMachine/StateMachine.h"
#include "../Events/EventQueue.h"
#include "../Events/AppEvents.h"
#include "../WebSocket/WebsocketClient.h"
#include "../Intent/IntentHandler.h"

#include <thread>
#include <atomic>
#include <string>
#include <vector>
#include <mutex>

class Application {
public:
    Application(const std::string& address, int port, const std::string& token, const std::string& deviceId, 
                const std::string& aliyun_api_key, int protocolVersion, 
                int sample_rate, int channels, int frame_duration);
    ~Application();

    void Run();

    void Stop(void) {
        eventQueue_.Enqueue(static_cast<int>(AppEvent::to_stop));
    }

    AudioProcess audio_processor_;
    StateMachine client_state_;
    EventQueue<int> eventQueue_;
    EventQueue<Json::Value> IntentQueue_;
    // EventQueue<>;
    WebSocketClient ws_client_;
    IntentHandler intent_handler_;
    
    void set_first_audio_msg_received(bool flag) {
        first_audio_msg_received_ = flag;
    }
    bool get_first_audio_msg_received() {
        return first_audio_msg_received_;
    }

    void set_tts_completed(bool flag) {
        tts_completed_ = flag;
    }
    bool get_tts_completed() {
        return tts_completed_;
    }

    void set_dialogue_completed(bool flag) {
        dialogue_completed_ = flag;
    }
    bool get_dialogue_completed() {
        return dialogue_completed_;
    }

    void set_aliyun_api_key(const std::string& key) {
        aliyun_api_key_ = key;
    }
    std::string get_aliyun_api_key() {
        return aliyun_api_key_;
    }

    void set_chat_interaction_enabled(bool flag) {
        chat_interaction_enabled_.store(flag);
    }
    bool get_chat_interaction_enabled() const {
        return chat_interaction_enabled_.load();
    }

    void set_notify_mode(bool flag) { notify_mode_ = flag; }
    bool get_notify_mode() const { return notify_mode_; }

    void set_notify_id(int id) { notify_id_ = id; }
    int get_notify_id() const { return notify_id_; }

    // 服务端合并播报时可能一次带多条 id，用于回 ACK。
    void set_notify_ids(const std::vector<int>& ids) {
        std::lock_guard<std::mutex> lock(notify_ids_mutex_);
        notify_ids_ = ids;
    }
    std::vector<int> get_notify_ids() {
        std::lock_guard<std::mutex> lock(notify_ids_mutex_);
        return notify_ids_;
    }

    void set_first_tts_audio_received(bool flag) {
        first_tts_audio_received_ = flag;
    }
    bool get_first_tts_audio_received() const {
        return first_tts_audio_received_;
    }

    void set_threads_stop_sig(bool flag) {
        threads_stop_flag_.store(flag);
    }
    bool get_threads_stop_sig() {
        return threads_stop_flag_.load();
    }
    
    void set_ws_protocolVersion(int version) {
        ws_protocolVersion_ = version;
    }
    int get_ws_protocolVersion() {
        return ws_protocolVersion_;
    }

    int getState() {
        return client_state_.GetCurrentState();
    }

private:
    bool first_audio_msg_received_ = false;
    bool tts_completed_ = false;
    bool dialogue_completed_ = false;
    std::string aliyun_api_key_;
    int ws_protocolVersion_;
    std::atomic<bool> notify_mode_{false};
    std::atomic<bool> chat_interaction_enabled_{false};
    std::atomic<int> notify_id_{0};
    std::atomic<bool> first_tts_audio_received_{false};
    std::vector<int> notify_ids_;
    mutable std::mutex notify_ids_mutex_;
    // 原子变量用于通知线程退出
    std::atomic<bool> threads_stop_flag_ = false;
    std::thread ws_msg_thread_;
    std::thread state_trans_thread_;
};

#endif // APPLICATION_H