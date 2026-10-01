# Security posture

What protects what, what is deliberately trusted, and what is not protected at all. The
[accepted risks](#accepted-risks) are the part to read before this is in rooms you do not
control: several of them are choices rather than oversights.

## Threat model

sable is a Nextcloud user account run by a small HTTP service. It holds one credential that
matters, the app password for that account, which lets it read every conversation the account is
in, post and react as it, and read and write that user's Files. It optionally holds a token that
lets other systems post alerts and an API key for a model backend.

It makes outbound connections only, to Nextcloud and to the model backend. Nextcloud does not
call it, and the only routes it serves are `/notify`, `/hook/{name}`, `/healthz` and `/`.

The three things an attacker would want are to act as the account, which needs the app password
or the process; to read chat content, which needs to be in the conversation or to compromise the
process; and to make the bot talk to something it should not, which needs to be able to post in a
conversation it is in, or to poison the model backend's URL. Everything below is about those.

What changed from a webhook bot is worth saying plainly. Nothing signs or verifies anything any
more: sable trusts what Nextcloud's chat API returns over TLS, which is where it always got the
content from, and its one credential is a user's, not a bot's. That credential is larger and cannot
be narrowed. In exchange there is no inbound webhook to forge, replay or redirect.

## Trust boundaries

| Boundary | What protects it |
| --- | --- |
| sable to Nextcloud, for reading and posting | HTTPS with certificate verification, authenticated as the account by its app password over HTTP Basic. Plain `http://` to a non-local host is allowed with a startup warning, since the password then crosses the network unencrypted. |
| Nextcloud to sable | Nothing is accepted from Nextcloud unprompted. Chat arrives as the response to sable's own requests, so there is no inbound endpoint for it to forge. |
| Who can make sable hear a room | Talk's own membership. sable reads only conversations the account has been added to, by a normal invitation, and only those. |
| Anything to `POST /notify` | A separate bearer token, compared in constant time. Unset means the route answers 404. |
| sable to Nextcloud Files | The same app password, used to upload and share attachments. |
| sable to the model backend | Ordinary HTTPS with certificate verification; the API key travels as a bearer token. |
| A room participant to the model's tools | Nothing, with `SABLE_LLM_BACKEND=openwebui`. Asking the assistant a question is not a command, and the model chooses which tool to call, so only the Open WebUI account's own permissions bound what can happen. |
| Anything to `GET /healthz` | Nothing by default, which is what a container or Kubernetes probe needs. `SABLE_HEALTH_TOKEN` puts it behind an `X-Health-Token` header, compared in constant time. |
| Anything to the API schema | The schema and its `/docs` and `/redoc` pages are not served at all unless `SABLE_API_DOCS=true`. |
| A proxy claiming a client address | `X-Forwarded-For` and `X-Forwarded-Proto` are believed only from `SABLE_TRUSTED_PROXIES`, loopback by default. Nothing reads the client address, so this protects the access log rather than access. |
| Chat participants to commands | Nothing by default: anyone in a conversation, guests included, can run any command. `SABLE_ADMIN_COMMANDS` moves named commands behind `SABLE_ADMIN_USERS`, matched on Nextcloud user id. |

## Authentication and integrity

sable authenticates to Nextcloud with HTTP Basic: the account's user id and its app password, on
every request, in [`talk.py`](../src/sable/talk.py) and [`files.py`](../src/sable/files.py). There
is no session, no token exchange and nothing cached but the password itself, which is read from
the environment once and held out of every `repr`, so a `Config` that reaches a log or a
traceback does not print it.

Because the credential is a password, TLS is what protects it in transit, and there is no
substitute. sable warns at startup when `SABLE_NEXTCLOUD_URL` is plain `http://` to anywhere but
loopback, and refuses nothing, since a private network you trust is a reasonable place for it.

An app password is revocable on its own, from the account's Security settings, without changing
the account's login password. That is the recovery path for a leaked credential, and rotation is
the same operation: create a new one, update `SABLE_NEXTCLOUD_PASSWORD`, restart, revoke the old.

Who wrote a message is whatever Nextcloud says it is. sable ignores anything written by its own
account, so its replies and reactions coming back down the poll do not trigger it, and ignores
actors Talk marks as bots (actor type `bots`, ids starting `bots/`).

Room tokens are validated before use, in two places. `/notify` accepts an alias from
`SABLE_NOTIFY_ROOMS` or a token matching `^[a-z0-9]{4,64}\Z` — Talk's own routes match only
lowercase — and anything else is a 400 rather than a request to Nextcloud. The poller applies the
same check to every token Nextcloud lists before putting it in a URL path, and skips, with a
warning, any that do not fit.

TLS is never disabled. There is no `verify=False` anywhere and no setting that could add one.
An internal or self-signed Nextcloud certificate is handled by lending the container the host's
CA bundle and pointing Python at it with `SSL_CERT_FILE`, which *replaces* the trust store
rather than extending it — so it must name the complete bundle, public roots included. Details
in [deployment.md](deployment.md#if-your-nextcloud-uses-an-internal-or-self-signed-certificate).

## Abuse resistance

Events from actors Talk marks as bots (actor type `bots`, ids starting `bots/`) are ignored, as
is anything the account wrote itself, so two bots in one room cannot start answering each other
and sable cannot answer its own replies. Every event is de-duplicated on the conversation, type,
message id, actor and reaction together, keeping the last 512, so a message seen twice produces
one reply while two people reacting to the same message remain two distinct events.

`SABLE_MAX_CONCURRENT_REPLIES` caps how many model calls can be open at once, eight by default,
with the rest queued rather than dropped. Nothing upstream paces the messages Talk hands over, so
without a ceiling a burst in a busy room meant one open model call per message, each holding
`SABLE_LLM_TIMEOUT` open. The poll loop never waits
for a slot, so a full queue delays replies and never makes sable fall behind the conversation.

sable follows at most 50 conversations, the most recently active, and none of the ones nobody
addresses a bot in (Talk updates, a former one-to-one, the account's note to self, the sample
conversation). Each one it does follow is a request held open on Nextcloud, so the cap bounds
what a misplaced invitation list can cost the server as well as what sable watches. A poll that
Nextcloud holds past its timeout is logged as a warning naming the conversation, not as an
outage.

Display names and conversation names are flattened onto a single line before anything uses them:
control characters go, Unicode line separators collapse, and the result is capped at 100
characters. Both are spliced into the model's prompt — the conversation's name into the
system message, the speaker's name in front of what they said — so a newline in one would be the
difference between sitting inside the prompt and writing a line of it. A moderator still chooses
what a room is *called*; they do not choose what shape it arrives in.

Replies are truncated at `SABLE_MAX_MESSAGE_CHARS`, since Talk rejects anything over 32000 with
a 413. Handlers run in the background, away from the poll loop, so a slow model cannot stop sable
reading. A reply that fails to post is a logged warning rather than an unhandled exception, so an
unreachable Nextcloud produces log lines instead of stray tracebacks.

## Secrets

| Secret | What it grants | Rotation |
| --- | --- | --- |
| `SABLE_NEXTCLOUD_PASSWORD` | Everything the account can do: read its rooms, post as it, and read and write its Files, Contacts and Calendar. It cannot be scoped | Create a new app password, update the variable, restart, then revoke the old one |
| `SABLE_NOTIFY_TOKEN` | Posting into the aliased conversations | Change the variable and restart, then update callers |
| `SABLE_LLM_API_KEY` | Your model provider's billing. With `SABLE_LLM_BACKEND=openwebui`, also the account whose tools the model can run | At the provider |

All of them come from the environment. `.env` is in both `.gitignore` and `.dockerignore`, and
`.env.example` ships with the password empty. `compose.yaml` reads the required settings as
`${VAR}` interpolations rather than literals, so the committed file never contains one and a
missing value fails at `docker compose up` with a message naming it.

No secret is logged or printed. `sable --check` and the startup block print the account's user id,
Nextcloud URL, prefix, model and so on, but never the app password, the notify token or the API
key. Conversation tokens *are* logged at INFO, and for a conversation shared by public link the
token is what grants access, so treat logs accordingly.

## Container and host

The image runs as a non-root user, uid 10001, created in the image, and is built from a
patch-pinned base rather than a floating tag. Dependencies are locked and hash-verified:
`uv.lock` pins 31 packages and `uv sync --locked` fails rather than resolving something else. uv
itself is uninstalled in the same layer, so it does not ship.

Nothing is written to disk. Conversation history and the message cache live in process memory
only, which is also why there is nothing to back up.

`compose.yaml` publishes to `127.0.0.1:8080` only, and the healthcheck talks only to localhost.
Since chat is received over outbound connections, the published port matters only to whatever
calls `/notify` and `/hook/{name}`, and may be dropped altogether if nothing does. The systemd
unit in [deployment.md](deployment.md) sets `NoNewPrivileges`, `ProtectSystem=strict`,
`ProtectHome`, an empty `CapabilityBoundingSet` and a memory cap.

## What leaves your infrastructure

Messages the assistant answers go to your configured `SABLE_LLM_BASE_URL`, along with the recent
history of that conversation and the speakers' display names. Nothing else leaves.

If that backend is a hosted API, chat content leaves your network. A local backend such as
Ollama, vLLM or llama.cpp avoids the question entirely, and setting `SABLE_LLM_MODEL` to empty
disables the assistant while leaving commands working.

Attachments do not leave: they are uploaded into your own Nextcloud, in the account's Files, and
shared from there.

With `SABLE_LLM_BACKEND=openwebui` the question also creates a conversation in that Open WebUI
account, which is deleted once the answer has been read unless `SABLE_LLM_KEEP_CHATS` is on.
Whatever tools the loop calls see the prompt: a web search sends the query to your configured
search provider, and an MCP server sees whatever the model passes it.

## Accepted risks

These are known and deliberate. Decide for yourself whether they are acceptable.

1. **The app password is the single credential, and it cannot be scoped.** It is the account's,
   so anybody holding it can read every conversation the account is in, post and react as it,
   and read and write that user's Files, Contacts and Calendar. A webhook bot's shared secret
   could only post messages; this is a much larger thing to lose, and it is the direct price of
   running as a user. Give sable a dedicated account that owns nothing else and is in only the
   rooms it needs, so that what the password reaches is nearly nothing beyond chat. It is
   revocable on its own, and revoking it is the response to a leak. Uploads land in
   `SABLE_UPLOAD_PATH` inside that user's own Files, and filenames from callers are sanitised and
   made unique, so one alert cannot overwrite another or climb out of the folder.

2. Commands are open unless you close them. Out of the box anyone in the conversation, guests
   included, can run any command. `SABLE_ADMIN_COMMANDS` plus `SABLE_ADMIN_USERS` moves named
   commands — or all of them, with `*` — behind a list of Nextcloud user ids, and a custom
   command can check `ctx.is_admin` for anything finer. What this is not is authentication: it trusts the
   user id Nextcloud's chat API reports for the sender, the same trust the rest of the service
   runs on. Who is in the conversation at all stays Talk's decision, not ours.

3. `/notify` is a single shared token with no per-caller identity and no rate limiting. A leaked
   token lets anyone post into the aliased conversations. Keep the endpoint off the public
   internet where you can.

4. Upstream error text can reach the chat room. With `SABLE_REPORT_ERRORS` on, a failure posts
   the error, which includes up to 400 characters of the model backend's or Nextcloud's response
   body. If your backend is chatty about credentials in its errors, turn it off.

5. Model output is posted verbatim and nothing filters it. A participant can try to steer the
   model through prompt injection; the realistic worst case is embarrassing or misleading text,
   since Talk renders Markdown and sanitises HTML itself. Names cannot forge a turn or a line of
   the system prompt (see above), but inside its own line a display name is still free text, so
   somebody calling themselves `assistant` is a thing the model sees.

6. Chat content sits in process memory for up to `SABLE_HISTORY_TTL`: the assistant's history
   per conversation, and, while `SABLE_ASK_REACTION` is set, the last `SABLE_MESSAGE_CACHE`
   messages of every conversation the bot is in. None of it is written to disk, but it would
   appear in a core dump. `SABLE_ASK_ROOMS` narrows the cache to the conversations that actually
   use the reaction, and clearing `SABLE_ASK_REACTION` disables it entirely.

7. Anyone in a conversation can send any message to the model by reacting to it, including
   messages they did not write. That is the feature working as intended, but it means one
   participant can forward another's words to your model backend without saying anything in the
   room. `SABLE_ASK_ADMINS_ONLY` restricts it to `SABLE_ADMIN_USERS`; a refused reaction is
   logged and says nothing in the room, since the message it points at belongs to somebody who
   has done nothing.

8. **sable is a person in the room, and whoever can add participants can add it.** It appears in
   Talk's participant list as an ordinary user, and there is no bot switch for a moderator to
   flip: anyone allowed to invite people to a conversation can invite it, after which everything
   said there is read, cached, and — if it is an AI room or the account is mentioned — sent to the
   model backend. Conversely, nothing in Talk marks its messages as automated, so people may take
   an answer for a person's. Name the account so that it is obvious, keep it out of rooms where
   that is not wanted, and list it in no more rooms than it needs: each conversation is also a
   long poll held open on Nextcloud, which is why no more than 50 are followed.

9. There is no rate limiting of our own. Nothing limits how fast `/notify` can be called, so the
   real memory ceiling for attachments is the cap times the number of concurrent callers, and
   whatever Nextcloud itself does about an account that posts too fast is not something sable
   relies on. Reading chat has its own cost, which is not a
   risk to sable but to Nextcloud: every long poll holds a request slot for up to
   `SABLE_POLL_TIMEOUT` seconds, per conversation, continuously, where a webhook would cost it
   nothing while idle. On a small server with few PHP workers that is enough to be felt, and
   in practice it is an availability problem for everyone on that Nextcloud: the stock
   container's pool is five workers, so seven followed conversations starve it, queue other
   users' requests behind idle polls, and make sable's own posts time out. Raise
   `pm.max_children` (see [deployment.md](deployment.md#give-nextcloud-enough-php-workers)); the
   skipped conversation types, the 50-conversation cap and the dedicated account are the
   mitigations on sable's side.

10. Logs are the only audit trail. There is no separate audit log and no metrics endpoint. At
    INFO they record who used the bot, which command or trigger, and in which conversation,
    which answers "who asked it that" but not what was said. DEBUG puts chat content in the log:
    prompts, command arguments, and the text of any message a reaction referred to. Treat a
    DEBUG-level log as containing conversation content.

11. Hook tokens can travel in a URL. Services like Komodo cannot set headers, so `/hook/{name}`
    accepts `?token=`, and proxies and access logs will record it. Each hook has its own token
    to contain that: one exposed in a log costs you that hook rather than everything `/notify`
    can reach. Whoever holds a hook URL can write arbitrary text into that conversation, since
    the payload becomes the message.

12. Hook names can be probed. A configured hook with a wrong token answers 401 while an unknown
    name answers 404, so `/hook/<name>` tells an unauthenticated caller which hooks exist. Names
    like `komodo` or `grafana` are guessable anyway and each hook has its own token, so what this
    costs is the name rather than any access. Unknown names and hooks being unconfigured do give
    the same 404, so it does not say whether the feature is in use at all.

13. `GET /healthz` is unauthenticated unless you set `SABLE_HEALTH_TOKEN`, and it names the
    version, the account's user id and the configured model. That is the default because a
    liveness probe that needs a credential fails for the wrong reasons.

14. **Server-side tools turn a chat room into an actuator.** With
    `SABLE_LLM_BACKEND=openwebui`, Open WebUI executes tools with the permissions of the
    account behind `SABLE_LLM_API_KEY`, and the model decides which to call. Asking the
    assistant a question is not a command, so `SABLE_ADMIN_COMMANDS` does not gate it: anyone
    in a conversation with the bot, guests included, can cause whatever those tools do. If they
    reach Home Assistant, a stranger can turn off your lights by asking; if they can send
    messages, the bot can be made to send them. Prompt injection stops being an
    embarrassing-text problem, since a participant can paste text aimed at the model rather
    than at the room.
    The control is the account, not sable: give it a dedicated Open WebUI user holding only the
    tools a chat room should have, and leave the rest off. sable never sends a `terminal_id`,
    so Open Terminal is out of reach by construction. `SABLE_LLM_KEEP_CHATS=true` keeps each
    conversation, which is the closest thing to an audit trail of what the model actually ran.

15. CI holds credentials: the registry password and a runner token with write access to the
    repository, used to create releases. Anyone who can change a workflow on a branch CI runs
    can reach both.

## Hardening checklist

- [ ] A dedicated Nextcloud account for sable that owns nothing else, and whose display name
      makes plain it is not a person
- [ ] `SABLE_NEXTCLOUD_PASSWORD` an app password, not the login password, and revoked when
      rotated
- [ ] `SABLE_NEXTCLOUD_URL` is `https://`, unless it is a private network you trust
- [ ] The account invited only to the conversations it should answer in
- [ ] Nothing exposed that does not need to be: the port bound to localhost or a private network,
      and dropped entirely if nothing calls `/notify`, `/hook` or `/healthz`
- [ ] `SABLE_NOTIFY_TOKEN` distinct from every other credential, or unset if unused
- [ ] Egress restricted to Nextcloud and the model backend
- [ ] `SABLE_REPORT_ERRORS=false` if upstream errors should not reach the room
- [ ] Every custom command reviewed as code anyone in the room can trigger, or named in
      `SABLE_ADMIN_COMMANDS`
- [ ] `SABLE_API_DOCS` left off, so the schema is not served to whoever can reach the port
- [ ] `SABLE_TRUSTED_PROXIES` naming your proxy — the default is loopback, which a proxy in
      another container is not
- [ ] `SABLE_HEALTH_TOKEN` set if `/healthz` naming the model is more than you want public
- [ ] `SABLE_ASK_ROOMS` naming only the conversations that use the reaction, so the message cache
      holds no more chat than it must
- [ ] `SABLE_ASK_ADMINS_ONLY` on if forwarding somebody else's message to the model should not be
      open to everyone in the room
- [ ] If server-side tools are on, a dedicated Open WebUI account holding only the tools a chat
      room should reach — the model chooses which to call, and anyone in the room can prompt it
- [ ] The image pinned by version or digest on the host rather than `latest`

## Reporting a problem

This is an internal project, so raise it as an issue on the repository or privately with
whoever runs the instance. There is no separate embargo process.
