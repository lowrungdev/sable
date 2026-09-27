# Security posture

What protects what, what is deliberately trusted, and what is not protected at all. Read the
[accepted risks](#accepted-risks) section before deciding this is safe for your environment —
several entries there are choices, not oversights.

## Threat model in one paragraph

sable is an HTTP service reachable from your Nextcloud, holding one shared secret that lets it
post as a bot, optionally a token that lets outside systems post alerts, and optionally an API
key for a model backend. The valuable things an attacker could want are: **posting as the bot**
(needs the bot secret), **reading chat content** (needs to be in the conversation, or to
compromise the process), and **making the bot talk to something it should not** (needs to forge
a signed webhook, or to poison the backend URL). Everything below is about those three.

## Trust boundaries

| Boundary | What protects it |
| --- | --- |
| Nextcloud → `POST /webhook` | HMAC-SHA256 over `X-Nextcloud-Talk-Random` + the **raw** request body, compared in constant time, **before the body is parsed**. Then the backend pin. |
| sable → Nextcloud bot API | The same shared secret, signed per endpoint with a fresh 32-byte random per request. |
| Anything → `POST /notify` | A separate bearer token, compared in constant time. Unset means the route answers 404 to everyone. |
| sable → model backend | Ordinary HTTPS with certificate verification; the API key travels as a bearer token. |
| Chat participants → commands | **Nothing.** Anyone in a conversation with the bot can run any command. See [accepted risks](#accepted-risks). |

## Authentication and integrity

**Incoming webhooks.** [`signing.py`](../src/sable/signing.py) computes
`HMAC-SHA256(random + raw_body, secret)` and compares with `hmac.compare_digest`. A body
rewritten after signing fails; a missing or empty header fails. Verification happens in
[`app.py`](../src/sable/app.py) before `json.loads`, so unauthenticated input never reaches the
parser.

**Outgoing calls.** Each call signs exactly one value — the message text for `/message`, the
emoji for a reaction, the conversation token for `ask-features` — with a 32-byte random from
`secrets.token_hex`, never reused.

**Secret strength is enforced at startup.** `SABLE_BOT_SECRET` must be 40–128 characters, the
same range Nextcloud accepts, so a two-character secret cannot be configured by accident.

**Backend pinning.** A signed event carries the server's own base URL in
`X-Nextcloud-Talk-Backend`, and that is where replies go. With `SABLE_PIN_BACKEND` on (the
default), an event claiming any other backend is refused with 403. Without it, a replayed
webhook could aim the bot's replies — and its signed credentials — at a server of the
attacker's choosing.

**Room tokens are validated.** `/notify` accepts an alias from `SABLE_NOTIFY_ROOMS` or a token
matching `^[A-Za-z0-9]{4,64}$`; anything else is a 400 rather than a request to Nextcloud.

**TLS is never disabled.** There is no `verify=False` anywhere and no setting that could add one.
Certificates are always verified, in both directions.

**An internal or self-signed Nextcloud certificate** is handled by lending the container the
host's CA bundle and pointing Python at it — `SSL_CERT_FILE`, set in `compose.yaml`. Note that
this *replaces* the trust store rather than extending it, so it must name the complete bundle
(public roots plus your internal CA); a file holding only the internal CA would break every
public HTTPS call, including the model backend. Installing the CA in the container's system store
alone does nothing, because httpx verifies against its bundled certifi store unless that variable
redirects it. Full detail in
[deployment.md](deployment.md#if-your-nextcloud-uses-an-internal-or-self-signed-certificate).

## Abuse resistance

| Behaviour | Why |
| --- | --- |
| Events from actors of type `Application` or id `bots/…` are ignored | Two bots in one room cannot start answering each other |
| Events de-duplicated by `(conversation, type, message id)`, last 512 | A redelivered webhook produces one reply, not two |
| Replies truncated at `SABLE_MAX_MESSAGE_CHARS` (30000) | Talk rejects over 32000 with a 413 |
| The webhook returns `200` immediately and works in the background | A slow model cannot hold Nextcloud's request open or cause retries |
| A failed reply is a logged warning, not an unhandled exception | No stray tracebacks, no crash loop from an unreachable Nextcloud |

## Secrets

| Secret | Holds | Rotation |
| --- | --- | --- |
| `SABLE_BOT_SECRET` | Post as the bot, in either direction | `occ talk:bot:uninstall` then `install` with a new value, then restart |
| `SABLE_NOTIFY_TOKEN` | Post into the aliased conversations | Change the env var and restart; update callers |
| `SABLE_LLM_API_KEY` | Your model provider's billing | At the provider |

- All three come from the environment. `.env` is in `.gitignore` **and** `.dockerignore`, and
  `.env.example` ships with empty values.
- `compose.yaml` reads them as `${VAR}` interpolations rather than literals, so the committed
  file never contains a secret, and a missing one fails at `docker compose up` with a message.
- **No secret is logged or printed.** `sable --check` prints the bot name, Nextcloud URL, command
  prefix, model, AI rooms and whether alerting is on — never the secret, the notify token or the
  API key.
- Conversation tokens *are* logged at INFO (`running ping for users/alice in abcd1234`). For a
  conversation shared by public link, the token is what grants access, so treat logs accordingly.

## Container and host

- Runs as a **non-root user** (uid 10001), created in the image.
- Base image pinned to a patch release (`python:3.14.7-slim`), not a floating tag.
- **Dependencies are locked and hash-verified**: `uv.lock` pins 31 packages, and `uv sync
  --locked` fails rather than resolving something else. uv itself is uninstalled in the same
  layer, so it is not in the shipped image.
- **No state on disk.** Nothing is persisted; conversation history lives in process memory only.
- `compose.yaml` publishes to `127.0.0.1:8080` only, expecting a TLS-terminating proxy.
- The systemd unit in [deployment.md](deployment.md) sets `NoNewPrivileges`, `ProtectSystem=strict`,
  `ProtectHome`, an empty `CapabilityBoundingSet` and `MemoryMax`.
- The healthcheck talks only to `127.0.0.1`.

## What leaves your infrastructure

| Data | Goes where |
| --- | --- |
| Messages the assistant answers, plus the recent history of that conversation and the speakers' display names | Your configured `SABLE_LLM_BASE_URL` |
| Nothing else | — |

If that backend is a hosted API, **chat content leaves your network**. A local backend (Ollama,
vLLM, llama.cpp) or a self-hosted gateway avoids the question entirely, and
`SABLE_LLM_MODEL=""` disables the assistant while leaving commands working.

## Accepted risks

These are known and deliberate. Decide for yourself whether they are acceptable.

1. **The bot secret is symmetric.** Anyone holding it can post as the bot *and* forge webhooks
   to it. It is the one value that matters.
2. **Commands have no authorization.** Anyone in the conversation — including guests, whose
   actor id is `guests/…` — can run any command. If you add a command that touches production,
   gate it yourself on `ctx.event.actor.user_id`.
3. **`/notify` is one shared token** with no per-caller identity and no rate limiting. A leaked
   token means anyone can post into the aliased conversations. Keep the endpoint off the public
   internet where you can.
4. **Upstream error text can reach the chat room.** With `SABLE_REPORT_ERRORS` on (the default),
   a failure posts the error, which includes up to 400 characters of the model backend's or
   Nextcloud's response body. If your backend is chatty about credentials in error responses,
   set `SABLE_REPORT_ERRORS=false`.
5. **Model output is posted verbatim.** Nothing filters it. A participant can try to steer the
   model through prompt injection; the worst realistic outcome is embarrassing or misleading
   text, since Talk renders Markdown and sanitises HTML itself.
6. **Chat content sits in process memory** for up to `SABLE_HISTORY_TTL` (default one hour):
   the assistant's per-conversation history, and — while `SABLE_ASK_REACTION` is set — the last
   `SABLE_MESSAGE_CACHE` messages of every conversation the bot is in, so a reaction can name one.
   Nothing is written to disk, but it would appear in a core dump. Setting `SABLE_ASK_REACTION=""`
   disables that cache entirely.
7. **Anyone in a conversation can send any message to the model** by reacting to it with ⁉️,
   including messages they did not write. That is the feature working as intended, but it means
   one participant can forward another's words to your model backend without saying anything in
   the room.
8. **No request size limit in the application.** The HMAC covers the whole body, so the body
   must be read before it can be checked. Cap it at the proxy — the nginx example in
   [deployment.md](deployment.md) sets `client_max_body_size 1m`.
9. **No rate limiting of our own.** Talk rate-limits bots (HTTP 429); nothing limits how fast
   `/notify` can be called.
10. **Logs are the only audit trail.** There is no separate audit log, and no metrics endpoint.
    At `INFO` they record who used the bot, which command or trigger, and in which conversation —
    enough to answer "who asked it that?" but not what was said. **`DEBUG` puts chat content in
    the log**: prompts, command arguments and the text of any message a ⁉️ referred to. Treat a
    DEBUG-level log as containing conversation content, with whatever that implies for where it
    is shipped and how long it is kept.
11. **File attachments need a second, much larger credential.** With
    `SABLE_NEXTCLOUD_USER` set, sable holds an app password for a Nextcloud user. That
    password cannot be scoped: it can read and write that user's Files, Contacts and Calendar.
    The bot secret can only post messages, so this is by far the biggest expansion of what a
    compromise of sable would reach. Give it a dedicated account that owns nothing else, and
    leave both variables empty if you do not need attachments — /notify then stays text-only.
    Uploads land in `SABLE_UPLOAD_PATH` inside that user's own Files and are capped at
    `SABLE_MAX_UPLOAD_BYTES`; filenames from callers are sanitised and made unique, so one
    alert cannot overwrite another or climb out of the folder.
12. **CI holds credentials**: the registry password (`RUNDECK_KEY_VALUE`) and a runner token with
    write access to the repository, used to create releases. Anyone who can change a workflow on
    a branch that CI runs can reach both.

## Hardening checklist

- [ ] `SABLE_BOT_SECRET` from `openssl rand -hex 32`, unique to this bot
- [ ] `SABLE_NEXTCLOUD_URL` set, `SABLE_PIN_BACKEND` left on
- [ ] TLS in front, application bound to `127.0.0.1` or a private network
- [ ] A body size limit at the proxy
- [ ] `SABLE_NOTIFY_TOKEN` distinct from the bot secret, or unset if unused
- [ ] Egress restricted to Nextcloud and the model backend
- [ ] `SABLE_REPORT_ERRORS=false` if upstream errors should not reach the room
- [ ] Every custom command reviewed as code anyone in the room can trigger
- [ ] The image pinned by version or digest on the host, not `:latest`

## Reporting a problem

This is an internal project: raise it as an issue on the repository, or privately to whoever
runs the instance. There is no separate embargo process.
