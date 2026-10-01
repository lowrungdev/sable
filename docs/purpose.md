# Purpose

What sable is for, what it deliberately leaves alone, and why it is built this way.

## The problem

Nextcloud Talk is where a lot of teams already are, but getting something into it
programmatically is awkward. You can write a Nextcloud app in PHP, which means shipping and
maintaining real server-side code against Nextcloud releases. You can register a bot through
Talk's webhook Bot API, which is the supported path but makes Nextcloud call you — so you need an
endpoint it can reach, signatures to verify, and a bot install that only an administrator with
shell access can do — and which, by design, cannot read a message back or attach a file. Or you
can run an ordinary user account that long-polls the chat API, which works with nothing installed
on the server, reads everything a person can, and appears in the room as a person rather than as a
bot.

sable is that last option, written once, with the jobs most teams actually want from a chat bot
already in it. It started as the second and was changed to the third, for the reasons below.

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
with a file attached. This is the one-way direction, into Talk: CI, an alertmanager, a cron job, a
deploy script. Aliases mean callers never need to know conversation tokens. Services that
cannot speak that shape, and often cannot set a header either, post to `POST /hook/{name}`
instead, which renders whatever JSON they send into a message.

## What it deliberately doesn't do

It does not get a bot's protections. It runs as a user, and everything that follows from that is
the trade-off this design makes, stated here rather than hidden. What it gains: it only ever
connects out, so nothing has to reach it and a host behind NAT or a firewall works; nothing is
installed in Nextcloud and no administrator has to run `occ`; there are no signatures, secrets to
keep in step or webhook to forge; and because it is a user it can read a message back and upload a
file, which a bot cannot. What it costs, honestly:

- **Its credential is a person's.** An app password cannot be scoped, so it reaches that user's
  Files, Contacts and Calendar as well as chat, where a bot's secret could only post messages. The
  answer is a dedicated account that owns nothing else, not a technical control.
- **It holds connections open.** Talk has no single feed across conversations, so sable keeps one
  long poll per conversation, each occupying a request slot on Nextcloud for up to
  `SABLE_POLL_TIMEOUT` seconds, all day. A webhook costs the server nothing while idle. sable
  follows at most 50 conversations because of it.
- **It is a person in the room.** Anyone who can invite participants can invite it, and nothing in
  Talk marks its messages as automated.
- **It hears about a new room late.** The conversation list is rescanned every
  `SABLE_ROOM_REFRESH` seconds, where a bot install is effective at once.

For a chat assistant on a server you run, reachable only from inside, that trade is worth it: the
requirement it removes is the hard one. If you would rather not give anything a user's
credential, or cannot spare the PHP workers, it is the wrong tool.

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

There is no user or permission management. Whether the account is in a conversation is Talk's
decision, made by whoever invites it. And it is not multi-tenant: one account, one password, one
Nextcloud. Run a second instance with a second account if you need a second bot, since they are
small.

## How it is built

*Read with long polls, work in the background.* One task per conversation asks Talk for messages
newer than the last it saw and waits, up to `SABLE_POLL_TIMEOUT` seconds, for something to
arrive. Each message is handed to a separate task and the loop goes straight back to listening,
because a model call routinely takes longer than is reasonable to block on. A separate scan
reconciles the list of conversations every `SABLE_ROOM_REFRESH` seconds. A conversation is first
followed from its newest message, so nothing said before sable noticed it is replayed.

*One identity, for everything.* The same account reads, posts, reacts and uploads, so there is one
credential to configure and one to lose, and sable knows its own user id exactly. That is what
lets it tell a real mention from a typed name and ignore its own replies when they come back down
the poll.

*Refuse to loop.* Its own messages and events from bots are ignored, and every event is
de-duplicated on the conversation, type, message id, actor and reaction together. A message seen
twice produces one reply rather than two, two bots in a room cannot start talking to each other,
and two people reacting to the same message are still two distinct events.

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
| sable to Nextcloud | HTTPS, authenticated as the account by its app password. It cannot be scoped, so it reaches everything that user can: chat, Files, Contacts and Calendar. |
| Nextcloud to sable | Nothing is accepted from Nextcloud unprompted: chat arrives as the answer to sable's own requests. |
| Anything to `/notify` | A separate bearer token, compared in constant time. Unset means the route answers 404. |
| Chat text to the model | Messages are sent verbatim to your configured backend. Whoever can talk to the bot can send text to that provider — and, with server-side tools on, can have it call one. |
| A command's own reach | Whatever you give it. Commands run with the bot's credentials, and anyone in the conversation can trigger any that is not named in `SABLE_ADMIN_COMMANDS`. |

The app password is the value that matters most, and revoking it in Nextcloud is how you
rotate it. [security.md](security.md) covers all of this properly, including the risks that are
accepted rather than solved.

## Where to extend it

| You want to | Look at |
| --- | --- |
| Add a command | [`commands.py`](../src/sable/commands.py), one decorator |
| Change when the model answers | `Bot.handle` in [`bot.py`](../src/sable/bot.py) |
| Keep history across restarts | `History` in [`history.py`](../src/sable/history.py) |
| Support a backend that isn't OpenAI-shaped | A sibling of [`openwebui.py`](../src/sable/openwebui.py) answering `complete(messages) -> str`, and one branch in `llm_client` |
| Act on reactions | The `Like` and `Undo` branches of `Bot.handle`, already parsed out of Talk's reaction system messages |
| Change which conversations are followed | `Poller.scan` in [`poller.py`](../src/sable/poller.py) |
| Add an HTTP route | [`app.py`](../src/sable/app.py) |

## Further reading

[configuration.md](configuration.md) documents every setting, [deployment.md](deployment.md)
covers running it for real, and [future.md](future.md) records the known limitations and what it
would take to lift them. The API underneath is the
[Nextcloud Talk chat API](https://nextcloud-talk.readthedocs.io/en/latest/chat/).
