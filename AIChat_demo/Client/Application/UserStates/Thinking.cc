#include "Thinking.h"
#include "../../Application/Application.h"
#include "../../Utils/user_log.h"
#include "../../Events/AppEvents.h"
#include <chrono>

std::atomic<bool> ThinkingState::state_running_{false};
std::thread ThinkingState::state_running_thread_;
std::atomic<long long> ThinkingState::timeout_generation_{0};

void ThinkingState::Enter(Application* app) {
    std::string json_message = R"({"type": "state", "state": "thinking"})";
    app->ws_client_.SendText(json_message);
    timeout_generation_.fetch_add(1);
    state_running_.store(true);
    state_running_thread_ = std::thread([app]() { Run(app); });
    USER_LOG_INFO("Into thinking state.");
}

void ThinkingState::Run(Application* app) {
    auto deadline = std::chrono::steady_clock::now()
                  + std::chrono::seconds(25);
    auto dialogue_end_deadline = std::chrono::steady_clock::time_point::max();
    long long observed_generation = timeout_generation_.load();
    while (state_running_.load()) {
        std::this_thread::sleep_for(std::chrono::milliseconds(500));
        if (app->get_tts_error()) {
            USER_LOG_WARN("Reply failed before TTS playback.");
            app->set_tts_error(false);
            app->eventQueue_.Enqueue(
                static_cast<int>(AppEvent::reply_failed));
            break;
        }
        if (
            app->get_dialogue_completed()
            && !app->get_first_tts_audio_received()
        ) {
            if (dialogue_end_deadline == std::chrono::steady_clock::time_point::max()) {
                dialogue_end_deadline = std::chrono::steady_clock::now()
                                      + std::chrono::seconds(8);
            }
            if (std::chrono::steady_clock::now() >= dialogue_end_deadline) {
                USER_LOG_WARN("Dialogue ended without TTS audio.");
                app->eventQueue_.Enqueue(
                    static_cast<int>(AppEvent::reply_failed));
                break;
            }
        }
        long long current_generation = timeout_generation_.load();
        if (current_generation != observed_generation) {
            observed_generation = current_generation;
            deadline = std::chrono::steady_clock::now()
                     + std::chrono::seconds(25);
        }
        if (std::chrono::steady_clock::now() >= deadline) {
            USER_LOG_WARN("Thinking timeout after 25 s without server progress.");
            app->eventQueue_.Enqueue(static_cast<int>(AppEvent::fault_happen));
            break;
        }
    }
}

void ThinkingState::RefreshTimeout(Application* app) {
    timeout_generation_.fetch_add(1);
    USER_LOG_INFO("Thinking timeout refreshed by server progress.");
}

void ThinkingState::Exit(Application* app) {
    state_running_.store(false);
    if (state_running_thread_.joinable()) {
        state_running_thread_.join();
    }
    USER_LOG_INFO("thinking state exit.");
}
