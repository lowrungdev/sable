# Deployment

Getting sable running against a real Nextcloud, and keeping it running.

## Prerequisites

You need Nextcloud with Talk installed and shell access to run `occ` as the web user, an HTTPS
URL that your Nextcloud server can reach pointing at sable, and either Docker or Python 3.11+ on
whatever host runs it.

The URL is the part people get wrong. This is a webhook bot, so Nextcloud makes the connection
and sable has to be reachable from the server — though not necessarily from the public internet,
since a private network or a VPN is fine. Nextcloud also refuses to call plain `http://` or
private-network URLs unless an admin has allowed local remotes with
`allow_local_remote_servers` in `config.php`, so prefer real TLS even internally.

## 1. Generate the secret

```bash
openssl rand -hex 32
```

That gives 64 hex characters, inside Talk's range of 40 to 128. The same value goes into
sable's `SABLE_BOT_SECRET` and into `occ talk:bot:install`, and it authenticates both
directions, so treat it like a password.

## 2. Configure

```bash
cp .env.example .env
$EDITOR .env
```

At minimum set `SABLE_BOT_SECRET` and `SABLE_NEXTCLOUD_URL`; everything else is in
[configuration.md](configuration.md). Then check what it resolved to before going further:

```bash
sable --check
```

## 3. Run it

### Docker Compose (recommended)

[`compose.yaml`](../compose.yaml) lists every setting in its `environment:` block, with only
`SABLE_BOT_SECRET` active and the rest commented out beside their defaults, so the bot can be
configured entirely in that one file. It also loads `.env` if one exists, and values in the
`environment:` block override it. Secrets are wired as `${VAR}` lookups so they stay in `.env`
rather than in a file you commit, and a missing one fails immediately:

```
error while interpolating services.sable.environment.SABLE_BOT_SECRET:
required variable SABLE_BOT_SECRET is missing a value: required — put it in .env
```

The service publishes to `127.0.0.1:8080` only, expecting a reverse proxy on the host:

```bash
docker compose up -d --build
docker compose logs -f sable
```

The image runs as a non-root user, holds no state, and has a healthcheck on `/healthz`, so
`docker ps` shows it healthy once it is up. If your proxy runs in Docker too, drop the `ports:`
block, put both services on one network and let the proxy reach `sable:8080` directly.

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

sable speaks plain HTTP and does not terminate TLS, so put any reverse proxy in front of it.
There is one hard requirement: it must pass the request body through byte for byte. The
signature covers the raw bytes, so anything that reformats, re-encodes or truncates JSON makes
every webhook fail verification with a 401.

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

`X-Forwarded-For` and `X-Forwarded-Proto` are believed only from the addresses in
`SABLE_TRUSTED_PROXIES`, which defaults to loopback and so covers both examples above as written.
A proxy in another container is not loopback and has to be named; a proxy that is never named just
means the access log shows the proxy rather than the client. nginx's
`$proxy_add_x_forwarded_for` appends to whatever the client sent, which is safe here — the
forwarded list is read from the right, so the first address that is not a trusted proxy wins, and
anything a client made up sits to the left of its real address. That only holds while the trusted
list is not `*`. Details in
[configuration.md](configuration.md#which-proxies-are-believed).

Confirm the path works before involving Nextcloud:

```bash
curl -fsS https://sable.example.org/healthz
# {"status":"ok","version":"0.6","bot":"sable","llm":"gpt-4o-mini","notify":true}

# ...or, with SABLE_HEALTH_TOKEN set:
curl -fsS -H "X-Health-Token: $SABLE_HEALTH_TOKEN" https://sable.example.org/healthz
```

### If your Nextcloud uses an internal or self-signed certificate

That is the outbound direction: sable verifying Nextcloud's certificate when it posts a reply.
It fails by default with `CERTIFICATE_VERIFY_FAILED`, because httpx verifies against its own
bundled certifi store rather than the system one, so installing your CA in the container's trust
store achieves nothing on its own.

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

Three things will bite you if you vary this. Point `SSL_CERT_FILE` at the complete bundle
rather than just your CA, because it replaces the trust store rather than adding to it: a file
holding only your internal CA makes Nextcloud verify while every public HTTPS call, your model
backend included, fails. Do not reach for `SSL_CERT_DIR` with a plain folder of `.crt` files
either, since OpenSSL only looks certificates up there by hashed filename, so an ordinary folder
trusts nothing while still replacing the store. And mount the directory rather than the single
file, because `update-ca-certificates` on the host writes a new file and a single-file bind
mount pins the old inode until the container restarts.

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

The URL must end in `/webhook`, and the name is what people see and type, so keep it in step
with `SABLE_BOT_NAME`, which is what mention detection matches on. Add `--no-setup` if you want
to stop moderators enabling the bot themselves and control that centrally.

Features are a bitmask. `webhook` (1) delivers chat messages to sable, `response` (2) lets it
post messages and reactions back, and `reaction` (8) adds notifications when somebody adds or
removes a reaction, which is what the ⁉️ feature needs. The remaining value, `event` (4), is for
bots running inside Nextcloud as PHP and is mutually exclusive with these. Omitting `--feature`
altogether gives you webhook and response for an HTTP URL — the two a webhook bot cannot work
without — so the command above differs from the default only in adding reaction.

`occ talk:bot:list` shows what a bot ended up with. At runtime
[`TalkClient.features()`](../src/sable/talk.py) asks Nextcloud and returns the same bitmask, so
11 means webhook, response and reaction together.

Neighbouring commands:

```bash
occ talk:bot:list                 # ids, names, URLs, state
occ talk:bot:state <id> <0|1>     # disable / enable globally
occ talk:bot:uninstall --id <id>  # remove it
```

Reinstalling with a changed secret or URL means uninstalling first — Nextcloud rejects
duplicates of either.

## 6. Enable it in a conversation

Talk only delivers events for conversations where the bot is switched on, which a moderator does
under Conversation settings, Bots. From the command line it is
`occ talk:bot:setup <bot-id> <token>`, taking the id from `occ talk:bot:list` and the token from
the end of the conversation's URL. Nothing reaches sable until then, which is the usual
explanation for a bot that looks dead.

sable logs the moment it happens, which doubles as proof that the whole inbound path works:

```
added to conversation abcd1234 ('Team chat') - now receiving its messages
```

## 7. Verify end to end

In the conversation:

```
!ping      →  pong 🏓
!version   →  sable 0.6 · model gpt-4o-mini
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

A 200 proves TLS, the proxy, the signature and parsing. The message appearing in the room
proves the reply direction and the other half of the secret. Corrupt one character of `SIG` and
it must come back 401; if a bad signature ever gets a 200, stop and investigate, because that is
the whole security model.

Use `--data-binary` rather than `-d`, or curl reshapes the body and the HMAC will not match.

And the alerting path:

```bash
curl -fsS -X POST https://sable.example.org/notify \
  -H "Authorization: Bearer $SABLE_NOTIFY_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"room": "alerts", "message": "deploy **v1.2.3** finished"}'
```

### Attaching a file to an alert

`/notify` takes a file in the same single call, in whichever shape suits the caller — multipart
for `curl`, base64 for a JSON client:

```bash
curl -fsS -X POST https://sable.example.org/notify -H "Authorization: Bearer $SABLE_NOTIFY_TOKEN" -F room=alerts -F message="nightly build" -F file=@report.pdf
```

```bash
curl -fsS -X POST https://sable.example.org/notify -H "Authorization: Bearer $SABLE_NOTIFY_TOKEN" -H 'Content-Type: application/json' -d "{\"room\":\"alerts\",\"message\":\"nightly build\",\"file\":{\"name\":\"report.pdf\",\"content\":\"$(base64 -w0 report.pdf)\"}}"
```

The `message` becomes the file's **caption**, so the file and its text arrive as one chat
message rather than two. Without a file, the endpoint behaves exactly as before.

This needs `SABLE_NEXTCLOUD_USER` and `SABLE_NEXTCLOUD_PASSWORD`, because the bot API cannot
attach files — see [configuration.md](configuration.md#file-attachments). Create a dedicated
Nextcloud user, generate an app password for it under **Settings → Security**, and add that user
to the conversations it should post files into. Without those set, a file gets a `503` and
naming the two variables; text-only calls keep working.

Responses: `201` with the stored filename, path and size; `413` over `SABLE_MAX_UPLOAD_BYTES`;
`422` for a bad base64 body or neither message nor file; `400` if Nextcloud rejected the share.
If the upload succeeds but the share fails, the uploaded file is deleted again rather than left
orphaned in the bot user's Files.

### Receiving alerts from other services

Komodo, Alertmanager, Grafana and most other tools will not send sable's `/notify` shape, and
often cannot set an `Authorization` header. Give each one a hook instead:

```ini
SABLE_HOOKS=komodo=a1b2c3d4
SABLE_HOOK_TOKEN_KOMODO=<a token just for this hook>
```

`a1b2c3d4` is the conversation's token, the lowercase string at the end of its URL, not the
name shown in the sidebar. An alias from `SABLE_NOTIFY_ROOMS` works here too. Either way it is
checked when sable starts, so a name put here fails at boot rather than when an alert fires.

In Komodo, create an Alerter with a Custom endpoint pointing at:

```
https://sable.example.org/hook/komodo?token=<the same token>
```

Komodo cannot set headers, so the token goes in the URL. Anything that can set one is better
off sending `Authorization: Bearer <token>`, which both are accepted.

That is the whole setup — the payload is rendered without further configuration. To control the
wording, add a format string:

```ini
SABLE_HOOK_TEMPLATE_KOMODO=**{level}** {data.type}: {data.data.name} on {data.data.server_name} went {data.data.from} to {data.data.to}
```

See [webhooks from other services](configuration.md#webhooks-from-other-services) for how the
rendering works and what the paths are.

## Operations

### What the log tells you

At `INFO`, sable logs its own lifecycle, its configuration, and every use — and nothing else, so
the interesting lines are not buried:

```
sable 0.6 starting
  listening on:   http://0.0.0.0:8080
  webhook URL:    POST /webhook  (give this to occ talk:bot:install)
  nextcloud:      https://cloud.example.org
  bot name:       'sable'   command prefix: '!'
  model:          gpt-4o-mini at https://api.openai.com/v1
  ask reaction:   ⁉️
  admin commands: reset - only for maser
  ai rooms:       (mentions only)
  alerting:       enabled, aliases: alerts
  attachments:    as sable-files into /sable, up to 100 MB
  hooks:          /hook/komodo -> abcd1234
  ignoring:       noisy-integration
  backend pin:    on, replies only to https://cloud.example.org
  proxy trust:    127.0.0.1, ::1
  api docs:       disabled (SABLE_API_DOCS=true to serve them)
  health check:   GET /healthz (open)
  log level:      INFO
connected to Nextcloud 31.0.4 at https://cloud.example.org
sable 0.6 ready
added to conversation abcd1234 ('Team chat') - now receiving its messages
Alice (users/alice) ran !ping in abcd1234
Alice (users/alice) asked the model in abcd1234 (22 chars)
gpt-4o-mini answered in 1.8s (243 chars)
Alice (users/alice) asked the model about message 12 in abcd1234, written by Bob
relayed an alert to abcd1234 (alias alerts) as message 4242
removed from conversation abcd1234 ('Team chat') - no further messages from it
sable 0.6 stopping
sable 0.6 stopped
```

The startup probe, the line reading `connected to Nextcloud`, calls `status.php`, which needs no
credentials. It proves DNS, TLS and that the thing on the other end really is a Nextcloud, so a
wrong URL or an untrusted certificate shows up at boot rather than on the first reply somebody
is waiting for. It is never fatal, since Nextcloud may simply not be up yet, and
`SABLE_STARTUP_CHECK=false` skips it.

Reachability is logged as transitions rather than per attempt, so an outage is two lines rather
than one per retry:

```
ERROR  sable.state: lost connection to Nextcloud: ConnectError: All connection attempts failed
INFO   sable.state: Nextcloud is reachable again
```

The same applies to the model backend. A transport failure counts as unreachable; an HTTP error
response does not, because the service answered, and that is logged with its status code and the
first 200 characters of the body.

Message text stays out of INFO. A use is logged as who, what and where, with sizes rather than
content, as in `asked the model in abcd1234 (22 chars)`. `SABLE_LOG_LEVEL=DEBUG` adds the
prompt, command arguments and the message a reaction referred to, plus a line for every event
sable decided not to act on and one per outbound HTTP call. Treat a DEBUG log as containing chat
content.

`GET /healthz` returns the version, the bot name, the configured model and whether alerting is
on. It does not call Nextcloud or the model, which keeps it honest as a liveness probe. It is open
unless `SABLE_HEALTH_TOKEN` is set, in which case the same answer needs that value in an
`X-Health-Token` header — see
[configuration.md](configuration.md#guarding-the-health-probe). FastAPI's schema and its `/docs`
and `/redoc` pages are **not** served unless `SABLE_API_DOCS=true`.

Upgrading is a restart. There is no state and no migration.

```bash
git pull && docker compose up -d --build     # or: uv sync --locked --no-dev && systemctl restart sable
```

In-flight replies get up to 30 seconds to finish during shutdown, so a rolling restart does not
lose an answer somebody is waiting for.

Rotating the bot secret means reinstalling, since Nextcloud rejects a duplicate URL:

```bash
occ talk:bot:uninstall --id <id>
occ talk:bot:install "sable" "<new secret>" "https://sable.example.org/webhook" "A helpful bot" --feature webhook --feature response --feature reaction
```

Then update `SABLE_BOT_SECRET` and restart. Expect a brief window where webhooks are rejected; a
bot is not a good place to need zero downtime.

Run one process. Conversation history, the message cache and the redelivery cache all live in
memory, so two workers would split them and replies would forget context depending on which
worker answered. One process handles chat traffic without breaking a sweat, at a few tens of
megabytes resident with all its I/O async. Scale by running separate bots rather than workers.

There is nothing to back up, because nothing is persisted. Keep `.env` in a secret store, since
it is the only thing that is hard to recreate.

## Hardening

The operational steps are below. [security.md](security.md) has the whole posture — what
protects each trust boundary, how secrets are handled, and the
[accepted risks](security.md#accepted-risks) worth reading before this is reachable from
anywhere you do not control.

Do not publish the port. Bind to localhost or a private network and let the proxy be the only
client, since Nextcloud is the only legitimate caller of `/webhook`. Leave `SABLE_PIN_BACKEND`
on, which stops a replayed webhook redirecting the bot's replies at another server, and restrict
egress to Nextcloud and the model backend if the model runs elsewhere.

Keep the credentials separate and proportionate. `SABLE_BOT_SECRET` is Nextcloud's;
`SABLE_NOTIFY_TOKEN` belongs to your alerting callers, so a leak there costs you noise rather
than the bot; and the upload account, if you use one, should own nothing else and appear in
`SABLE_IGNORE_USERS`. Aliases in `SABLE_NOTIFY_ROOMS` mean callers never learn conversation
tokens.

Two things are easy to forget. Every message the assistant answers is sent to your model
backend, which for a hosted provider means chat content leaving your infrastructure. And anyone
in a conversation can trigger any command not named in `SABLE_ADMIN_COMMANDS`, so one that shells
out or touches production belongs in that list, with the people allowed to run it in
`SABLE_ADMIN_USERS`.

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
| `/hook/<name>` returns 404 | No hook by that name, or `SABLE_HOOKS` is unset. A configured hook with a bad token answers 401 instead, so 404 means the name. |
| `!reset is for administrators only` | The sender's Nextcloud user id is not in `SABLE_ADMIN_USERS`. The log line names who was refused. Display names are never matched, only user ids. |
| `/docs` or `/openapi.json` returns 404 | Expected: set `SABLE_API_DOCS=true` to serve them. |
| `/healthz` returns 401 | `SABLE_HEALTH_TOKEN` is set, so the probe needs an `X-Health-Token` header. The image's own healthcheck sends it; anything else calling `/healthz` has to as well. |
| Access log shows the proxy's IP, not the client's | The proxy's address is not in `SABLE_TRUSTED_PROXIES`, so its `X-Forwarded-For` is ignored. In Docker that is the usual case: name the network's subnet. |
| `/notify` returns 400 | The `room` is neither a known alias nor a plausible conversation token, or Talk rejected it. |
| Config error on startup | See [the table in configuration.md](configuration.md#startup-errors-and-what-they-mean). |
