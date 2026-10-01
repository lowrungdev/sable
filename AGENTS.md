# AGENTS.md

Read [CLAUDE.md](CLAUDE.md) for the full guidance; it applies to every coding agent and tool,
whatever its name. [CONTRIBUTING.md](CONTRIBUTING.md) is the equivalent for humans.

```bash
uv sync --locked --extra dev
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
```

Do not commit, push, tag or release unless the user asks you to in this conversation.
