[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Build](https://github.com/just-jeb/ha-openai-realtime-voice/actions/workflows/build-addon.yml/badge.svg)](https://github.com/just-jeb/ha-openai-realtime-voice/actions/workflows/build-addon.yml)

# OpenAI Realtime Voice

Voice interface for Home Assistant using the OpenAI Realtime API for low-latency, natural conversation. The device wakes on a phrase, streams speech to the addon, and plays back answers; web search is available for live information (e.g. current weather, today’s events) via the Responses API.

## Purpose and intended use

- **Primary use:** Stationary voice Q&A device — you can tune it (e.g. short answers, language, tone) via the addon’s `instructions`. Common use cases include a family/kids device or a general-purpose voice assistant.
- **Voice path:** Wake word on device → WebSocket → HA addon → OpenAI Realtime API (speech-to-speech). No smart home control in this pipeline.
- **Web search:** Handled inside the addon via a `search_web` tool that calls the OpenAI Responses API (optional separate API key).
- **Smart home:** Can be a separate, fully local HA voice pipeline (e.g. different wake word, Whisper + Piper + Assist). Not part of this addon.

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

- Wake word detection on device (e.g. “Okay Nabu”, “Hey Jarvis”).
- Real-time voice over WebSocket to the addon.
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

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for how to report issues, submit pull requests, and set up a development environment.

Found a bug? [Open an issue](https://github.com/just-jeb/ha-openai-realtime-voice/issues/new/choose).

## Acknowledgments

The client firmware and WebSocket component in this repo were forked from [fjfricke/ha-openai-realtime](https://github.com/fjfricke/ha-openai-realtime) (Felix Fricke), which in turn builds on [esphome/home-assistant-voice-pe](https://github.com/esphome/home-assistant-voice-pe). Thanks to both for the Voice PE integration and the bridge idea.

## License

MIT — see [LICENSE](LICENSE).
