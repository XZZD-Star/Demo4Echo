#include "Thinking.h"
#include "../../Utils/user_log.h"
#include "../../Events/AppEvents.h"
#include <chrono>

std::atomic<bool> ThinkingState::state_running_{false};
std::thread ThinkingState::state_running_thread_;

void ThinkingState::Enter(Application* app) {
    std::string json_message = R"({"type": "state", "state": "thinking"})";
    app->ws_client_.SendText(json_message);
    state_running_.store(true);
    state_running_thread_ = std::thread([app]() { Run(app); });
    USER_LOG_INFO("Into thinking state.");
}

void ThinkingState::Run(Application* app) {
    const auto started = std::chrono::steady_clock::now();
    while (state_running_.load()) {
        std::this_thread::sleep_for(std::chrono::milliseconds(500));
        auto elapsed = std::chrono::duration_cast<std::chrono::seconds>(
            std::chrono::steady_clock::now() - started).count();
        if (elapsed >= 12) {
            USER_LOG_WARN("Thinking timeout after %lld s.", (long long)elapsed);
            app->eventQueue_.Enqueue(static_cast<int>(AppEvent::fault_happen));
            break;
        }
    }
}

void ThinkingState::Exit(Application* app) {
    state_running_.store(false);
    if (state_running_thread_.joinable()) {
        state_running_thread_.join();
    }
    USER_LOG_INFO("thinking state exit.");
}
