# Client Firmware — Agent Guide

## Design Intent

This is ESPHome firmware for the Home Assistant Voice PE (ESP32-S3). It
listens for wake words locally, then streams microphone audio to the
server addon over WebSocket and plays back the response through the
speaker. All voice processing (STT, LLM, TTS) happens server-side —
the ESP32 only handles audio I/O and wake word detection.

## Key Design Decisions

**Microphone is never stopped.** `micro_wake_word` and
`voice_assistant_websocket` share the same I2S microphone. When the
voice assistant disconnects, the mic keeps running so wake word
detection continues without interruption. This is intentional — stopping
and restarting the mic causes glitches in wake word detection.

**Mic is muted during bot speech.** While the bot is speaking
(`is_bot_speaking()` — 500ms threshold since last received audio), the
component does not send mic audio to the server. This prevents the bot's
own output from being picked up by the mic and causing echo/feedback
loops. Hardware AEC handles some of this, but blocking at the source is
more reliable.

**Auto-stop on inactivity.** If no speaker audio is received for 20
seconds, the session auto-stops. This is tracked by speaker output only,
not mic input, because the mic always picks up ambient noise. The timeout
means: if the user stops talking and the bot has nothing to say, the
session ends cleanly.

**Interrupt via wake word.** If the bot is speaking and the user says
a wake word, the client sends `{"type":"interrupt"}` and clears its
local audio queue/speaker. If the bot is NOT speaking and a wake word
is detected, the client disconnects instead (session is over, user wants
a fresh start). This is the wake word's dual role: start OR interrupt.

**Audio resampling is split across layers.** The mic captures at 16kHz
(required by `micro_wake_word`). The C++ component resamples to 24kHz
for the server. Received 24kHz audio is passed to ESPHome's resampler
speaker which converts to 48kHz for the I2S hardware. The C++ component
does not handle output resampling — ESPHome's speaker pipeline does.

**Component is pulled from git.** The `voice_assistant_websocket`
component is referenced via git URL in `external_components`, not as a
local path. This means ESPHome downloads it fresh on each compile. Users
(and the owner's HAOS setup) don't need to manually copy component files.

## Audio Format Responsibilities

```
Mic (16kHz/32bit/stereo)
  → C++ component: stereo→mono, 32→16bit, 16kHz→24kHz (linear interp)
  → WebSocket: 24kHz/16bit/mono raw PCM
  ════════════════════════════════════
  ← WebSocket: 24kHz/16bit/mono raw PCM
  → ESPHome resampler: 24kHz→48kHz, mono→stereo, 16→32bit
  → I2S speaker (48kHz/32bit/stereo)
```

If the server's audio format changes, the C++ component's constants
(`INPUT_SAMPLE_RATE`, `OUTPUT_SAMPLE_RATE`, `BYTES_PER_SAMPLE`) and the
ESPHome resampler config in `voice_pe_config.yaml` must both be updated.

## ESPHome Component Structure

The `voice_assistant_websocket` component lives under
`home-assistant-voice-pe/esphome/components/voice_assistant_websocket/`:

- `.esphome_component.yml` — Component metadata and git source (referenced from `voice_pe_config.yaml` external_components).
- `__init__.py` — ESPHome config schema, actions (`start`, `stop`,
  `interrupt`), conditions (`is_running`, `is_connected`,
  `is_bot_speaking`). This is the ESPHome integration glue.
- `.h` — State enum, class declaration, constants, action/condition
  template classes.
- `.cpp` — WebSocket lifecycle, audio processing, resampling,
  event handling.

When adding a new action or condition, it must be registered in
`__init__.py`, `.h`, and `.cpp`. The `voice_assistant_webrtc` directory in the same tree is metadata-only (no implementation); all runtime use is WebSocket.

## Keeping This Doc Current

Update this file when:
- The audio format or resampling chain changes
- Wake word behavior or models change
- The interrupt/disconnect protocol changes
- New ESPHome actions or conditions are added to the component
- Buffer sizes or timing constants are retuned
- The component source (git URL/ref) changes
- Hardware changes (different board, different I2S pins)
