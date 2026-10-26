#include "Idle.h"
#include "../../Utils/user_log.h"
#include "../../third_party/snowboy/include/snowboy-detect-c-wrapper.h"
#include "../../Events/AppEvents.h"

// 静态成员变量定义
std::atomic<bool> IdleState::state_running_{false};
std::thread IdleState::state_running_thread_;

void IdleState::Enter(Application* app) {
    std::string json_message = R"({"type": "state", "state": "idle"})";
    app->ws_client_.SendText(json_message);
    app->audio_processor_.clearRecordedAudioQueue();
    state_running_.store(true);
    state_running_thread_ = std::thread([app]() { Run(app); });
    USER_LOG_INFO("Into Idle state (interaction=%d).",
                  app->get_chat_interaction_enabled());
}

void IdleState::Run(Application* app) {
    USER_LOG_INFO("Idle state run.");
    SnowboyDetect* detector = nullptr;
    bool interaction_enabled = false;
    std::vector<int16_t> data;

    while (state_running_.load() == true) {
        bool should_enable = app->get_chat_interaction_enabled()
                          && !app->get_notify_mode();

        if (should_enable && !interaction_enabled) {
            detector = SnowboyDetectConstructor(
                "third_party/snowboy/resources/common.res",
                "third_party/snowboy/resources/models/echo.pmdl");
            if (!detector) {
                USER_LOG_ERROR("Failed to create wake detector.");
                std::this_thread::sleep_for(std::chrono::milliseconds(200));
                continue;
            }
            SnowboyDetectSetSensitivity(detector, "0.5");
            SnowboyDetectSetAudioGain(detector, 1);
            SnowboyDetectApplyFrontend(detector, false);
            app->audio_processor_.clearRecordedAudioQueue();
            app->audio_processor_.startRecording();
            interaction_enabled = true;
            USER_LOG_INFO("Chat interaction enabled.");
        } else if (!should_enable && interaction_enabled) {
            app->audio_processor_.stopRecording();
            app->audio_processor_.clearRecordedAudioQueue();
            if (detector) {
                SnowboyDetectDestructor(detector);
                detector = nullptr;
            }
            interaction_enabled = false;
            USER_LOG_INFO("Chat interaction disabled.");
        }

        if (interaction_enabled
            && app->audio_processor_.recordedQueueIsEmpty() == false) {
            app->audio_processor_.getRecordedAudio(data);
            int result = SnowboyDetectRunDetection(
                detector, data.data(), data.size(), false);
            if (result > 0) {
                USER_LOG_INFO("Wake detected.");
                app->audio_processor_.stopRecording();
                app->audio_processor_.clearRecordedAudioQueue();
                SnowboyDetectDestructor(detector);
                detector = nullptr;
                interaction_enabled = false;
                app->eventQueue_.Enqueue(
                    static_cast<int>(AppEvent::wake_detected));
                break;
            }
        } else if (!interaction_enabled) {
            std::this_thread::sleep_for(std::chrono::milliseconds(50));
        }
    }

    if (interaction_enabled) {
        app->audio_processor_.stopRecording();
        app->audio_processor_.clearRecordedAudioQueue();
    }
    if (detector) {
        SnowboyDetectDestructor(detector);
    }
}

void IdleState::Exit(Application* app) {
    // stop running
    state_running_.store(false);
    state_running_thread_.join();

    // 主动播报时用提示音，普通唤醒仍用原唤醒音。
    std::string tone_path = app->get_notify_mode()
        ? "third_party/audio/notify.pcm"
        : "third_party/audio/waked.pcm";
    USER_LOG_INFO("Idle exit tone: %s", tone_path.c_str());
    auto audioQueue = app->audio_processor_.loadAudioFromFile(tone_path, 40);
    while (!audioQueue.empty()) {
        const auto& frame = audioQueue.front();
        app->audio_processor_.addFrameToPlaybackQueue(frame);
        audioQueue.pop();
    }
    app->set_tts_completed(true);
    std::this_thread::sleep_for(std::chrono::milliseconds(200));
    USER_LOG_INFO("Idle State exit.");
}