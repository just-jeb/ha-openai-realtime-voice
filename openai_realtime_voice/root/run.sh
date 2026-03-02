#!/usr/bin/with-contenv bashio
set -e

OPENAI_API_KEY=$(bashio::config 'openai_api_key')
WEBSOCKET_PORT=$(bashio::config 'websocket_port')
WEB_SEARCH_API_KEY=$(bashio::config 'web_search_api_key')
VAD_THRESHOLD=$(bashio::config 'vad_threshold')
VAD_PREFIX_PADDING_MS=$(bashio::config 'vad_prefix_padding_ms')
VAD_SILENCE_DURATION_MS=$(bashio::config 'vad_silence_duration_ms')
INSTRUCTIONS=$(bashio::config 'instructions')
ENABLE_RECORDING=$(bashio::config 'enable_recording')

if [ -z "$OPENAI_API_KEY" ]; then
    bashio::log.error "OPENAI_API_KEY is required but not set"
    exit 1
fi

export OPENAI_API_KEY
export WEBSOCKET_PORT
export VAD_THRESHOLD
export VAD_PREFIX_PADDING_MS
export VAD_SILENCE_DURATION_MS
export INSTRUCTIONS
export ENABLE_RECORDING

if [ -n "$WEB_SEARCH_API_KEY" ]; then
    export WEB_SEARCH_API_KEY
fi

export PYTHONUNBUFFERED=1
exec python3 -m app.main
