#include "Listening.h"
#include "../../Utils/user_log.h"
#include "../../Events/AppEvents.h"
#include <chrono>

// 静态成员变量定义
std::atomic<bool> ListeningState::state_running_{false};
std::thread ListeningState::state_running_thread_;

void ListeningState::Enter(Application* app) {
    std::string json_message = R"({"type": "state", "state": "listening"})";
    app->ws_client_.SendText(json_message);
    // clear首次接收server音频消息
    app->set_first_audio_msg_received(true);
    app->set_tts_completed(false);
    app->set_tts_error(false);
    app->set_dialogue_completed(false);
    app->audio_processor_.clearRecordedAudioQueue();
    // start录音
    app->audio_processor_.startRecording();
    // running
    state_running_.store(true);
    state_running_thread_ = std::thread([app]() { Run(app); });
    USER_LOG_INFO("Into listening state.");
}

void ListeningState::Run(Application* app) {
    const std::string turn_id = app->get_active_turn_id();
    auto phase_started = std::chrono::steady_clock::now();
    auto prepare_deadline = phase_started + std::chrono::seconds(8);
    AsrPhase last_phase = app->get_asr_phase(turn_id);
    bool ready_logged = false;
    while (state_running_.load() == true) {
        auto now = std::chrono::steady_clock::now();
        AsrPhase phase = app->get_asr_phase(turn_id);
        if (phase == AsrPhase::terminal || phase == AsrPhase::none) {
            break;
        }
        if (phase != last_phase) {
            last_phase = phase;
            phase_started = now;
        }
        if (phase == AsrPhase::preparing && now >= prepare_deadline) {
            if (app->mark_asr_terminal(turn_id)) {
                USER_LOG_ERROR("ASR ready timeout after 8 s.");
                app->eventQueue_.Enqueue(
                    static_cast<int>(AppEvent::asr_timeout));
            }
            break;
        }
        if (
            phase == AsrPhase::capturing
            && now - phase_started > std::chrono::seconds(10)
        ) {
            if (app->mark_asr_terminal(turn_id)) {
                USER_LOG_ERROR("ASR produced no progress for 10 s.");
                app->eventQueue_.Enqueue(
                    static_cast<int>(AppEvent::asr_timeout));
            }
            break;
        }
        if (
            phase == AsrPhase::awaiting_result
            && now - phase_started > std::chrono::seconds(15)
        ) {
            if (app->mark_asr_terminal(turn_id)) {
                USER_LOG_ERROR("ASR result timeout after 15 s.");
                app->eventQueue_.Enqueue(
                    static_cast<int>(AppEvent::asr_timeout));
            }
            break;
        }
        if (!app->get_asr_ready()) {
            std::this_thread::sleep_for(std::chrono::milliseconds(20));
            continue;
        }
        if (!ready_logged) {
            ready_logged = true;
            USER_LOG_INFO("ASR ready; sending recorded audio.");
        }

        std::vector<int16_t> audio_frame;
        if(app->audio_processor_.recordedQueueIsEmpty() == false) {
            if(app->audio_processor_.getRecordedAudio(audio_frame)) {
                // 编码
                uint8_t opus_data[1536];
                size_t opus_data_size;
                if (app->audio_processor_.encode(audio_frame, opus_data, opus_data_size)) {
                    // 打包
                    BinProtocol* packed_frame = app->audio_processor_.PackBinFrame(opus_data, opus_data_size, app->get_ws_protocolVersion());
                    if (packed_frame) {
                        // 发送
                        app->ws_client_.SendBinary(reinterpret_cast<uint8_t*>(packed_frame), sizeof(BinProtocol) + opus_data_size);
                    } else {
                        USER_LOG_WARN("Audio Packing failed");
                    }
                } else {
                    USER_LOG_WARN("Audio Encoding failed");
                }
            }
        } else {
            std::this_thread::sleep_for(std::chrono::milliseconds(5));
        }
    }
}

void ListeningState::Exit(Application* app) {
    // stop录音
    app->audio_processor_.stopRecording();
    // stop running
    state_running_.store(false);
    state_running_thread_.join();
    USER_LOG_INFO("Listening exit.");
}
