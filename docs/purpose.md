# Purpose

What sable is for, what it deliberately leaves alone, and why it is built this way.

## The problem

Nextcloud Talk is where a lot of teams already are, but getting something into it
programmatically is awkward. You can write a Nextcloud app in PHP, which means shipping and
maintaining real server-side code against Nextcloud releases. You can run a bot user account
that long-polls the chat API, which works but holds a connection open per conversation, ties up
a PHP worker for each one, and appears in the room as a person rather than a bot. Or you can use
Talk's webhook Bot API, which is the supported path but leaves you to implement the signature
scheme, the ActivityStreams payloads and the reply endpoints yourself.

sable is that third option, written once, with the jobs most teams actually want from a chat bot
already in it.

## What it does

**Commands.** A prefix router over a registry, `!` by default. It ships with `!help`, `!ping`,
`!whoami`, `!echo`, `!ai`, `!reset` and `!version`, and a new one is a decorated async function
that returns Markdown. This is the seam most people will use; the rest of the bot exists so that
writing a command is boring.

**An assistant.** Mention the bot and it answers through any endpoint that speaks OpenAI's
`/chat/completions` shape, keeping a short rolling history per conversation. React to a message
with ⁉️ and it answers that message instead, threaded underneath it. Pointed at Open WebUI it
can also use tools — search, MCP servers, whatever that instance offers — with Open WebUI
running the loop.

Model-agnosticism here is a requirement rather than a nicety. The chat-completions shape is the
one interface that OpenAI, Ollama, vLLM, llama.cpp, LiteLLM, OpenRouter, Groq and Together all
speak, so talking it over plain HTTP instead of importing a vendor SDK means switching providers
is two environment variables, self-hosting needs no code change, and one dependency does the
work. Anything a particular provider wants that the common shape lacks goes in
`SABLE_LLM_EXTRA_BODY` and is merged into the request body last.

**Alerting.** `POST /notify` with a bearer token puts a message in a conversation, optionally
with a file attached. This is the outbound-only direction: CI, an alertmanager, a cron job, a
deploy script. Aliases mean callers never need to know conversation tokens. Services that
cannot speak that shape, and often cannot set a header either, post to `POST /hook/{name}`
instead, which renders whatever JSON they send into a message.

## What it deliberately doesn't do

It does not poll. This is a webhook bot, so if you cannot expose an HTTPS endpoint to your
Nextcloud server, it is the wrong tool. Nothing is installed into Nextcloud either, beyond the
one row that `occ talk:bot:install` writes.

Replies are not streamed. A token-by-token edit loop would hammer the API for little gain, so
one answer is one message.

Memory is not durable. History is an in-process cache with a turn cap and a time limit, and a
restart forgets it. That is the right default for a chat bot; if you need recall across
restarts, `History` is one small class with three methods, and swapping it is a contained
change.

sable does not execute tools itself, and has no retrieval. A model that asks for a tool gets no
answer from the portable backend, because a single round trip cannot give it one. What sable
will do is hand the whole job to a server that runs the loop already — `SABLE_LLM_BACKEND=openwebui`
— which keeps the tool registry, the credentials and the authorization in one place that was
built for them, rather than growing a second one here. The cost is that the room's participants
can set those tools off, which [security.md](security.md#accepted-risks) states plainly.

There is no user or permission management. Whether the bot is in a conversation is Talk's
decision, made by a moderator. And it is not multi-tenant: one bot, one secret, one Nextcloud.
Run a second instance if you need a second bot, since they are small.

One exception is worth naming, because it qualifies the first paragraph. File attachments do use
a Nextcloud user account, since the bot API has no upload endpoint. That account is optional,
used only on the `/notify` path and only when a file is attached; receiving stays entirely on
the signed webhook.

## How it is built

*Answer the webhook, then work.* Talk waits only a short time for the webhook to return and
treats a slow endpoint as a failure, while a model call routinely takes longer than that. So
`/webhook` verifies, parses, spawns a task and returns `200 {"status":"accepted"}`. The reply
arrives later through the bot API, which is how a person answers a chat message too.

*The signature is the whole authentication story, in both directions.* Incoming events are
HMAC-SHA256 over the random header plus the raw body. Outgoing calls are signed over the random
plus one endpoint-specific value: the message text when posting, the emoji when reacting, the
token when asking about features — not the serialised JSON body. Getting this subtly wrong is
the most common way a Talk bot fails, so it lives in one small module whose tests pin the
construction against the documented one.

*Verify before parsing.* Nothing touches the payload until the signature checks out, so a
malformed body from an unauthenticated caller never reaches the parser.

*Pin the backend.* A signed event carries the server's own base URL in a header, and that is
where replies go. With `SABLE_PIN_BACKEND` on, events claiming any other backend are refused, so
a replayed webhook cannot aim the bot's replies — and its credentials — at somebody else's
server.

*Refuse to loop.* Events from bots are ignored, and every event is de-duplicated on the
conversation, type, message id, actor and reaction together. A redelivered webhook produces one
reply rather than two, two bots in a room cannot start talking to each other, and two people
reacting to the same message are still two distinct events.

*Configuration is environment variables and nothing else.* No file format to learn, no parser to
maintain, and it drops straight into a container, a systemd unit or a `.env` file. `sable
--check` prints what it resolved to and exits, so a bad configuration fails at deploy time
rather than on the first message.

*Everything is an explicit seam.* `Bot` takes its HTTP client, model client, history and command
registry as constructor arguments. That is why the tests cover every endpoint end to end with no
network, no Nextcloud and no model, and why replacing any one of those pieces is a small change
rather than a fork.

## Trust boundaries

| Boundary | What protects it |
| --- | --- |
| Nextcloud to `/webhook` | HMAC-SHA256 over the raw body, so a body rewritten after signing fails. Then a replay check on the random, the backend pin, and the conversation token. |
| sable to the Talk bot API | The same shared secret, signed per endpoint. Anyone holding it can post as the bot. |
| Anything to `/notify` | A separate bearer token, compared in constant time. Unset means the route answers 404. |
| sable to Nextcloud Files | A user account's app password, used only to upload and share attachments. It cannot be scoped, so it reaches everything that user can. |
| Chat text to the model | Messages are sent verbatim to your configured backend. Whoever can talk to the bot can send text to that provider — and, with server-side tools on, can have it call one. |
| A command's own reach | Whatever you give it. Commands run with the bot's credentials, and anyone in the conversation can trigger any that is not named in `SABLE_ADMIN_COMMANDS`. |

The bot secret authenticates both directions, so it is the value that matters most; rotating it
means running `occ talk:bot:install` again. [security.md](security.md) covers all of this
properly, including the risks that are accepted rather than solved.

## Where to extend it

| You want to | Look at |
| --- | --- |
| Add a command | [`commands.py`](../src/sable/commands.py), one decorator |
| Change when the model answers | `Bot.handle` in [`bot.py`](../src/sable/bot.py) |
| Keep history across restarts | `History` in [`history.py`](../src/sable/history.py) |
| Support a backend that isn't OpenAI-shaped | A sibling of [`openwebui.py`](../src/sable/openwebui.py) answering `complete(messages) -> str`, and one branch in `llm_client` |
| Act on reactions or on joining a conversation | The `Like`, `Undo`, `Join` and `Leave` branches of `Bot.handle`, already parsed |
| Add an HTTP route | [`app.py`](../src/sable/app.py) |

## Further reading

[configuration.md](configuration.md) documents every setting, [deployment.md](deployment.md)
covers running it for real, and [future.md](future.md) records the known limitations and what it
would take to lift them. The API underneath is the
[Nextcloud Talk bot documentation](https://nextcloud-talk.readthedocs.io/en/latest/bots/).
