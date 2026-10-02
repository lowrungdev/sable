# sable

An assistant for [Nextcloud Talk](https://nextcloud-talk.readthedocs.io/en/latest/) that runs as
an ordinary Nextcloud user account. It signs in as that user and long-polls the chat API, so it
only ever connects out: nothing has to reach it, and nothing is installed in Nextcloud. Its one
credential is that user's app password, which cannot be scoped, so give it an account of its own.

- Commands: `!help`, `!ping`, `!ai` and whatever you add, as a decorated async function, or as a
  [plugin](docs/plugins.md): a Python file and a settings file in a mounted directory, run in a
  process of its own. A plugin can also answer to a phrase in ordinary chat, not just a command,
  or run on its own timer (cron or a plain interval).
- An assistant that answers through any OpenAI-compatible model backend, or through Open WebUI
  with tools.
- Alerts into a conversation from CI, Alertmanager or a cron job with `POST /notify`, optionally
  with a file attached, and `POST /hook/{name}` for services that cannot speak that shape.
- One container, configured by environment variables, with no database and nothing to back up.

Out of the box it is open: it answers anyone in any conversation it is invited to. Set
`SABLE_ALLOWED_ROOMS` before you rely on it
([how the access layers combine](docs/configuration.md#how-the-access-layers-combine)).

## Quickstart

In Nextcloud, create a user for sable and give it an app password (Settings → Security →
Devices & sessions). Then:

```bash
cp .env.example .env     # set SABLE_NEXTCLOUD_URL, SABLE_NEXTCLOUD_USER, SABLE_NEXTCLOUD_PASSWORD
docker compose up -d --build
```

Set `SABLE_ALLOWED_ROOMS` to the tokens of the conversations it should serve (the last part of
each one's URL), and `SABLE_LLM_BASE_URL`, `SABLE_LLM_API_KEY` and `SABLE_LLM_MODEL` for the
assistant; with no model it is a command bot. `sable --check` validates the configuration without
starting anything. Then invite the user to a conversation like any other participant: within a
minute `!ping` answers `pong`.

## Documentation

| Document | What is in it |
| --- | --- |
| [purpose.md](docs/purpose.md) | What this is for, what it leaves alone, and how it is built |
| [configuration.md](docs/configuration.md) | Every environment variable, with provider recipes and worked examples |
| [plugins.md](docs/plugins.md) | Adding commands, phrase and schedule triggers without changing sable: the layout, the settings file, the author API, access and failures |
| [deployment.md](docs/deployment.md) | Running it for real: the account, Docker, systemd, TLS, verifying, plugins, operating |
| [security.md](docs/security.md) | Trust boundaries, how secrets are handled, and the risks that are accepted rather than solved |
| [releasing.md](docs/releasing.md) | Branches, the version scheme, and the Forgejo pipeline that publishes |
| [future.md](docs/future.md) | Known limitations, what it would take to lift them, and decisions worth revisiting |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Setup, tests, the code map, adding a command or a setting |
| [SECURITY.md](SECURITY.md) | Supported versions and how to report a vulnerability |
| [CHANGELOG.md](CHANGELOG.md) | What changed per release |

Coding agents should read [CLAUDE.md](CLAUDE.md) (also reachable as [AGENTS.md](AGENTS.md)).

## License

MIT, see [LICENSE](LICENSE).
