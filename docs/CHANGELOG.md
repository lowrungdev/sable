# Changelog

One section per release, newest first.

**The section matching `version` in `pyproject.toml` becomes the release body in
Forgejo**, so write the notes before you bump the version. A missing or empty
section fails the test suite and the release, by design — a release with no notes
is not worth publishing.

Format: `## <version>`, optionally followed by a date. Anything until the next
`##` heading is the body.

## Unreleased

- **React to a message with ⁉️ and the bot answers it**, in a reply threaded
  under the original. It works on anyone's message, its own answers included,
  which makes it a quick way to ask a follow-up. `SABLE_ASK_REACTION` sets the
  emoji and empty disables the feature.
  A reaction event carries the message id and never the text, and the bot API
  cannot read a message back — that needs a user account rather than bot
  credentials — so this depends on a new bounded, expiring per-conversation
  cache of recent messages (`SABLE_MESSAGE_CACHE`, 200). React to something
  older than the cache and the bot says so rather than guessing. Nothing is
  cached when the feature is off.
- The documented `occ talk:bot:install` now asks for `--feature reaction`
  alongside `webhook` and `response`, so reaction events are delivered. They are
  still only logged, but enabling the feature at install means no reinstall when
  a handler lands.
- Fixed a latent bug that enabling that feature would have exposed: the
  redelivery cache keyed on `(conversation, type, message id)`, and for a
  reaction the id is the message reacted *to* — so two people reacting to one
  message, or one person reacting twice with different emoji, looked like a
  redelivery and the second event was dropped. The key now includes the actor
  and the emoji.

## 0.3

- `compose.yaml` now mounts the host's CA directory read-only and sets
  `SSL_CERT_FILE`, so the bot can verify a Nextcloud using an internal or
  self-signed certificate. This needs no application code: httpx verifies
  against its bundled certifi store by default, and that variable is what
  redirects it to the mounted bundle. It replaces the trust store rather than
  extending it, so it must name the complete host bundle — documented, along
  with why `SSL_CERT_DIR` and single-file mounts are traps.
- Two new documents: `docs/security.md` sets out the trust boundaries, what
  protects each one, how secrets are handled and the accepted risks;
  `docs/future.md` records known limitations with their fixes, Talk features not
  yet used, maintenance cadence and decisions worth revisiting.
- Every published image is now tagged with the full commit sha it was built
  from, so any image in Packages maps back to its source without inspecting
  labels. Release builds push `:<version>`, `:latest` and `:<commit>`; manual
  builds push `:dev` and `:<commit>`.
- The `:build-<n>` tag is gone. It named a CI run rather than anything about the
  code, and the commit tag and image digest both say more. Both workflows now
  print the digest and a ready-to-use `@sha256:…` pin line.
- The source archives attached to a release no longer carry CI-only files:
  `tests/`, `.forgejo/`, `.gitignore` and `.dockerignore` are `export-ignore`d,
  taking the zip from 44 entries to 28. `docs/` and `uv.lock` stay, since an
  archive without them cannot be installed.

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
