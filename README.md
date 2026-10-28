[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Build](https://github.com/just-jeb/ha-openai-realtime-voice/actions/workflows/build-addon.yml/badge.svg)](https://github.com/just-jeb/ha-openai-realtime-voice/actions/workflows/build-addon.yml)

# OpenAI Realtime Voice

Voice interface for Home Assistant using the OpenAI Realtime API for low-latency, natural conversation. The device wakes on a phrase, streams speech to the addon, and plays back answers; web search is available for live information (e.g. current weather, today’s events) via the Responses API.

## Purpose and intended use

- **Primary use:** Stationary voice Q&A device — you can tune it (e.g. short answers, language, tone) via the addon’s `instructions`. Common use cases include a family/kids device or a general-purpose voice assistant.
- **Voice path (OpenAI):** "Okay Nabu" / button → WebSocket → HA addon → OpenAI Realtime API (speech-to-speech).
- **Web search:** Handled inside the addon via a `search_web` tool that calls the OpenAI Responses API (optional separate API key).
- **Smart home (HA Assist):** "Hey Mycroft" (99% cutoff) / double-click → ESPHome built-in `voice_assistant` → HA Assist pipeline (Whisper STT → Assist → Piper TTS). Fully local, no cloud. See [HA Assist setup](#ha-assist-pipeline-setup) below.

## Components

- **Server** (`openai_realtime_voice/`): Home Assistant addon — WebSocket server that bridges the device to the OpenAI Realtime API and to web search (Responses API).
- **Client** (`home-assistant-voice-pe/`): ESPHome configuration and custom WebSocket component for the Home Assistant Voice PE (ESP32-S3).

## Features

### Server (addon)

- Direct bridge to OpenAI Realtime API (one connection per conversation; no 60‑minute orphan sessions).
- WebSocket server for the device (raw PCM audio and JSON control).
- Server-side VAD for turn-taking.
- Two tools: end conversation (`disconnect_client`) and live info (`search_web` via Responses API).
- Optional audio recording for debugging.

### Client (Voice PE)

- Wake word detection on device with wake-word-based pipeline routing.
- **OpenAI pipeline:** “Okay Nabu” / single-click → real-time voice over WebSocket to the addon.
- **HA Assist pipeline:** “Hey Mycroft” (99% probability cutoff) / double-click → local smart home commands via HA Assist.
- Each wake word detection is logged and fired as the HA event `esphome.wake_word_detected` (`wake_word` data) for tracing false wakes.
- Interrupt by saying the wake word while the assistant is speaking.
- LED feedback, button, mute switch, AGC, AEC.

### Conversation behavior

- Back-and-forth in a single session until the user stops (goodbye, button, or inactivity).
- Each new wake word starts a new session; no cross-session context.

## Documentation

- **Server:** [openai_realtime_voice/README.md](openai_realtime_voice/README.md) — install and configure the addon.
- **Client:** [home-assistant-voice-pe/README.md](home-assistant-voice-pe/README.md) — build and flash the device.
- **Development:** [DEVELOPMENT.md](DEVELOPMENT.md) — local run and Docker build for server and client.

## Quick start

1. Install and configure the addon (see server README). Required: `openai_api_key`. Optional: `web_search_api_key` for web search.
2. Build and flash the ESP32 with the client config (see client README). Set `server_url` in secrets to your addon WebSocket URL (e.g. `ws://homeassistant.local:8080`).

## HA Assist pipeline setup

The HA Assist pipeline handles local smart home commands ("turn on the kitchen lights", "set thermostat to 72") entirely through Home Assistant — no cloud needed.

### Prerequisites

1. **Whisper addon** (local speech-to-text): Settings → Add-ons → Add-on Store → "Whisper". Configure: model `tiny-int8` or `base-int8`, set language.
2. **Piper addon** (local text-to-speech): Settings → Add-ons → Add-on Store → "Piper". Configure: select a voice (e.g. `en_US-lessac-medium`).

openWakeWord is **not needed** — wake word detection runs on the device.

### Configuration

1. **Create a voice pipeline:** Settings → Voice assistants → Add assistant. Set conversation agent to "Home Assistant", STT to Whisper, TTS to Piper. Leave wake word empty.
2. **Assign pipeline to device:** Settings → Devices → select your Voice PE → Configure → select the pipeline you created.
3. **Expose entities:** Settings → Voice assistants → Expose tab. Select which entities (lights, switches, climate, etc.) Assist can control. Most entities are **not exposed by default**.

The device must be adopted in Home Assistant (Settings → Devices → ESPHome) for the Assist pipeline to work.

### Limitations

- HA's built-in conversation agent handles basic commands only. For complex queries, consider the [Extended OpenAI Conversation](https://github.com/jekalmin/extended_openai_conversation) custom integration.
- Latency is higher than the OpenAI pipeline (Whisper transcription + Piper synthesis vs. streaming speech-to-speech).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for how to report issues, submit pull requests, and set up a development environment.

Found a bug? [Open an issue](https://github.com/just-jeb/ha-openai-realtime-voice/issues/new/choose).

## Acknowledgments

The client firmware and WebSocket component in this repo were forked from [fjfricke/ha-openai-realtime](https://github.com/fjfricke/ha-openai-realtime) (Felix Fricke), which in turn builds on [esphome/home-assistant-voice-pe](https://github.com/esphome/home-assistant-voice-pe). Thanks to both for the Voice PE integration and the bridge idea.

## License

MIT — see [LICENSE](LICENSE).
