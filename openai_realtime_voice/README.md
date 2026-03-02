# OpenAI Realtime Voice — Server Addon

Home Assistant addon that bridges the Voice PE device to the OpenAI Realtime API over WebSocket. One device connection = one Realtime session; sessions are closed when the device disconnects.

## Purpose

- **Voice Q&A:** Low-latency speech-to-speech with the Realtime API for natural back-and-forth conversation.
- **Web search:** Optional live information via a `search_web` tool that calls the OpenAI Responses API.
- **No smart home in this addon:** Smart home control is intended as a separate, local HA voice pipeline.

## Installation

### From repository

1. **Supervisor** → **Add-on Store** → **⋮** → **Repositories**
2. Add: `https://github.com/just-jeb/ha-openai-realtime-voice` (or your fork)
3. Install **OpenAI Realtime Voice**

### Manual

1. Copy `openai_realtime_voice/` into your HA `addons/` directory
2. Restart Supervisor, then install from **Add-on Store** → **Local Add-ons**

## Configuration

In **Supervisor** → **OpenAI Realtime Voice** → **Configuration**:

**Required**

- `openai_api_key`: OpenAI API key (used for Realtime and, if set below, for web search)

**Optional**

- `websocket_port`: WebSocket port (default `8080`)
- `instructions`: System prompt for the model (e.g. language, tone, length of answers)
- `web_search_api_key`: API key for Responses API web search; if empty, `openai_api_key` is used
- `vad_threshold`, `vad_prefix_padding_ms`, `vad_silence_duration_ms`: Voice activity detection
- `enable_recording`: Set to `true` to record input/output WAVs under `recordings/` (debug only)

Then start the addon.

## Features

- WebSocket server for the Voice PE device (raw PCM 24 kHz, 16-bit mono)
- One OpenAI Realtime session per device connection; session ends when the device disconnects
- Server-side VAD for turn-taking
- Tools: `disconnect_client` (end conversation), `search_web` (Responses API with web search)
- Optional audio recording for debugging

## Troubleshooting

- **Device won’t connect:** Check `server_url` on the device (e.g. `ws://homeassistant.local:8080`) and that the addon is running and the port is open.
- **No speech / one-sided audio:** Check addon logs and device logs; confirm mic/speaker and format (24 kHz, 16-bit mono).
- **Web search not used:** Ensure `instructions` tell the model it can search the web when needed; optionally set `web_search_api_key` if you use a separate key.
