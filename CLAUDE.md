# CLAUDE.md

Guidance for AI coding agents working in this repository. It applies to every agent and tool.
Humans: see [CONTRIBUTING.md](CONTRIBUTING.md).

## What this is

sable is a Nextcloud Talk assistant that runs as an ordinary Nextcloud **user account**, not a
Talk bot: it long-polls chat as that user, answers commands and (optionally) an OpenAI-compatible
model, and exposes `/notify` and `/hook/{name}` for inbound alerts. Python 3.11+, FastAPI, httpx,
uv, hatchling. Read before changing anything non-trivial:

- [README.md](README.md), [CONTRIBUTING.md](CONTRIBUTING.md)
- [docs/purpose.md](docs/purpose.md): what it is for and deliberately does not do
- [docs/security.md](docs/security.md): threat model and accepted risks
- [Agents/README.md](Agents/README.md): the Talk API reference and the verify-against-docs rule

## Commands

```bash
uv sync --locked --extra dev      # install exactly what uv.lock pins
uv run pytest                     # whole suite; offline, no Nextcloud or model needed
uv run ruff check .               # lint
uv run ruff format --check .      # formatting
uv run mypy                       # types
uv run python -m sable --check    # validate the environment's configuration and exit
```

`PYTHONPATH=src` is not needed after a proper `uv sync`. Run one test with
`uv run pytest tests/test_bot.py -k name`. Run the whole suite, ruff and mypy before saying a
task is done.

## Architecture

`src/sable/`:

- `__main__.py` CLI, `.env` loader, `--check`, starts uvicorn. `app.py` FastAPI app, lifespan,
  reply ceiling, `/notify`, `/hook/{name}`, `/healthz`.
- `poller.py` one long poll per allowed room. `events.py` Talk message to `TalkEvent`.
- `bot.py` screening, routing, running commands and model calls. `commands.py` registry and the
  built-in commands.
- `plugins.py` plugins, core side: discovery, the settings schema, `Worker` (one subprocess and
  its protocol), `PluginManager` (validation, the access decision `allows`, `PhraseMatcher` and
  `phrase_hits`/`claim_phrase` for phrase triggers, what a plugin may post, `!plugins`).
  `plugin_host.py` the worker process. `plugin_api.py` what a plugin imports, including the
  `@command` and `@on_phrase` decorators. Author and operator docs:
  [docs/plugins.md](docs/plugins.md); examples: `examples/plugins/`.
- `llm.py` chat-completions client. `openwebui.py` Open WebUI backend (it runs the tool loop).
- `talk.py` Talk client. `files.py` upload and share. `hooks.py` webhook payload to message.
- `config.py` all settings. `limits.py` body caps. `ratelimit.py` per-person limit.
  `history.py` in-memory history. `mentions.py` defuses mass mentions. `state.py`, `logs.py`.

**Message flow.** `Poller._dispatch` -> `events.parse_message` -> `Bot.would_handle` (pure
pre-filter; no rate-limit token consumed) -> `Bot.handle` as a background task -> a command, the
model, or the ask reaction -> `TalkClient`. `would_handle` and `handle` share `_screen` and
`_route`; keep them that way so the pre-filter cannot drift from the handler.

**Config.** Environment variables only (`SABLE_*`). `Config.from_env` parses and validates;
invalid input raises `ConfigError` (exit 2 at startup); risky-but-legal settings add to
`config.warnings`. Every setting is listed in `.env.example`, `compose.yaml` and
`docs/configuration.md`, and `tests/test_docs.py` checks the defaults in the last one against
what an unset environment really gives.

**Access layers, in the order `bot.py` applies them** (`_screen`, `_route`, `handle`, then the
path-specific checks):

1. Allowed room (`SABLE_ALLOWED_ROOMS`; the poller never follows others).
2. Ignore list (`SABLE_IGNORE_USERS`).
3. Our own account, then any actor of type `bots`.
4. Is it a trigger at all: prefix command, mention, AI room, or the ask reaction.
5. Per-person rate limit (`SABLE_RATE_LIMIT`), counted before the checks below.
6. Then by route. Reaction: `SABLE_ASK_ADMINS_ONLY`, then `SABLE_LLM_USERS`. Command:
   a plugin's command first meets its plugin's access (`PluginManager.allows`: `NOT_HERE` is
   answered as an unknown command, `NOT_YOU` as "not available to you"), then
   `SABLE_ADMIN_COMMANDS`/`SABLE_ADMIN_USERS`; a command that calls the model meets
   `SABLE_LLM_USERS` in `answer_with_llm`. Mention or AI-room message: `SABLE_LLM_USERS`.
7. Tools only in rooms named by `SABLE_LLM_TOOL_ROOMS` (`LLMConfig.tools_in`).

Admin and model checks refuse bots inside the decision itself (`is_admin_actor`,
`can_use_model`), not only in `_screen`. Keep it so.

**HTTP layer.** `limits.py` caps request bodies before authentication, so oversize requests get
413 before any token check. Tokens are compared as bytes with `hmac.compare_digest`. `/notify`
and `/hook/{name}` answer 404 when not configured. `/healthz` is open unless
`SABLE_HEALTH_TOKEN` is set. Tools are gated per room, and `SABLE_LLM_EXTRA_BODY` may not carry
tool keys (`TOOL_BODY_KEYS` in `config.py`): that would bypass the room gate.

**Plugins.** Each plugin runs in its own worker process and everything it sends is untrusted.
Read [docs/plugins.md](docs/plugins.md) and the plugin section of
[docs/security.md](docs/security.md) before touching them. The rules:

- Redaction is for the core's own text about a plugin, not for the plugin's own words. A
  `PluginError`'s text is shown to chat and logged exactly as the plugin wrote it, by design (the
  plugin is telling the user something; only `_visible_text` strips control characters). It is the
  core's own failure and log text about a plugin - a crash message, a `check()` failure, stderr, a
  line in `!plugins` or `--check` - that must never carry a setting unredacted: that text goes
  through `PluginRecord.redact` (`Worker._text`) first, and settings values themselves are never
  printed anywhere. What a plugin posts, `PluginError` text included, still goes through
  `_ChatSink` (defanged, capped, its own rooms only). Access is decided in `PluginManager.allows`
  before a worker is called, for a phrase handler exactly as for a command.
- `plugin_api.py` imports the standard library and nothing else from `sable`; `plugin_host.py`
  imports nothing from `sable` but `plugin_api`. Both run in the worker, whose environment is
  built from nothing (`worker_environment`): never pass `os.environ` through or add a `SABLE_*`.
- Re-validate what a worker declares (`parse_declaration`); a malformed line is a protocol
  violation. Each `Bot` has its own `Registry.copy()`, so never register into the module-level one.
- Plugin tests start real processes and are POSIX-only (`posix_only` in `tests/plugin_helpers.py`);
  use `tests/fake_worker.py` for misbehaviour and keep real-worker tests few and fast.
  `examples/plugins` is linted by ruff, skipped by mypy, and loaded by `test_plugin_examples.py`.
- A change to what a worker can do or see updates the two lists in `docs/security.md`.

## Conventions and gotchas

- **Talk API versions differ.** Rooms are `api/v4`; chat and reactions are `api/v1`. Wrong
  guesses 404. Verify any external API against the official docs before coding, and cite them;
  start from [Agents/API/talk-user-api-reference.md](Agents/API/talk-user-api-reference.md) and
  [Agents/API/verify-apis-against-docs.md](Agents/API/verify-apis-against-docs.md). Say what came
  from docs and what from memory.
- **`.env` lines cannot carry a trailing `# comment`**: sable's loader keeps it in the value.
  Comments go on their own line, in `.env.example` too.
- **Never log or print secrets** (app password, API key, hook, notify and health tokens). The
  password field is `repr=False` and a test enforces it. Do not put real credentials in files,
  fixtures or commit messages.
- **Tests stay offline.** Mock HTTP with respx. No real Nextcloud, no real model, no sleeping on
  real time where a fake will do.
- **Docs and settings move together.** `tests/test_docs.py` fails if a variable is missing from
  `.env.example`, `compose.yaml` or `docs/configuration.md`, if a documented default is wrong, if
  the startup banner drifts from `docs/deployment.md`, or if a link breaks. Fix the docs or the
  code; never weaken the test.
- **Dependencies are locked with hashes** in `uv.lock`. After editing `pyproject.toml` run
  `uv lock`; CI uses `--locked` and fails on a stale lock. No new dependency without a reason.
- **The version lives only in `pyproject.toml`.** Do not write it anywhere else.
- Keep changes minimal and match the surrounding style. Prefer editing existing files over
  adding new ones. Comments say why, not what.
- When you add a setting or change behaviour, update the docs and add a line under
  `## Unreleased` in `CHANGELOG.md`.
- Consult [docs/security.md](docs/security.md) before touching auth, limits, the poller or the
  tools gating.

## Rules for agents

- Do **not** commit, push, merge, tag, release, bump the version, delete branches or worktrees,
  or edit `main` or `release`, unless the user explicitly asks in this conversation. Releases
  follow [docs/releasing.md](docs/releasing.md) exactly.
- Work on `dev` or a branch from it. Do not touch files outside the task.
- If a secret appears anywhere (a file, a log, chat), do not copy it onward; tell the user to
  rotate it.
- Report outcomes faithfully. A failing test or check is reported with its output, not
  summarised away or described as flaky. Say plainly what you did not run or could not verify.
