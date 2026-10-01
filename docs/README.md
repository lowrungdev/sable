# sable

An assistant for [Nextcloud Talk](https://nextcloud-talk.readthedocs.io/en/latest/) that runs as
an ordinary Nextcloud user account.

It signs in as that user, long-polls the Talk chat API for the conversations the user is in, runs
commands (`!help`, `!ping`, `!ai` and whatever you add), answers questions through any
OpenAI-compatible model backend, and relays alerts from other systems into a conversation with
`POST /notify`, optionally with a file attached, or `POST /hook/{name}` for services that cannot
speak that shape. It only ever connects out to Nextcloud, so nothing has to reach it, and nothing
is installed in Nextcloud. The price is that its one credential is a user's app password, which
cannot be scoped. Every alerting call needs its own token, and commands are open to everyone in
the conversation unless you name them in `SABLE_ADMIN_COMMANDS`.

## Documentation

| Document | What is in it |
| --- | --- |
| [purpose.md](purpose.md) | What this is for, what it leaves alone, and how it is built |
| [configuration.md](configuration.md) | Every environment variable, with provider recipes and worked examples |
| [deployment.md](deployment.md) | Running it for real: the account, Docker, systemd, TLS, verifying, operating |
| [security.md](security.md) | Trust boundaries, how secrets are handled, and the risks that are accepted rather than solved |
| [releasing.md](releasing.md) | Branches, the version scheme, and the Forgejo pipeline that publishes |
| [future.md](future.md) | Known limitations, what it would take to lift them, and decisions worth revisiting |
| [CHANGELOG.md](CHANGELOG.md) | What changed per release |

## How it fits together

```
Nextcloud Talk ◀── long polls, as a user ──────── sable ──┬──▶ command handler ──┐
   (outbound only)                                        │                      │
                   Prometheus/CI ──POST /notify──▶ ───────┼──▶ model backend ────┤
          Komodo/Grafana ──POST /hook/{name}──▶ ──────────┤   (or Open WebUI,    │
                                                          │    which runs tools) │
                                                          │                      │
Nextcloud Talk ◀──── posts, reactions, file shares, as the same user ────────────┘
```

One long poll is held open per conversation, and each message it returns is handled in the
background, because a model call routinely takes longer than is comfortable to wait for in the
loop that is reading. Each held poll occupies a PHP worker on Nextcloud, and the stock pool is
five, so raise it before connecting an account that is in more than a few conversations
([how](deployment.md#give-nextcloud-enough-php-workers)).

## Quickstart

In Nextcloud, create a user for sable and give it an app password under Settings → Security →
Devices & sessions. Then add the user to a conversation, the way you would add anybody.

```bash
cp .env.example .env
```

Set `SABLE_NEXTCLOUD_URL`, `SABLE_NEXTCLOUD_USER` and `SABLE_NEXTCLOUD_PASSWORD`. For the
assistant, add `SABLE_LLM_BASE_URL`, `SABLE_LLM_API_KEY` and `SABLE_LLM_MODEL`; leaving the model
empty is a supported mode that gives you a command bot and no model calls at all.

```bash
docker compose up -d --build   # or: uv sync --locked --no-dev && uv run sable
sable --check                  # validate the configuration and print what it resolved to
```

Within a minute sable notices the invitation, and you can say hello with `!ping`.
[deployment.md](deployment.md) has the full path, including TLS, reverse proxies, systemd and
how to verify each half of the round trip.

## Using it in chat

Commands start with `SABLE_COMMAND_PREFIX`, `!` by default, and `!help` lists them.

The assistant answers when a message mentions the account, picked from Talk's mention list or
typed as its user id at the start of a line, when someone uses `!ai <question>`, and for every
message in the conversations named in `SABLE_AI_ROOMS`. You can also react to any message with ⁉️
and it will answer that message, threaded underneath — useful for someone else's question or as
a follow-up on its own reply. That last one only works on messages sable saw arrive, because a
reaction names the message and does not carry its text.

History is per conversation, held in memory, capped by `SABLE_HISTORY_TURNS` and
`SABLE_HISTORY_TTL`, and cleared by `!reset`. It is a cache rather than a record, so a restart
forgets it. Speaker names are prefixed onto each turn so the model can tell a busy room apart,
and the current date and time go in the system prompt so "right now" means something.

The assistant has no tools of its own — a single request cannot run one — but pointed at Open
WebUI with `SABLE_LLM_BACKEND=openwebui` it uses whatever that instance offers, with Open WebUI
executing the loop. Everyone in the room can set those tools off, so read
[letting the model use tools](configuration.md#letting-the-model-use-tools) before enabling it.

## Alerting

Other systems post to `/notify` with a bearer token and either an alias or a raw conversation
token:

```bash
curl -fsS https://sable.example.org/notify -H "Authorization: Bearer $SABLE_NOTIFY_TOKEN" -H 'Content-Type: application/json' -d '{"room": "alerts", "message": "disk full on db01, 98% of /var"}'
```

You get `201` with the new message id, `400` for a room Talk rejected, `401` for a bad token,
`502` if Nextcloud is unreachable, and `404` when `SABLE_NOTIFY_TOKEN` is unset.

The same single call takes a file, as multipart or base64, and the message becomes its caption:

```bash
curl -fsS https://sable.example.org/notify -H "Authorization: Bearer $SABLE_NOTIFY_TOKEN" -F room=alerts -F message="nightly build" -F file=@report.pdf
```

The file is uploaded as the same account that posts the text, so there is nothing more to set up.
See [file attachments](configuration.md#file-attachments), and [security.md](security.md) for
what that account's credential reaches.

## Adding a command

```python
# src/sable/commands.py
@registry.command("deploy", help="Show the last deploy.", usage="deploy [env]")
async def deploy(ctx: Context) -> str:
    env = ctx.argv[0] if ctx.argv else "prod"
    if env not in {"prod", "staging"}:
        raise CommandError(f"I don't know the {env} environment.")
    return f"**{env}** is on `abc1234`, deployed 20 minutes ago."
```

Return Markdown to reply, or `None` to stay quiet. A `CommandError` is posted to the room as
written; anything else is logged and reported as a crash. `ctx` carries the parsed event, the
raw argument string, a shell-split `argv`, and `ctx.bot` for `answer_with_llm`, `history` and
`reply`.

Bear in mind that by default anyone in the conversation can run any command. If yours touches
something that matters, name it in `SABLE_ADMIN_COMMANDS` and the people allowed to run it in
`SABLE_ADMIN_USERS`; for anything finer, `ctx.is_admin` says whether the sender is one of them.

## Development

```bash
uv sync --locked --extra dev
uv run pytest
```

`--locked` installs exactly what `uv.lock` pins and fails if the lock and `pyproject.toml`
disagree. Without uv, `pip install -e '.[dev]'` still works; you just get whatever pip resolves
at that moment rather than the locked set.

The suite covers the poll loop, event parsing, routing, the Talk client, the model client and the
file client against mocked backends, and the HTTP endpoints end to end, `/notify` and
`/hook/{name}`. It needs no network, no Nextcloud and no model.

The code is small enough to read in a sitting:
[poller.py](../src/sable/poller.py) for long-polling the account's conversations,
[events.py](../src/sable/events.py) for turning Talk's chat messages into dataclasses,
[talk.py](../src/sable/talk.py) for the chat API client,
[files.py](../src/sable/files.py) for uploading and sharing attachments,
[bot.py](../src/sable/bot.py) for deciding what to do with an event,
[commands.py](../src/sable/commands.py) for the registry and built-ins,
[llm.py](../src/sable/llm.py) for chat completions,
[openwebui.py](../src/sable/openwebui.py) for the server-side tool loop,
[hooks.py](../src/sable/hooks.py) for rendering somebody else's webhook into a message,
[app.py](../src/sable/app.py) for the HTTP surface,
[config.py](../src/sable/config.py) for the environment, and
[history.py](../src/sable/history.py), [state.py](../src/sable/state.py) and
[logs.py](../src/sable/logs.py) for the small pieces around the edges.

## License

MIT, see [LICENSE](LICENSE).
