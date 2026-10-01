# Deployment

Getting sable running against a real Nextcloud, and keeping it running.

## Prerequisites

You need Nextcloud with Talk installed, permission to create a user on it, and either Docker or Python 3.11+ on whatever host runs sable.

sable is a Nextcloud user, so the connection goes one way: sable reaches out to Nextcloud, and
Nextcloud never calls sable. It needs outbound access to your Nextcloud and to the model
backend, and nothing inbound at all unless something calls `/notify` or `/hook/{name}`, or you
probe `/healthz` from another host. There is no HTTPS endpoint to expose to Nextcloud and nothing
to install in it.

### Give Nextcloud enough PHP workers

Each conversation sable follows holds one PHP-FPM worker for up to `SABLE_POLL_TIMEOUT` seconds.
The stock pool in the official Nextcloud image is `pm.max_children = 5`, so an account in seven
conversations queues two of its polls behind the others: in a test with seven simultaneous polls
they took 41 to 90 seconds each instead of 30, and a queued request can outlast sable's own
timeout, so its posts can fail too. The queue is shared with everything else that uses Nextcloud, so it also slows
your browser. Raise the pool before pointing sable at it, to the number of conversations the
account is in plus headroom for everyone else, and check that the RAM covers it (a worker can
take around 100 MB).

Check the current value:

```bash
docker exec <nextcloud-container> php-fpm -tt 2>&1 | grep pm.max_children
```

and override it with a file mounted into the container, which survives the container being
recreated (the `zz-` prefix makes it load after the image's `www.conf`):

```ini
[www]
pm = dynamic
pm.max_children = 32
pm.start_servers = 6
pm.min_spare_servers = 4
pm.max_spare_servers = 8
```

```yaml
    volumes:
      - ./php-fpm-pool.conf:/usr/local/etc/php-fpm.d/zz-pool.conf:ro
```

## 1. Create the account

sable signs in as an ordinary Nextcloud user, so make one for it. Through the admin UI, or:

```bash
sudo -u www-data php occ user:add --display-name="Sable" sable
```

The user id is what people `@`-mention, so pick one you would like to type. Log in as that user
once to check it works, then create an **app password** under **Settings → Security → Devices &
sessions**. That is the credential sable uses; the login password never leaves your head.

Keep the account to itself. An app password cannot be scoped: it reaches everything that user
can, across Files, Contacts and Calendar, so the account should own nothing else, and sable
should be in only the conversations it is meant to answer in. See [security.md](security.md).

## 2. Configure

```bash
cp .env.example .env
$EDITOR .env
```

At minimum set `SABLE_NEXTCLOUD_URL`, `SABLE_NEXTCLOUD_USER` and `SABLE_NEXTCLOUD_PASSWORD`;
everything else is in [configuration.md](configuration.md). Set `SABLE_ALLOWED_ROOMS` too, to the
tokens of the conversations it should serve: left empty it follows every room it is invited to,
and says so in a warning. Then check what it resolved to before going further:

```bash
sable --check
```

## 3. Run it

### Docker Compose (recommended)

[`compose.yaml`](../compose.yaml) lists every setting in its `environment:` block, with only the
three required ones active and the rest commented out beside their defaults, so sable can be
configured entirely in that one file. It also loads `.env` if one exists, and values in the
`environment:` block override it. The required settings are wired as `${VAR}` lookups so they stay
in `.env` rather than in a file you commit, and a missing one fails immediately:

```
error while interpolating services.sable.environment.SABLE_NEXTCLOUD_PASSWORD:
required variable SABLE_NEXTCLOUD_PASSWORD is missing a value: required — put it in .env
```

The service publishes to `127.0.0.1:8080` only, for whatever calls `/notify` and `/hook/{name}`:

```bash
docker compose up -d --build
docker compose logs -f sable
```

The image runs as a non-root user, holds no state, and has a healthcheck on `/healthz`, so
`docker ps` shows it healthy once it is up. If your proxy runs in Docker too, drop the `ports:`
block, put both services on one network and let the proxy reach `sable:8080` directly. If nothing
calls sable from outside the host you can drop it regardless, since reading and replying to chat
does not use the port.

#### Container hardening

`compose.yaml` also locks the container down, since sable writes nothing and needs no privileges.
Keep all of it:

| Setting | Effect |
| --- | --- |
| `read_only: true` | The root filesystem is read-only, so nothing can be planted in the image |
| `tmpfs: /tmp:size=256m` | The only writable path, held in RAM and gone on restart. A multipart upload to `/notify` spools here, so it must be larger than `SABLE_MAX_UPLOAD_BYTES` (25 MB by default; 256m leaves room for several at once) |
| `cap_drop: [ALL]` | No Linux capabilities: it listens on 8080 and makes outbound requests |
| `security_opt: no-new-privileges:true` | A setuid binary cannot gain privileges |
| `pids_limit: 256` | A runaway or a fork bomb stops well short of the host's limits |
| `mem_limit: 768m` | A memory cap, which has to cover the `/tmp` tmpfs when it is full |

If you raise `SABLE_MAX_UPLOAD_BYTES` above about 200 MB, grow the tmpfs `size=` with it and raise
`mem_limit` to match, or a large upload can fill the tmpfs before it is finished. A
base64 upload (the JSON shape) is held in memory rather than spooled, so it counts against
`mem_limit` directly. The app caps request bodies itself, before the proxy matters: see
[request size caps](configuration.md#request-size-caps).

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
Description=sable (Nextcloud Talk assistant)
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

# sable writes nothing and needs no privileges. This is the same lockdown
# compose.yaml applies to the container.
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadOnlyPaths=/opt/sable
CapabilityBoundingSet=
MemoryMax=512M
TasksMax=256

[Install]
WantedBy=multi-user.target
```

`PrivateTmp=true` gives the service a `/tmp` of its own, which is where a multipart upload to
`/notify` spools. Unlike the container's tmpfs it is on disk and not size-capped, so
`SABLE_MAX_UPLOAD_BYTES` is the only limit; make sure the filesystem behind it has the room.
`TasksMax` and `MemoryMax` are the equivalents of `pids_limit` and `mem_limit`; raise `MemoryMax`
if you raise `SABLE_MAX_UPLOAD_BYTES` a long way, since a base64 upload is held in memory.

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now sable
journalctl -u sable -f
```

## 4. Put TLS in front of it, if anything calls it

Reading chat and replying are outbound, so a sable that only answers people needs no proxy and
no public address. You need one when something calls `/notify`, `/hook/{name}` or `/healthz`
from another host. sable speaks plain HTTP and does not terminate TLS, so put any reverse proxy
in front of it.

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

        # A little over what /notify accepts: SABLE_MAX_UPLOAD_BYTES x 4/3 plus 64 KiB,
        # about 33.4 MiB by default, since a base64 file is a third bigger than the
        # file. nginx's own default of 1m would refuse an attachment to /notify. sable
        # enforces its own caps whatever this says; this is defence in depth.
        client_max_body_size 36m;
    }
}
```

`X-Forwarded-For` and `X-Forwarded-Proto` are believed only from the addresses in
`SABLE_TRUSTED_PROXIES`, which defaults to loopback and so covers both examples above as written.
A proxy in another container is not loopback and has to be named; a proxy that is never named just
means the access log shows the proxy rather than the client. nginx's
`$proxy_add_x_forwarded_for` appends to whatever the client sent, which is safe here — the
forwarded list is read from the right, so the first address that is not a trusted proxy wins, and
anything a client made up sits to the left of its real address. That only holds while the trusted
list is not `*`. Details in
[configuration.md](configuration.md#which-proxies-are-believed).

Confirm the path works:

```bash
curl -fsS https://sable.example.org/healthz
# {"status":"ok","version":"0.9","user":"sable","llm":"gpt-4o-mini","notify":true,"nextcloud":true}

# ...or, with SABLE_HEALTH_TOKEN set:
curl -fsS -H "X-Health-Token: $SABLE_HEALTH_TOKEN" https://sable.example.org/healthz
```

### If your Nextcloud uses an internal or self-signed certificate

That is the outbound direction, and for sable it is the only direction that matters: sable
verifying Nextcloud's certificate on every poll and every reply. It fails by default with
`CERTIFICATE_VERIFY_FAILED`, because httpx verifies against its own bundled certifi store rather
than the system one, so installing your CA in the container's trust store achieves nothing on its
own.

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

To confirm it worked, look for the `signed in to` line at startup. A failure logs
`could not reach Nextcloud at …` with `CERTIFICATE_VERIFY_FAILED` in it.

## 5. Invite it to a conversation

Add the account to a conversation the way you would add any person: Conversation settings,
Participants, Add participants. Nothing is installed in Nextcloud and there is nothing to enable;
a moderator can remove it again the same way.

sable looks at which conversations it is in every `SABLE_ROOM_REFRESH` seconds, 60 by default,
and starts reading a new one from its newest message on, so anything said before that moment is
not seen. It logs when that happens, which doubles as proof that the whole receiving path works:

```
following conversation abcd1234 ('Team chat')
```

With `SABLE_ALLOWED_ROOMS` set, only the listed conversations are followed, so **add the new
room's token there too** (the last part of its URL) or sable will never follow it. It says why at
DEBUG: `not following abcd1234: not in SABLE_ALLOWED_ROOMS`. An unlisted room costs no held
request, and with `SABLE_LEAVE_UNLISTED_ROOMS=true` the account leaves it altogether, so an
invitation to a room you have not listed is undone within a scan or two.

It follows at most 50 conversations, the most recently active ones, and skips the Talk updates
room, a former one-to-one, its own note to self and the "Let's get started!" sample. Each one
it does follow is a request held open on your Nextcloud server, so keep the account out of
rooms it has no business in, and make sure the PHP pool
[has room for them](#give-nextcloud-enough-php-workers).

## 6. Verify end to end

In the conversation, within a minute of the invitation:

```
!ping                 →  pong 🏓
!version              →  sable 0.9 · model gpt-4o-mini
@sable are you there  →  the model's answer, with @sable picked from Talk's mention list
```

`!ping` proves signing in, reading and replying. The mention proves the account's user id is
recognised as yours and that the model backend answers.

And the alerting path, from wherever your callers live:

```bash
curl -fsS -X POST https://sable.example.org/notify \
  -H "Authorization: Bearer $SABLE_NOTIFY_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"room": "alerts", "message": "deploy **v1.2.3** finished"}'
```

The account has to be in the conversation it posts to.

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

The file is uploaded into the account's own Files, under `SABLE_UPLOAD_PATH`, and shared into the
conversation — see [configuration.md](configuration.md#file-attachments). There is nothing to
enable: it is the same account that posts the text.

Responses: `201` with the stored filename, path and size; `401` for a bad token; `413` over
`SABLE_MAX_UPLOAD_BYTES`, or for a request body over the cap sable puts on `/notify` (that one
before the token is even checked; see [request size caps](configuration.md#request-size-caps));
`400` for JSON or UTF-8 that does not parse, nesting too deep, or if Nextcloud rejected the share;
`422` for a body that is not a JSON object, a bad base64 file, or neither message nor file; `502` if
Nextcloud could not be reached or failed in some other way. If the
upload succeeds but the share fails, the uploaded file is deleted again rather than left orphaned
in the account's Files.

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

### Giving the assistant tools, through Open WebUI

Ask a model with tools what gold costs and it replies asking for a search tool to be called.
Something has to run it. sable does not — it makes one request and posts the reply — so the
answer you get is an error naming the tool nobody ran. Open WebUI runs the loop itself:

```ini
SABLE_LLM_BACKEND=openwebui
SABLE_LLM_BASE_URL=https://ai.example.org/api
SABLE_LLM_API_KEY=<a key belonging to an account made for sable>
SABLE_LLM_MODEL=<the workspace model, not the underlying one>
SABLE_LLM_TOOL_IDS=server:mcp:1,server:mcp:2
SABLE_LLM_FEATURES=web_search
SABLE_LLM_TOOL_ROOMS=e5f6g7h8
SABLE_LLM_USERS=alice,bob
SABLE_LLM_TIMEOUT=300
SABLE_THINKING_REACTION=⏳
```

`SABLE_LLM_TOOL_ROOMS` is not optional: empty, tools are off everywhere, whatever the two
settings above it say, and sable warns that they are configured and unusable. Name a room whose
membership you control, and `SABLE_LLM_USERS` so that only people you trust can ask in it. In every
other room the model is called without tools. `tool_ids` and `features` may not be smuggled in
through `SABLE_LLM_EXTRA_BODY`; that is a startup error.

The tool ids are per-account, and MCP servers are addressed as `server:mcp:<id>` rather than
appearing in this list:

```bash
curl -s -H "Authorization: Bearer $KEY" https://ai.example.org/api/v1/tools/ | jq '.[] | {id, name}'
```

Two settings in Open WebUI decide whether anything runs: the model needs **Native** function
calling, and its *Stream Chat Response* parameter must not be off, since it overrides the
request. sable reports the second as a loop that finished without writing an answer.

The startup block prints what the model can reach:

```
tools:          server-side loop via Open WebUI · tools: server:mcp:1, server:mcp:2 · in rooms: e5f6g7h8 · built-ins: web_search
```

Read that as a list of what a stranger in those rooms can set off, because it is one. The tools
run as the account behind the API key and the model picks which to call, so give it an account
of its own. Answers take tens of seconds, which is why the timeout is raised and the thinking
reaction earns its keep.

## Operations

### What the log tells you

At `INFO`, sable logs its own lifecycle, its configuration, and every use — and nothing else, so
the interesting lines are not buried:

```
sable 0.9 starting
  listening on:   http://0.0.0.0:8080
  nextcloud:      https://cloud.example.org as sable
  receiving:      long polls of up to 30s, conversations rescanned every 60s
  command prefix: '!'
  model:          gpt-4o-mini at https://api.openai.com/v1
  concurrency:    up to 8 replies at once, the rest queued (at most 20 waiting, then dropped: SABLE_MAX_QUEUED_REPLIES)
  ask reaction:   ⁉️
  rooms:          abcd1234, efgh5678 (leaves the others, except /notify and /hook destinations)
  model users:    alice, bob and the administrators
  rate limit:     20 triggers a minute per person
  admin commands: reset - only for maser
  ai rooms:       (mentions only)
  alerting:       enabled, aliases: alerts
  attachments:    into /sable, up to 25 MB
  hooks:          /hook/komodo -> abcd1234
  ignoring:       noisy-integration
  proxy trust:    127.0.0.1, ::1 - believed by sable's own uvicorn, and read by nothing else
  api docs:       disabled (SABLE_API_DOCS=true to serve them)
  health check:   GET /healthz (open)
  log level:      INFO
signed in to https://cloud.example.org as sable (Sable)
sable 0.9 ready
following conversation abcd1234 ('Team chat')
Alice (users/alice) ran !ping in abcd1234
Alice (users/alice) asked the model in abcd1234 (22 chars)
gpt-4o-mini answered in 1.8s (243 chars)
Alice (users/alice) asked the model about message 12 in abcd1234, written by Bob
relayed an alert to abcd1234 (alias alerts) as message 4242
left conversation zzzz9999 ('Lunch'): not in SABLE_ALLOWED_ROOMS and not a /notify or /hook destination
no longer in conversation abcd1234 ('Team chat') - no further messages from it
sable 0.9 stopping
sable 0.9 stopped
```

Settings that are probably a mistake are logged as warnings straight after the block (an empty
`SABLE_ALLOWED_ROOMS`, tools configured with no room to use them; the full list is in
[configuration.md](configuration.md#startup-warnings)). When the model backend is Open WebUI the
block gains a `tools:` line between `model:` and `concurrency:`, shown
[below](#giving-the-assistant-tools-through-open-webui). Refusals show up as warnings you can
grep for: `rate limit: ignoring …` once per person per minute,
`dropping replies: …` at most every 30 seconds when the queue is full, `refused !reset for …` for
an admin-only command, and `cannot leave conversation …` for a room Talk would not let the
account leave.

The startup probe, the line reading `signed in to`, asks Nextcloud who the credentials belong to.
It proves DNS, TLS and the app password in one request, and tells you the display name Nextcloud
has for the account, so a wrong URL, an untrusted certificate or a revoked password shows up at
boot rather than on the first reply somebody is waiting for. A rejected password logs an error
naming `SABLE_NEXTCLOUD_PASSWORD`. It is never fatal, since Nextcloud may simply not be up yet —
sable keeps retrying with backoff either way — and `SABLE_STARTUP_CHECK=false` skips it.

Reachability is logged as transitions rather than per attempt, so an outage is two lines rather
than one per retry:

```
ERROR  sable.state: lost connection to Nextcloud: ConnectError: All connection attempts failed
INFO   sable.state: Nextcloud is reachable again
```

A long poll that Nextcloud accepts and then holds past its timeout is not an outage, and is not
reported as one. It is a single warning per streak, naming the conversation:

```
WARNING  sable.poller: Nextcloud held the poll of conversation abcd1234 ('Team chat') past 45s without answering; asking again. If this repeats, its PHP-FPM pool is probably too small for this many conversations (see docs/deployment.md).
```

It means every PHP worker was busy and the poll queued, which is what a pool of five does to an
account in seven conversations.

The same applies to the model backend. A transport failure counts as unreachable; an HTTP error
response does not, because the service answered, and that is logged with its status code and the
first 200 characters of the body.

Message text stays out of INFO. A use is logged as who, what and where, with sizes rather than
content, as in `asked the model in abcd1234 (22 chars)`. `SABLE_LOG_LEVEL=DEBUG` adds the
prompt, command arguments and the message a reaction referred to, plus a line for every event
sable decided not to act on and one per outbound HTTP call. Treat a DEBUG log as containing chat
content.

`GET /healthz` returns the version, the account's user id (as `user`), the configured model,
whether alerting is on, and a `nextcloud` field saying whether the last call to Nextcloud
succeeded (`null` until one has been made). It does not call Nextcloud or the model *to answer
the probe*, which keeps it honest as a liveness probe — the field reports what the long polls and
ordinary traffic already discovered, and the status stays `ok` either way. It is open
unless `SABLE_HEALTH_TOKEN` is set, in which case the same answer needs that value in an
`X-Health-Token` header — see
[configuration.md](configuration.md#guarding-the-health-probe). FastAPI's schema and its `/docs`
and `/redoc` pages are **not** served unless `SABLE_API_DOCS=true`.

A successful probe is not logged. The container asks every thirty seconds, which would be some
2,900 identical access lines a day; one that *fails* still appears, and
`SABLE_LOG_HEALTH_CHECKS=true` brings the rest back.

Upgrading is a restart. There is no state and no migration.

```bash
git pull && docker compose up -d --build     # or: uv sync --locked --no-dev && systemctl restart sable
```

In-flight replies get up to 30 seconds to finish during shutdown, so a rolling restart does not
lose an answer somebody is waiting for. What a restart does lose is the gap: a conversation is
followed again from its newest message, so anything said while sable was down is not answered.

Rotating the app password needs no reinstalling. Create a new one under Settings → Security,
put it in `SABLE_NEXTCLOUD_PASSWORD`, restart, then revoke the old one. Until the restart, a
revoked password means every call to Nextcloud is refused with a 401.

Run one process, and one per account. Conversation history, the rate
limiter and the position in each conversation all live in memory, so two workers would split them
and replies would forget context depending on which worker answered. Worse, two processes signed
in as the same account would each read every message and each answer it. One process handles chat
traffic without breaking a sweat, at a few tens of megabytes resident with all its I/O async.
Scale by running separate accounts rather than workers.

There is nothing to back up, because nothing is persisted. Keep `.env` in a secret store, since
it is the only thing that is hard to recreate.

## Hardening

The operational steps are below. [security.md](security.md) has the whole posture — what
protects each trust boundary, how secrets are handled, and the
[accepted risks](security.md#accepted-risks) worth reading before sable is in rooms you do not
control.

Do not publish the port. Bind to localhost or a private network and let the proxy be the only
client, since the only things that call sable are your own alerting systems. If none do, nothing
needs to reach it from outside the host at all. Restrict egress to Nextcloud and the model
backend if the model runs elsewhere.

Keep the credentials separate and proportionate. The app password is the one that matters: it
belongs to a user account and cannot be scoped, so the account should own nothing else and sable
should be in only the conversations it needs. `SABLE_NOTIFY_TOKEN` belongs to your alerting
callers, so a leak there costs you noise rather than the account. Aliases in `SABLE_NOTIFY_ROOMS`
mean callers never learn conversation tokens.

Two things are easy to forget. Every message the assistant answers is sent to your model
backend, which for a hosted provider means chat content leaving your infrastructure. And anyone
in a conversation can trigger any command not named in `SABLE_ADMIN_COMMANDS`, so one that shells
out or touches production belongs in that list, with the people allowed to run it in
`SABLE_ADMIN_USERS`.

Narrow who can reach it at all, because every default is open. Set `SABLE_ALLOWED_ROOMS` to the
rooms it serves (and `SABLE_LEAVE_UNLISTED_ROOMS` if it should not sit in the others), set
`SABLE_LLM_USERS` to the people who may use the model, and keep tools for a dedicated room named
in `SABLE_LLM_TOOL_ROOMS`. Leave the container hardening in `compose.yaml` as it is
([above](#container-hardening)), and the rate limit and queue caps at their defaults unless you
have measured a reason. [How the layers combine](configuration.md#how-the-access-layers-combine)
says what each one does and does not cover. The full checklist is in
[security.md](security.md#hardening-checklist).

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| Nothing happens at all | The account is not in that conversation, or sable cannot sign in. Look for `signed in to` at startup, and for `following conversation <token>` once the account has been invited. A new invitation takes up to `SABLE_ROOM_REFRESH` seconds to be noticed. |
| `refused the credentials for '…' (HTTP 401)` in the log | `SABLE_NEXTCLOUD_USER` or `SABLE_NEXTCLOUD_PASSWORD` is wrong, the password is not an app password, it was revoked, or the user is disabled. Generate a new app password under Settings → Security. |
| `could not reach Nextcloud at …` | The URL, DNS, or the certificate. `CERTIFICATE_VERIFY_FAILED` means an internal CA is not trusted — see [the self-signed section](#if-your-nextcloud-uses-an-internal-or-self-signed-certificate). |
| Invited to a room, still silent after a minute | The room is not in `SABLE_ALLOWED_ROOMS`, which is the commonest cause once that is set: at DEBUG the log says `not following <token>: not in SABLE_ALLOWED_ROOMS`. Add its token (and with `SABLE_LEAVE_UNLISTED_ROOMS` the account may already have left it). Otherwise it is one sable does not follow — Talk updates, a former one-to-one, the account's note to self or the "Let's get started!" sample — or the account is already in more than 50 conversations and this one is not among the most recently active — the log warns once. Leave the ones it does not need. Otherwise check `SABLE_ROOM_REFRESH`. |
| Answers in a room only without the tools | The room is not in `SABLE_LLM_TOOL_ROOMS`, which is empty by default: tools are off everywhere until a room is named. Add the token, and check the `tools:` line of the startup block for `in rooms:`. |
| `You are not allowed to use the assistant.` | `SABLE_LLM_USERS` is set and the sender's Nextcloud user id is not in it (administrators always are). Add the id; display names never match. Plain messages in an AI room and ⁉️ reactions from the same person get no reply at all, only an INFO line `not in SABLE_LLM_USERS`. |
| `rate limit: ignoring …` in the log, and the bot stops answering one person | They set off more than `SABLE_RATE_LIMIT` triggers (20 by default) within a minute. It is silent in the room and lifts a minute after their last accepted trigger. Raise it, or `0` turns it off. |
| `dropping replies: …` in the log, and some questions get no answer | More replies were running or waiting than `SABLE_MAX_CONCURRENT_REPLIES` plus `SABLE_MAX_QUEUED_REPLIES` allow, so new work is dropped, silently to the user. A slow model backend and a long `SABLE_LLM_TIMEOUT` are the usual cause; raise the ceiling if the backend can take it. |
| Silent in a room from before sable started | Expected: a conversation is followed from its newest message, so nothing earlier is replayed. |
| `Nextcloud held the poll of conversation … past …s without answering` | Nextcloud accepted the request and did not answer in time, almost always because its PHP-FPM pool is full and the poll is queued. sable keeps asking and does not treat it as an outage. Raise `pm.max_children` — see [give Nextcloud enough PHP workers](#give-nextcloud-enough-php-workers). You can confirm it by running seven `curl` polls at once: with a big enough pool they all return in about 30 seconds. |
| Nextcloud shows many long-running requests from one user | That is the long polling: one per conversation, each up to `SABLE_POLL_TIMEOUT` seconds. See [how chat is received](configuration.md#how-chat-is-received). |
| Replies never appear, `could not post to <token>` in logs | The account cannot post there (a read-only conversation, or it was removed), or `SABLE_NEXTCLOUD_URL` is unreachable. |
| Answers are slow or absent, `completion failed` in logs | Model timeout. Raise `SABLE_LLM_TIMEOUT`, lower `SABLE_LLM_MAX_TOKENS`, or pick a faster model. |
| `the model returned an empty message` | A reasoning model spent its whole budget thinking. Raise `SABLE_LLM_MAX_TOKENS` or lower reasoning effort via `SABLE_LLM_EXTRA_BODY`. |
| `called <tool> and nothing executed it` | The backend offered the model tools but does not run them, so there is no answer in the reply. Either stop offering them, or set `SABLE_LLM_BACKEND=openwebui` so Open WebUI runs the loop. |
| `the loop finished without writing an answer` | Open WebUI accepted the work and wrote nothing. Almost always the model's own *Stream Chat Response* parameter, which overrides `stream: true` and stops the tool loop running. |
| Tool answers are stale or invented | The model has no clock unless you give it one. Check the date line in the system prompt, and set `SABLE_TIMEZONE`. |
| Replies are cut short with `_[truncated]_` | The answer exceeded `SABLE_MAX_MESSAGE_CHARS`; Talk's own ceiling is 32000 characters. |
| `HTTP 429` from Talk | Nextcloud is throttling the account, most likely for posting too fast. Batch or slow down whatever is calling `/notify`. |
| Mentions ignored | Pick the account from Talk's mention list, or start the message with its user id. `SABLE_NEXTCLOUD_USER` has to be the id people mention. Set `SABLE_LOG_LEVEL=DEBUG` and watch for `message in <token> was not for me`. |
| The ⁉️ reaction does nothing | Set `SABLE_LOG_LEVEL=DEBUG` and react again. Silence is by design when the reactor is outside `SABLE_ALLOWED_ROOMS`, `SABLE_LLM_USERS` or `SABLE_ADMIN_USERS` (with `SABLE_ASK_ADMINS_ONLY`), when they are over the rate limit, when the message's author is in `SABLE_IGNORE_USERS`, or when it is a system message; each leaves a log line. If no `received reaction` line appears at all, the reaction never arrived through the chat poll — see [future.md](future.md#talk-features-not-yet-used). |
| The ⁉️ reaction says `I cannot find that message` | Talk answered 404 to the read-back: the message was deleted, or the account cannot see it. The call is `GET /chat/{token}/{messageId}/context`, which needs the `chat-get-context` capability. `That message has been deleted.` and `…has no text for me to read` are the other two replies. Any other failure to read it (Nextcloud unreachable, or an HTTP error other than 404) instead says `⚠️ Sorry - I could not read the message you reacted to (…)`, is logged, and is not posted at all with `SABLE_REPORT_ERRORS=false`. |
| `/notify` returns 404 | `SABLE_NOTIFY_TOKEN` is unset, so the route is disabled. |
| `/hook/<name>` returns 404 | No hook by that name, or `SABLE_HOOKS` is unset. A configured hook with a bad token answers 401 instead, so 404 means the name. |
| `!reset is for administrators only` | The sender's Nextcloud user id is not in `SABLE_ADMIN_USERS`. The log line names who was refused. Display names are never matched, only user ids. |
| `/docs` or `/openapi.json` returns 404 | Expected: set `SABLE_API_DOCS=true` to serve them. |
| `/healthz` returns 401 | `SABLE_HEALTH_TOKEN` is set, so the probe needs an `X-Health-Token` header. The image's own healthcheck sends it; anything else calling `/healthz` has to as well. |
| Access log shows the proxy's IP, not the client's | The proxy's address is not in `SABLE_TRUSTED_PROXIES`, so its `X-Forwarded-For` is ignored. In Docker that is the usual case: name the network's subnet. |
| `/notify` returns 400 | The `room` is neither a known alias nor a plausible conversation token, or Talk rejected it — including because the account is not in that conversation. Invalid JSON or UTF-8 and nesting too deep are 400 as well. |
| `/notify` or `/hook` returns 413 | The body is over the cap sable enforces itself: `SABLE_MAX_UPLOAD_BYTES` × 4/3 + 64 KiB for `/notify`, `SABLE_MAX_HOOK_BYTES` + 1 KiB for `/hook`, 64 KiB elsewhere. It is refused before the token is checked. See [request size caps](configuration.md#request-size-caps). |
| `/notify` returns 422 | The body is not a JSON object, a field is missing or invalid, the base64 file is bad, or there is neither a message nor a file. |
| Config error on startup | See [the table in configuration.md](configuration.md#startup-errors-and-what-they-mean). |
