# sable

A [Nextcloud Talk](https://nextcloud-talk.readthedocs.io/en/latest/bots/) bot, built on the
official webhook Bot API. It does three things:

1. **Commands** — `!help`, `!ping`, `!whoami`, `!echo`, `!ai`, `!reset`, `!version`, plus
   whatever you add in one decorator.
2. **A chat assistant** — mention it (`@sable why is the build red?`) and it answers through
   any **OpenAI `/chat/completions`-compatible** backend. Model-agnostic on purpose: OpenAI,
   Ollama, vLLM, llama.cpp, LiteLLM, OpenRouter, Groq, Together — set a base URL and a model
   name, nothing else changes.
3. **Alerting** — other systems `POST /notify` with a bearer token and the message lands in a
   conversation.

Every webhook is HMAC-SHA256 verified before anything else happens, and every call back into
Talk is signed the way the Bot API expects.

## Documentation

| Document | What is in it |
| --- | --- |
| [purpose.md](purpose.md) | What this is for, what it deliberately does not do, why it is built this way, and the trust boundaries |
| [configuration.md](configuration.md) | Every environment variable, with value formats, provider recipes and worked examples |
| [deployment.md](deployment.md) | Getting it running for real: Docker, systemd, TLS, `occ talk:bot:install`, verification, operations, troubleshooting |
| [releasing.md](releasing.md) | The `dev` / `main` / `release` branch model, the `MAJOR.MINOR` version scheme, and the Forgejo Actions pipeline that publishes when `release` moves |
| [security.md](security.md) | Trust boundaries, what protects each one, how secrets are handled, and the accepted risks — read before exposing this anywhere |
| [future.md](future.md) | Known limitations with their fixes, features not yet used, maintenance cadence, and decisions worth revisiting |
| [CHANGELOG.md](CHANGELOG.md) | What changed per release. The current version's section becomes the Forgejo release body, so it is required |
| [LICENSE](LICENSE) | MIT |

## How it fits together

```
Nextcloud Talk ──POST /webhook (signed)──▶ sable ──┬──▶ command handler ──┐
                                                   │                      │
                    Prometheus/CI ──POST /notify──▶ ├──▶ LLM backend ──────┤
                                                   │   (chat completions) │
                    ◀──── POST /bot/{token}/message (signed) ─────────────┘
```

The webhook answers `200 {"status":"accepted"}` immediately and does the work in the
background, because a model call routinely takes longer than Talk is willing to wait.

## Quickstart

```bash
cp .env.example .env
openssl rand -hex 32          # a 64-char secret; Talk requires 40–128
```

Put that secret in `SABLE_BOT_SECRET`, set `SABLE_NEXTCLOUD_URL`, and — if you want the
assistant — `SABLE_LLM_BASE_URL`, `SABLE_LLM_API_KEY` and `SABLE_LLM_MODEL`. Leaving
`SABLE_LLM_MODEL` empty is a supported mode: you get a command bot and no model calls.

```bash
docker compose up -d --build   # or: uv sync --locked --no-dev && uv run sable
sable --check                  # validate the config and print what it resolved to
```

Then register it with Nextcloud, on the Nextcloud server as the web user:

```bash
occ talk:bot:install "sable" "<the same secret>" "https://sable.example.org/webhook" "A helpful bot" --feature webhook --feature response --feature reaction
```

…and enable it per conversation under **Conversation settings → Bots**. Say hello:

```
!ping
@sable summarise the last deploy
!help
```

The full path — TLS, reverse proxies, systemd, feature flags, verification — is in
[deployment.md](deployment.md).

## Using it in chat

**Commands** start with `SABLE_COMMAND_PREFIX` (`!` by default): `!help` lists them.

**The assistant** runs when a message mentions the bot (`@sable ...`, or `sable: ...` at the
start of a line), when you use `!ai <question>`, or for *every* message in conversations listed
in `SABLE_AI_ROOMS` (`*` for all of them).

**Or react with ⁉️** to any message and the bot answers *that* message, threaded underneath it —
handy for someone else's question, or as a follow-up on the bot's own reply. It only works on
messages the bot saw arrive, because a reaction event carries the message id and not its text.

History is per-conversation, in-process, capped by `SABLE_HISTORY_TURNS` and
`SABLE_HISTORY_TTL`, and cleared by `!reset`. It is a cache, not a record — a restart forgets
everything. Speaker names are prefixed onto each turn so the model can tell a busy room apart.

**Alerting** takes a bearer token and an alias or raw conversation token:

```bash
curl -fsS https://sable.example.org/notify \
  -H "Authorization: Bearer $SABLE_NOTIFY_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"room": "alerts", "message": "**disk full** on db01 — 98% of /var", "silent": false}'
```

`201` with the new message id, `400` for a room Talk rejected, `401` for a bad token, `502` if
Nextcloud is unreachable, `404` if `SABLE_NOTIFY_TOKEN` is unset.

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

Return Markdown to reply, or `None` to stay quiet. `CommandError` is posted to the room as-is;
anything else is logged and reported as a crash. `ctx` carries the parsed event, the raw `args`
string, a shell-split `argv`, and `ctx.bot` for `answer_with_llm`, `history` and `reply`.

## Development

```bash
uv sync --locked --extra dev    # exactly the 31 packages uv.lock pins
uv run pytest
```

Without uv, `pip install -e '.[dev]'` still works — you just get whatever pip resolves at that
moment rather than the locked set.

The suite covers the signature scheme in both directions, event parsing (including rich-object
placeholders, reactions and join/leave), routing, the LLM client against a mocked backend, and
both HTTP endpoints end to end. No network, no Nextcloud, no model needed.

Layout: [signing.py](../src/sable/signing.py) (HMAC both ways) ·
[events.py](../src/sable/events.py) (ActivityStreams → dataclasses) ·
[talk.py](../src/sable/talk.py) (Bot API client) · [bot.py](../src/sable/bot.py) (routing) ·
[commands.py](../src/sable/commands.py) (registry + built-ins) ·
[llm.py](../src/sable/llm.py) (chat completions) · [app.py](../src/sable/app.py) (FastAPI) ·
[config.py](../src/sable/config.py) (environment) · [history.py](../src/sable/history.py).

## License

MIT — see [LICENSE](LICENSE).
