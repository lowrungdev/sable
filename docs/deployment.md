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

### If your Nextcloud uses an internal or self-signed certificate

That is the *outbound* direction — sable verifying Nextcloud's certificate when it posts a reply.
It fails by default with `CERTIFICATE_VERIFY_FAILED`, because **httpx verifies against its own
bundled certifi store, not the system one**, so installing your CA in the container's trust store
achieves nothing on its own.

The fix is two lines in [`compose.yaml`](../compose.yaml), already there:

```yaml
volumes:
  - ${SABLE_HOST_CA_DIR:-/etc/ssl/certs}:/etc/ssl/certs:ro
environment:
  SSL_CERT_FILE: /etc/ssl/certs/ca-certificates.crt
```

The mount lends the container the host's CA bundle — which already contains your internal CA,
since the host trusts it — and `SSL_CERT_FILE` is what redirects Python away from certifi to that
bundle. No application setting is involved; sable has no TLS options and no way to disable
verification.

Three things that will bite you if you vary this:

- **Point it at the complete bundle, not just your CA.** `SSL_CERT_FILE` *replaces* the trust
  store rather than adding to it. A file containing only your internal CA makes the internal
  Nextcloud work and every public HTTPS call — your model backend included — fail.
- **Do not use `SSL_CERT_DIR` with a plain folder of `.crt` files.** OpenSSL only looks up
  certificates there by hashed filename, so an ordinary folder silently trusts nothing, and
  because it also replaces the store, public TLS breaks too. `openssl rehash` fixes the lookup
  but not the replacement.
- **Mount the directory, not the single file.** `update-ca-certificates` on the host writes a new
  file, and a single-file bind mount pins the old inode until the container restarts.

If your distribution keeps certificates elsewhere — RHEL and Fedora use `/etc/pki/tls/certs` —
set `SABLE_HOST_CA_DIR` accordingly. Beware that Docker creates a missing bind-mount source as an
empty directory, which would leave the container trusting nothing at all.

On bare metal the host bundle is already in use, so the systemd unit needs only:

```ini
Environment=SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
```

To confirm it worked, watch for the reply direction succeeding — `!ping` answering in the room is
the real test. A failure logs `could not post to <token>: … CERTIFICATE_VERIFY_FAILED`.

## 5. Register the bot with Nextcloud

On the Nextcloud server, as the web user (`www-data`, `nginx`, `apache`, depending on your
setup):

```bash
sudo -u www-data php occ talk:bot:install "sable" "<the same secret>" "https://sable.example.org/webhook" "A helpful bot" --feature webhook --feature response --feature reaction
```

- The URL must end in **`/webhook`**.
- **Features** are a bitmask: `webhook` (1) delivers chat messages to sable, `response` (2) lets
  it post messages and reactions back, `reaction` (8) adds notifications when someone adds or
  removes a reaction. `event` (4) is for bots running inside Nextcloud as PHP and is mutually
  exclusive with these. Omitting `--feature` entirely gives you `webhook` + `response` for an
  HTTP URL — the two a webhook bot cannot work without — so the flags above differ from the
  default only in adding `reaction`.
  Check what a bot ended up with using `occ talk:bot:list`; at runtime,
  [`TalkClient.features()`](../src/sable/talk.py) asks Nextcloud and returns the bitmask, so
  `11` means webhook + response + reaction.
- Reaction events are **parsed but not yet acted on**: `Like` and `Undo` arrive fully decoded in
  [`events.py`](../src/sable/events.py) and `Bot.handle` logs them. Enabling the feature now
  costs nothing and means no reinstall when a handler lands — see
  [future.md](future.md#talk-features-not-yet-used).
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

### What the log tells you

At `INFO`, sable logs its own lifecycle, its configuration, and every use — and nothing else, so
the interesting lines are not buried:

```
sable 0.3 starting
  listening on:   http://0.0.0.0:8080
  webhook URL:    POST /webhook  (give this to occ talk:bot:install)
  nextcloud:      https://cloud.example.org
  bot name:       'sable'   command prefix: '!'
  model:          gpt-4o-mini at https://api.openai.com/v1
  ask reaction:   ⁉️
  ai rooms:       (mentions only)
  alerting:       enabled, aliases: alerts
  backend pin:    on
  log level:      INFO
connected to Nextcloud 31.0.4 at https://cloud.example.org
sable 0.3 ready
added to conversation abcd1234 ('Team chat') - now receiving its messages
Alice (users/alice) ran !ping in abcd1234
Alice (users/alice) asked the model in abcd1234 (22 chars)
gpt-4o-mini answered in 1.8s (243 chars)
Alice (users/alice) asked the model about message 12 in abcd1234, written by Bob
relayed an alert to abcd1234 (alias alerts) as message 4242
removed from conversation abcd1234 ('Team chat') - no further messages from it
sable 0.3 stopping
sable 0.3 stopped
```

**The startup probe** (`connected to Nextcloud …`) calls `status.php`, which needs no
credentials. It proves DNS, TLS and that the thing on the other end is a Nextcloud — so a wrong
URL or an untrusted certificate is reported at boot rather than on the first reply someone is
waiting for. It is never fatal: Nextcloud may simply not be up yet. `SABLE_STARTUP_CHECK=false`
skips it.

**Reachability is logged as transitions**, not per attempt, so an outage is two lines rather than
one per retry:

```
ERROR  sable.state: lost connection to Nextcloud: ConnectError: All connection attempts failed
INFO   sable.state: Nextcloud is reachable again
```

The same applies to the model backend. A transport failure counts as unreachable; an HTTP error
response does not — the service answered, and that is logged with its status code and the first
200 characters of the body.

**Message text stays out of `INFO`.** A use is logged as who, what and where, with sizes rather
than content: `asked the model in abcd1234 (22 chars)`. `SABLE_LOG_LEVEL=DEBUG` adds the prompt,
command arguments and the message a ⁉️ referred to, plus a line for every event sable decided
*not* to act on and one per outbound HTTP call. Treat DEBUG as containing chat content.

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
occ talk:bot:install "sable" "<new secret>" "https://sable.example.org/webhook" "A helpful bot" --feature webhook --feature response --feature reaction
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

The operational steps are below. [security.md](security.md) has the whole posture — what
protects each trust boundary, how secrets are handled, and the
[accepted risks](security.md#accepted-risks) worth reading before this is reachable from
anywhere you do not control.

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
