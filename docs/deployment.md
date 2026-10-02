# Deployment

Getting sable running against a real Nextcloud, and keeping it running.

## Prerequisites

You need Nextcloud with Talk installed, permission to create a user on it, and either Docker or
Python 3.11+ on whatever host runs sable. sable only reaches out, to your Nextcloud and to the
model backend: nothing inbound is needed unless something calls `/notify` or `/hook/{name}`, or
you probe `/healthz` from another host, and nothing is installed in Nextcloud.

### Give Nextcloud enough PHP workers

Each conversation sable follows holds one PHP-FPM worker for up to `SABLE_POLL_TIMEOUT` seconds.
The stock pool in the official Nextcloud image is `pm.max_children = 5`, so an account in seven
conversations queues two of its polls behind the others: in a test with seven simultaneous polls
they took 41 to 90 seconds each instead of 30, and a queued request can outlast sable's own
timeout, so its posts can fail too. The queue is shared with everything else that uses Nextcloud,
so it also slows your browser. Raise the pool before pointing sable at it, to the number of
conversations the account is in plus headroom for everyone else, and check that the RAM covers it
(a worker can take around 100 MB).

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

Keep the account to itself: it owns nothing else, and sable is in only the conversations it is
meant to answer in ([why](security.md#accepted-risks)).

## 2. Configure

```bash
cp .env.example .env
$EDITOR .env
```

Set the three required Nextcloud settings and `SABLE_ALLOWED_ROOMS` (the tokens of the
conversations it should serve; left empty it follows every room it is invited to). Everything
else is in [configuration.md](configuration.md). Then check what it resolved to:

```bash
sable --check
```

## 3. Run it

### Docker Compose (recommended)

[`compose.yaml`](../compose.yaml) lists every setting and loads `.env` if there is one
([how the two stack](configuration.md#how-settings-are-loaded)). The required settings are wired
as `${VAR}` lookups so they stay in `.env` rather than in a file you commit, and a missing one
fails immediately:

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
block, put both services on one network and let the proxy reach `sable:8080` directly; if nothing
calls sable from outside the host you can drop it regardless.

To run a published release instead of building, replace the `build:` and `image:` lines with
`image: forgejo.subversive.link/subversive/sable:0.9`. Pin the version rather than `latest`, so a
`docker compose pull` cannot move you ([what is published](releasing.md#what-gets-published)).

#### Container hardening

`compose.yaml` also locks the container down, since sable writes nothing and needs no privileges.
Keep all of it:

| Setting | Effect |
| --- | --- |
| `read_only: true` | The root filesystem is read-only, so nothing can be planted in the image |
| `tmpfs: /tmp:size=256m` | The only writable path, held in RAM and gone on restart. A multipart upload to `/notify` spools here, so it must be larger than `SABLE_MAX_UPLOAD_BYTES` (25 MB by default; 256m leaves room for several at once) |
| `cap_drop: [ALL]` | No Linux capabilities: it listens on 8080 and makes outbound requests |
| `security_opt: no-new-privileges:true` | A setuid binary cannot gain privileges |
| `pids_limit: 256` | A runaway or a fork bomb stops well short of the host's limits. With plugins on it also has to cover the plugin workers ([sizing](#running-plugins-optional)) |
| `mem_limit: 768m` | A memory cap, which has to cover the `/tmp` tmpfs when it is full, and the plugin workers' own memory |
| no `init: true` | sable is PID 1, so its environment, the app password included, is out of a same-uid plugin's reach. Do not add an init ([why](security.md#plugins-and-the-process-boundary)) |

If you raise `SABLE_MAX_UPLOAD_BYTES` above about 200 MB, grow the tmpfs `size=` with it and raise
`mem_limit` to match, or a large upload can fill the tmpfs before it is finished. A base64 upload
(the JSON shape) is held in memory rather than spooled, so it counts against `mem_limit` directly.

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

        # A little over what /notify accepts (about 33.4 MiB at the default upload
        # cap: see "request size caps"). nginx's default of 1m would refuse an
        # attachment. sable enforces its own caps; this is defence in depth.
        client_max_body_size 36m;
    }
}
```

`SABLE_TRUSTED_PROXIES` defaults to loopback, which covers both examples as written. A proxy in
another container is not loopback and has to be named, or the access log shows the proxy rather
than the client ([which proxies are believed](configuration.md#which-proxies-are-believed)).

Confirm the path works:

```bash
curl -fsS https://sable.example.org/healthz
# {"status":"ok","version":"0.9","user":"sable","llm":"gpt-4o-mini","notify":true,"nextcloud":true}

# ...or, with SABLE_HEALTH_TOKEN set:
curl -fsS -H "X-Health-Token: $SABLE_HEALTH_TOKEN" https://sable.example.org/healthz
```

### If your Nextcloud uses an internal or self-signed certificate

That is the outbound direction, and for sable the only one that matters: sable verifying
Nextcloud's certificate on every poll and every reply. It fails by default with
`CERTIFICATE_VERIFY_FAILED`, because httpx verifies against its own bundled certifi store rather
than the system one, so installing your CA in the container's trust store achieves nothing on its
own. There is no `SABLE_` setting for this and no way to disable verification; the fix is two
lines in [`compose.yaml`](../compose.yaml), already there:

```yaml
volumes:
  - ${SABLE_HOST_CA_DIR:-/etc/ssl/certs}:/etc/ssl/certs:ro
environment:
  SSL_CERT_FILE: /etc/ssl/certs/ca-certificates.crt
```

The mount lends the container the host's CA bundle, which already contains your internal CA since
the host trusts it, and `SSL_CERT_FILE` is what redirects Python away from certifi to that bundle.
Three things will bite you if you vary this:

- Point `SSL_CERT_FILE` at the complete bundle rather than just your CA. It replaces the trust
  store rather than adding to it, so a file holding only your internal CA makes Nextcloud verify
  while every public HTTPS call, your model backend included, fails.
- Do not reach for `SSL_CERT_DIR` with a plain folder of `.crt` files: OpenSSL only looks
  certificates up there by hashed filename, so an ordinary folder trusts nothing while still
  replacing the store.
- Mount the directory rather than the single file, because `update-ca-certificates` on the host
  writes a new file and a single-file bind mount pins the old inode until the container restarts.

If your distribution keeps certificates elsewhere (RHEL and Fedora use `/etc/pki/tls/certs`), set
`SABLE_HOST_CA_DIR`. Docker creates a missing bind-mount source as an empty directory, which would
leave the container trusting nothing at all. On bare metal the host bundle is already in use, so
the systemd unit needs only:

```ini
Environment=SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt
```

To confirm it worked, look for the `signed in to` line at startup. A failure logs
`could not reach Nextcloud at …` with `CERTIFICATE_VERIFY_FAILED` in it.

## 5. Invite it to a conversation

Add the account to a conversation the way you would add any person: Conversation settings,
Participants, Add participants. There is nothing to enable in Nextcloud, and a moderator can
remove it again the same way.

sable looks at which conversations it is in every `SABLE_ROOM_REFRESH` seconds, 60 by default,
and starts reading a new one from its newest message on. It logs when that happens, which doubles
as proof that the whole receiving path works:

```
following conversation abcd1234 ('Team chat')
```

With `SABLE_ALLOWED_ROOMS` set, **add the new room's token there too** (the last part of its URL)
or sable will never follow it; at DEBUG it says `not following abcd1234: not in
SABLE_ALLOWED_ROOMS`. With `SABLE_LEAVE_UNLISTED_ROOMS=true` an invitation to a room you have not
listed is undone within a scan or two. It skips some kinds of conversation and follows at most 50
([which](configuration.md#how-chat-is-received)), and each one it does follow is a request held
open on your Nextcloud server, so keep the account out of rooms it has no business in and make
sure the PHP pool [has room for them](#give-nextcloud-enough-php-workers).

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

The account has to be in the conversation it posts to. A success is `201` with the new message
id; every other status is listed [below](#attaching-a-file-to-an-alert).

### Attaching a file to an alert

`/notify` takes a file in the same single call, in whichever shape suits the caller — multipart
for `curl`, base64 for a JSON client:

```bash
curl -fsS -X POST https://sable.example.org/notify -H "Authorization: Bearer $SABLE_NOTIFY_TOKEN" -F room=alerts -F message="nightly build" -F file=@report.pdf
```

```bash
curl -fsS -X POST https://sable.example.org/notify -H "Authorization: Bearer $SABLE_NOTIFY_TOKEN" -H 'Content-Type: application/json' -d "{\"room\":\"alerts\",\"message\":\"nightly build\",\"file\":{\"name\":\"report.pdf\",\"content\":\"$(base64 -w0 report.pdf)\"}}"
```

The `message` becomes the file's **caption**, so the file and its text arrive as one chat message
rather than two ([file attachments](configuration.md#file-attachments)).

Responses, for a call with or without a file: `201` with the new message id (with a file, also the
stored filename, path and size); `401` for a bad token and `404` when `SABLE_NOTIFY_TOKEN` is
unset; `413` over `SABLE_MAX_UPLOAD_BYTES`, or for a body over the cap sable puts on `/notify`
(that one before the token is even checked: [request size
caps](configuration.md#request-size-caps)); `400` when the `room` is neither a known alias nor a
plausible conversation token, when Talk rejects the post or the share (including because the
account is not in that conversation), or for JSON or UTF-8 that does not parse or nests too
deeply; `422` for a body that is not a JSON object, a field that is missing or invalid, a bad
base64 file, or neither message nor file; `502` if Nextcloud could not be reached or failed in
some other way. If the upload succeeds but the share fails, the uploaded file is deleted again
rather than left orphaned in the account's Files.

### Receiving alerts from other services

Komodo, Alertmanager, Grafana and most other tools will not send sable's `/notify` shape, and
often cannot set an `Authorization` header. Give each one a hook instead:

```ini
SABLE_HOOKS=komodo=a1b2c3d4
SABLE_HOOK_TOKEN_KOMODO=<a token just for this hook>
```

`a1b2c3d4` is the conversation's token, not the name shown in the sidebar; an alias from
`SABLE_NOTIFY_ROOMS` works too, and either is checked when sable starts. In Komodo, create an
Alerter with a Custom endpoint pointing at:

```
https://sable.example.org/hook/komodo?token=<the same token>
```

Komodo cannot set headers, so the token goes in the URL. Anything that can set one is better off
sending `Authorization: Bearer <token>`; both are accepted. That is the whole setup: the payload
is rendered without further configuration, and
[webhooks from other services](configuration.md#webhooks-from-other-services) covers the
rendering and the format strings that control the wording.

## Running plugins (optional)

[Plugins](plugins.md) are off until `SABLE_PLUGINS_DIR` is set. Each one is a separate process
running code you mount, so this is a deployment decision as much as a configuration one: read
[what it does and does not protect](security.md#plugins-and-the-process-boundary) first.

With Compose, put the plugins in a directory beside `compose.yaml` and uncomment the two lines
already there, the mount and the setting:

```yaml
    volumes:
      - ./plugins:/plugins:ro
    environment:
      SABLE_PLUGINS_DIR: "/plugins"
```

- **Mount it read-only (`:ro`).** A plugin that could write to the directory it is loaded from
  could rewrite itself, or another plugin, and keep the change across restarts. sable warns at
  startup when the directory is writable to it. A changed plugin takes effect when sable restarts:
  there is no reload.
- **Do not add an init process** (`init: true` in Compose, `--init` with `docker run`).
  `compose.yaml` leaves it out on purpose: sable has to be PID 1, because it marks itself
  non-dumpable and so a plugin worker, which is the same user, cannot read its environment. An
  init is a PID 1 that holds the full environment and is not non-dumpable, which would hand the
  app password to every plugin. The cost of leaving it out: a plugin worker that is killed on a
  timeout takes its process group with it, but anything it started in a session of its own is
  orphaned onto PID 1, which does not reap it, so it stays a zombie, using a slot of
  `pids_limit`, until the container restarts
  ([accepted risk 18](security.md#accepted-risks)).
- **Size `pids_limit` and `mem_limit` for the workers.** Each active plugin is a Python process of
  its own, plus whatever it starts. An idle worker held about 21 MB resident when measured once
  (Python in WSL, with nothing but the plugin API imported), and a plugin that imports `httpx` or
  holds data costs more. The 1 GiB limit on a worker is on address space, not memory in use, so
  the container's `mem_limit` is what actually bounds them together. The `/tmp` tmpfs is theirs to
  fill as well. Raise both limits by what the plugins you run need; the defaults in `compose.yaml`
  were sized for sable alone.
- **Restrict what the container can reach**, if the plugins do not need the internet: a plugin has
  whatever network the container has. sable has no setting for it; use a Docker network policy or
  an egress proxy.
- **systemd**: keep the plugins under `/opt/sable` (the unit's `ReadOnlyPaths` then makes them
  read-only to the service), and put the environment where the service user cannot read it. The
  example above keeps `/opt/sable/.env` readable by the `sable` user, which is also the user
  plugins run as; with plugins on, load it with `EnvironmentFile=` from a file only root can read
  (systemd reads it before dropping to `User=`) and stop passing `--env-file`. That arrangement
  was not run for this page. `TasksMax` and `MemoryMax` cover the workers like `pids_limit` and
  `mem_limit` do, and want the same raising.

Check before restarting, against the real settings:

```bash
sable --check
```

It starts each plugin that has rooms, runs its `check()` and shuts it down again, and prints one
line per plugin ([what it checks](plugins.md#operating)). Then, in a conversation, as an
administrator, `!plugins` shows what the running process made of them, and a command of the
plugin's own shows it works. The startup block has a `plugins:` line, `off (SABLE_PLUGINS_DIR is
empty)` while the setting is empty and a count of what loaded otherwise
(`2 active, 1 inactive, 1 failed (/plugins)`), and each failed plugin is a warning naming it and
why.

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
  plugins:        off (SABLE_PLUGINS_DIR is empty)
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

With the Open WebUI backend the block gains a `tools:` line between `model:` and `concurrency:`.
Read it as a list of what a stranger in those rooms can set off, because it is one
([tools are a decision about a room](configuration.md#letting-the-model-use-tools)):

```
tools:          server-side loop via Open WebUI · tools: server:mcp:1, server:mcp:2 · in rooms: e5f6g7h8 · built-ins: web_search
```

[Startup warnings](configuration.md#startup-warnings) follow the block. Refusals show up as
warnings you can grep for: `rate limit: ignoring …` once per person per minute,
`dropping replies: …` at most every 30 seconds when the queue is full, `refused !reset for …` for
an admin-only command, and `cannot leave conversation …` for a room Talk would not let the account
leave.

The startup probe, the line reading `signed in to`, asks Nextcloud who the credentials belong to.
It proves DNS, TLS and the app password in one request, so a wrong URL, an untrusted certificate
or a revoked password shows up at boot rather than on the first reply somebody is waiting for. A
rejected password logs an error naming `SABLE_NEXTCLOUD_PASSWORD`. It is never fatal, since
Nextcloud may simply not be up yet and sable keeps retrying with backoff either way;
`SABLE_STARTUP_CHECK=false` skips it.

Reachability is logged as transitions rather than per attempt, so an outage is two lines rather
than one per retry:

```
ERROR  sable.state: lost connection to Nextcloud: ConnectError: All connection attempts failed
INFO   sable.state: Nextcloud is reachable again
```

A long poll that Nextcloud accepts and then holds past its timeout is not an outage. It is a
single warning per streak, naming the conversation, and means every PHP worker was busy and the
poll queued ([give Nextcloud enough PHP workers](#give-nextcloud-enough-php-workers)):

```
WARNING  sable.poller: Nextcloud held the poll of conversation abcd1234 ('Team chat') past 45s without answering; asking again. If this repeats, its PHP-FPM pool is probably too small for this many conversations (see docs/deployment.md).
```

The same applies to the model backend. A transport failure counts as unreachable; an HTTP error
response does not, because the service answered, and that is logged with its status code and the
first 200 characters of the body.

Message text stays out of INFO. A use is logged as who, what and where, with sizes rather than
content, as in `asked the model in abcd1234 (22 chars)`. `SABLE_LOG_LEVEL=DEBUG` adds the
prompt, command arguments and the message a reaction referred to, plus a line for every event
sable decided not to act on and one per outbound HTTP call, so treat a DEBUG log as containing
chat content. A successful `GET /healthz` is not logged (`SABLE_LOG_HEALTH_CHECKS=true` brings
those lines back); one that *fails* still appears.

### Upgrading, rotating, backing up

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

Run one process, and one per account. Conversation history, the rate limiter and the position in
each conversation all live in memory, so two workers would split them and replies would forget
context depending on which worker answered. Worse, two processes signed in as the same account
would each read every message and each answer it. One process handles chat traffic without
breaking a sweat, at a few tens of megabytes resident. Scale by running separate accounts rather
than workers.

There is nothing to back up, because nothing is persisted. Keep `.env` in a secret store, since
it is the only thing that is hard to recreate. For what to lock down, see the
[hardening checklist](security.md#hardening-checklist).

## Troubleshooting

| Symptom | Cause and fix |
| --- | --- |
| Nothing happens at all | The account is not in that conversation, or sable cannot sign in. Look for `signed in to` at startup, and for `following conversation <token>` once the account has been invited. A new invitation takes up to `SABLE_ROOM_REFRESH` seconds to be noticed. |
| `refused the credentials for '…' (HTTP 401)` in the log | `SABLE_NEXTCLOUD_USER` or `SABLE_NEXTCLOUD_PASSWORD` is wrong, the password is not an app password, it was revoked, or the user is disabled. Generate a new app password under Settings → Security. |
| `could not reach Nextcloud at …` | The URL, DNS, or the certificate. `CERTIFICATE_VERIFY_FAILED` means an internal CA is not trusted — see [the self-signed section](#if-your-nextcloud-uses-an-internal-or-self-signed-certificate). |
| Invited to a room, still silent after a minute | The room is not in `SABLE_ALLOWED_ROOMS`, which is the commonest cause once that is set: at DEBUG the log says `not following <token>: not in SABLE_ALLOWED_ROOMS`. Add its token (and with `SABLE_LEAVE_UNLISTED_ROOMS` the account may already have left it). Otherwise it is one sable does not follow, or the account is in more than 50 conversations and this one is not among the most recently active — the log warns once ([which](configuration.md#how-chat-is-received)). Otherwise check `SABLE_ROOM_REFRESH`. |
| Answers in a room only without the tools | The room is not in `SABLE_LLM_TOOL_ROOMS`, which is empty by default. Add the token, and check the `tools:` line of the startup block for `in rooms:`. |
| `You are not allowed to use the assistant.` | `SABLE_LLM_USERS` is set and the sender's Nextcloud user id is not in it (administrators always are). Add the id; display names never match. Plain messages in an AI room and ⁉️ reactions from the same person get no reply at all, only an INFO line `not in SABLE_LLM_USERS`. |
| `rate limit: ignoring …` in the log, and the bot stops answering one person | They set off more than `SABLE_RATE_LIMIT` triggers within a minute. It is silent in the room and lifts a minute after their last accepted trigger. Raise it, or `0` turns it off. |
| `dropping replies: …` in the log, and some questions get no answer | More replies were running or waiting than `SABLE_MAX_CONCURRENT_REPLIES` plus `SABLE_MAX_QUEUED_REPLIES` allow, so new work is dropped, silently to the user. A slow model backend and a long `SABLE_LLM_TIMEOUT` are the usual cause; raise the ceiling if the backend can take it. |
| Silent in a room from before sable started | Expected: a conversation is followed from its newest message, so nothing earlier is replayed. |
| `Nextcloud held the poll of conversation … past …s without answering` | Nextcloud accepted the request and did not answer in time, almost always because its PHP-FPM pool is full and the poll is queued. sable keeps asking and does not treat it as an outage. Raise `pm.max_children` — see [give Nextcloud enough PHP workers](#give-nextcloud-enough-php-workers). You can confirm it by running seven `curl` polls at once: with a big enough pool they all return in about 30 seconds. |
| Nextcloud shows many long-running requests from one user | That is the long polling: one per conversation, each up to `SABLE_POLL_TIMEOUT` seconds. |
| Replies never appear, `could not post to <token>` in logs | The account cannot post there (a read-only conversation, or it was removed), or `SABLE_NEXTCLOUD_URL` is unreachable. |
| Answers are slow or absent, `completion failed` in logs | Model timeout. Raise `SABLE_LLM_TIMEOUT`, lower `SABLE_LLM_MAX_TOKENS`, or pick a faster model. |
| `the model returned an empty message` | A reasoning model spent its whole budget thinking. Raise `SABLE_LLM_MAX_TOKENS` or lower reasoning effort via `SABLE_LLM_EXTRA_BODY`. |
| `called <tool> and nothing executed it` | The backend offered the model tools but does not run them, so there is no answer in the reply. Either stop offering them, or set `SABLE_LLM_BACKEND=openwebui` so Open WebUI runs the loop. |
| `the loop finished without writing an answer` | Open WebUI accepted the work and wrote nothing. Almost always the model's own *Stream Chat Response* parameter, which overrides `stream: true` and stops the tool loop running. |
| Tool answers are stale or invented | The model has no clock unless you give it one. Check the date line in the system prompt, and set `SABLE_TIMEZONE`. |
| Replies are cut short with `_[truncated]_` | The answer exceeded `SABLE_MAX_MESSAGE_CHARS`; Talk's own ceiling is 32000 characters. |
| `HTTP 429` from Talk | Nextcloud is throttling the account, most likely for posting too fast. Batch or slow down whatever is calling `/notify`. |
| Mentions ignored | Pick the account from Talk's mention list, or start the message with its user id. `SABLE_NEXTCLOUD_USER` has to be the id people mention. Set `SABLE_LOG_LEVEL=DEBUG` and watch for `message in <token> was not for me`. |
| The ⁉️ reaction does nothing | Set `SABLE_LOG_LEVEL=DEBUG` and react again. Silence is by design when the reactor fails the usual checks (room, rate limit, `SABLE_LLM_USERS`, `SABLE_ASK_ADMINS_ONLY`), when the message's author is in `SABLE_IGNORE_USERS`, or when it is a system message; each leaves a log line. If no `received reaction` line appears at all, the reaction never arrived through the chat poll — see [future.md](future.md#talk-features-not-yet-used). |
| The ⁉️ reaction says `I cannot find that message` | Talk answered 404 to the read-back: the message was deleted, or the account cannot see it. Other read failures are reported separately ([the replies](configuration.md#asking-about-a-message-by-reacting-to-it)). |
| A plugin's command answers "I have no `x` command" | Deliberate for a room or a state the plugin does not serve: it must be active, with the conversation's token in its `access.rooms` and in `SABLE_ALLOWED_ROOMS` if that is set. `!plugins <name>` as an administrator shows its status, rooms and last error; a plugin with no rooms is `inactive: no rooms set` ([why a plugin may be hidden](plugins.md#who-may-run-a-plugin-command)). |
| `!plugins` says `failed: …`, or a plugin is missing from it | The reason follows `failed:`. A plugin that is not listed at all is usually a file name sable does not recognise: the log and `sable --check` name settings files that are nearly right ([the layout rules](plugins.md#layout-on-disk)). |
| `!plugins` says `switched off, retrying in N min` | The worker died or timed out more than three times in five minutes. After the wait one call tries it again ([the breaker](plugins.md#when-a-plugin-fails)). The log has the reason under `plugin <name>`. |
| `/notify` returns 404 | `SABLE_NOTIFY_TOKEN` is unset, so the route is disabled. |
| `/hook/<name>` returns 404 | No hook by that name, or `SABLE_HOOKS` is unset. A configured hook with a bad token answers 401 instead, so 404 means the name. |
| `!reset is for administrators only` | The sender's Nextcloud user id is not in `SABLE_ADMIN_USERS`. The log line names who was refused. Display names are never matched, only user ids. |
| `/docs` or `/openapi.json` returns 404 | Expected: set `SABLE_API_DOCS=true` to serve them. |
| `/healthz` returns 401 | `SABLE_HEALTH_TOKEN` is set, so the probe needs an `X-Health-Token` header. The image's own healthcheck sends it; anything else calling `/healthz` has to as well. |
| Access log shows the proxy's IP, not the client's | The proxy's address is not in `SABLE_TRUSTED_PROXIES`, so its `X-Forwarded-For` is ignored. In Docker that is the usual case: name the network's subnet. |
| `/notify` returns 400 or 422 | The request or the room is invalid; the cases are listed under *Responses* in [verify end to end](#6-verify-end-to-end). |
| `/notify` or `/hook` returns 413 | The body is over the cap sable enforces itself, and was refused before the token was checked ([request size caps](configuration.md#request-size-caps)). |
| Config error on startup | See [the table in configuration.md](configuration.md#startup-errors-and-what-they-mean). |
