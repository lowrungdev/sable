# Deployment

Getting sable running against a real Nextcloud, and keeping it running.

## Prerequisites

- **Nextcloud with Talk installed**, and shell access to run `occ` as the web user.
- **An HTTPS URL that your Nextcloud server can reach**, pointing at sable. This is a webhook
  bot: Nextcloud makes the connection, so sable must be reachable *from the server*, though not
  necessarily from the public internet. A private network or a VPN is fine.
- **Python 3.11+ or Docker** on the host running sable.

Nextcloud refuses to call `http://` or private-network webhook URLs unless an admin has
explicitly allowed local remotes (`allow_local_remote_servers` in `config.php`). Prefer real
TLS, even internally.

## 1. Generate the secret

```bash
openssl rand -hex 32
```

64 hex characters, inside Talk's 40–128 range. The *same* value goes into sable's
`SABLE_BOT_SECRET` and into `occ talk:bot:install` — it authenticates both directions, so treat
it like a password.

## 2. Configure

```bash
cp .env.example .env
$EDITOR .env
```

At minimum `SABLE_BOT_SECRET` and `SABLE_NEXTCLOUD_URL`. Everything else is in
[configuration.md](configuration.md). Then:

```bash
sable --check
```

## 3. Run it

### Docker Compose (recommended)

[`compose.yaml`](../compose.yaml) lists **every** setting in its `environment:` block, with
only `SABLE_BOT_SECRET` active and the rest commented out showing their defaults — so you can
configure the bot entirely in that one file. It also loads `.env` if one exists (optional), and
values in the `environment:` block override it. Secrets are wired as `${VAR}` lookups, so they
stay in `.env` rather than in a file you commit; missing ones fail immediately:

```
error while interpolating services.sable.environment.SABLE_BOT_SECRET:
required variable SABLE_BOT_SECRET is missing a value: required — put it in .env
```

The service publishes to `127.0.0.1:8080` only, expecting a reverse proxy on the host:

```bash
docker compose up -d --build
docker compose logs -f sable
```

The image runs as a non-root user (uid 10001), holds no state, and has a `HEALTHCHECK` on
`/healthz`, so `docker ps` shows `healthy` once it is up.

If your proxy runs in Docker too, drop the `ports:` block, put both services on one network and
let the proxy reach `sable:8080` directly.

### systemd

```bash
sudo useradd --system --home /opt/sable --shell /usr/sbin/nologin sable
sudo install -d -o sable -g sable /opt/sable
sudo -u sable git clone <your-fork> /opt/sable/app
sudo -u sable python3 -m venv /opt/sable/venv
sudo -u sable /opt/sable/venv/bin/pip install uv
# --locked installs exactly the versions uv.lock pins, verified by hash.
cd /opt/sable/app && sudo -u sable /opt/sable/venv/bin/uv sync --locked --no-dev
sudo install -m 0640 -o sable -g sable /opt/sable/app/.env.example /opt/sable/.env
sudo $EDITOR /opt/sable/.env
```

That puts the environment in `/opt/sable/app/.venv`, which is what the unit below runs. Plain
`pip install /opt/sable/app` works too, but resolves dependencies fresh instead of using the
lock, so two servers installed a month apart will not match.

`/etc/systemd/system/sable.service`:

```ini
[Unit]
Description=sable (Nextcloud Talk bot)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=sable
Group=sable
WorkingDirectory=/opt/sable
ExecStart=/opt/sable/app/.venv/bin/sable --env-file /opt/sable/.env
Restart=on-failure
RestartSec=5s

# The bot writes nothing and needs no privileges.
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadOnlyPaths=/opt/sable
CapabilityBoundingSet=
MemoryMax=512M

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now sable
journalctl -u sable -f
```

## 4. Put TLS in front of it

sable speaks plain HTTP and does not terminate TLS. Any reverse proxy will do, with one hard
requirement: **it must pass the request body through byte for byte.** The signature covers the
raw bytes, so anything that reformats, re-encodes or truncates JSON will cause every webhook to
fail verification with a 401.

### Caddy

```
sable.example.org {
    reverse_proxy 127.0.0.1:8080
}
```

### nginx

```nginx
server {
    listen 443 ssl http2;
    server_name sable.example.org;

    ssl_certificate     /etc/letsencrypt/live/sable.example.org/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/sable.example.org/privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        # Never transform the body: the HMAC covers the exact bytes.
        proxy_request_buffering on;
        client_max_body_size 1m;
    }
}
```

The `X-Nextcloud-Talk-*` headers need no special handling — both proxies forward them as-is.

Confirm the path works before involving Nextcloud:

```bash
curl -fsS https://sable.example.org/healthz
# {"status":"ok","version":"0.1.0","bot":"sable","llm":"gpt-4o-mini","notify":true}
```

## 5. Register the bot with Nextcloud

On the Nextcloud server, as the web user (`www-data`, `nginx`, `apache`, depending on your
setup):

```bash
sudo -u www-data php occ talk:bot:install \
  "sable" \
  "<the same secret>" \
  "https://sable.example.org/webhook" \
  "A helpful bot" \
  --feature webhook --feature response
```

- The URL must end in **`/webhook`**.
- `webhook` delivers chat messages to sable; `response` lets it post back. Both are needed.
  Add `--feature reaction` to also receive reaction events. Omitting `--feature` installs the
  default set.
- `--no-setup` prevents moderators from enabling the bot themselves, if you want to control
  that centrally.
- The name is what people will see and type. Keep it in step with `SABLE_BOT_NAME`, which is
  what mention detection matches on.

Neighbouring commands:

```bash
occ talk:bot:list                 # ids, names, URLs, state
occ talk:bot:state <id> <0|1>     # disable / enable globally
occ talk:bot:uninstall --id <id>  # remove it
```

Reinstalling with a changed secret or URL means uninstalling first — Nextcloud rejects
duplicates of either.

## 6. Enable it in a conversation

Talk only delivers events for conversations where the bot is switched on. A moderator does this
in **Conversation settings → Bots**. Nothing reaches sable until then, which is the usual
explanation for a bot that looks dead.

## 7. Verify end to end

In the conversation:

```
!ping      →  pong 🏓
!version   →  sable 0.1.0 · model gpt-4o-mini
```

To test the webhook path without Nextcloud — this is exactly what Talk does, with the signature
computed by hand:

```bash
SECRET='<your bot secret>'
BODY='{"type":"Create","actor":{"type":"Person","id":"users/alice","name":"Alice"},"object":{"type":"Note","id":"7","name":"message","content":"{\"message\":\"!ping\",\"parameters\":{}}","mediaType":"text/markdown"},"target":{"type":"Collection","id":"<conversation token>","name":"Test"}}'
RAND=$(openssl rand -hex 32)
SIG=$(printf '%s' "$RAND$BODY" | openssl dgst -sha256 -hmac "$SECRET" | awk '{print $2}')

curl -sS -i -X POST https://sable.example.org/webhook \
  -H "X-Nextcloud-Talk-Random: $RAND" \
  -H "X-Nextcloud-Talk-Signature: $SIG" \
  -H "X-Nextcloud-Talk-Backend: https://cloud.example.org" \
  -H 'Content-Type: application/json' \
  --data-binary "$BODY"
# 200 {"status":"accepted"}  — and "pong 🏓" appears in the conversation
```

A `200` proves TLS, the proxy, the signature and parsing; the message appearing in the room
proves the reply direction and the secret's other half. Corrupt one character of `SIG` and it
must come back `401`.

And the alerting path:

```bash
curl -fsS -X POST https://sable.example.org/notify \
  -H "Authorization: Bearer $SABLE_NOTIFY_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"room": "alerts", "message": "deploy **v1.2.3** finished"}'
```

## Operations

**Logs are the whole observability story.** `INFO` gives you one line per handled event
(`running ping for users/alice in abcd1234`), warnings for rejected signatures, failed model
calls and failed posts. `SABLE_LOG_LEVEL=DEBUG` adds a line for every message the bot decided
*not* to act on — the fastest way to debug prefix and mention matching.

**Health:** `GET /healthz` returns the version, the bot name, the configured model and whether
alerting is on. It does not call Nextcloud or the model, so it stays honest as a liveness probe.

**Upgrades** are a restart; there is no state and no migration.

```bash
git pull && docker compose up -d --build     # or: uv sync --locked --no-dev && systemctl restart sable
```

In-flight replies get up to 30 seconds to finish during shutdown, so a rolling restart does not
lose an answer someone is waiting for.

**Rotating the secret:**

```bash
occ talk:bot:uninstall --id <id>
occ talk:bot:install "sable" "<new secret>" "https://sable.example.org/webhook" "A helpful bot"
```

…then update `SABLE_BOT_SECRET` and restart. Expect a brief window where webhooks are rejected;
a bot is not a good place to need zero downtime.

**Run one process.** Conversation history and the redelivery de-duplication cache live in
memory, so `--workers 2` would split both: replies would forget context depending on which
worker answered. One process handles chat traffic without breaking a sweat (a few tens of MB
resident, and all I/O is async). Scale by running separate bots, not workers.

**Backups:** none. Nothing is persisted. Keep `.env` in a secret store — it is the only thing
that is hard to recreate.

## Hardening

- **Do not publish the port.** Bind to `127.0.0.1` (or a private network) and let the proxy be
  the only client. Nextcloud is the only legitimate caller of `/webhook`.
- **Keep the two secrets separate.** `SABLE_BOT_SECRET` is Nextcloud's; `SABLE_NOTIFY_TOKEN` is
  your alerting callers'. A leaked notify token then costs you noise, not the bot.
- **Leave `SABLE_PIN_BACKEND` on.** It stops a replayed webhook from redirecting the bot's
  replies at another server.
- **Use aliases in `SABLE_NOTIFY_ROOMS`** so alerting callers never learn conversation tokens.
- **Restrict egress** if the model runs elsewhere: sable needs to reach only Nextcloud and the
  LLM base URL.
- **Remember what the assistant forwards.** Every message it answers is sent to your configured
  LLM backend. For a hosted provider, that is chat content leaving your infrastructure — a
  local backend or a self-hosted gateway avoids the question.
- **Treat commands as public API.** Anyone in a conversation with the bot can trigger any
  command, so a command that shells out or touches production needs its own authorisation check
  (`ctx.event.actor.user_id` tells you who is asking).

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| Nothing happens at all | The bot is not enabled in that conversation (**Conversation settings → Bots**), or `occ talk:bot:list` shows it disabled. Talk sends nothing until it is on. |
| `401 invalid signature` in sable's logs | The secret differs from the registered one, or a proxy is altering the body. Compare `SABLE_BOT_SECRET` against `occ talk:bot:list`; test with the hand-signed curl above, which bypasses Nextcloud. |
| `403 unexpected backend` | `SABLE_NEXTCLOUD_URL` does not exactly match the URL Nextcloud reports for itself (scheme, host, no trailing slash, no port mismatch). Fix the URL, or set `SABLE_PIN_BACKEND=false`. |
| Webhook returns 200, no message appears | The reply direction is failing. Look for `could not post to <token>` — usually `SABLE_NEXTCLOUD_URL` unreachable from sable, or the bot lacks the `response` feature. |
| Nextcloud logs webhook timeouts | Something in front of sable is slow or buffering; sable itself answers before doing any work. Check the proxy, not the bot. |
| Answers are slow or absent, `completion failed` in logs | Model timeout. Raise `SABLE_LLM_TIMEOUT`, lower `SABLE_LLM_MAX_TOKENS`, or pick a faster model. |
| `the model returned an empty message` | A reasoning model spent its whole budget thinking. Raise `SABLE_LLM_MAX_TOKENS` or lower reasoning effort via `SABLE_LLM_EXTRA_BODY`. |
| Replies are cut short with `_[truncated]_` | The answer exceeded `SABLE_MAX_MESSAGE_CHARS`; Talk's own ceiling is 32000 characters. |
| `HTTP 429` from Talk | The bot is posting too fast. Talk rate-limits bots; batch or slow down whatever is calling `/notify`. |
| Mentions ignored | `SABLE_BOT_NAME` must match what people type. Set `SABLE_LOG_LEVEL=DEBUG` and watch for `message in <token> was not for me`. |
| `/notify` returns 404 | `SABLE_NOTIFY_TOKEN` is unset, so the route is disabled. |
| `/notify` returns 400 | The `room` is neither a known alias nor a plausible conversation token, or Talk rejected it. |
| Config error on startup | See [the table in configuration.md](configuration.md#startup-errors-and-what-they-mean). |
