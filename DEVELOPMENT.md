# Development Setup

This repo has two parts: a **server** addon (Python) and a **client** (ESPHome). They can be developed with different tooling.

## Server (`openai_realtime_voice/`)

The addon uses **pip** and `requirements.txt`.

**Local run (outside Docker):**

```bash
cd openai_realtime_voice
pip install -r requirements.txt
# Set env vars (OPENAI_API_KEY, WEBSOCKET_PORT, etc.) then:
python -m app.main
```

**Build Docker image:**

```bash
cd openai_realtime_voice
docker build -t openai-realtime-voice .
```

## Client (`home-assistant-voice-pe/`)

ESPHome config and the custom `voice_assistant_websocket` component. Use **ESPHome** with either pip or Poetry.

**With pip:**

```bash
pip install esphome
cd home-assistant-voice-pe
esphome compile voice_pe_config.yaml
esphome upload voice_pe_config.yaml --device /dev/cu.usbmodem101
```

**With Poetry (optional):**

```bash
cd home-assistant-voice-pe
poetry install
poetry run esphome compile voice_pe_config.yaml
poetry run esphome upload voice_pe_config.yaml --device /dev/cu.usbmodem101
```

See [home-assistant-voice-pe/README.md](home-assistant-voice-pe/README.md) for full client setup.
