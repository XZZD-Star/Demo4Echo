#include "Speaking.h"
#include "../../Utils/user_log.h"
#include "../../Events/AppEvents.h"
#include <chrono>

// 静态成员变量定义
std::atomic<bool> SpeakingState::state_running_{false};
std::thread SpeakingState::state_running_thread_;

void SpeakingState::Enter(Application* app) {
    if (app->get_notify_mode()) {
        // 主动播报：Idle::Exit 刚把提示音放进播放队列，这里不能清空。
        app->set_tts_completed(false);
        app->set_dialogue_completed(false);
        USER_LOG_INFO("Speaking enter for notify id=%d.",
                      app->get_notify_id());
    }
    std::string json_message = R"({"type": "state", "state": "speaking"})";
    app->ws_client_.SendText(json_message);
    // start播放
    app->audio_processor_.startPlaying();
    // running
    state_running_.store(true);
    state_running_thread_ = std::thread([app]() { Run(app); });
    USER_LOG_INFO("Into speaking state.");
}

void SpeakingState::Run(Application* app) {
    USER_LOG_INFO("Speaking state run.");
    auto notify_start = std::chrono::steady_clock::now();
    int empty_polls = 0;
    while(state_running_.load() == true) {
        std::this_thread::sleep_for(std::chrono::milliseconds(500));

        if(app->get_notify_mode()) {
            if(app->audio_processor_.playbackQueueIsEmpty()) {
                empty_polls++;
            } else {
                empty_polls = 0;
            }

            bool finished = app->get_tts_completed() && empty_polls >= 2;
            auto elapsed = std::chrono::duration_cast<std::chrono::seconds>(
                std::chrono::steady_clock::now() - notify_start).count();

            // 首包音频 8 秒内没到，或整体超过 40 秒，都放弃本轮。
            bool stuck = (!app->get_first_tts_audio_received() && elapsed > 8)
                       || elapsed > 40;

            if(finished || stuck) {
                if(stuck && !finished) {
                    USER_LOG_WARN("Notify playback timeout after %lld s.",
                                  (long long)elapsed);
                } else {
                    USER_LOG_INFO("Notify playback end.");
                }

                // 回播放确认，仅用于服务端统计，不触发重试。
                auto notify_ids = app->get_notify_ids();
                if(!notify_ids.empty()) {
                    std::string ack = "{\"type\":\"notify_ack\",\"ids\":[";
                    for(size_t i = 0; i < notify_ids.size(); ++i) {
                        if(i > 0) ack += ",";
                        ack += std::to_string(notify_ids[i]);
                    }
                    ack += stuck ? "],\"status\":\"timeout\"}"
                               : "],\"status\":\"played\"}";
                    app->ws_client_.SendText(ack);
                    USER_LOG_INFO("Notify ACK sent: %s", ack.c_str());
                }
                app->set_notify_ids({});
                app->set_notify_mode(false);
                app->set_notify_id(0);
                app->set_tts_completed(false);
                app->set_dialogue_completed(false);
                app->eventQueue_.Enqueue(static_cast<int>(AppEvent::notify_end));
                break;
            }
            continue;
        }

        if(app->get_tts_completed() && app->audio_processor_.playbackQueueIsEmpty()) {
            std::this_thread::sleep_for(std::chrono::milliseconds(500));
            USER_LOG_INFO("Speaking end.");
            if(app->get_dialogue_completed() == false) {
                app->eventQueue_.Enqueue(static_cast<int>(AppEvent::speaking_end));
            } else {
                app->eventQueue_.Enqueue(static_cast<int>(AppEvent::dialogue_end));
            }
            app->set_tts_completed(false);
            app->set_dialogue_completed(false);
            break;
        }
    }
}

void SpeakingState::Exit(Application* app) {
    // clear playback audio queue
    app->audio_processor_.clearPlaybackAudioQueue();
    // stop播放
    app->audio_processor_.stopPlaying();
    // stop state running
    state_running_.store(false);
    state_running_thread_.join();
    USER_LOG_INFO("Speaking exit.");
}
