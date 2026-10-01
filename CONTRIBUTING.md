# Contributing to sable

Thanks for looking. sable is a small project and is meant to stay one, so the most useful
contributions are the ones that make it more correct, easier to trust or easier to run:

- **Bug reports** with the version (`!version` or `GET /healthz`), the relevant log lines with
  secrets removed, and what you expected instead.
- **Documentation fixes.** The docs are tested against the code, so a wrong default or a stale
  link is a real bug.
- **Tests** for behaviour that has none.
- **Features that fit the project.** Read [docs/purpose.md](docs/purpose.md) first, in particular
  [what it deliberately doesn't do](docs/purpose.md#what-it-deliberately-doesnt-do). A feature
  that needs one of those to be reversed is better discussed in an issue before any code is
  written.

## Tech stack

| Part | Choice |
| --- | --- |
| Language | Python 3.11 or newer |
| HTTP service | FastAPI on uvicorn |
| Outbound HTTP | httpx (Talk, Files, the model backend) |
| Packaging | uv, with a lock file; hatchling as the build backend |
| Tests | pytest, pytest-asyncio, respx |
| Lint, format, types | ruff, mypy, run by pre-commit and in CI |
| Delivery | Docker image; CI and releases on Forgejo workflows |

## Repository layout

```
src/sable/
  __main__.py     command line: .env loading, --check, starts uvicorn
  app.py          FastAPI app: /notify, /hook/{name}, /healthz, startup and shutdown
  poller.py       long-polls every allowed conversation the account is in
  events.py       turns a Talk message into a TalkEvent
  bot.py          decides whether an event is a trigger, then runs it
  commands.py     the command registry and the built-in commands
  llm.py          OpenAI-compatible /chat/completions client
  openwebui.py    Open WebUI backend, where Open WebUI runs the tool loop
  talk.py         Nextcloud Talk client (rooms, chat, reactions, who am I)
  files.py        uploads attachments and shares them into a conversation
  hooks.py        renders another service's webhook payload as a message
  config.py       every setting, parsing and validation, startup warnings
  limits.py       request body caps, applied before authentication
  ratelimit.py    per-person trigger limit
  history.py      per-conversation rolling history (in memory)
  mentions.py     stops posted text from pinging a whole room
  state.py        tracks whether Nextcloud and the model are reachable
  logs.py         keeps the health-check access lines out of the log
tests/            offline test suite, one file per module plus test_docs.py
docs/             purpose, configuration, deployment, security, future, releasing
Agents/           notes for people and models changing sable, incl. the Talk API reference
.forgejo/         issue and pull request templates, and the workflows in workflows/
```

A message travels `poller.py` (one long poll per conversation) → `events.parse_message` →
`Bot.would_handle` (a side-effect-free filter) → `Bot.handle` as a background task (rate limit,
then a command, the model, or the ask reaction) → `talk.py`. `/notify` and `/hook/{name}` enter
at `app.py` instead and never pass through `Bot.handle`. Why it is built this way:
[docs/purpose.md](docs/purpose.md); which setting gates which step:
[how the access layers combine](docs/configuration.md#how-the-access-layers-combine).

## Getting started in 5 minutes

You need [uv](https://docs.astral.sh/uv/). It will fetch a suitable Python if you have none.

```bash
git clone <this repository> && cd sable
git switch dev
uv sync --locked --extra dev
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run mypy
uv run pre-commit install        # optional: runs the checks on each commit
```

`--locked` installs exactly what `uv.lock` pins and fails if the lock and `pyproject.toml`
disagree. Without uv, `pip install -e '.[dev]'` still works; you just get whatever pip resolves
at that moment rather than the locked set.

To run it against a real Nextcloud, copy `.env.example` to `.env`, fill in the three required
settings (the rest is in [docs/configuration.md](docs/configuration.md)), and check the result:

```bash
cp .env.example .env
uv run python -m sable --check            # validates the configuration and exits
uv run python -m sable --host 127.0.0.1   # serves, and starts polling
```

Pass `--host 127.0.0.1` on a laptop: the default `SABLE_HOST` is `0.0.0.0`, which the container
needs. Use a **throwaway Nextcloud account** with its own app password, in a room you do not mind
breaking, since an app password reaches everything its user can. `.env` is ignored by git; never
commit it, and never paste it into an issue.

## Testing

The suite runs offline. Talk, Files and the model backend are mocked with respx, so no
Nextcloud, model or network is needed, and a failing test means the code is wrong, not the
environment.

| File | Covers |
| --- | --- |
| `test_bot.py` | routing, the access layers, commands, the reaction, the model path |
| `test_poller.py` | the long-poll loop against a scripted Talk |
| `test_talk.py`, `test_files.py` | the Talk and Files clients and their error handling |
| `test_llm.py`, `test_openwebui.py` | the two model backends |
| `test_app.py` | the HTTP surface: auth, body caps, `/notify`, `/hook`, `/healthz` |
| `test_config.py`, `test_operator_config.py` | parsing, validation, warnings; a realistic `.env` |
| `test_events.py`, `test_hooks.py`, `test_mentions.py`, `test_ratelimit.py`, `test_history.py`, `test_logs.py` | the smaller modules |
| `test_main.py`, `test_version.py` | `--check`; the version and its changelog section |
| `test_docs.py` | the documentation, read back as claims about the code |

`tests/test_docs.py` fails when:

- the code reads a `SABLE_*` variable that `.env.example`, `compose.yaml` or
  `docs/configuration.md` does not mention, or one of them mentions a variable nothing reads;
- a Default in the `| Variable | Default | Notes |` tables of `docs/configuration.md` differs
  from what an unset environment really gives;
- the startup banner's labels, or its `tools:` line, differ from the sample in
  `docs/deployment.md`;
- a relative link or `#anchor` in the docs does not resolve;
- a document still describes the removed webhook-bot design.

The failure message names the file and the setting. Fix whichever side is wrong; do not loosen
the test.

## How do I...

### Add a command

- [ ] Add an async function to `src/sable/commands.py` with `@registry.command(name, help=...)`.
  It takes a `Context` and returns Markdown, or `None` to stay silent. Raise `CommandError` for
  a user-facing refusal. If it calls the model, go through `ctx.bot.answer_with_llm`, which
  enforces `SABLE_LLM_USERS`.
- [ ] Add tests in `tests/test_bot.py`, including that `SABLE_ADMIN_COMMANDS` can restrict it.
- [ ] Mention it where commands are listed in `docs/purpose.md`, and add a line under
  `## Unreleased` in `CHANGELOG.md`.

A command in full:

```python
# src/sable/commands.py
@registry.command("deploy", help="Show the last deploy.", usage="deploy [env]")
async def deploy(ctx: Context) -> str:
    env = ctx.argv[0] if ctx.argv else "prod"
    if env not in {"prod", "staging"}:
        raise CommandError(f"I don't know the {env} environment.")
    return f"**{env}** is on `abc1234`, deployed 20 minutes ago."
```

A `CommandError` is posted to the room as written; anything else is logged and reported as a
crash. `ctx` carries the parsed event, the raw argument string, a shell-split `argv`, and
`ctx.bot` for `answer_with_llm`, `history` and `reply`. Commands are open to everyone by default;
`ctx.is_admin` says whether the sender is an administrator
([who may run which command](docs/configuration.md#who-may-run-which-command)).

### Add a setting

- [ ] In `src/sable/config.py`: a field, parsing in `Config.from_env`, validation that raises
  `ConfigError` with a message naming the variable, and a warning in `config.warnings` if the
  default is the risky one.
- [ ] Document it in all three operator-facing files: `.env.example`, `compose.yaml` and a row
  in the `| Variable | Default | Notes |` table of `docs/configuration.md`.
- [ ] Add it to the default mapping at the top of `tests/test_docs.py` (`DOCUMENTED_LLM_DEFAULTS`
  for a field of `config.llm`) if it has a documented default, and tests for parsing and
  validation in `tests/test_config.py`.
- [ ] If an operator should see it at startup, add a line to the banner in `app.py` and the
  sample in `docs/deployment.md` (the test compares labels), and to `--check` in `__main__.py`.
- [ ] A line under `## Unreleased` in `CHANGELOG.md`.

### Add or change a Talk API call

- [ ] Read the official docs first (https://nextcloud-talk.readthedocs.io/en/latest/) and cite
  the page in your change. Do not write it from memory; see
  [Agents/API/verify-apis-against-docs.md](Agents/API/verify-apis-against-docs.md).
- [ ] Mind the versions: rooms are API `v4`, chat and reactions are `v1`.
- [ ] Update [Agents/API/talk-user-api-reference.md](Agents/API/talk-user-api-reference.md).
- [ ] Mock it with respx in `tests/test_talk.py` and cover the error statuses the docs list.

## Code style

- ruff lints and formats (`uv run ruff check .`, `uv run ruff format --check .`; add `--fix` and
  drop `--check` to apply), and mypy checks types (`uv run mypy`). CI runs the same commands.
- Type-hint public functions. Comments explain why, not what; if the reason is a past incident,
  say so. Match the density and tone of the code around yours.
- Keep functions small and single-purpose. Prefer editing an existing module to adding one, and
  do not add a dependency without a reason that you can state in the pull request.
- Never log or print a secret. The app password is a `repr=False` field and a test checks it;
  keep it that way for anything you add.

## Commits and pull requests

The history (`git log`) uses a short imperative subject that says what changed and, where it
helps, why it matters ("Skip rooms nobody talks to, treat a held poll as slow"), with a body
when the reason is not obvious. One logical change per commit. Conventional Commits are not
required.

Open pull requests against `dev`. Before you do:

- [ ] Tests added or updated, and `uv run pytest` passes.
- [ ] `uv run ruff check .`, `uv run ruff format --check .` and `uv run mypy` pass.
- [ ] Docs and `.env.example` updated if behaviour or a setting changed.
- [ ] A line under `## Unreleased` in [CHANGELOG.md](CHANGELOG.md) (add that heading above the
  newest release if it is not there: a release renames it).

Releases are the maintainers' job ([docs/releasing.md](docs/releasing.md)). To report a
vulnerability, follow [SECURITY.md](SECURITY.md). For anything else, ask in an issue on the
repository: say what you tried and paste the failing command's output.
