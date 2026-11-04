#ifndef THINKING_STATE_H
#define THINKING_STATE_H

#include "../../Application/Application.h"
#include <vector>
#include <thread>
#include <atomic>

class ThinkingState {
public:
    static void Enter(Application* app);
    static void Exit(Application* app);
    static void RefreshTimeout(Application* app);

private:
    static void Run(Application* app);
    static std::atomic<bool> state_running_;
    static std::thread state_running_thread_;
    static std::atomic<long long> timeout_generation_;
};

#endif // THINKING_STATE_H
