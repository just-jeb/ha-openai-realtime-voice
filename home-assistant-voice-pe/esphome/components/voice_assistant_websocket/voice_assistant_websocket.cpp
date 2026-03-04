#include "voice_assistant_websocket.h"
#include "esphome/core/log.h"
#include "esphome/core/helpers.h"
#include "esphome/components/audio/audio.h"
#include "esphome/core/hal.h"
#include <cstring>
#include <algorithm>
#include <queue>

#ifdef USE_ESP_IDF
#include "esp_system.h"
#endif

static const char *TAG = "voice_assistant_websocket";

namespace esphome {
namespace voice_assistant_websocket {

void VoiceAssistantWebSocket::websocket_cleanup_task_(void *arg) {
  uint32_t t0 = xTaskGetTickCount();
  ESP_LOGI(TAG, "Background WS cleanup started");
  esp_websocket_client_handle_t handle = static_cast<esp_websocket_client_handle_t>(arg);
  if (esp_websocket_client_is_connected(handle)) {
    esp_websocket_client_close(handle, pdMS_TO_TICKS(1000));
  }
  esp_websocket_client_stop(handle);
  esp_websocket_client_destroy(handle);
  uint32_t elapsed = (xTaskGetTickCount() - t0) * portTICK_PERIOD_MS;
  ESP_LOGI(TAG, "[diag] cleanup finished in %ums", (unsigned) elapsed);
  ESP_LOGI(TAG, "Background WS cleanup finished");
  vTaskDelete(nullptr);
}

void VoiceAssistantWebSocket::setup() {
  ESP_LOGCONFIG(TAG, "Setting up Voice Assistant WebSocket...");
  this->input_buffer_.reserve(INPUT_BUFFER_SIZE);
  this->output_buffer_.reserve(4096);
  this->mono_buffer_.reserve(INPUT_BUFFER_SIZE / 2);
  this->resampled_buffer_.reserve(INPUT_BUFFER_SIZE * 3 / 2);
  this->output_stereo_buffer_.reserve(4096 * 2);
  this->state_ = VOICE_ASSISTANT_WEBSOCKET_IDLE;

  if (this->microphone_ != nullptr) {
    this->microphone_->add_data_callback([this](const std::vector<uint8_t> &data) {
      this->on_microphone_data_(data);
    });
  }
}

void VoiceAssistantWebSocket::loop() {
  // Drain deferred triggers (set by WS event handler in library task; run in main loop)
  uint32_t triggers = this->pending_triggers_;
  this->pending_triggers_ = 0;
  const char *stop_reason = this->pending_stop_reason_;
  this->pending_stop_reason_ = nullptr;

  if (triggers & TRIGGER_CONNECTED) {
    if (this->state_callback_) {
      this->state_callback_(this->state_);
    }
    this->connected_trigger_.trigger();
  }
  if (triggers & TRIGGER_READY) {
    if (this->state_callback_) {
      this->state_callback_(this->state_);
    }
    this->ready_trigger_.trigger();
  }
  if (triggers & TRIGGER_THINKING) {
    this->thinking_trigger_.trigger();
  }
  if (triggers & TRIGGER_REPLYING) {
    this->replying_trigger_.trigger();
  }
  if (triggers & TRIGGER_LISTENING) {
    this->listening_trigger_.trigger();
  }
  if (triggers & TRIGGER_SEARCHING) {
    this->searching_trigger_.trigger();
  }
  if (triggers & TRIGGER_DISCONNECTED) {
    this->disconnected_trigger_.trigger();
    this->stop(stop_reason ? stop_reason : "ws_disconnected");
  }
  if (triggers & TRIGGER_ERROR) {
    this->error_trigger_.trigger();
    this->stop(stop_reason ? stop_reason : "ws_error");
  }
  if (triggers & TRIGGER_STOPPED) {
    this->stop(stop_reason ? stop_reason : "action");
  }

  if (this->speaker_ != nullptr && this->speaker_->is_running() && !this->audio_queue_.empty()) {
    const std::vector<uint8_t> &queued_data = this->audio_queue_.front();
    size_t queued_written = this->speaker_->play(queued_data.data(), queued_data.size());

    if (queued_written == queued_data.size()) {
      this->audio_queue_.pop();
      ESP_LOGD(TAG, "Sent queued audio chunk from loop (%zu bytes)", queued_data.size());
    } else if (queued_written > 0) {
      if (queued_written < queued_data.size()) {
        std::vector<uint8_t> remainder(queued_data.begin() + queued_written, queued_data.end());
        this->audio_queue_.pop();
        this->audio_queue_.push(remainder);
      } else {
        this->audio_queue_.pop();
      }
    }
  }

  if (this->pending_start_ && this->state_ == VOICE_ASSISTANT_WEBSOCKET_IDLE) {
    this->pending_start_ = false;
    this->start();
  }

  if (this->state_ == VOICE_ASSISTANT_WEBSOCKET_STARTING) {
    if (this->starting_millis_ > 0) {
      uint32_t elapsed = millis() - this->starting_millis_;
      if (elapsed > READY_TIMEOUT_MS) {
        ESP_LOGW(TAG, "Ready timeout (%u ms), stopping", (unsigned) READY_TIMEOUT_MS);
        this->stop("ready_timeout");
      }
    }
  }

  if (this->state_ == VOICE_ASSISTANT_WEBSOCKET_RUNNING) {
    uint32_t current_time = millis();
    uint32_t time_since_speaker_audio = current_time - this->last_speaker_audio_time_;
    uint32_t threshold_ms = this->searching_phase_active_ ? AUTO_STOP_SEARCHING_MS
                                                         : this->auto_stop_inactivity_ms_;

    if (this->last_speaker_audio_time_ > 0 &&
        time_since_speaker_audio > threshold_ms) {
      ESP_LOGI(TAG, "Auto-stopping: Speaker inactive for %u ms (threshold: %u ms)",
               (unsigned) time_since_speaker_audio, (unsigned) threshold_ms);
      this->stop("auto_stop_timeout");
    }
  }
}

void VoiceAssistantWebSocket::dump_config() {
  ESP_LOGCONFIG(TAG, "Voice Assistant WebSocket:");
  ESP_LOGCONFIG(TAG, "  Server URL: %s", this->server_url_.c_str());
  ESP_LOGCONFIG(TAG, "  Auto-stop timeout: %u ms", (unsigned) this->auto_stop_inactivity_ms_);
  ESP_LOGCONFIG(TAG, "  Microphone Sample Rate: %u Hz", (unsigned) MICROPHONE_SAMPLE_RATE);
  ESP_LOGCONFIG(TAG, "  Input Sample Rate (after resampling): %u Hz", (unsigned) INPUT_SAMPLE_RATE);
  ESP_LOGCONFIG(TAG, "  Output Sample Rate: %u Hz", (unsigned) OUTPUT_SAMPLE_RATE);
  ESP_LOGCONFIG(TAG, "  Microphone: %s", this->microphone_ ? "Yes" : "No");
  ESP_LOGCONFIG(TAG, "  Speaker: %s", this->speaker_ ? "Yes" : "No");
  ESP_LOGCONFIG(TAG, "  Max Queue Size: %zu chunks", (size_t) MAX_QUEUE_SIZE);
}

void VoiceAssistantWebSocket::start() {
  if (this->state_ == VOICE_ASSISTANT_WEBSOCKET_RUNNING) {
    ESP_LOGW(TAG, "Already running");
    return;
  }

  if (this->state_ != VOICE_ASSISTANT_WEBSOCKET_IDLE && this->websocket_client_ != nullptr) {
    ESP_LOGW(TAG, "Connection in progress or stale handle, ignoring start");
    return;
  }

  this->connection_count_++;
  this->connect_millis_ = 0;
  this->audio_chunks_sent_ = 0;
  this->starting_millis_ = millis();
  ESP_LOGI(TAG, "[diag] start conn #%u heap=%u", (unsigned) this->connection_count_,
           (unsigned) esp_get_free_heap_size());

  ESP_LOGI(TAG, "Starting voice assistant...");
  this->state_ = VOICE_ASSISTANT_WEBSOCKET_STARTING;
  this->last_speaker_audio_time_ = 0;
  this->searching_phase_active_ = false;
  this->interrupt_time_ = 0;
  this->first_audio_sent_ = false;
  this->first_audio_received_ = false;
  this->was_bot_speaking_ = false;

  if (this->microphone_ != nullptr) {
    if (this->microphone_->is_stopped()) {
      this->microphone_->start();
    }
  }

  if (this->speaker_ != nullptr) {
    audio::AudioStreamInfo input_stream_info(16, 1, 24000);
    this->speaker_->set_audio_stream_info(input_stream_info);
    if (this->speaker_->is_stopped()) {
      this->speaker_->start();
    }
  }

  if (this->state_callback_) {
    this->state_callback_(this->state_);
  }

  this->connect_websocket_();
}

void VoiceAssistantWebSocket::stop(const char *reason) {
  if (this->state_ == VOICE_ASSISTANT_WEBSOCKET_IDLE) {
    return;
  }

  ESP_LOGI(TAG, "stop() called, reason: %s, was in state: %d", reason, this->state_);
  this->state_ = VOICE_ASSISTANT_WEBSOCKET_IDLE;
  this->searching_phase_active_ = false;

  if (this->speaker_ != nullptr) {
    this->speaker_->stop();
  }
  while (!this->audio_queue_.empty()) {
    this->audio_queue_.pop();
  }
  this->input_buffer_.clear();
  this->output_buffer_.clear();

  if (this->websocket_client_ != nullptr) {
    esp_websocket_client_handle_t handle = this->websocket_client_;
    this->websocket_client_ = nullptr;
    BaseType_t created =
        xTaskCreate(websocket_cleanup_task_, "ws_cleanup", 4096, handle, 1, nullptr);
    if (created != pdPASS) {
      ESP_LOGW(TAG, "Failed to create cleanup task, doing synchronous cleanup");
      if (esp_websocket_client_is_connected(handle)) {
        esp_websocket_client_close(handle, pdMS_TO_TICKS(1000));
      }
      esp_websocket_client_stop(handle);
      esp_websocket_client_destroy(handle);
      ESP_LOGI(TAG, "Synchronous WS cleanup finished");
    }
  }

  if (this->state_callback_) {
    this->state_callback_(this->state_);
  }

  this->stopped_trigger_.trigger();
  ESP_LOGI(TAG, "Voice assistant stopped, state: IDLE");
}

void VoiceAssistantWebSocket::request_start() {
  this->pending_start_ = true;
}

void VoiceAssistantWebSocket::connect_websocket_() {
  if (this->websocket_client_ != nullptr) {
    ESP_LOGW(TAG, "WebSocket client already exists, cannot start new connection");
    return;
  }

  if (this->server_url_.empty()) {
    ESP_LOGE(TAG, "Server URL not set!");
    this->state_ = VOICE_ASSISTANT_WEBSOCKET_IDLE;
    if (this->state_callback_) {
      this->state_callback_(this->state_);
    }
    return;
  }

  ESP_LOGI(TAG, "Connecting to WebSocket server: %s", this->server_url_.c_str());

  esp_websocket_client_config_t websocket_cfg = {};
  websocket_cfg.uri = this->server_url_.c_str();
  websocket_cfg.user_context = this;
  websocket_cfg.buffer_size = 4096;
  websocket_cfg.task_prio = 5;
  websocket_cfg.task_stack = 8192;
  websocket_cfg.transport = WEBSOCKET_TRANSPORT_OVER_TCP;
  websocket_cfg.network_timeout_ms = 30000;
  websocket_cfg.reconnect_timeout_ms = 0;  // Disable auto-reconnect; we use wake word for new session
  websocket_cfg.ping_interval_sec = 20;
  websocket_cfg.pingpong_timeout_sec = 10;

  this->websocket_client_ = esp_websocket_client_init(&websocket_cfg);
  if (this->websocket_client_ == nullptr) {
    ESP_LOGE(TAG, "Failed to initialize WebSocket client");
    this->state_ = VOICE_ASSISTANT_WEBSOCKET_IDLE;
    if (this->state_callback_) {
      this->state_callback_(this->state_);
    }
    return;
  }

  esp_websocket_register_events(this->websocket_client_,
                               (esp_websocket_event_id_t) WEBSOCKET_EVENT_ANY,
                               websocket_event_handler_,
                               this);

  esp_err_t err = esp_websocket_client_start(this->websocket_client_);
  if (err != ESP_OK) {
    ESP_LOGE(TAG, "Failed to start WebSocket client: %s", esp_err_to_name(err));
    esp_websocket_client_destroy(this->websocket_client_);
    this->websocket_client_ = nullptr;
    this->state_ = VOICE_ASSISTANT_WEBSOCKET_IDLE;
    if (this->state_callback_) {
      this->state_callback_(this->state_);
    }
  }
}

void VoiceAssistantWebSocket::send_audio_chunk_(const uint8_t *data, size_t len) {
  if (this->websocket_client_ == nullptr || !esp_websocket_client_is_connected(this->websocket_client_)) {
    return;
  }

  int sent = esp_websocket_client_send_bin(this->websocket_client_,
                                          (const char *) data,
                                          len,
                                          pdMS_TO_TICKS(SEND_AUDIO_TIMEOUT_MS));
  this->audio_chunks_sent_++;
  if (this->audio_chunks_sent_ <= 3) {
    ESP_LOGI(TAG, "[diag] send #%u ret=%d +%ums",
             (unsigned) this->audio_chunks_sent_, sent,
             this->connect_millis_ ? (unsigned) (millis() - this->connect_millis_) : 0u);
  }
  if (sent < 0) {
    ESP_LOGW(TAG, "Send timeout (%ums), dropping audio chunk", (unsigned) SEND_AUDIO_TIMEOUT_MS);
    return;
  }
  if (!this->first_audio_sent_) {
    this->first_audio_sent_ = true;
    ESP_LOGI(TAG, "First audio chunk sent to server (%zu bytes)", len);
  }
}

void VoiceAssistantWebSocket::process_received_audio_(const uint8_t *data, size_t len) {
  if (this->speaker_ == nullptr) {
    return;
  }
  if (this->state_ != VOICE_ASSISTANT_WEBSOCKET_RUNNING) {
    return;
  }

  if (this->interrupt_time_ > 0) {
    uint32_t time_since_interrupt = millis() - this->interrupt_time_;
    if (time_since_interrupt < INTERRUPT_IGNORE_AUDIO_MS) {
      return;
    }
    this->interrupt_time_ = 0;
    ESP_LOGI(TAG, "Resuming audio processing after interrupt");
  }

  if (!this->first_audio_received_) {
    this->first_audio_received_ = true;
    ESP_LOGI(TAG, "First audio chunk received from server (%zu bytes)", len);
  }

  this->last_speaker_audio_time_ = millis();

  if (this->speaker_->is_stopped()) {
    this->speaker_->start();
  }

  while (!this->audio_queue_.empty()) {
    const std::vector<uint8_t> &queued_data = this->audio_queue_.front();
    size_t queued_written = this->speaker_->play(queued_data.data(), queued_data.size());

    if (queued_written == queued_data.size()) {
      this->audio_queue_.pop();
    } else if (queued_written > 0) {
      if (queued_written < queued_data.size()) {
#ifdef USE_ESP_IDF
        if (esp_get_free_heap_size() < MIN_FREE_HEAP_BYTES) {
          this->audio_queue_.pop();
          break;
        }
#endif
        std::vector<uint8_t> remainder(queued_data.begin() + queued_written, queued_data.end());
        this->audio_queue_.pop();
        this->audio_queue_.push(remainder);
      } else {
        this->audio_queue_.pop();
      }
      break;
    } else {
      break;
    }
  }

  size_t bytes_written = this->speaker_->play(data, len);

  if (bytes_written == 0 && len > 0) {
#ifdef USE_ESP_IDF
    if (esp_get_free_heap_size() < MIN_FREE_HEAP_BYTES) {
      return;
    }
#endif
    if (this->audio_queue_.size() >= MAX_QUEUE_SIZE) {
      ESP_LOGW(TAG, "Audio queue at max size (%zu), dropping chunk", (size_t) MAX_QUEUE_SIZE);
      return;
    }
    this->audio_queue_.push(std::vector<uint8_t>(data, data + len));
  } else if (bytes_written < len) {
#ifdef USE_ESP_IDF
    if (esp_get_free_heap_size() < MIN_FREE_HEAP_BYTES) {
      return;
    }
#endif
    if (this->audio_queue_.size() >= MAX_QUEUE_SIZE) {
      return;
    }
    this->audio_queue_.push(std::vector<uint8_t>(data + bytes_written, data + len));
  }
}

void VoiceAssistantWebSocket::on_microphone_data_(const std::vector<uint8_t> &data) {
  if (this->websocket_client_ == nullptr || this->state_ != VOICE_ASSISTANT_WEBSOCKET_RUNNING) {
    return;
  }
  if (!esp_websocket_client_is_connected(this->websocket_client_)) {
    return;
  }
  if (this->is_bot_speaking()) {
    this->was_bot_speaking_ = true;
    return;
  }
  if (this->was_bot_speaking_) {
    this->was_bot_speaking_ = false;
    ESP_LOGI(TAG, "Mic unmuted, resuming audio send to server");
  }

  size_t stereo_32bit_samples = data.size() / (4 * 2);
  size_t mono_16khz_samples = stereo_32bit_samples;

  if (this->mono_buffer_.size() < mono_16khz_samples) {
    this->mono_buffer_.resize(mono_16khz_samples);
  }

  const int32_t *stereo_32bit = reinterpret_cast<const int32_t *>(data.data());
  int16_t *mono_16bit = this->mono_buffer_.data();

  for (size_t i = 0; i < stereo_32bit_samples; i++) {
    int32_t left_sample = stereo_32bit[i * 2];
    mono_16bit[i] = static_cast<int16_t>((left_sample >> 16));
  }

  size_t resampled_24khz_samples =
      (mono_16khz_samples * INPUT_SAMPLE_RATE) / MICROPHONE_SAMPLE_RATE;
  if (this->resampled_buffer_.size() < resampled_24khz_samples) {
    this->resampled_buffer_.resize(resampled_24khz_samples);
  }

  int16_t *resampled_24khz = this->resampled_buffer_.data();

  for (size_t i = 0; i < resampled_24khz_samples; i++) {
    float source_pos = (float) i * (float) MICROPHONE_SAMPLE_RATE / (float) INPUT_SAMPLE_RATE;
    size_t source_idx = (size_t) source_pos;
    float fraction = source_pos - source_idx;

    if (source_idx + 1 < mono_16khz_samples) {
      int16_t sample0 = mono_16bit[source_idx];
      int16_t sample1 = mono_16bit[source_idx + 1];
      resampled_24khz[i] = static_cast<int16_t>(sample0 + (sample1 - sample0) * fraction);
    } else if (source_idx < mono_16khz_samples) {
      resampled_24khz[i] = mono_16bit[source_idx];
    } else {
      resampled_24khz[i] = mono_16bit[mono_16khz_samples - 1];
    }
  }

  size_t resampled_bytes = resampled_24khz_samples * BYTES_PER_SAMPLE;
  this->send_audio_chunk_(reinterpret_cast<const uint8_t *>(resampled_24khz), resampled_bytes);
}

bool VoiceAssistantWebSocket::is_bot_speaking() const {
  if (this->last_speaker_audio_time_ == 0) {
    return false;
  }
  uint32_t time_since_last_audio = millis() - this->last_speaker_audio_time_;
  return time_since_last_audio < 500;
}

void VoiceAssistantWebSocket::interrupt() {
  if (this->websocket_client_ == nullptr ||
      !esp_websocket_client_is_connected(this->websocket_client_)) {
    ESP_LOGW(TAG, "Cannot send interrupt - not connected");
    return;
  }

  ESP_LOGI(TAG, "Sending interrupt message to server");
  const char *interrupt_msg = "{\"type\":\"interrupt\"}";
  int sent = esp_websocket_client_send_text(this->websocket_client_,
                                            interrupt_msg,
                                            strlen(interrupt_msg),
                                            pdMS_TO_TICKS(SEND_INTERRUPT_TIMEOUT_MS));

  if (sent < 0) {
    ESP_LOGW(TAG, "Interrupt send timeout (%ums)", (unsigned) SEND_INTERRUPT_TIMEOUT_MS);
  }
  if (this->speaker_ != nullptr) {
    this->speaker_->stop();
  }
  while (!this->audio_queue_.empty()) {
    this->audio_queue_.pop();
  }
  this->interrupt_time_ = millis();
  ESP_LOGI(TAG, "Cleared audio queue and ignoring incoming audio for %u ms",
           (unsigned) INTERRUPT_IGNORE_AUDIO_MS);
}

void VoiceAssistantWebSocket::websocket_event_handler_(void *handler_args,
                                                        esp_event_base_t base,
                                                        int32_t event_id,
                                                        void *event_data) {
  VoiceAssistantWebSocket *instance = static_cast<VoiceAssistantWebSocket *>(handler_args);
  instance->handle_websocket_event_((esp_websocket_event_id_t) event_id,
                                   (esp_websocket_event_data_t *) event_data);
}

void VoiceAssistantWebSocket::handle_websocket_event_(esp_websocket_event_id_t event_id,
                                                      esp_websocket_event_data_t *event_data) {
  // Diagnostic: log non-audio events so we can see ordering (skip binary audio to avoid spam)
  if (event_id != WEBSOCKET_EVENT_DATA || event_data == nullptr ||
      event_data->op_code != 0x02) {
    ESP_LOGI(TAG, "[diag] WS event id=%d state=%d",
             (int) event_id, (int) this->state_);
  }

  switch (event_id) {
    case WEBSOCKET_EVENT_BEFORE_CONNECT:
      ESP_LOGI(TAG, "WebSocket connection attempt starting...");
      break;

    case WEBSOCKET_EVENT_CONNECTED:
      this->connect_millis_ = millis();
      ESP_LOGI(TAG, "WebSocket connected, state: STARTING (waiting for ready)");
      ESP_LOGI(TAG, "[diag] connected conn #%u heap=%u",
               (unsigned) this->connection_count_,
               (unsigned) esp_get_free_heap_size());
      this->state_ = VOICE_ASSISTANT_WEBSOCKET_STARTING;
      this->pending_triggers_ |= TRIGGER_CONNECTED;
      break;

    case WEBSOCKET_EVENT_DISCONNECTED:
      ESP_LOGW(TAG, "WebSocket disconnected");
      this->pending_stop_reason_ = "ws_disconnected";
      this->pending_triggers_ |= TRIGGER_DISCONNECTED;
      break;

    case WEBSOCKET_EVENT_DATA:
      if (event_data->op_code == 0x02) {
        this->process_received_audio_(reinterpret_cast<const uint8_t *>(event_data->data_ptr),
                                     event_data->data_len);
      } else if (event_data->data_len > 0 && event_data->data_ptr != nullptr) {
        ESP_LOGI(TAG, "WS text event: op=0x%02x len=%d off=%d total=%d",
                 event_data->op_code, event_data->data_len,
                 event_data->payload_offset, event_data->payload_len);
        std::string message((const char *) event_data->data_ptr, event_data->data_len);
        if (message.find("\"type\":\"ready\"") != std::string::npos ||
            message.find("\"type\": \"ready\"") != std::string::npos) {
          if (this->state_ == VOICE_ASSISTANT_WEBSOCKET_STARTING) {
            this->state_ = VOICE_ASSISTANT_WEBSOCKET_RUNNING;
            this->pending_triggers_ |= TRIGGER_READY;
            ESP_LOGI(TAG, "Ready received, state: RUNNING");
          }
        } else if (message.find("\"type\":\"phase\"") != std::string::npos ||
                   message.find("\"type\": \"phase\"") != std::string::npos) {
          if (message.find("\"phase\":\"thinking\"") != std::string::npos ||
              message.find("\"phase\": \"thinking\"") != std::string::npos) {
            this->pending_triggers_ |= TRIGGER_THINKING;
            ESP_LOGI(TAG, "Phase: thinking");
          } else if (message.find("\"phase\":\"replying\"") != std::string::npos ||
                     message.find("\"phase\": \"replying\"") != std::string::npos) {
            this->searching_phase_active_ = false;
            this->last_speaker_audio_time_ = millis();
            this->pending_triggers_ |= TRIGGER_REPLYING;
            ESP_LOGI(TAG, "Phase: replying, searching_active=false, auto_stop_threshold=%u ms",
                     (unsigned) this->auto_stop_inactivity_ms_);
          } else if (message.find("\"phase\":\"listening\"") != std::string::npos ||
                     message.find("\"phase\": \"listening\"") != std::string::npos) {
            this->searching_phase_active_ = false;
            this->pending_triggers_ |= TRIGGER_LISTENING;
            ESP_LOGI(TAG, "Phase: listening, searching_active=false, auto_stop_threshold=%u ms",
                     (unsigned) this->auto_stop_inactivity_ms_);
          } else if (message.find("\"phase\":\"searching\"") != std::string::npos ||
                     message.find("\"phase\": \"searching\"") != std::string::npos) {
            this->searching_phase_active_ = true;
            this->last_speaker_audio_time_ = millis();
            this->pending_triggers_ |= TRIGGER_SEARCHING;
            ESP_LOGI(TAG, "Phase: searching, searching_active=true, auto_stop_threshold=%u ms",
                     (unsigned) AUTO_STOP_SEARCHING_MS);
          }
        } else if (message.find("\"type\":\"interrupt\"") != std::string::npos ||
                   message.find("\"type\": \"interrupt\"") != std::string::npos) {
          if (this->speaker_ != nullptr) {
            this->speaker_->stop();
          }
        } else if (message.find("\"type\":\"disconnect\"") != std::string::npos ||
                   message.find("\"type\": \"disconnect\"") != std::string::npos) {
          ESP_LOGI(TAG, "Disconnect message received from server");
          this->pending_stop_reason_ = "disconnect_message";
          this->pending_triggers_ |= TRIGGER_STOPPED;
        } else {
          ESP_LOGW(TAG, "Unknown text message len=%zu: %.80s",
                   (size_t) message.size(), message.c_str());
        }
      }
      break;

    case WEBSOCKET_EVENT_ERROR:
      if (event_data != nullptr) {
        ESP_LOGE(TAG, "WebSocket error - Type: %d, Socket errno: %d",
                 event_data->error_handle.error_type,
                 event_data->error_handle.esp_transport_sock_errno);
        ESP_LOGE(TAG, "[diag] error conn #%u +%ums type=%d errno=%d chunks_sent=%u heap=%u",
                 (unsigned) this->connection_count_,
                 this->connect_millis_ ? (unsigned) (millis() - this->connect_millis_) : 0u,
                 event_data->error_handle.error_type,
                 event_data->error_handle.esp_transport_sock_errno,
                 (unsigned) this->audio_chunks_sent_,
                 (unsigned) esp_get_free_heap_size());
      } else {
        ESP_LOGE(TAG, "WebSocket error (no event data)");
      }
      this->pending_stop_reason_ = "ws_error";
      this->pending_triggers_ |= TRIGGER_ERROR;
      break;

    default:
      break;
  }
}

}  // namespace voice_assistant_websocket
}  // namespace esphome
