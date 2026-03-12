# Changelog

## 1.0.0

First public release.

### Features

- **Speech-to-speech voice assistant** using the OpenAI Realtime API (GA) with server-side VAD for natural turn-taking.
- **Web search** via `search_web` tool — calls the OpenAI Responses API for live information (weather, news, etc.).
- **Conversation lifecycle** — back-and-forth in a single session until the user says goodbye, presses the button, or times out. Each wake word starts a fresh session.
- **Phase protocol** — server sends `thinking`, `replying`, and `searching` phases for client LED/UX feedback. Client derives `listening` locally to avoid phase races.
- **Interrupt** — say the wake word while the assistant is speaking to interrupt and ask something new.
- **Configurable voice, model, and web search model** — `voice`, `realtime_model`, and `web_search_model` options in the addon config.
- **Audio pacing** — token-bucket sender matches playback rate so the ESP32 buffer doesn't overflow.
- **Tool call deduplication** — duplicate in-flight `search_web` calls for the same query are collapsed.
- **Optional audio recording** — save input/output WAVs for debugging.
- **ESP32-S3 Voice PE client** — ESPHome firmware with wake word detection, audio resampling, LED feedback, button, and mute switch.
