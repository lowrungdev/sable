# Example plugins

Three small plugins in the layout [docs/plugins.md](../../docs/plugins.md) describes:

- `dice/`: `!roll 2d6`. One command, no settings, a user-facing error for bad input.
- `uptime/`: `!up`. Reads `ctx.settings` and has a `check()` that refuses the shipped
  placeholders.
- `certcheck/`: `!cert <host[:port]>`. Blocking I/O (`asyncio.to_thread`), a real, verified TLS
  handshake, and a failed verification's own reason surfaced instead of a crash.

All three ship inactive (`rooms: []`). Copy a folder into your plugins directory, put your own
conversation tokens in its `*_settings.yaml`, and read the code before you mount it: a plugin
is code that runs on your host. Setup, limits and the security picture are in
[docs/plugins.md](../../docs/plugins.md).
