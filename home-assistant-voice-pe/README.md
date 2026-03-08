# OpenAI Realtime Voice — Client

ESPHome configuration for the Home Assistant Voice PE (ESP32-S3) to connect to the OpenAI Realtime addon. The device listens for a wake word, streams mic audio to the addon over WebSocket, and plays back the assistant’s voice. Used with the server addon in this repo for voice Q&A (and optional web search).

Based on [esphome/home-assistant-voice-pe](https://github.com/esphome/home-assistant-voice-pe).

## Prerequisites

- ESPHome 2025.11.0 or higher
- Voice PE Hardware (Home Assistant Voice Pod Edition) or compatible ESP32-S3 device
- Home Assistant with the OpenAI Realtime Addon installed and running
- Python 3.11+ with Poetry

## Installation

### 1. Install Dependencies

```bash
cd home-assistant-voice-pe
poetry install
```

### 2. Configure Secrets

Copy `secrets.yaml.example` to `secrets.yaml` and fill in your values:

```bash
cp secrets.yaml.example secrets.yaml
```

Edit `secrets.yaml`:
- `wifi_ssid`: Your WiFi network name
- `wifi_password`: Your WiFi password
- `api_encryption_key`: Home Assistant API encryption key
- `ota_password`: Password for OTA updates
- `server_url`: WebSocket URL for the OpenAI Realtime addon (e.g., `ws://homeassistant.local:8080`)
- `wifi_static_ip`: (Optional) Static IP for the device — recommended if the ESPHome dashboard shows the device as offline
- `wifi_gateway`: (Optional) Gateway IP (usually your router, e.g., `192.168.1.1`)
- `wifi_subnet`: (Optional) Subnet mask (usually `255.255.255.0`)

### 3. Compile and Flash

```bash
# Compile
poetry run esphome compile voice_pe_config.yaml

# Flash via USB
poetry run esphome upload voice_pe_config.yaml --device /dev/cu.usbmodem101 (or the correct device name for your device. See `ls /dev/cu.*` for the correct device name.)

# Or flash via OTA (after first USB upload)
poetry run esphome upload voice_pe_config.yaml
```

## Configuration

The main configuration file is `voice_pe_config.yaml`. Key settings:

- Device name: Change `esphome.name` if desired
- Wake words: Configured wake words ("Okay Nabu", "Hey Jarvis", "Hey Mycroft")
- Audio settings: Microphone and speaker configuration
- LED ring: Visual feedback for device states

## Features

- **Voice Assistant**: Real-time voice interaction with OpenAI Realtime API
- **Wake Word Detection**: Multiple wake words supported
- **LED Feedback**: Visual status indicators (green = ready, blue = thinking, cyan = replying, red-yellow = quota error)
- **Audio Feedback**: Chime on ready, cha-ching on quota error, optional wake sound
- **Hardware Controls**: Button controls and mute switch
- **Auto Gain Control**: Hardware-based AGC for consistent audio levels
- **Echo Cancellation**: Hardware-based AEC prevents feedback

## Sound files

Sound files (`wake_sound.flac`, `ready_sound.flac`, `error_quota_sound.flac`) are fetched automatically from GitHub at compile time and embedded in firmware. **No manual file copying is needed.**

To use a custom sound, override the substitution in `voice_pe_config.yaml`:

```yaml
substitutions:
  wake_word_triggered_sound_file: /config/esphome/my_custom_wake.flac
  # or a URL:
  ready_sound_file: https://example.com/my_chime.flac
```

Sound files must be FLAC format, 48 kHz, mono, 16-bit. See `sounds/LICENSE.md` for attribution.

## Troubleshooting

### Connection Issues

- **Device shows offline in ESPHome dashboard**: The dashboard can't resolve the device via mDNS. Set `wifi_static_ip`, `wifi_gateway`, and `wifi_subnet` in `secrets.yaml` to assign a static IP (see `secrets.yaml.example`)
- **Device doesn't connect**: Check `server_url` in `secrets.yaml` matches your addon configuration
- **No audio**: Check hardware mute switch and verify microphone initialization in logs
- **View logs**: `poetry run esphome logs voice_pe_config.yaml`

