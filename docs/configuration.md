# Configuration

Every setting is a `SABLE_`-prefixed environment variable. There is no config file format.
[`.env.example`](../.env.example) is a copy-ready version of this document's defaults.

## How settings are loaded

1. `sable` reads `./.env` if it exists (`--env-file PATH` to point elsewhere, a path that does
   not exist is silently skipped).
2. **Real environment variables always win** over anything in the `.env` file, which is what
   makes `docker compose` overrides and one-off `SABLE_LOG_LEVEL=DEBUG sable` work.
3. `--host` and `--port` on the command line override `SABLE_HOST` / `SABLE_PORT`.

Under Docker Compose, [`compose.yaml`](../compose.yaml) carries the same list in its
`environment:` block — every variable, commented out with its default, and only
`SABLE_BOT_SECRET` active. Those entries **override** the `.env` file, which is itself optional
there; secrets are written as `${VAR}` lookups so their values stay out of the committed file.

Check the result before deploying — this validates everything and exits without starting a
server:

```bash
sable --check
```

```
sable 0.1.0 config OK
  bot name:   sable
  nextcloud:  https://cloud.example.org
  prefix:     !
  model:      gpt-4o-mini @ https://api.openai.com/v1
  ai rooms:   *
  notify:     enabled aliases: alerts, deploys
```

A bad value exits `2` with a message naming the variable. Configuration errors are fatal at
startup by design: better a failed deploy than a bot that silently ignores half its settings.

### Value formats

| Kind | Accepted |
| --- | --- |
| Boolean | `1`, `true`, `yes`, `on` / `0`, `false`, `no`, `off` — case-insensitive. Anything else is an error. |
| Number | Plain integer or decimal. Empty means "use the default". |
| List | Comma-separated; whitespace around entries is trimmed. |
| Map | `alias=value,other=value2`, or a JSON object: `{"alias": "value"}`. |
| JSON | A JSON **object**, e.g. `{"top_k": 40}`. |

Values are trimmed, and URLs have trailing slashes stripped, so a stray space or slash in a
`.env` file will not break anything.

## Talk bot identity

| Variable | Default | Notes |
| --- | --- | --- |
| `SABLE_BOT_SECRET` | **required** | The shared secret, identical to the one given to `occ talk:bot:install`. Must be 40–128 characters — Nextcloud enforces the same range. `openssl rand -hex 32` gives a good 64-char value. |
| `SABLE_BOT_NAME` | `sable` | Drives mention detection, and should match the name you installed the bot under. `@sable ...` or `sable: ...` at the start of a message triggers the assistant. |
| `SABLE_NEXTCLOUD_URL` | *(from the webhook header)* | Your Nextcloud base URL, no trailing slash, e.g. `https://cloud.example.org`. Optional for the webhook path, because each signed event carries the server URL in `X-Nextcloud-Talk-Backend`. **Required** if you enable `/notify`, which has no incoming request to learn it from. |
| `SABLE_PIN_BACKEND` | `true` | Reject webhooks whose backend header is not `SABLE_NEXTCLOUD_URL`. Automatically disabled when no URL is set. Leave it on unless the header genuinely differs from the URL you configured — see the 403 entry in [deployment.md](deployment.md#troubleshooting). |

## Chat behaviour

| Variable | Default | Notes |
| --- | --- | --- |
| `SABLE_COMMAND_PREFIX` | `!` | Any string. `/` is a reasonable alternative; note Talk itself uses `/` for some client-side commands. |
| `SABLE_AI_ROOMS` | *(empty)* | Conversations where **every** message goes to the model, no mention needed. Comma-separated conversation **tokens or names**, or `*` for all of them. Empty means mentions and `!ai` only. See [below](#which-identifier-goes-in-sable_ai_rooms). |
| `SABLE_REPLY_AS_REPLY` | `false` | Post answers as threaded replies to the triggering message instead of plain messages. |
| `SABLE_THINKING_REACTION` | *(empty)* | A single emoji stuck on the triggering message while the model works, then removed — e.g. `👀`. Empty disables it, which saves two API calls per answer. Failures here are ignored; a reaction is never load-bearing. |
| `SABLE_ASK_REACTION` | `⁉️` | React to any message with this and the bot sends that message to the model, answering in a reply threaded under it. Empty disables the feature **and** the message cache behind it. Needs `--feature reaction` at install. |
| `SABLE_MESSAGE_CACHE` | `200` | Recent messages remembered per conversation, so a reaction can name one. Expires with `SABLE_HISTORY_TTL`. |
| `SABLE_UNKNOWN_COMMAND_HINT` | `true` | Reply "I have no `!foo` command" on an unknown command. Turn off in busy rooms where people use other bots with the same prefix. |
| `SABLE_REPORT_ERRORS` | `true` | Post failures into the conversation (`⚠️ Sorry — …`) as well as logging them. Off means failures are logged only and the room stays quiet. |
| `SABLE_STARTUP_CHECK` | `true` | Call Nextcloud's `status.php` at startup and log what answered, so a wrong URL or an untrusted certificate shows up at boot. Never fatal. Needs `SABLE_NEXTCLOUD_URL`. |
| `SABLE_MAX_MESSAGE_CHARS` | `30000` | Replies longer than this are clipped with a `_[truncated]_` marker. Talk hard-rejects anything over 32000 with HTTP 413, which is the real ceiling. |

### When does the assistant answer?

| The message | Answers? |
| --- | --- |
| `!ping` | Command, always |
| `@sable how are you` / `sable: how are you` | Assistant |
| `hey @sable look at this` (mention mid-sentence) | Assistant, with the whole message as the prompt |
| `!ai how are you` | Assistant, no mention needed |
| `sabletooth tigers` | No — mention matching respects word boundaries |
| `just chatting` | Only in a conversation listed in `SABLE_AI_ROOMS` |
| Anything from another bot | Never |
| A ⁉️ reaction on any message | Assistant, answering that message |

### Which identifier goes in `SABLE_AI_ROOMS`

Either the conversation's **token** or its **display name**:

```ini
SABLE_AI_ROOMS=a1b2c3d4          # the token, from the conversation's URL
SABLE_AI_ROOMS=AI                # the name shown in Talk
SABLE_AI_ROOMS=AI,a1b2c3d4       # a mix is fine
SABLE_AI_ROOMS=*                 # every conversation the bot is in
```

The token is the last segment of the conversation's URL —
`https://cloud.example.org/call/a1b2c3d4` → `a1b2c3d4`. Names match ignoring case and surrounding
space, so `ai`, `AI` and `  AI  ` all match a conversation called "AI".

**Prefer tokens where it matters.** A token never changes; a name can be changed by any moderator
of the conversation, which would silently start or stop the bot answering everything in it. Names
are also not unique — two conversations called "AI" would both match.

If a room is not behaving as you expect, `SABLE_LOG_LEVEL=DEBUG` prints both identifiers for
every message it decided to ignore, so you can see exactly what to configure:

```
message in a1b2c3d4 ('AI') was not for me - no prefix, no mention, and not an AI room
```

### Asking about a message by reacting to it

React with `SABLE_ASK_REACTION` (⁉️ by default) and the bot answers the message you reacted to,
in a reply threaded under it. It works on anyone's message, the bot's own answers included, which
makes it a quick way to ask a follow-up.

The catch: **a reaction event carries the message id, not its text.** The bot API cannot read a
message back — that needs a user account rather than bot credentials — so sable can only answer
about messages it saw arrive, and keeps the last `SABLE_MESSAGE_CACHE` per conversation for that
purpose. React to something older, or posted before the bot joined, and it says so instead of
guessing. Nothing is cached at all when `SABLE_ASK_REACTION` is empty.

## Conversation memory

| Variable | Default | Notes |
| --- | --- | --- |
| `SABLE_HISTORY_TURNS` | `12` | Turns kept per conversation; a turn is a question and its answer, so this holds 24 messages. |
| `SABLE_HISTORY_TTL` | `3600` | Seconds before a message ages out, so a room picked up tomorrow does not resume yesterday's thread. `0` disables expiry. |

History is per-conversation, in-process and lost on restart, and `!reset` clears one
conversation. It is a cache, not a record. Raising `SABLE_HISTORY_TURNS` costs tokens on every
request, since the whole window is sent each time.

## The model

Any endpoint that implements OpenAI's `POST /chat/completions` works.

| Variable | Default | Notes |
| --- | --- | --- |
| `SABLE_LLM_BASE_URL` | `https://api.openai.com/v1` | Base URL **including** the version segment; `/chat/completions` is appended. |
| `SABLE_LLM_API_KEY` | *(empty)* | Sent as `Authorization: Bearer …`. Omit for a local backend that wants no auth — the header is then not sent at all. |
| `SABLE_LLM_MODEL` | *(empty)* | **Empty disables the assistant entirely**: commands still work, no model is ever called, and mentions are ignored. |
| `SABLE_LLM_SYSTEM_PROMPT` | *(a short default)* | The system message. The conversation's name is appended automatically, so the model knows which room it is in. |
| `SABLE_LLM_TEMPERATURE` | *(unset)* | Omitted from the request when unset, letting the backend's own default apply. Some newer models reject an explicit temperature. |
| `SABLE_LLM_MAX_TOKENS` | *(unset)* | Sent as `max_tokens`. Also omitted when unset. |
| `SABLE_LLM_TIMEOUT` | `120` | Seconds to wait for a completion. On timeout the room gets an error message (if `SABLE_REPORT_ERRORS` is on) rather than silence. |
| `SABLE_LLM_EXTRA_BODY` | `{}` | A JSON object merged into the request body **last**, so it overrides everything above. The escape hatch for provider-specific fields. |

### Provider recipes

| Backend | `SABLE_LLM_BASE_URL` | `SABLE_LLM_MODEL` | Key |
| --- | --- | --- | --- |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` | required |
| Ollama (local) | `http://localhost:11434/v1` | `llama3.1:8b` | not needed |
| vLLM | `http://localhost:8000/v1` | the served model name | as configured |
| llama.cpp server | `http://localhost:8080/v1` | any | not needed |
| LiteLLM proxy | `http://localhost:4000/v1` | whatever the proxy routes | proxy key |
| OpenRouter | `https://openrouter.ai/api/v1` | `anthropic/claude-sonnet-4.5` | required |
| Groq | `https://api.groq.com/openai/v1` | `llama-3.3-70b-versatile` | required |
| Together | `https://api.together.xyz/v1` | `meta-llama/Llama-3.3-70B-Instruct-Turbo` | required |

Azure OpenAI does not use the same URL shape; put LiteLLM (or any gateway) in front of it and
point sable at the gateway.

Reasoning models that spend their whole budget before answering would otherwise return an empty
message; sable falls back to `reasoning_content` when a provider supplies it, and reports a
clear error naming `finish_reason` when there is nothing at all. If answers come back
truncated, raise `SABLE_LLM_MAX_TOKENS` or lower the reasoning effort via
`SABLE_LLM_EXTRA_BODY`.

## Alerting endpoint

| Variable | Default | Notes |
| --- | --- | --- |
| `SABLE_NOTIFY_TOKEN` | *(empty)* | Bearer token for `POST /notify`. **Empty disables the route**, which then answers 404 to everyone. Unrelated to the bot secret — generate a separate value. |
| `SABLE_NOTIFY_ROOMS` | *(empty)* | Aliases so callers need not know conversation tokens: `alerts=a1b2c3d4,deploys=e5f6g7h8`. An unrecognised name is treated as a raw token and must look like one, otherwise the request is a 400. |

Setting `SABLE_NOTIFY_TOKEN` without `SABLE_NEXTCLOUD_URL` is a startup error: an outbound-only
message has no incoming webhook to learn the server address from.

## Process

| Variable | Default | Notes |
| --- | --- | --- |
| `SABLE_HOST` | `0.0.0.0` | Bind address. Use `127.0.0.1` when a reverse proxy on the same host is the only client. |
| `SABLE_PORT` | `8080` | |
| `SABLE_LOG_LEVEL` | `INFO` | `INFO` logs the lifecycle, the resolved configuration, and who used what. `DEBUG` adds message text, prompts, command arguments, every outbound HTTP call, and why a message was *not* acted on — the fastest way to debug mention and prefix matching, but it puts chat content in the log. See [deployment.md](deployment.md#what-the-log-tells-you). |

## TLS trust, for an internal or self-signed Nextcloud

There is no `SABLE_` setting for this, and no way to disable certificate verification. Trust is
configured the standard way, with the variable OpenSSL and httpx already understand:

| Variable | Notes |
| --- | --- |
| `SSL_CERT_FILE` | Path to a CA bundle **inside the container**. `compose.yaml` mounts the host's `/etc/ssl/certs` read-only and sets this to `/etc/ssl/certs/ca-certificates.crt`. |

It **replaces** the trust store rather than adding to it, so the file must be the complete bundle
— public roots *and* your internal CA. Pointing it at a file holding only your CA makes the
internal Nextcloud verify and every public HTTPS call fail. `SSL_CERT_DIR` is a trap here: OpenSSL
only finds certificates in such a directory by hashed filename, so a plain folder of `.crt` files
trusts nothing while still replacing the store. See
[deployment.md](deployment.md#if-your-nextcloud-uses-an-internal-or-self-signed-certificate).

## Worked examples

### Commands only, no model

```ini
SABLE_BOT_SECRET=<64 hex chars>
SABLE_NEXTCLOUD_URL=https://cloud.example.org
```

Nothing else is needed. Mentions are ignored, `!help` works.

### Assistant on a local model, answering everything in two rooms

```ini
SABLE_BOT_SECRET=<64 hex chars>
SABLE_NEXTCLOUD_URL=https://cloud.example.org
SABLE_LLM_BASE_URL=http://localhost:11434/v1
SABLE_LLM_MODEL=llama3.1:8b
SABLE_LLM_TIMEOUT=300
SABLE_AI_ROOMS=a1b2c3d4,e5f6g7h8
SABLE_THINKING_REACTION=👀
```

A local model on modest hardware is slow, hence the longer timeout and the reaction so people
can see it is working.

### Alerting only

```ini
SABLE_BOT_SECRET=<64 hex chars>
SABLE_NEXTCLOUD_URL=https://cloud.example.org
SABLE_NOTIFY_TOKEN=<a different random value>
SABLE_NOTIFY_ROOMS=alerts=a1b2c3d4,deploys=e5f6g7h8
SABLE_UNKNOWN_COMMAND_HINT=false
```

The bot still answers `!ping`, but its job is to relay what CI posts to `/notify`.

### Everything, hosted model, quiet about its own failures

```ini
SABLE_BOT_SECRET=<64 hex chars>
SABLE_NEXTCLOUD_URL=https://cloud.example.org
SABLE_BOT_NAME=sable
SABLE_COMMAND_PREFIX=!
SABLE_LLM_BASE_URL=https://api.openai.com/v1
SABLE_LLM_API_KEY=sk-...
SABLE_LLM_MODEL=gpt-4o-mini
SABLE_LLM_MAX_TOKENS=800
SABLE_LLM_SYSTEM_PROMPT=You are sable, the ops assistant. Be terse. Prefer bullet points.
SABLE_HISTORY_TURNS=20
SABLE_REPLY_AS_REPLY=true
SABLE_REPORT_ERRORS=false
SABLE_NOTIFY_TOKEN=<a different random value>
SABLE_NOTIFY_ROOMS=alerts=a1b2c3d4
SABLE_LOG_LEVEL=INFO
```

## Startup errors and what they mean

| Message | Fix |
| --- | --- |
| `SABLE_BOT_SECRET is required` | Set it to the value you gave `occ talk:bot:install`. |
| `SABLE_BOT_SECRET must be 40-128 characters` | Nextcloud's own limit. `openssl rand -hex 32`. |
| `SABLE_NEXTCLOUD_URL is required when SABLE_NOTIFY_TOKEN is set` | Set the URL, or drop the notify token. |
| `… must be a boolean` / `… must be an integer` / `… must be a number` | A typo in the value; see [value formats](#value-formats). |
| `… is not valid JSON` / `must be a JSON object` | `SABLE_LLM_EXTRA_BODY` needs an object: `{"top_k": 40}`. Quote it in a shell. |
| `… entries must look like alias=token` | `SABLE_NOTIFY_ROOMS` wants `name=token` pairs or a JSON object. |

Runtime problems — 401s, 403s, silence — are in
[deployment.md](deployment.md#troubleshooting).
