# OpenAI Realtime Voice

## What this is

A voice system for Home Assistant using the OpenAI Realtime API. Two parts talk over a WebSocket with raw PCM audio:

- **Server** (`openai_realtime_voice/`): HA addon — WebSocket server that bridges the ESP32 to the OpenAI Realtime API and to web search (Responses API).
- **Client** (`home-assistant-voice-pe/`): ESPHome firmware for the Home Assistant Voice PE (ESP32-S3).

Each component has its own `AGENTS.md` with component-specific guidance.

## Architecture

```
ESP32-S3 Voice PE                    HA Addon
┌──────────────────┐  WebSocket     ┌─────────────────────────┐
│ micro_wake_word  │   :8080       │  RealtimeVoiceBridge    │
│       ↓          │  ── PCM ───►  │    client_ws ↔ openai_ws │
│ voice_assistant_ │               │    (one Realtime session│
│ websocket (C++)  │  ◄── PCM ──   │     per connection)     │
│       ↓          │  ◄── JSON ──  │    + search_web → Resp. │
│ I2S Speaker      │               │         API             │
└──────────────────┘               └─────────────────────────┘
```

## Contract between client and server

Both sides must follow this. If one side changes, the other may need updates.

**WebSocket endpoint:** `ws://<host>:<port>/` (default 8080)

**Audio (both ways):** 24 kHz, 16-bit, mono PCM in binary WebSocket frames. No extra headers or framing.

**Control (JSON text frames):**

| Direction        | Message              | Meaning                                      |
|-----------------|----------------------|----------------------------------------------|
| Client → Server | `{"type":"interrupt"}` | Stop current response and listen for input |
| Server → Client | `{"type":"ready"}` | OpenAI connected; client should start sending audio and show "listening" (e.g. green LEDs). |
| Server → Client | `{"type":"phase","phase":"thinking"}` | User stopped speaking; OpenAI is processing. |
| Server → Client | `{"type":"phase","phase":"replying"}` | Bot started speaking. |
| Server → Client | `{"type":"phase","phase":"listening"}` | Bot finished; ready for next user input. |
| Server → Client | `{"type":"phase","phase":"searching"}` | Background tool (e.g. web search) running; client should show processing+listening state and use extended auto-stop timeout (e.g. 60s). |
| Server → Client | `{"type":"disconnect"}` (optional: `"message"`, `"reason"`) | Session ended (e.g. user said goodbye); client should stop and go idle. |

**Client:** Resample mic 16 kHz → 24 kHz before send; resample received 24 kHz → 48 kHz for the speaker.

**Server:** Encode PCM to base64 and send as `input_audio_buffer.append`; decode `response.output_audio.delta` (Realtime API GA) and send raw PCM to the client. Tool calls such as `search_web` run in the background so the event loop stays responsive.

## Installation and deployment

### Server addon on HAOS

The addon lives in the repo under `openai_realtime_voice/` (directory name matches slug). To install from this repo:

1. **Settings → Add-ons → Add-on Store → ⋮ → Repositories** → add `https://github.com/just-jeb/ha-openai-realtime-voice`
2. Install “OpenAI Realtime Voice”
3. Configure in the addon Configuration tab (see `openai_realtime_voice/config.yaml`). Required: `openai_api_key`. Optional: `web_search_api_key`, VAD, `instructions`, `enable_recording`

### Client firmware

The ESPHome config in `home-assistant-voice-pe/` pulls the `voice_assistant_websocket` component from this repo via git on compile. See `home-assistant-voice-pe/AGENTS.md` for details.

## Owner’s deployed setup

HAOS on a dedicated host. ESPHome config under e.g.:

```
/homeassistant/esphome/
├── home-assistant-voice-0acf1a.yaml
├── secrets.yaml
└── wake_sound.flac
```

Device name, friendly name, wake words, and other production settings can differ from the repo default.

## Keeping docs current

| What changed                         | Update |
|--------------------------------------|--------|
| Audio format, WebSocket protocol, control messages | This file (contract) |
| Server tools, config, Docker         | `openai_realtime_voice/AGENTS.md` |
| Client firmware, ESPHome, hardware  | `home-assistant-voice-pe/AGENTS.md` |
| Deployment, owner setup              | This file (deployment) |

If a change affects how the two components talk, update this file. If it’s internal to one component, update that component’s `AGENTS.md`.
