// FaultState.cc
#include "Fault.h"
#include "../../Utils/user_log.h"
#include "../../Application/Application.h"
#include "../../Events/AppEvents.h"

void FaultState::Enter(Application* app) {
    USER_LOG_WARN("Into fault state.");
    if (!app->ws_client_.IsConnected()) {
        USER_LOG_WARN("fault: not connect to server");
        // A disconnect must release playback immediately. Otherwise the
        // Speaking thread waits for its 40-second safety timeout.
        app->audio_processor_.clearPlaybackAudioQueue();
        app->audio_processor_.stopPlaying();
        app->eventQueue_.Enqueue(static_cast<int>(AppEvent::to_stop));
    }
    else {
        app->eventQueue_.Enqueue(static_cast<int>(AppEvent::fault_solved));
    }
}

void FaultState::Exit(Application* app) {
    USER_LOG_WARN("Fault exit.");
}