# Security policy

## Supported versions

Only the latest release is supported. Fixes land in the next release; there are no
backports. The current version is `version` in [`pyproject.toml`](pyproject.toml), and
`!version` or `/healthz` reports the one a running instance is on.

## Reporting a problem

sable is an internal project, so there is no separate embargo process. Either:

- open an issue on the repository, at
  https://forgejo.subversive.link/subversive/sable/issues, or
- tell whoever runs your instance, privately, if the details would help someone abuse it.

Do not put tokens, app passwords or other secrets in an issue. Redact them from logs.

## What is in scope

- Anything that lets someone who should not be able to make sable answer, run a command,
  post to a room or reach `/notify` and `/hook/{name}` do so.
- Leaks of secrets (the account's app password, the LLM key, the notify and health tokens)
  into logs, replies or responses.
- Request handling that can be made to fail or exhaust the process (oversized bodies,
  unbounded queues, parsing errors).

Risks the design accepts on purpose, such as sable holding a whole user's credential, are
listed in [docs/security.md](docs/security.md) together with the threat model. Read it
before reporting something it already names.
