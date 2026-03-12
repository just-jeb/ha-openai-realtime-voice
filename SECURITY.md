# Security Policy

## Reporting a vulnerability

If you discover a security vulnerability, please report it through [GitHub's private vulnerability reporting](https://github.com/just-jeb/ha-openai-realtime-voice/security/advisories/new).

**Do not open a public issue for security vulnerabilities.**

You should receive a response within a few days. If the vulnerability is confirmed, a fix will be prioritized and released as soon as possible.

## Scope

This project bridges audio between a local ESP32 device and the OpenAI API over a local network WebSocket. Security-relevant areas include:

- WebSocket server exposed on the local network (default port 8080)
- API keys stored in addon configuration
- Audio data transmitted over the local network
