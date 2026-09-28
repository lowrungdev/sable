# sable

A [Nextcloud Talk](https://nextcloud-talk.readthedocs.io/en/latest/bots/) bot, built on the
official webhook Bot API.

It runs commands (`!help`, `!ping`, `!ai` and whatever you add), answers questions through any
OpenAI-compatible model backend, and relays alerts from other systems into a conversation with
`POST /notify`, optionally with a file attached, or `POST /hook/{name}` for services that cannot
speak that shape. Every webhook is HMAC-SHA256 verified before anything else happens, every
alerting call needs its own token, and every call back into Talk is signed the way the bot API
expects. Commands are open to everyone in the conversation unless you name them in
`SABLE_ADMIN_COMMANDS`.

## Documentation

| Document | What is in it |
| --- | --- |
| [purpose.md](purpose.md) | What this is for, what it leaves alone, and how it is built |
| [configuration.md](configuration.md) | Every environment variable, with provider recipes and worked examples |
| [deployment.md](deployment.md) | Running it for real: Docker, systemd, TLS, registering the bot, verifying, operating |
| [security.md](security.md) | Trust boundaries, how secrets are handled, and the risks that are accepted rather than solved |
| [releasing.md](releasing.md) | Branches, the version scheme, and the Forgejo pipeline that publishes |
| [future.md](future.md) | Known limitations, what it would take to lift them, and decisions worth revisiting |
| [CHANGELOG.md](CHANGELOG.md) | What changed per release |

## How it fits together

```
Nextcloud Talk ──POST /webhook (signed)──▶ sable ──┬──▶ command handler ──┐
                                                   │                      │
                   Prometheus/CI ──POST /notify──▶ ┼──▶ model backend ────┤
          Komodo/Grafana ──POST /hook/{name}──▶ ───┤   (chat completions) │
                                                   │                      │
                    ◀──── POST /bot/{token}/message (signed) ─────────────┘
```

The webhook answers `200 {"status":"accepted"}` straight away and does the work in the
background, because a model call routinely takes longer than Talk is willing to wait.

## Quickstart

Copy the example configuration and generate a secret. Talk wants 40 to 128 characters, and
`openssl rand -hex 32` gives a good one.

```bash
cp .env.example .env
openssl rand -hex 32
```

Put that in `SABLE_BOT_SECRET` and set `SABLE_NEXTCLOUD_URL`. For the assistant, add
`SABLE_LLM_BASE_URL`, `SABLE_LLM_API_KEY` and `SABLE_LLM_MODEL`; leaving the model empty is a
supported mode that gives you a command bot and no model calls at all.

```bash
docker compose up -d --build   # or: uv sync --locked --no-dev && uv run sable
sable --check                  # validate the configuration and print what it resolved to
```

Then register the bot with Nextcloud, running this on the Nextcloud server as the web user:

```bash
occ talk:bot:install "sable" "<the same secret>" "https://sable.example.org/webhook" "A helpful bot" --feature webhook --feature response --feature reaction
```

Enable it in a conversation under Conversation settings, Bots, and say hello with `!ping`.
[deployment.md](deployment.md) has the full path, including TLS, reverse proxies, systemd and
how to verify each half of the round trip.

## Using it in chat

Commands start with `SABLE_COMMAND_PREFIX`, `!` by default, and `!help` lists them.

The assistant answers when a message mentions the bot, as `@sable ...` or `sable: ...` at the
start of a line, when someone uses `!ai <question>`, and for every message in the conversations
named in `SABLE_AI_ROOMS`. You can also react to any message with ⁉️ and it will answer that
message, threaded underneath — useful for someone else's question or as a follow-up on the
bot's own reply. That last one only works on messages the bot saw arrive, because a reaction
event carries the message id and not its text.

History is per conversation, held in memory, capped by `SABLE_HISTORY_TURNS` and
`SABLE_HISTORY_TTL`, and cleared by `!reset`. It is a cache rather than a record, so a restart
forgets it. Speaker names are prefixed onto each turn so the model can tell a busy room apart.

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

Attachments need a Nextcloud user account as well as the bot secret, because the bot API cannot
upload files. See [file attachments](configuration.md#file-attachments) for why, and
[security.md](security.md) for what that second credential costs you.

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

The suite covers the signature scheme in both directions, event parsing, routing, the model
client and the file client against mocked backends, and all three HTTP endpoints end to end,
`/webhook`, `/notify` and `/hook/{name}`. It needs no network, no Nextcloud and no model.

The code is small enough to read in a sitting:
[signing.py](../src/sable/signing.py) for the HMAC in both directions,
[events.py](../src/sable/events.py) for turning ActivityStreams payloads into dataclasses,
[talk.py](../src/sable/talk.py) for the bot API client,
[files.py](../src/sable/files.py) for uploading and sharing attachments,
[bot.py](../src/sable/bot.py) for deciding what to do with an event,
[commands.py](../src/sable/commands.py) for the registry and built-ins,
[llm.py](../src/sable/llm.py) for chat completions,
[app.py](../src/sable/app.py) for the HTTP surface,
[config.py](../src/sable/config.py) for the environment, and
[history.py](../src/sable/history.py) and [state.py](../src/sable/state.py) for the small pieces
of in-memory state.

## License

MIT, see [LICENSE](LICENSE).
