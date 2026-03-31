# AGENTS.md

This repository is a Codex-adapted fork of CCBot.
The CLI/package name stays `ccbot`.

## Common Commands

```bash
uv run ruff check src/ tests/
uv run ruff format src/ tests/
uv run pyright src/ccbot/
uv run pytest
./scripts/restart.sh
ccbot hook --install
```

## Working Notes

- Keep changes small and follow existing patterns.
- Do not hardcode machine-specific paths; prefer `Path.home()` or env vars.
- Preserve the topic -> tmux window -> session mapping.
- Keep `ccbot` CLI compatibility unless there is a strong reason to break it.
- Validate with lint, typecheck, and relevant tests before committing.
