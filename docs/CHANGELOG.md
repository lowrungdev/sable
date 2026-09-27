# Changelog

One section per release, newest first.

**The section matching `version` in `pyproject.toml` becomes the release body in
Forgejo**, so write the notes before you bump the version. A missing or empty
section fails the test suite and the release, by design — a release with no notes
is not worth publishing.

Format: `## <version>`, optionally followed by a date. Anything until the next
`##` heading is the body.

## 0.5

- **`/notify` can attach a file** — same URL, same single call. It accepts the
  original JSON, JSON with a base64 `file`, or `multipart/form-data` with an
  uploaded `file`; Content-Type decides. The `message` becomes the file's
  caption, so the file and its text arrive as one chat message rather than two.
  Text-only calls behave exactly as before.
  This is a hybrid, not a change of model: the Talk bot API has no upload
  endpoint and does not accept bot signatures on the ones that could, so
  attachments use a separate Nextcloud **user** account — `SABLE_NEXTCLOUD_USER`
  and `SABLE_NEXTCLOUD_PASSWORD` — to PUT the file over WebDAV and share it into
  the conversation. Receiving stays on the signed webhook, so there is no
  polling, no per-conversation connections and no cursor to persist.
  With no user configured, attachments answer `503` naming the two variables and
  everything else keeps working. Uploads are capped by `SABLE_MAX_UPLOAD_BYTES`
  (25 MiB), land in `SABLE_UPLOAD_PATH` (`/sable`), have their filenames
  sanitised and made unique, and are deleted again if the share fails rather
  than left orphaned.
  That account's app password cannot be scoped — it reaches that user's Files,
  Contacts and Calendar — so it is optional, used only on this path, and
  recorded in `docs/security.md` as the largest credential sable can hold.
- The startup block names the account attachments are posted as, the folder they
  land in, and the size limit in MB rather than bytes — plus who is being
  ignored: `attachments:    as sable-files into /bot-uploads, up to 100 MB`.
- **`SABLE_IGNORE_USERS`** drops everything from the listed people: commands,
  mentions and reactions, and their messages are never cached for ⁉️ either, so
  their words do not reach the model even when somebody else asks about them.
  Entries match a bare user id, a full actor id, or a display name.
- Conversations are checked when sable starts. `SABLE_HOOKS` and
  `SABLE_NOTIFY_ROOMS` entries must name a conversation *token* — the lowercase
  string at the end of the conversation's URL — or, for a hook, an alias from
  `SABLE_NOTIFY_ROOMS`. A room's name where a token belongs now fails at boot
  with a message saying where to find the token, instead of reaching Talk and
  coming back as an opaque `998 Invalid query` the first time an alert fires.
  Hooks also resolve aliases now, which the documentation had claimed and the
  code had not done.

## 0.4

- `SABLE_AI_ROOMS` now accepts a conversation's **display name** as well as its
  token, matching case- and space-insensitively. It previously matched tokens
  only, so putting the name in the list silently did nothing. Tokens are still
  the better choice for anything that matters, since a moderator can rename a
  conversation at any time — and two conversations can share a name.
  The DEBUG line for an ignored message now prints both identifiers, so it shows
  you what to configure: `message in a1b2c3d4 ('AI') was not for me - no prefix,
  no mention, and not an AI room`.
- **Much more verbose logging**, aimed at an operator reading the first and last
  few lines of a container log:
  - start and stop are logged, with the resolved configuration in between —
    bind address, the webhook path to register, the Nextcloud URL, bot name,
    prefix, model and its base URL, ask reaction, AI rooms, alerting and its
    aliases, backend pinning, log level;
  - a startup probe calls Nextcloud's `status.php` and logs the product and
    version it reached, so a wrong URL or an untrusted certificate is obvious
    at boot rather than on the first reply. Never fatal, and
    `SABLE_STARTUP_CHECK=false` skips it;
  - reachability is logged as *transitions* — one line when Nextcloud or the
    model backend becomes unreachable and one when it returns — instead of a
    line per retry. A transport failure counts as unreachable; an HTTP error
    response does not, and is logged with its status and body;
  - model calls log their outcome: duration and answer size on success, the URL
    with status and body on an error, the limit on a timeout;
  - joining and leaving a conversation name the conversation and say what it
    means;
  - every use logs who (display name and id), what (command, mention, or which
    message a ⁉️ referred to) and where.
  Message text stays out of `INFO`: uses are logged with sizes, not content.
  `DEBUG` adds prompts, command arguments and referenced message text, so a
  DEBUG log contains chat content — noted in `docs/security.md`.
- httpx's own per-request `INFO` line is quieted unless the whole application is
  at `DEBUG`; it said less than sable's own line about the same call and buried
  it.
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
