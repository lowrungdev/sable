# Purpose

What sable is for, what it deliberately leaves alone, and why it is built this way.

## The problem

Nextcloud Talk is where a lot of teams already are, but getting something into it
programmatically is awkward. You can write a Nextcloud app in PHP, which means shipping and
maintaining real server-side code against Nextcloud releases. Or you can run an ordinary user
account that long-polls the chat API, which works with nothing installed on the server, reads
everything a person can, and appears in the room as a person.

sable is that second option, written once, with the jobs most teams actually want from a chat
assistant already in it.

## What it does

**Commands.** A prefix router over a registry, `!` by default. It ships with `!help`, `!ping`,
`!whoami`, `!ai`, `!reset` and `!version`, and a new one is a decorated async function that
returns Markdown. This is the seam most people will use; the rest of the bot exists so that
writing a command is boring.

**An assistant.** Mention the account and it answers through any endpoint that speaks OpenAI's
`/chat/completions` shape, keeping a short rolling history per conversation. React to a message
with ⁉️ and it answers that message instead, threaded underneath it. Pointed at Open WebUI it can
also use tools — search, MCP servers, whatever that instance offers — with Open WebUI running the
loop, in the rooms you name. [When it answers](configuration.md#when-does-the-assistant-answer) is
in the configuration reference.

Model-agnosticism is a requirement rather than a nicety. The chat-completions shape is the one
interface that OpenAI, Ollama, vLLM, llama.cpp, LiteLLM, OpenRouter, Groq and Together all speak,
so talking it over plain HTTP instead of importing a vendor SDK means switching providers is two
environment variables, self-hosting needs no code change, and one dependency does the work.
Anything a particular provider wants that the common shape lacks goes in `SABLE_LLM_EXTRA_BODY`,
merged into the request body last.

**Alerting.** `POST /notify` with a bearer token puts a message in a conversation, optionally
with a file attached: the one-way direction, into Talk, for CI, an alertmanager, a cron job or a
deploy script. Aliases mean callers never need to know conversation tokens. Services that cannot
speak that shape, and often cannot set a header either, post to `POST /hook/{name}` instead,
which renders whatever JSON they send into a message.

## What it deliberately doesn't do

It runs as a user, and everything that follows from that is the trade-off this design makes. What
it gains: it only ever connects out, so nothing has to reach it and a host behind NAT or a
firewall works; nothing is installed in Nextcloud; and because it is a user it can read a message
back and upload a file. What it costs:

- **Its credential is a person's.** An app password cannot be scoped, so it reaches that user's
  Files, Contacts and Calendar as well as chat. The answer is a dedicated account that owns
  nothing else, not a technical control ([accepted risk 1](security.md#accepted-risks)).
- **It holds connections open.** Talk has no single feed across conversations, so sable keeps one
  long poll per conversation, each occupying a request slot on Nextcloud all day. On a stock
  Nextcloud that pool is too small and has to be
  [raised before sable is pointed at it](deployment.md#give-nextcloud-enough-php-workers).
- **It is a person in the room.** Anyone who can invite participants can invite it, and nothing in
  Talk marks its messages as automated. `SABLE_ALLOWED_ROOMS` is the answer to the first half.
- **It hears about a new room late.** The conversation list is rescanned every
  `SABLE_ROOM_REFRESH` seconds.

For a chat assistant on a server you run, reachable only from inside, that trade is worth it: the
requirement it removes is the hard one. If you would rather not give anything a user's
credential, or cannot spare the PHP workers, it is the wrong tool.

Beyond that trade:

- **No streaming.** A token-by-token edit loop would hammer the API for little gain, so one
  answer is one message.
- **No durable memory.** History is an in-process cache with a turn cap and a time limit, and a
  restart forgets it, which is the right default for a chat bot
  ([what it would take](future.md#limitations-with-a-known-fix)).
- **No tool execution of its own, and no retrieval.** A single round trip cannot run a tool a
  model asks for. sable hands the whole job to a server that already runs the loop,
  `SABLE_LLM_BACKEND=openwebui`, which keeps the tool registry, credentials and authorization in
  one place built for them. The cost is that whoever can ask the model in a tools room can set
  those tools off, so tools are a decision about a room (`SABLE_LLM_TOOL_ROOMS`, off everywhere by
  default; [accepted risk 14](security.md#accepted-risks)).
- **No user or permission management** beyond a handful of lists of Nextcloud user ids and
  conversation tokens, not roles or groups
  ([how they combine](configuration.md#how-the-access-layers-combine)). Whether the account is in
  a conversation is Talk's decision, made by whoever invites it.
- **Not multi-tenant:** one account, one password, one Nextcloud. Run a second instance with a
  second account if you need a second assistant, since they are small.

## How it is built

```
Nextcloud Talk ◀── long polls, as a user ──────── sable ──┬──▶ command handler ──┐
   (outbound only)                                        │                      │
                   Prometheus/CI ──POST /notify──▶ ───────┼──▶ model backend ────┤
          Komodo/Grafana ──POST /hook/{name}──▶ ──────────┤   (or Open WebUI,    │
                                                          │    which runs tools) │
                                                          │                      │
Nextcloud Talk ◀──── posts, reactions, file shares, as the same user ────────────┘
```

*Read with long polls, work in the background.* One task per conversation asks Talk for messages
newer than the last it saw and waits for something to arrive. Each message is handed to a
separate task and the loop goes straight back to listening, because a model call routinely takes
longer than is reasonable to block on. A separate scan reconciles the list of conversations, and a
conversation is first followed from its newest message, so nothing said before sable noticed it
is replayed ([which conversations, and when](configuration.md#how-chat-is-received)).

*One identity, for everything.* The same account reads, posts, reacts and uploads, so there is one
credential to configure and one to lose, and sable knows its own user id exactly. That is what
lets it tell a real mention from a typed name, and ignore its own replies and anything from an
actor Talk marks as a bot when they come back down the poll, so it cannot loop with itself or
with another assistant in the room.

*Configuration is environment variables and nothing else.* No file format to learn, no parser to
maintain, and it drops straight into a container, a systemd unit or a `.env` file. `sable
--check` prints what it resolved to and exits, so a bad configuration fails at deploy time
rather than on the first message.

*Everything is an explicit seam.* `Bot` takes its HTTP client, model client, history and command
registry as constructor arguments. That is why the tests cover every endpoint end to end with no
network, no Nextcloud and no model, and why replacing any one of those pieces is a small change
rather than a fork.

### The Talk calls it makes

Every call is checked against the [Nextcloud Talk API documentation](https://nextcloud-talk.readthedocs.io/en/latest/),
and [`Agents/API/talk-user-api-reference.md`](../Agents/API/talk-user-api-reference.md) lists the
endpoints and what is still unverified. Conversations are API `v4`, chat and reactions `v1`.
Polling passes `noStatusUpdate=1`, so it does not mark the account as online, and
`setReadMarker=0`, so it does not mark conversations as read. The ⁉️ reaction reads the message
back from the context endpoint (capability `chat-get-context`) with `limit=3`, because Talk has no
single-message endpoint, and picks the message out of the answer, among its neighbours, by id.
Files go up by WebDAV `PUT` and into the conversation by a share with `shareType=10`.

## Where to extend it

| You want to | Look at |
| --- | --- |
| Add a command | [`commands.py`](../src/sable/commands.py), one decorator |
| Change when the model answers | `Bot._route` and `Bot.handle` in [`bot.py`](../src/sable/bot.py) |
| Keep history across restarts | `History` in [`history.py`](../src/sable/history.py), one small class |
| Support a backend that isn't OpenAI-shaped | A sibling of [`openwebui.py`](../src/sable/openwebui.py) answering `complete(messages) -> str`, and one branch in `llm_client` |
| Act on reactions | `Bot._route` in [`bot.py`](../src/sable/bot.py): `reaction` events are already parsed out of Talk's reaction system messages, and only the ask emoji is acted on. A removed reaction is not parsed at all, in `parse_message` in [`events.py`](../src/sable/events.py) |
| Change which conversations are followed | `Poller.scan` in [`poller.py`](../src/sable/poller.py) |
| Add an HTTP route | [`app.py`](../src/sable/app.py) |

## Further reading

[configuration.md](configuration.md) documents every setting, [deployment.md](deployment.md)
covers running it for real, [security.md](security.md) the trust boundaries and the risks that
are accepted rather than solved, and [future.md](future.md) the known limitations and what it
would take to lift them. [CONTRIBUTING.md](../CONTRIBUTING.md) is the place to start changing it.
