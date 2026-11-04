#ifndef APP_EVENTS_H
#define APP_EVENTS_H

enum class AppEvent {
    fault_happen,
    fault_solved,
    startup_done,
    to_stop,
    wake_detected,
    vad_no_speech,
    vad_end,
    asr_received,
    asr_timeout,
    speaking_msg_received,
    speaking_end,
    reply_failed,
    dialogue_end,
    notify_received,
    notify_end,
    // Add more events here...
};

#endif // APP_EVENTS_H
