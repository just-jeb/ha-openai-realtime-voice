#pragma once

#include "esphome.h"
#include "esphome/components/microphone/microphone.h"
#include "esphome/components/speaker/speaker.h"
#include "esphome/core/automation.h"
#ifdef USE_ESP_IDF
#include "esp_websocket_client.h"
#include "esp_http_client.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_system.h"
#endif
#include <string>
#include <vector>
#include <queue>

namespace esphome {
namespace voice_assistant_websocket {

enum VoiceAssistantWebSocketState {
  VOICE_ASSISTANT_WEBSOCKET_IDLE = 0,
  VOICE_ASSISTANT_WEBSOCKET_STARTING,
  VOICE_ASSISTANT_WEBSOCKET_RUNNING,
};

class VoiceAssistantWebSocket : public Component {
 public:
  void setup() override;
  void loop() override;
  void dump_config() override;

  void set_server_url(const std::string &url) { this->server_url_ = url; }
  void set_microphone(microphone::Microphone *mic) { this->microphone_ = mic; }
  void set_speaker(speaker::Speaker *spkr) { this->speaker_ = spkr; }
  void set_auto_stop_timeout(uint32_t ms) { this->auto_stop_inactivity_ms_ = ms; }

  void start();
  void stop(const char *reason = "action");
  void request_start();
  void interrupt();

  bool is_running() const { return this->state_ != VOICE_ASSISTANT_WEBSOCKET_IDLE; }
  bool is_connected() const {
    return this->websocket_client_ != nullptr &&
           esp_websocket_client_is_connected(this->websocket_client_);
  }
  bool is_bot_speaking() const;

  void set_state_callback(std::function<void(VoiceAssistantWebSocketState)> &&callback) {
    this->state_callback_ = std::move(callback);
  }

  Trigger<> *get_connected_trigger() { return &this->connected_trigger_; }
  Trigger<> *get_disconnected_trigger() { return &this->disconnected_trigger_; }
  Trigger<> *get_error_trigger() { return &this->error_trigger_; }
  Trigger<> *get_stopped_trigger() { return &this->stopped_trigger_; }
  Trigger<> *get_ready_trigger() { return &this->ready_trigger_; }
  Trigger<> *get_thinking_trigger() { return &this->thinking_trigger_; }
  Trigger<> *get_replying_trigger() { return &this->replying_trigger_; }
  Trigger<> *get_listening_trigger() { return &this->listening_trigger_; }
  Trigger<> *get_searching_trigger() { return &this->searching_trigger_; }

  protected:
  void connect_websocket_();
  void send_audio_chunk_(const uint8_t *data, size_t len);
  void process_received_audio_(const uint8_t *data, size_t len);
  void on_microphone_data_(const std::vector<uint8_t> &data);
  static void websocket_event_handler_(void *handler_args, esp_event_base_t base, int32_t event_id,
                                       void *event_data);
  void handle_websocket_event_(esp_websocket_event_id_t event_id,
                               esp_websocket_event_data_t *event_data);
  static void websocket_cleanup_task_(void *arg);

  std::string server_url_;
  microphone::Microphone *microphone_{nullptr};
  speaker::Speaker *speaker_{nullptr};

#ifdef USE_ESP_IDF
  esp_websocket_client_handle_t websocket_client_{nullptr};
#else
  void *websocket_client_{nullptr};
#endif
  VoiceAssistantWebSocketState state_{VOICE_ASSISTANT_WEBSOCKET_IDLE};

  std::function<void(VoiceAssistantWebSocketState)> state_callback_;

  Trigger<> connected_trigger_{};
  Trigger<> disconnected_trigger_{};
  Trigger<> error_trigger_{};
  Trigger<> stopped_trigger_{};
  Trigger<> ready_trigger_{};
  Trigger<> thinking_trigger_{};
  Trigger<> replying_trigger_{};
  Trigger<> listening_trigger_{};
  Trigger<> searching_trigger_{};

  // Deferred triggers: set in WS event handler (library task), drained in loop() (main loop)
  static const uint32_t TRIGGER_CONNECTED = 1 << 0;
  static const uint32_t TRIGGER_READY = 1 << 1;
  static const uint32_t TRIGGER_THINKING = 1 << 2;
  static const uint32_t TRIGGER_REPLYING = 1 << 3;
  static const uint32_t TRIGGER_LISTENING = 1 << 4;
  static const uint32_t TRIGGER_SEARCHING = 1 << 5;
  static const uint32_t TRIGGER_DISCONNECTED = 1 << 6;
  static const uint32_t TRIGGER_ERROR = 1 << 7;
  static const uint32_t TRIGGER_STOPPED = 1 << 8;  // e.g. server sent disconnect message
  static const uint32_t PHASE_MASK = TRIGGER_THINKING | TRIGGER_REPLYING | TRIGGER_LISTENING | TRIGGER_SEARCHING;
  volatile uint32_t pending_triggers_{0};
  const char *pending_stop_reason_{nullptr};

  bool searching_phase_active_{false};
  static const uint32_t AUTO_STOP_SEARCHING_MS = 60000;

  uint32_t starting_millis_{0};
  static const uint32_t READY_TIMEOUT_MS = 15000;
  static const uint8_t MAX_READY_RETRIES = 2;
  uint8_t ready_timeout_retries_{0};
  uint32_t last_ready_diag_millis_{0};  // throttle "waiting for ready" log to once per 3s

  std::vector<uint8_t> input_buffer_;
  std::vector<uint8_t> output_buffer_;

  std::queue<std::vector<uint8_t>> audio_queue_;
  static const size_t MAX_QUEUE_SIZE = 10;
  static const size_t MIN_FREE_HEAP_BYTES = 15000;

  static const uint32_t AUDIO_SEND_INTERVAL_MS = 100;
  static const uint32_t MICROPHONE_SAMPLE_RATE = 16000;
  static const uint32_t INPUT_SAMPLE_RATE = 24000;
  static const uint32_t OUTPUT_SAMPLE_RATE = 24000;
  static const uint32_t BYTES_PER_SAMPLE = 2;
  static const uint32_t INPUT_BUFFER_SIZE =
      (INPUT_SAMPLE_RATE * BYTES_PER_SAMPLE * AUDIO_SEND_INTERVAL_MS) / 1000;

  uint32_t last_speaker_audio_time_{0};
  uint32_t auto_stop_inactivity_ms_{20000};  // Configurable via YAML, default 20s

  std::vector<int16_t> mono_buffer_;
  std::vector<int16_t> resampled_buffer_;
  std::vector<uint8_t> output_stereo_buffer_;

  bool pending_start_{false};
  uint32_t interrupt_time_{0};
  static const uint32_t INTERRUPT_IGNORE_AUDIO_MS = 500;

  // Diagnostic logging
  bool first_audio_sent_{false};
  bool first_audio_received_{false};
  bool was_bot_speaking_{false};
  uint32_t connection_count_{0};
  uint32_t connect_millis_{0};
  uint32_t audio_chunks_sent_{0};

  static const uint32_t SEND_AUDIO_TIMEOUT_MS = 100;
  static const uint32_t SEND_INTERRUPT_TIMEOUT_MS = 200;
};

template <typename... Ts>
class VoiceAssistantWebSocketStartAction : public Action<Ts...> {
 public:
  VoiceAssistantWebSocketStartAction(VoiceAssistantWebSocket *parent) : parent_(parent) {}
  void play(const Ts &...x) override { this->parent_->start(); }

 protected:
  VoiceAssistantWebSocket *parent_;
};

template <typename... Ts>
class VoiceAssistantWebSocketStopAction : public Action<Ts...> {
 public:
  VoiceAssistantWebSocketStopAction(VoiceAssistantWebSocket *parent) : parent_(parent) {}
  void play(const Ts &...x) override { this->parent_->stop("action"); }

 protected:
  VoiceAssistantWebSocket *parent_;
};

template <typename... Ts>
class VoiceAssistantWebSocketIsRunningCondition : public Condition<Ts...> {
 public:
  VoiceAssistantWebSocketIsRunningCondition(VoiceAssistantWebSocket *parent) : parent_(parent) {}
  bool check(const Ts &...x) override { return this->parent_->is_running(); }

 protected:
  VoiceAssistantWebSocket *parent_;
};

template <typename... Ts>
class VoiceAssistantWebSocketIsConnectedCondition : public Condition<Ts...> {
 public:
  VoiceAssistantWebSocketIsConnectedCondition(VoiceAssistantWebSocket *parent) : parent_(parent) {}
  bool check(const Ts &...x) override { return this->parent_->is_connected(); }

 protected:
  VoiceAssistantWebSocket *parent_;
};

template <typename... Ts>
class VoiceAssistantWebSocketIsBotSpeakingCondition : public Condition<Ts...> {
 public:
  VoiceAssistantWebSocketIsBotSpeakingCondition(VoiceAssistantWebSocket *parent)
      : parent_(parent) {}
  bool check(const Ts &...x) override { return this->parent_->is_bot_speaking(); }

 protected:
  VoiceAssistantWebSocket *parent_;
};

template <typename... Ts>
class VoiceAssistantWebSocketInterruptAction : public Action<Ts...> {
 public:
  VoiceAssistantWebSocketInterruptAction(VoiceAssistantWebSocket *parent) : parent_(parent) {}
  void play(const Ts &...x) override { this->parent_->interrupt(); }

 protected:
  VoiceAssistantWebSocket *parent_;
};

}  // namespace voice_assistant_websocket
}  // namespace esphome
