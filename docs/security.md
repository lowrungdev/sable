# Security posture

What protects what, what is deliberately trusted, and what is not protected at all. The
[accepted risks](#accepted-risks) are the part to read before this is reachable from anywhere
you do not control: several of them are choices rather than oversights.

## Threat model

sable is an HTTP service reachable from your Nextcloud. It holds one shared secret that lets it
post as a bot, optionally a token that lets other systems post alerts, optionally an API key for
a model backend, and optionally an app password for a Nextcloud user that can upload files.

The three things an attacker would want are to post as the bot, which needs the bot secret; to
read chat content, which needs to be in the conversation or to compromise the process; and to
make the bot talk to something it should not, which needs a forged webhook or a poisoned backend
URL. Everything below is about those.

## Trust boundaries

| Boundary | What protects it |
| --- | --- |
| Nextcloud to `POST /webhook` | HMAC-SHA256 over the random header plus the raw body, compared in constant time, before the body is parsed. Then the backend pin. |
| sable to the Talk bot API | The same shared secret, signed per endpoint with a fresh 32-byte random each time. |
| Anything to `POST /notify` | A separate bearer token, compared in constant time. Unset means the route answers 404. |
| sable to Nextcloud Files | An app password for a user account, used only to upload and share attachments. |
| sable to the model backend | Ordinary HTTPS with certificate verification; the API key travels as a bearer token. |
| Chat participants to commands | Nothing. Anyone in a conversation with the bot can run any command. |

## Authentication and integrity

Incoming webhooks are verified in [`signing.py`](../src/sable/signing.py), which computes
`HMAC-SHA256(random + raw_body, secret)` and compares it with `hmac.compare_digest`. A body
rewritten after signing fails, and a missing or empty header fails. The check happens in
[`app.py`](../src/sable/app.py) before `json.loads`, so unauthenticated input never reaches the
parser.

Outgoing calls sign exactly one value each: the message text when posting, the emoji when
reacting, the conversation token when asking about features. The random is 32 bytes from
`secrets.token_hex` and is never reused.

`SABLE_BOT_SECRET` must be 40 to 128 characters, the same range Nextcloud accepts, so a
two-character secret cannot be configured by accident.

Backend pinning matters more than it first appears. A signed event carries the server's own base
URL in `X-Nextcloud-Talk-Backend`, and that is where replies go. With `SABLE_PIN_BACKEND` on,
which is the default, an event claiming any other backend is refused with a 403. Without it, a
replayed webhook could aim the bot's replies, and its signed credentials, at a server of the
attacker's choosing.

Room tokens are validated before use: `/notify` accepts an alias from `SABLE_NOTIFY_ROOMS` or a
token matching `^[A-Za-z0-9]{4,64}$`, and anything else is a 400 rather than a request to
Nextcloud.

TLS is never disabled. There is no `verify=False` anywhere and no setting that could add one.
An internal or self-signed Nextcloud certificate is handled by lending the container the host's
CA bundle and pointing Python at it with `SSL_CERT_FILE`, which *replaces* the trust store
rather than extending it — so it must name the complete bundle, public roots included. Details
in [deployment.md](deployment.md#if-your-nextcloud-uses-an-internal-or-self-signed-certificate).

## Abuse resistance

Events from actors Talk marks as applications, or whose id starts with `bots/`, are ignored, so
two bots in one room cannot start answering each other. Every event is de-duplicated on the
conversation, type, message id, actor and reaction together, keeping the last 512, so a
redelivered webhook produces one reply rather than two while two people reacting to the same
message remain two distinct events.

Replies are truncated at `SABLE_MAX_MESSAGE_CHARS`, since Talk rejects anything over 32000 with
a 413. The webhook returns 200 immediately and does its work in the background, so a slow model
cannot hold Nextcloud's request open or provoke retries. A reply that fails to post is a logged
warning rather than an unhandled exception, so an unreachable Nextcloud produces log lines
instead of stray tracebacks.

## Secrets

| Secret | What it grants | Rotation |
| --- | --- | --- |
| `SABLE_BOT_SECRET` | Posting as the bot, in either direction | `occ talk:bot:uninstall`, then install with a new value, then restart |
| `SABLE_NOTIFY_TOKEN` | Posting into the aliased conversations | Change the variable and restart, then update callers |
| `SABLE_NEXTCLOUD_PASSWORD` | Everything that Nextcloud user can do | Revoke the app password in Nextcloud, generate another |
| `SABLE_LLM_API_KEY` | Your model provider's billing | At the provider |

All of them come from the environment. `.env` is in both `.gitignore` and `.dockerignore`, and
`.env.example` ships with empty values. `compose.yaml` reads the secrets as `${VAR}`
interpolations rather than literals, so the committed file never contains one and a missing
value fails at `docker compose up` with a message naming it.

No secret is logged or printed. `sable --check` and the startup block print the bot name,
Nextcloud URL, prefix, model, upload account and so on, but never the secret, the notify token,
the app password or the API key. Conversation tokens *are* logged at INFO, and for a
conversation shared by public link the token is what grants access, so treat logs accordingly.

## Container and host

The image runs as a non-root user, uid 10001, created in the image, and is built from a
patch-pinned base rather than a floating tag. Dependencies are locked and hash-verified:
`uv.lock` pins 32 packages and `uv sync --locked` fails rather than resolving something else. uv
itself is uninstalled in the same layer, so it does not ship.

Nothing is written to disk. Conversation history and the message cache live in process memory
only, which is also why there is nothing to back up.

`compose.yaml` publishes to `127.0.0.1:8080` only, expecting a TLS-terminating proxy in front,
and the healthcheck talks only to localhost. The systemd unit in [deployment.md](deployment.md)
sets `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`, an empty `CapabilityBoundingSet`
and a memory cap.

## What leaves your infrastructure

Messages the assistant answers go to your configured `SABLE_LLM_BASE_URL`, along with the recent
history of that conversation and the speakers' display names. Nothing else leaves.

If that backend is a hosted API, chat content leaves your network. A local backend such as
Ollama, vLLM or llama.cpp avoids the question entirely, and setting `SABLE_LLM_MODEL` to empty
disables the assistant while leaving commands working.

Attachments do not leave: they are uploaded into your own Nextcloud, in the upload account's
Files, and shared from there.

## Accepted risks

These are known and deliberate. Decide for yourself whether they are acceptable.

1. The bot secret is symmetric. Anyone holding it can post as the bot and forge webhooks to it.
   It is the one value that matters most.

2. Commands have no authorization. Anyone in the conversation, guests included, can run any
   command. If you add one that touches production, gate it yourself on
   `ctx.event.actor.user_id`.

3. `/notify` is a single shared token with no per-caller identity and no rate limiting. A leaked
   token lets anyone post into the aliased conversations. Keep the endpoint off the public
   internet where you can.

4. Upstream error text can reach the chat room. With `SABLE_REPORT_ERRORS` on, a failure posts
   the error, which includes up to 400 characters of the model backend's or Nextcloud's response
   body. If your backend is chatty about credentials in its errors, turn it off.

5. Model output is posted verbatim and nothing filters it. A participant can try to steer the
   model through prompt injection; the realistic worst case is embarrassing or misleading text,
   since Talk renders Markdown and sanitises HTML itself.

6. Chat content sits in process memory for up to `SABLE_HISTORY_TTL`: the assistant's history
   per conversation, and, while `SABLE_ASK_REACTION` is set, the last `SABLE_MESSAGE_CACHE`
   messages of every conversation the bot is in. None of it is written to disk, but it would
   appear in a core dump. Clearing `SABLE_ASK_REACTION` disables that cache entirely.

7. Anyone in a conversation can send any message to the model by reacting to it, including
   messages they did not write. That is the feature working as intended, but it means one
   participant can forward another's words to your model backend without saying anything in the
   room.

8. There is no request size limit in the application for webhooks. The HMAC covers the whole
   body, so the body has to be read before it can be checked. Cap it at the proxy. Attachments
   on `/notify` are a separate matter and *are* capped, by `SABLE_MAX_UPLOAD_BYTES`, read in
   chunks so an oversized body is refused rather than buffered whole.

9. There is no rate limiting of our own. Talk rate-limits bots with HTTP 429; nothing limits how
   fast `/notify` can be called, so the real memory ceiling for attachments is the cap times the
   number of concurrent callers.

10. Logs are the only audit trail. There is no separate audit log and no metrics endpoint. At
    INFO they record who used the bot, which command or trigger, and in which conversation,
    which answers "who asked it that" but not what was said. DEBUG puts chat content in the log:
    prompts, command arguments, and the text of any message a reaction referred to. Treat a
    DEBUG-level log as containing conversation content.

11. File attachments need a second, much larger credential. With `SABLE_NEXTCLOUD_USER` set,
    sable holds an app password for a Nextcloud user, and an app password cannot be scoped — it
    reaches that user's Files, Contacts and Calendar. The bot secret can only post messages, so
    this is by far the biggest expansion of what a compromise of sable would reach. Give it a
    dedicated account that owns nothing else, and leave both variables empty if you do not need
    attachments. Uploads land in `SABLE_UPLOAD_PATH` inside that user's own Files, and filenames
    from callers are sanitised and made unique, so one alert cannot overwrite another or climb
    out of the folder.

12. Hook tokens can travel in a URL. Services like Komodo cannot set headers, so `/hook/{name}`
    accepts `?token=`, and proxies and access logs will record it. Each hook has its own token
    to contain that: one exposed in a log costs you that hook rather than everything `/notify`
    can reach. Whoever holds a hook URL can write arbitrary text into that conversation, since
    the payload becomes the message.

13. CI holds credentials: the registry password and a runner token with write access to the
    repository, used to create releases. Anyone who can change a workflow on a branch CI runs
    can reach both.

## Hardening checklist

- [ ] `SABLE_BOT_SECRET` generated with `openssl rand -hex 32`, unique to this bot
- [ ] `SABLE_NEXTCLOUD_URL` set and `SABLE_PIN_BACKEND` left on
- [ ] TLS in front, the application bound to localhost or a private network
- [ ] A body size limit at the proxy
- [ ] `SABLE_NOTIFY_TOKEN` distinct from the bot secret, or unset if unused
- [ ] The upload account, if any, dedicated to this and owning nothing else
- [ ] The upload account listed in `SABLE_IGNORE_USERS`, so its own file messages are not acted on
- [ ] Egress restricted to Nextcloud and the model backend
- [ ] `SABLE_REPORT_ERRORS=false` if upstream errors should not reach the room
- [ ] Every custom command reviewed as code anyone in the room can trigger
- [ ] The image pinned by version or digest on the host rather than `latest`

## Reporting a problem

This is an internal project, so raise it as an issue on the repository or privately with
whoever runs the instance. There is no separate embargo process.
