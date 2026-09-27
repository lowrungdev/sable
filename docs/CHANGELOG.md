# Changelog

One section per release, newest first.

**The section matching `version` in `pyproject.toml` becomes the release body in
Forgejo**, so write the notes before you bump the version. A missing or empty
section fails the test suite and the release, by design — a release with no notes
is not worth publishing.

Format: `## <version>`, optionally followed by a date. Anything until the next
`##` heading is the body.

## Unreleased

_Nothing yet._

## 0.2

- Dependencies are locked with `uv.lock`: 31 packages pinned to exact versions
  and verified by hash, so a given commit always installs the same set. CI and
  the image both install with `uv sync --locked`, which fails rather than
  re-resolving if the lock and `pyproject.toml` disagree.
- The image moves to `python:3.14.7-slim`, pinned to the patch release.
- Tests no longer run inside the Docker build: the Dockerfile is a single stage
  whose only job is the runtime image, and CI runs pytest itself.

## 0.1

First release.

- A Nextcloud Talk bot on the official webhook Bot API. Incoming webhooks are
  HMAC-SHA256 verified over the raw body before anything parses them; outgoing
  calls are signed per endpoint. Events from other bots are ignored and
  redeliveries de-duplicated, so two bots cannot loop.
- A command router with `!help`, `!ping`, `!whoami`, `!echo`, `!ai`, `!reset`
  and `!version`, extended by one decorator.
- A chat assistant over any OpenAI `/chat/completions`-compatible backend —
  OpenAI, Ollama, vLLM, llama.cpp, LiteLLM, OpenRouter, Groq, Together — with
  per-conversation rolling history, triggered by a mention, `!ai`, or every
  message in the conversations listed in `SABLE_AI_ROOMS`.
- `POST /notify`, a bearer-token endpoint for relaying alerts from CI, an
  alertmanager or a cron job into a conversation, with aliases so callers never
  need conversation tokens.
- `GET /healthz`, reporting the version, bot name, configured model and whether
  alerting is enabled.
- 26 settings, all `SABLE_`-prefixed environment variables, validated at startup
  with `sable --check`.
- A non-root container image, a Compose file listing every setting inline, and
  docs covering purpose, configuration, deployment and releasing.
