# Changelog

One section per release, newest first.

**The section matching `version` in `pyproject.toml` becomes the release body in
Forgejo**, so write the notes before you bump the version. A missing or empty
section fails the test suite and the release, by design — a release with no notes
is not worth publishing.

Format: `## <version>`, optionally followed by a date. Anything until the next
`##` heading is the body.

## Unreleased

- **BREAKING: sable now runs as an ordinary Nextcloud user, not as a Talk bot.**
  It used to be a webhook bot on Talk's Bot API: Nextcloud called `/webhook`,
  every event was HMAC-signed, and replies were signed back with a shared
  secret. That is gone. sable signs in as a user with an app password and
  long-polls the chat API for the conversations that user is in, then posts,
  reacts and uploads as the same user. The webhook, the signing code
  (`signing.py`), the replay cache and the backend pin are removed, and so is
  the requirement that Nextcloud be able to reach sable over HTTPS: sable only
  needs outbound access to Nextcloud now, and its own port is for `/notify`,
  `/hook/{name}` and `/healthz` alone.
  Why: a user can read a message back and upload a file, which a bot cannot, and
  nothing has to be installed in Nextcloud or reachable from it. What it costs is
  in [purpose.md](purpose.md) and [security.md](security.md) - chiefly that the
  credential is a user's app password, which cannot be scoped and reaches that
  user's Files, Contacts and Calendar, so give sable an account that owns nothing
  else; and that every long poll holds a request slot on Nextcloud for up to
  `SABLE_POLL_TIMEOUT` seconds, which is why at most 50 conversations are
  followed (the most recently active).
  **Migrating:** (1) create a Nextcloud user for sable and an app password for it
  under Settings > Security > Devices & sessions; (2) invite that user to each
  conversation it should be in - a normal invitation, there is no bot switch;
  (3) delete the old bot with `occ talk:bot:uninstall --id <id>` (`occ
  talk:bot:list` shows the id); (4) set `SABLE_NEXTCLOUD_URL`,
  `SABLE_NEXTCLOUD_USER` and `SABLE_NEXTCLOUD_PASSWORD`, all now required; and
  (5) remove `SABLE_BOT_SECRET`, `SABLE_BOT_SECRET_PREVIOUS`, `SABLE_BOT_NAME`
  and `SABLE_PIN_BACKEND`, which no longer exist. `SABLE_NEXTCLOUD_USER` and
  `SABLE_NEXTCLOUD_PASSWORD` were the optional upload account before; the same
  account now does everything, and `/notify` attachments are always available.
  `SABLE_UPLOAD_PATH` and `SABLE_MAX_UPLOAD_BYTES` are unchanged. The old
  upload account can be reused as the new one or retired; listing it in
  `SABLE_IGNORE_USERS` is no longer needed, because sable ignores its own
  messages.
  Also different: people now address sable by @-mentioning its user id (picked
  from Talk's list, or typed at the start of a message) rather than a configured
  name, and one-to-one conversations have no special case. A conversation joined
  after sable started is followed from its newest message at the next scan, so
  nothing said before is replayed, and neither is anything said while sable was
  down. `/healthz` reports `user` where it reported `bot`.
- **New settings for reading chat.** `SABLE_POLL_TIMEOUT` (default 30) is how
  many seconds each long poll may wait, clamped to Talk's maximum of 60 and an
  error below 1. `SABLE_ROOM_REFRESH` (default 60) is how often the conversation
  list is rescanned for rooms sable was added to or removed from, an error below
  5. The startup banner gains a `receiving` line and loses `webhook URL`, `bot
  name` and `backend pin`; it now reads `nextcloud: <url> as <user>`, and logs
  `signed in to <url> as <id> (<display name>)` once the credentials are
  confirmed.
- **`SABLE_STARTUP_CHECK` now verifies the credentials.** It asks Nextcloud who
  the account is (`cloud/user`) instead of fetching `status.php`, so a wrong URL,
  an untrusted certificate or a rejected app password all show up at boot. A
  plain `http://` URL to a host that is not local gets a warning, since the
  password crosses the network unencrypted.
- **The ⁉️ reaction is unverified against a live server.** It depends on Talk
  delivering reactions as system messages through the chat poll, which the code
  and tests assume but which has not yet been confirmed; see
  [future.md](future.md#talk-features-not-yet-used).

## 0.7

- **Successful health checks no longer fill the log.** The container's
  healthcheck asks `GET /healthz` every thirty seconds and uvicorn logged each
  one, which is about 2,900 identical lines a day with everything else buried
  between them. They are dropped now. A probe that *fails* - a 401 once
  `SABLE_HEALTH_TOKEN` is set, a 503 while something is wrong - is still logged,
  which is why this filters rather than turning the access log off, and every
  other route is untouched. `SABLE_LOG_HEALTH_CHECKS=true` brings them back.
- **The assistant can use tools, through Open WebUI.**
  `SABLE_LLM_BACKEND=openwebui` hands the whole agentic loop to Open WebUI: it
  offers the model your MCP servers, workspace tools and built-ins, executes
  whatever the model calls, feeds the result back and asks again until there is
  an answer. sable's own backend cannot do this and is not being changed — one
  request, one answer, portable to anything that speaks chat completions.
  It is a separate client because Open WebUI's loop is not chat completions at
  all. That loop lives in the code that streams events into a chat, so it only
  runs for a request naming a chat and an assistant message inside it, with
  `stream: true`, and the answer is written into the chat rather than returned.
  Each question is four calls: create a conversation, start the completion, wait
  for the tasks to drain, read the message. The conversation is deleted
  afterwards unless `SABLE_LLM_KEEP_CHATS` is set; sable keeps its own history
  as before.
  New settings: `SABLE_LLM_BACKEND`, `SABLE_LLM_TOOL_IDS`, `SABLE_LLM_FEATURES`,
  `SABLE_LLM_BUILTIN_TOOLS`, `SABLE_LLM_POLL_INTERVAL`, `SABLE_LLM_KEEP_CHATS`
  and `SABLE_LLM_SHOW_SOURCES`. The base URL must end in `/api` and an API key
  is required, both checked at startup, as is every feature name.
  Worth reading before turning it on: the tools run with the permissions of the
  account behind that API key, and anyone in a conversation can prompt the model
  into calling one. Accepted risk 15 in `docs/security.md` covers it.
- **The model is told what day it is.** The system prompt now ends with the
  current date, time and zone, set by `SABLE_TIMEZONE` or the host clock. This
  is not cosmetic: asked what gold was worth "right now", a model with no clock
  answered with the 1933 statutory price and a figure from two years ago, and
  had no way to notice either was stale. The same question with a date in it
  came back correct to the dollar. A zone name that is not an IANA zone is a
  startup error rather than a silent fall back to UTC.
- **A tool call nobody executed is no longer posted as an answer.** A backend
  that offers a model tools but does not run them hands the call straight back,
  and the reply then contains no answer at all - only `tool_calls`, and often a
  page of `reasoning_content` listing every tool the model considered. sable was
  posting that reasoning to the room and storing it in conversation history,
  where it taught the model to do the same thing next turn. Now it raises an
  error naming the tool that went unanswered, which is usually enough to find
  the misconfiguration at the backend. Reasoning still stands in for an
  ordinary empty answer, which is what that fallback was for.
- Text containing tool-call markup - a model writing `<|tool_call>` rather than
  calling one - is refused for the same reason, rather than posted verbatim.
- `SABLE_LLM_EXTRA_BODY` refuses `stream` and `messages`. Both are built by
  sable, and overriding `stream` in particular left it parsing an event stream
  as JSON.
- **A trailing newline in a conversation token returned 500.** `TOKEN_RE` ended in
  `$`, which in Python also matches immediately before a final newline, so
  `{"room": "abcd1234"}` plus one cleared the boundary check on `POST /notify`,
  reached httpx as a request path and raised there. The same value uppercased
  correctly answered 400. Anchored on the end of the string instead.
- **A replayed webhook is refused.** `X-Nextcloud-Talk-Random` was verified as
  part of the signature and then forgotten, so a captured webhook replayed for
  ever. The last 4096 are remembered and a repeat answers 401. What that buys is
  bounded and `docs/security.md` says so: the cache is process memory, a restart
  forgets it, and Talk sends no timestamp, so there is no age to enforce. The
  check runs after the signature, so nobody can fill the cache with randoms they
  invented and have real webhooks refused.
- **`SABLE_MAX_CONCURRENT_REPLIES`** caps open model calls, eight by default, `0`
  for no ceiling. Talk rate-limits the replies sable sends but not what it
  delivers, so a redelivered batch used to mean one open model call per event,
  each holding `SABLE_LLM_TIMEOUT`. The wait happens in the background task, so
  the webhook still answers 200 before asking for a slot and a full queue never
  becomes a Talk timeout.
- **`SABLE_BOT_SECRET_PREVIOUS`** closes the rotation window. Talk holds one
  secret per bot install, so rotating meant uninstall, install, restart — and
  every webhook in between answered 401. The previous secret is accepted for
  incoming verification only; outgoing calls are always signed with the current
  one. Clear it when the rotation is done.
- **`SABLE_ASK_ADMINS_ONLY`** restricts the ⁉️ reaction to `SABLE_ADMIN_USERS`,
  and **`SABLE_ASK_ROOMS`** narrows which conversations are cached for it at all.
  Accepted risks 6 and 7 — chat content held for every room the bot is in, and
  any participant able to forward somebody else's message to the model — now
  each have a switch. A refused reaction is logged and says nothing in the room:
  it asks nobody anything, and the message it points at belongs to a third party
  who has done nothing. Empty `SABLE_ASK_ROOMS` means every room, unlike
  `SABLE_AI_ROOMS` where empty means none; the asymmetry is deliberate, so that
  upgrading does not silently switch the feature off, and it is documented as
  such rather than hidden.
- **The admin decision refuses a bot actor itself.** An actor typed
  `Application` with an id like `users/maser` resolves an administrator's user
  id, so `is_admin_user` said yes to it. What stopped it was the `is_bot` early
  return in `handle` happening to run first — true, and only true until somebody
  moves a line. Both the command gate and `ctx.is_admin`, which is what `!help`
  filters on and what a custom command is told to use, now ask a check that
  refuses a bot wherever it is called from.
- **The webhook validates its conversation token.** It goes straight into the
  outbound URL path, and httpx normalises `..` segments, so an unexpected token
  could move a request off the bot API. The check sits in the handler rather than
  in `parse_event`, so the answer is a 400 naming the token instead of a
  misleading "unparseable event".
- `GET /healthz` reports whether Nextcloud was reachable on the last call
  (`true`, `false`, or `null` before any). The reachability state was already
  tracked and logged and nothing read it. The status stays `ok` and the code 200
  when Nextcloud is down, because a liveness probe that fails on a dependency
  gets a healthy process restarted.
- Startup names the settings that decide who the bot answers: a `concurrency:`
  line, an `ask rooms:` line, `(administrators only)` on the reaction, and a
  warning for the `SABLE_IGNORE_USERS` entries that look like display names,
  since matching a name means somebody can quietly un-ignore themselves by
  renaming. The `proxy trust:` line now says the setting is applied by sable's
  own uvicorn, which is the only thing that reads it.
- **The documentation is tested.** `tests/test_docs.py` asserts that every
  `SABLE_*` the code reads appears in `.env.example`, `compose.yaml` and
  `docs/configuration.md`, that each documented default is the real one, that the
  startup-log sample in `docs/deployment.md` has exactly the lines the code
  prints in the order it prints them, and that every internal link and anchor
  resolves. Eight stale facts had survived repeated review before this existed.
- The suite grew from 302 to over 500 tests, most of it input diversity it did
  not have: it previously exercised one conversation token and essentially one
  actor shape across 122 parsed events, which is how the `TOKEN_RE` bug went
  unnoticed. Guests, bots, `Application` actors and federated users are now
  distinct cases, and `signed_headers` mints a fresh random per call, so the
  replay refusal cannot ambush the next test that posts twice.

## 0.6

- **`SABLE_ADMIN_COMMANDS` and `SABLE_ADMIN_USERS`** put commands behind a list of
  people. Name the commands only administrators may run and the Nextcloud user
  ids that may run them; everything else stays open to everyone in the
  conversation, which is where all of them were before. `SABLE_ADMIN_COMMANDS=*`
  inverts it, closing every command and letting `SABLE_NORMAL_COMMANDS` name the
  exceptions — the safer shape once you have commands with side effects, since
  then the mistake is forgetting to open one rather than forgetting to close one.
  Restricting a command restricts its aliases with it, so `reset` covers
  `!forget`. `!help` lists only what the asker can run and marks the rest
  `(admin)` for those who can; running one you may not answers plainly and logs a
  warning naming you. An admin is matched on their user id and never their
  display name — anybody can set that to yours — so guests, having no user id,
  are never administrators. A command in both lists, or admin commands with no
  admin users, is refused at startup. `ctx.is_admin` is the hook for a custom
  command that needs something these two lists cannot say.
  Worth knowing: restricting `ai` restricts the `!ai` command and nothing else
  — a mention, an `SABLE_AI_ROOMS` conversation and `SABLE_ASK_REACTION` are not
  commands and still reach the model.
- **Display names and conversation names are flattened onto one line** before
  anything uses them: control characters removed, Unicode line separators
  collapsed, capped at 100 characters. Both are spliced into the model's prompt,
  so a newline in one was the difference between sitting inside the prompt and
  writing a line of it — a moderator renaming a room could add a line to the
  system message. They still choose what a room is called, not what shape the
  name arrives in.
- The `backend pin:` line at startup now says *why* it is off and what that
  means, rather than just `off`: with nothing to pin against, the unsigned
  backend header on each webhook decides where the replies to it go. The startup
  block and `--check` also name the admin commands.
- **`SABLE_API_DOCS`**, off by default, decides whether FastAPI's generated
  schema and its `/docs` and `/redoc` pages are served. They were on, as FastAPI
  ships them, which described every route, header and body shape to anybody who
  could reach the service — and the webhook has to be reachable. Off removes
  the routes, so they answer 404 rather than 401.
- **`SABLE_HEALTH_TOKEN`** puts `GET /healthz` behind an `X-Health-Token` header,
  compared in constant time. Empty, the default, leaves the probe open: a
  container healthcheck and a kubelet probe both call it without credentials.
  The image's healthcheck reads the variable and sends the header when it is set,
  so guarding the probe does not fail the container it is checking.
- **`SABLE_TRUSTED_PROXIES`** replaces trusting every client's
  `X-Forwarded-For`. uvicorn was started with `forwarded_allow_ips="*"`, so any
  client could claim any address and the access log would record it. The default
  is now loopback, uvicorn's own, and the value takes IP addresses, CIDR ranges,
  `*`, or nothing at all to ignore the headers entirely. A hostname or a range
  with host bits set is refused at startup rather than kept as a literal that
  silently never matches, which is what uvicorn does with one.
  Nothing in sable reads the client address, so this is about the access log
  telling the truth. **In Docker the proxy is another container and not
  loopback**, so name its network's subnet or its address.
- The comment claiming `/hook/<name>` could not be probed for which hooks exist
  was wrong, and is now accurate: an unknown name answers 404 while a configured
  one answers 401, so names are discoverable. Each hook has its own token, so
  that costs the name rather than access. Recorded as an accepted risk.
- The startup block gained `proxy trust:`, `api docs:` and `health check:` lines,
  and the sample of it in `docs/deployment.md` was three versions stale and
  missing the `hooks:` line; both it and the `--check` sample now match what the
  code prints.
- `docs/security.md` said conversation tokens were matched against
  `^[A-Za-z0-9]{4,64}$`. The code has always matched `^[a-z0-9]{4,64}$`, which is
  what Talk's own routes accept; the documentation was wrong, not the check.

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
