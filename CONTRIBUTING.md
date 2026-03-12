# Contributing

Thanks for your interest in contributing to OpenAI Realtime Voice!

## Reporting issues

- Use the [bug report template](https://github.com/just-jeb/ha-openai-realtime-voice/issues/new?template=bug_report.yml) for bugs.
- Use the [feature request template](https://github.com/just-jeb/ha-openai-realtime-voice/issues/new?template=feature_request.yml) for suggestions.
- Include addon and client logs when reporting bugs — they make debugging much faster.

## Pull requests

1. Fork the repo and create a branch from `main`.
2. Make your changes. Keep PRs focused — one logical change per PR.
3. Follow [Conventional Commits](https://www.conventionalcommits.org/) for commit messages (`fix:`, `feat:`, `chore:`, etc.). See [AGENTS.md](AGENTS.md) for the full list.
4. Run server tests before submitting: `cd openai_realtime_voice && .venv/bin/pytest tests/ -v`
5. Open a PR against `main`. The PR template will guide you through the checklist.

## Development setup

See [DEVELOPMENT.md](DEVELOPMENT.md) for local run and Docker build instructions for both the server addon and client firmware.

## Project structure

- `openai_realtime_voice/` — Server addon (Python, WebSocket bridge)
- `home-assistant-voice-pe/` — Client firmware (ESPHome, C++ component)
- Each component has its own `AGENTS.md` with architecture and contribution guidance.

## Code style

- **Server:** Standard Python conventions. No strict formatter enforced — just keep it consistent with existing code.
- **Client:** C++ ESPHome component conventions. Follow the existing patterns in the `.cpp` and `.h` files.

## License

By contributing, you agree that your contributions will be licensed under the [MIT License](LICENSE).
