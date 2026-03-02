# Server Addon — Agent Guide

## Design intent

This addon is a **direct WebSocket bridge** between the Voice PE device and the OpenAI Realtime API. It receives raw PCM over WebSocket, forwards it to Realtime, and streams response audio back. Implementation is a single Python module (`app/main.py`) plus optional recording.

**Lifecycle:** One client connection = one OpenAI Realtime session. The
three forwarding tasks (client→OpenAI, OpenAI→client, audio_sender) run
concurrently; the first to finish (client disconnect, OpenAI disconnect,
or `disconnect_client` tool) triggers cancellation of the other two and
immediate closure of both WebSockets. No polling a done flag — cleanup
is prompt and predictable. Session end is logged with duration and reason.

## Architecture

```
ESP32 Voice PE                    Addon
+------------------+  WebSocket   +---------------------------+
| micro_wake_word  |   :8080     |  RealtimeVoiceBridge      |
| voice_assistant  | --- PCM ---> |    client_ws <-> openai_ws|
| _websocket       | <--- PCM --- |    (one openai_ws per     |
| I2S Speaker      | <-- JSON --  |     client connection)    |
+------------------+             +---------------------------+
                                       | disconnect_client tool
                                       | search_web tool ------> Responses API
```

- **Audio:** 24 kHz, 16-bit, mono PCM. Device binary frames are base64-encoded and sent as `input_audio_buffer.append`; `response.audio.delta` is base64-decoded and sent as binary to the device.
- **Interrupt:** Device sends `{"type":"interrupt"}`; server sends `response.cancel` to OpenAI.
- **Disconnect:** When the user says goodbye, OpenAI calls `disconnect_client`; server sends `{"type":"disconnect"}` to the device, returns from the handler so the task ends, and the cancellation cascade closes both WebSockets.
- **Web search:** The `search_web` tool calls the OpenAI Responses API (GA `web_search` tool, `gpt-5-nano` model) using `WEB_SEARCH_API_KEY` or `OPENAI_API_KEY`. The key must have **Responses (Write)** permission in the OpenAI dashboard.

## Design decisions

- **No MCP.** Smart home is intended as a separate, local HA voice pipeline (e.g. different wake word). This addon is for voice Q&A and web search only.
- **Two tools:** `disconnect_client` and `search_web`, both implemented in `main.py` in `_handle_tool_call`.
- **Config:** HA Addon UI → `config.yaml` → `run.sh` (bashio) → env vars → `main.py`. No config file parsing in Python.

## Config options

- `openai_api_key` — Required. Must have **Realtime (Request)** permission. Also used for web search if `web_search_api_key` is unset, in which case it must also have **Responses (Write)**.
- `websocket_port` — Default 8080.
- `vad_threshold`, `vad_prefix_padding_ms`, `vad_silence_duration_ms` — Server-side VAD.
- `instructions` — System prompt for the model.
- `web_search_api_key` — Optional; if empty (or whitespace-only), `openai_api_key` is used for web search. Must have **Responses (Write)** permission. A 401 on web search means the key lacks this permission.
- `enable_recording` — If true, WAVs written under `recordings/` via `app/audio_recorder.py`.

## Adding a tool

1. Add the tool schema to the `TOOLS` list in `main.py`.
2. In `_handle_tool_call`, handle the tool name: parse arguments, run logic, call `_send_tool_result(openai_ws, call_id, result_json_string)` and send `response.create` if needed.

## Docker build

Single-stage Alpine image. Dependencies in `requirements.txt` (websockets, httpx, python-dotenv). Dockerfile runs `pip install -r requirements.txt`; no Poetry or LLVM.

## Version

When you change the addon (behavior, config, contract, or fixes), update the addon version in `config.yaml` using semver-like rules: breaking → bump major, new feature → minor, fix → patch. Do this until we have a better release/versioning solution.

## File layout (addon directory: `openai_realtime_voice/`)

- `config.yaml` — Addon options schema and image URL (used by Supervisor).
- `root/run.sh` — Entrypoint; reads bashio config and exports env for `app.main`.
- `app/main.py` — Bridge logic, tools, WebSocket handlers.
- `app/audio_recorder.py` — Optional WAV recording when `enable_recording` is true.
- `requirements.txt` — websockets, httpx, python-dotenv.
- `requirements-dev.txt` — for tests: pytest, pytest-asyncio.
- `tests/` — pytest tests (happy-flow and regression).
- `pytest.ini` — asyncio_mode = auto for tests.
- `Dockerfile` — Single-stage; no Poetry. Used by `.github/workflows/build-addon.yml`.

## Keeping this doc current

Update when:

- The WebSocket contract (audio format, control messages) changes
- A config option or tool is added or changed
- The Docker build or dependencies change
- When shipping a fix or feature: update version in `config.yaml` (semver-like; see Version above).
