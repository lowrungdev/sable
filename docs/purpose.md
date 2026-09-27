# Purpose

What sable is for, what it deliberately is not, and why it is built the way it is.

## The problem

Nextcloud Talk is where a lot of teams already are. Getting anything *into* it programmatically
— an alert, an answer, a small internal tool — normally means one of:

- a **PHP Nextcloud app**, which is a real app to build, ship and keep compatible with server
  releases;
- a **bot user account** that long-polls the chat API, which works but is chatty, needs an app
  password, appears as a person rather than a bot, and has no signature story;
- a **webhook bot** on Talk's official Bot API, which is the supported path but leaves you to
  implement the signature scheme, the ActivityStreams payloads and the reply endpoints.

sable is the third option, done once, with the three jobs most teams actually want from a chat
bot already in the box.

## What it does

### 1. Commands

A prefix router (`!` by default) over a registry. `!help`, `!ping`, `!whoami`, `!echo`, `!ai`,
`!reset` and `!version` ship with it; a new command is a decorated async function that returns
Markdown. This is the seam most people will use — the rest of the bot exists so that this part
is boring to write.

### 2. A model-agnostic assistant

Mention the bot and it answers through an OpenAI `/chat/completions`-compatible endpoint, with
a short rolling per-conversation history.

"Model-agnostic" is a hard requirement, not a nice-to-have: the chat-completions shape is the
one interface that OpenAI, Ollama, vLLM, llama.cpp, LiteLLM, OpenRouter, Groq and Together all
speak. Talking that shape over plain HTTP — rather than importing a vendor SDK — means
switching providers is a change to two environment variables, self-hosting a model needs no
code change, and there is exactly one dependency (`httpx`) doing the work.

Anything a specific provider wants that the common shape lacks goes in `SABLE_LLM_EXTRA_BODY`
as JSON and is merged into the request body last.

### 3. Alerting

`POST /notify` with a bearer token puts a message in a conversation. This is the "outbound
only" direction: CI, Prometheus' Alertmanager, a cron job, a deploy script. Aliases
(`alerts=a1b2c3d4`) mean callers never need to know conversation tokens.

## Non-goals

Things it does not do, so you do not go looking:

- **No polling and no bot user account.** It is a webhook bot; if you cannot expose an HTTPS
  endpoint to your Nextcloud server, this is the wrong tool.
- **No PHP / no Nextcloud app.** Nothing is installed into Nextcloud except one row from
  `occ talk:bot:install`.
- **No streaming replies.** Talk messages are posted whole; a token-by-token edit loop would
  hammer the API for little gain. One answer, one message.
- **No durable memory.** History is an in-process cache with a turn cap and a TTL. A restart
  forgets. If you need recall across restarts, replace `History` — it is one small class with
  three methods.
- **No retrieval, tools or function calling.** The assistant sees the conversation and nothing
  else. Hooking a tool loop in belongs in your own command, where you control the blast radius.
- **No user or permission management.** Whether the bot is in a conversation is Talk's
  decision, made by a moderator in the conversation settings.
- **Not multi-tenant.** One bot, one secret, one Nextcloud. Run a second instance for a second
  bot — they are small.

## Design decisions

**Answer the webhook, then work.** Talk waits a short time for the webhook to return and treats
a slow endpoint as a failure. A model call routinely takes longer than that. So `/webhook`
verifies, parses, spawns a task and returns `200 {"status":"accepted"}` — the reply arrives
later through the Bot API, which is exactly how a human answers a chat message too.

**The signature is the whole authentication story, in both directions.** Incoming events are
HMAC-SHA256 over `X-Nextcloud-Talk-Random` + the *raw* body; outgoing calls are signed over the
random plus one endpoint-specific value — the message text for `/message`, the emoji for
reactions, the token for `ask-features`. Not the serialised JSON body. Getting this subtly
wrong is the single most common way a Talk bot fails, so it lives in one 40-line module with
tests that pin the construction against the documented one.

**Verify before parsing.** Nothing touches the payload until the signature checks out, so a
malformed body from an unauthenticated caller cannot reach the parser.

**Pin the backend.** A signed event carries the server's own base URL in a header, and that is
where replies go. `SABLE_PIN_BACKEND` (on by default) refuses events claiming to come from
anywhere other than the configured Nextcloud, so a replayed webhook cannot aim the bot's
replies at somebody else's server.

**Refuse to loop.** Messages from actors Talk marks as applications or `bots/…` are ignored,
and events are de-duplicated by `(conversation, type, message id)`, so a redelivered webhook
produces one reply rather than two, and two bots in one room cannot start a conversation with
each other.

**Configuration is environment variables only.** No config file format to learn, no parser to
maintain; it drops straight into a container, a systemd unit or a `.env` file. `sable --check`
prints what it resolved to and exits, so a bad config fails at deploy time rather than on the
first message.

**Everything is an explicit seam.** `Bot` takes its HTTP client, LLM client, history and
command registry as constructor arguments. That is why the test suite can cover both endpoints
end to end with no network, no Nextcloud and no model — and why replacing any one of those
pieces is a small change rather than a fork.

## Trust boundaries

| Boundary | What protects it |
| --- | --- |
| Nextcloud → `/webhook` | HMAC-SHA256 over the raw body; a body rewritten after signing fails. Then the backend pin. |
| sable → Nextcloud | The same shared secret, signed per endpoint. Anyone holding the secret can post as the bot. |
| Anything → `/notify` | A separate bearer token, compared in constant time. Unset means the route answers 404. |
| Chat text → the model | Room messages are sent verbatim to your configured backend. Whoever can talk to the bot can send text to that provider — worth knowing before pointing it at a hosted API. |
| A command's own reach | Whatever you give it. Commands run with the bot's credentials; treat a command as code anyone in the conversation can trigger. |

The bot secret authenticates *both* directions, so it is the one value that matters: rotating
it means `occ talk:bot:install` again with the new value.

## Where to extend it

| You want to… | Touch |
| --- | --- |
| Add a command | [`commands.py`](../src/sable/commands.py) — one decorator |
| Change when the model answers | `Bot.handle` in [`bot.py`](../src/sable/bot.py) |
| Keep history across restarts | `History` in [`history.py`](../src/sable/history.py) |
| Support a non-OpenAI-shaped backend | `LLMClient` in [`llm.py`](../src/sable/llm.py) |
| React to reactions or join/leave | The `Like` / `Undo` / `Join` / `Leave` branches of `Bot.handle` (already parsed for you) |
| Add an HTTP route | [`app.py`](../src/sable/app.py) |

## Further reading

- [Nextcloud Talk bot documentation](https://nextcloud-talk.readthedocs.io/en/latest/bots/) —
  the API this is built on
- [configuration.md](configuration.md) — every setting
- [deployment.md](deployment.md) — running it for real
