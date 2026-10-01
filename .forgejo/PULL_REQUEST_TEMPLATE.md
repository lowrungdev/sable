## What and why

<!-- What changed, and the reason for it. -->

## Checklist

Read [CONTRIBUTING.md](https://forgejo.subversive.link/subversive/sable/src/branch/dev/CONTRIBUTING.md)
first. Pull requests go against `dev`.

- [ ] Tests added or updated, and `uv run pytest` passes
- [ ] Lint and types are clean: `uv run ruff check . && uv run ruff format --check . && uv run mypy`
- [ ] If a setting or behaviour changed, the docs agree: `.env.example`, `compose.yaml`,
      `docs/configuration.md`, and the mapping in `tests/test_docs.py`
- [ ] `CHANGELOG.md` has an entry under `## Unreleased`
- [ ] No secrets, tokens or real hostnames in the diff, the tests or the logs
