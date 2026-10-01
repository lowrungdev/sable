# Security posture

What protects what, what is deliberately trusted, and what is not protected at all. The
[accepted risks](#accepted-risks) are the part to read before this is in rooms you do not
control: several of them are choices rather than oversights. This page names settings; how each
one behaves is in [configuration.md](configuration.md).

## Threat model

sable is a Nextcloud user account run by a small HTTP service. It holds one credential that
matters, the app password for that account, which lets it read every conversation the account is
in, post and react as it, and read and write that user's Files. It optionally holds a token that
lets other systems post alerts and an API key for a model backend.

It makes outbound connections only, to Nextcloud and to the model backend. Nothing is accepted
from Nextcloud unprompted: chat arrives as the response to sable's own requests, so there is no
inbound endpoint to forge or redirect, and it trusts what Nextcloud's chat API returns over TLS
and checks it no further. The only routes it serves are `/notify`, `/hook/{name}`, `/healthz`
and `/`.

The three things an attacker would want are to act as the account, which needs the app password
or the process; to read chat content, which needs to be in the conversation or to compromise the
process; and to make the bot talk to something it should not, which needs to be able to post in a
conversation it is in, or to poison the model backend's URL. Everything below is about those.

Who may talk to the bot is decided in layers, and every default is open: any user who can invite
the account into a conversation can use it, and everyone in it can run every command and ask the
model ([the layers](configuration.md#how-the-access-layers-combine)).

## Trust boundaries

| Boundary | What protects it |
| --- | --- |
| sable to Nextcloud, for reading, posting and Files | HTTPS with certificate verification, authenticated as the account by its app password over HTTP Basic. Plain `http://` to a non-local host is allowed with a startup warning. |
| Who can make sable hear a room | Talk's own membership, narrowed by `SABLE_ALLOWED_ROOMS` to listed conversation **tokens**. Rooms are never matched by name, because anybody can name their own conversation after yours. |
| Who can make the model answer | Nothing by default. `SABLE_LLM_USERS`, for every path to the model; guests, federated users and bots never match once it is set. |
| Chat participants to commands | Nothing by default: anyone in a conversation, guests included, can run any command. `SABLE_ADMIN_COMMANDS` moves named commands behind `SABLE_ADMIN_USERS` ([accepted risk 2](#accepted-risks)). |
| A room participant to the model's tools | With `SABLE_LLM_BACKEND=openwebui`, the room: tools are offered only in `SABLE_LLM_TOOL_ROOMS`, empty by default, and `SABLE_LLM_EXTRA_BODY` cannot add them behind that gate. Within a tools room the Open WebUI account's own permissions bound what can happen ([accepted risk 14](#accepted-risks)). |
| Anything to `POST /notify` | A separate bearer token, compared in constant time on UTF-8 bytes (a non-ASCII token is a 401, never an error). Unset means the route answers 404. `/hook/{name}` has a token per hook. |
| An oversized request body | Refused by the app with a 413 before the token is checked or anything is parsed. A proxy limit in front is defence in depth. |
| Anything to `GET /healthz` | Nothing by default, which is what a container or Kubernetes probe needs. `SABLE_HEALTH_TOKEN` puts it behind an `X-Health-Token` header, compared in constant time. |
| Anything to the API schema | Not served at all unless `SABLE_API_DOCS=true`. |
| A proxy claiming a client address | `X-Forwarded-For` and `X-Forwarded-Proto` are believed only from `SABLE_TRUSTED_PROXIES`. Nothing reads the client address, so this protects the access log rather than access. |
| Chat text to other people's notifications | Mass mentions in everything posted on behalf of chat or a webhook are defanged ([accepted risk 17](#accepted-risks)). `/notify` text is not. |
| sable to the model backend | Ordinary HTTPS with certificate verification; the API key travels as a bearer token. |

## Authentication and integrity

sable authenticates to Nextcloud with HTTP Basic: the account's user id and its app password, on
every request, in [`talk.py`](../src/sable/talk.py) and [`files.py`](../src/sable/files.py). There
is no session, no token exchange and nothing cached but the password itself, which is read from
the environment once and held out of every `repr`, so a `Config` that reaches a log or a
traceback does not print it.

Because the credential is a password, TLS is what protects it in transit, and there is no
substitute. TLS is never disabled: there is no `verify=False` anywhere and no setting that could
add one. An internal CA is handled by trusting it
([how](deployment.md#if-your-nextcloud-uses-an-internal-or-self-signed-certificate)), and plain
`http://` is warned about but not refused, since a private network you trust is a reasonable place
for it. An app password is revocable on its own, which is the recovery path for a leak.

Who wrote a message is whatever Nextcloud says it is. sable ignores anything written by its own
account (recognised by user id), so its replies and reactions coming back down the poll do not
trigger it, and ignores actors Talk marks as bots (actor type `bots`, ids starting `bots/`), so
two bots in one room cannot start answering each other.

Room tokens are validated before use, in several places. `/notify` accepts an alias from
`SABLE_NOTIFY_ROOMS` or a token matching `^[a-z0-9]{4,64}\Z` — Talk's own routes match only
lowercase — and anything else is a 400 rather than a request to Nextcloud. The poller applies the
same check to every token Nextcloud lists before putting it in a URL path, and skips, with a
warning, any that do not fit. `SABLE_ALLOWED_ROOMS`, `SABLE_AI_ROOMS` and `SABLE_LLM_TOOL_ROOMS`
take only tokens, so a display name is a startup error rather than a match. The Open WebUI chat
id, which that server hands back, is percent-encoded wherever it goes into a URL path, so an id
holding `/` or `..` cannot reach another endpoint.

Display names and conversation names are flattened onto a single line before anything uses them:
control characters go, Unicode line separators collapse, and the result is capped at 100
characters. Both are spliced into the model's prompt — the conversation's name into the system
message, the speaker's name in front of what they said — so a newline in one would be the
difference between sitting inside the prompt and writing a line of it. A moderator still chooses
what a room is *called*; they do not choose what shape it arrives in.

## Abuse resistance

Each limit is explained where its setting is; this is what each one is for.

- **Model calls:** nothing upstream paces the messages Talk hands over, so
  `SABLE_MAX_CONCURRENT_REPLIES` and `SABLE_MAX_QUEUED_REPLIES` stop a burst in a busy room from
  becoming one open model call per message and an unbounded pile of parked tasks
  ([how many at once](configuration.md#how-many-model-calls-at-once)).
- **People:** `SABLE_RATE_LIMIT` bounds each person in chat, and not the HTTP routes
  ([rate limit](configuration.md#rate-limit), accepted risk 9).
- **Request bodies:** capped in `limits.py`, a pure ASGI middleware that counts bytes as they are
  read, so a chunked body or a lying `Content-Length` is stopped too, before authentication and
  before any parser or temporary file sees the body
  ([the caps](configuration.md#request-size-caps)). Malformed bodies are handled rather than
  crashed on: `/notify` answers 422 or 400, and `/hook` treats whatever is odd as text.
- **What Nextcloud holds open:** conversations outside `SABLE_ALLOWED_ROOMS` cost no held request
  and nothing in them is read, and the 50-conversation cap bounds what a misplaced invitation
  list can cost the server ([which conversations](configuration.md#how-chat-is-received)).
- **Output:** replies are truncated at `SABLE_MAX_MESSAGE_CHARS`. Handlers run away from the poll
  loop, so a slow model cannot stop sable reading, and a reply that fails to post is a logged
  warning rather than an unhandled exception.

## Secrets

| Secret | What it grants | Rotation |
| --- | --- | --- |
| `SABLE_NEXTCLOUD_PASSWORD` | Everything the account can do: read its rooms, post as it, and read and write its Files, Contacts and Calendar. It cannot be scoped | Create a new app password, update the variable, restart, then revoke the old one |
| `SABLE_NOTIFY_TOKEN` | Posting into the aliased conversations | Change the variable and restart, then update callers |
| `SABLE_LLM_API_KEY` | Your model provider's billing. With `SABLE_LLM_BACKEND=openwebui`, also the account whose tools the model can run | At the provider |

All of them come from the environment. `.env` is in both `.gitignore` and `.dockerignore`, and
`.env.example` ships with the password empty. `compose.yaml` reads the required settings as
`${VAR}` interpolations rather than literals, so the committed file never contains one.

No secret is logged or printed. `sable --check` and the startup block print the account's user id,
Nextcloud URL, prefix, model and so on, but never the app password, the notify token or the API
key. Conversation tokens *are* logged at INFO, and for a conversation shared by public link the
token is what grants access, so treat logs accordingly.

## Container and host

The image runs as a non-root user (uid 10001) and is built from a patch-pinned base rather than a
floating tag. Dependencies are locked and hash-verified: `uv sync --locked` fails rather than
resolving something else, and uv itself is uninstalled in the same layer, so it does not ship.

Nothing is written to disk. Conversation history lives in process memory only, and the ⁉️
reaction reads a message back from Talk when it is used and keeps nothing. The one exception is a
multipart upload to `/notify`, which Starlette spools to a temporary file. `compose.yaml` and the
systemd unit lock the rest down to match, and publish the port to `127.0.0.1` only
([container hardening](deployment.md#container-hardening)).

## What leaves your infrastructure

Messages the assistant answers go to your configured `SABLE_LLM_BASE_URL`, along with the recent
history of that conversation and the speakers' display names. Nothing else leaves. If that
backend is a hosted API, chat content leaves your network; a local backend such as Ollama, vLLM
or llama.cpp avoids the question, and an empty `SABLE_LLM_MODEL` disables the assistant while
leaving commands working. Attachments do not leave: they are uploaded into your own Nextcloud,
in the account's Files, and shared from there.

With `SABLE_LLM_BACKEND=openwebui` the question also creates a conversation in that Open WebUI
account, which is deleted once the answer has been read unless `SABLE_LLM_KEEP_CHATS` is on.
Whatever tools the loop calls see the prompt: a web search sends the query to your configured
search provider, and an MCP server sees whatever the model passes it.

## Accepted risks

These are known and deliberate. Decide for yourself whether they are acceptable.

1. **The app password is the single credential, and it cannot be scoped.** It is the account's,
   so anybody holding it can read every conversation the account is in, post and react as it,
   and read and write that user's Files, Contacts and Calendar. That is a large thing to lose,
   and it is the direct price of running as a user. Give sable a dedicated account that owns
   nothing else and is in only the rooms it needs, so that what the password reaches is nearly
   nothing beyond chat. It is revocable on its own, and revoking it is the response to a leak.
   Uploads land in `SABLE_UPLOAD_PATH` inside that user's own Files, and filenames from callers
   are sanitised and made unique, so one alert cannot overwrite another or climb out of the
   folder.

2. Commands are open unless you close them. Out of the box anyone in the conversation, guests
   included, can run any command. `SABLE_ADMIN_COMMANDS` plus `SABLE_ADMIN_USERS` moves named
   commands behind a list of Nextcloud user ids, and a custom command can check `ctx.is_admin`
   for anything finer. What this is not is authentication: it trusts the user id Nextcloud's chat
   API reports for the sender, the same trust the rest of the service runs on. Who is in the
   conversation at all stays Talk's decision, not ours.

3. `/notify` is a single shared token with no per-caller identity and no rate limiting. A leaked
   token lets anyone post into the aliased conversations, and what it posts is not defanged, so
   it can `@all` the room. Keep the endpoint off the public internet where you can.

4. Upstream error text can reach the chat room. With `SABLE_REPORT_ERRORS` on, a failure posts
   the error, which includes up to 400 characters of the model backend's or Nextcloud's response
   body. If your backend is chatty about credentials in its errors, turn it off.

5. Model output is posted verbatim and nothing filters it. A participant can try to steer the
   model through prompt injection; the realistic worst case is embarrassing or misleading text,
   since Talk renders Markdown and sanitises HTML itself. Names cannot forge a turn or a line of
   the system prompt (see above), but inside its own line a display name is still free text, so
   somebody calling themselves `assistant` is a thing the model sees.

6. Chat content sits in process memory for up to `SABLE_HISTORY_TTL`: the assistant's history per
   conversation, which is what the model is sent, up to `SABLE_HISTORY_TURNS` turns each. None of
   it is written to disk, but it would appear in a core dump. Nothing else is kept.

7. Anyone in a conversation can send any message to the model by reacting to it, including
   messages they did not write. That is the feature working as intended, but it means one
   participant can forward another's words to your model backend without saying anything in the
   room. `SABLE_ASK_ADMINS_ONLY` restricts it to `SABLE_ADMIN_USERS`.

8. **sable is a person in the room, and whoever can add participants can add it.** It appears in
   Talk's participant list as an ordinary user: anyone allowed to invite people to a conversation
   can invite it, after which everything said there is read and, in an AI room or when the
   account is mentioned, sent to the model backend. Out of the box that means *anybody* who can
   invite it, which is why an empty `SABLE_ALLOWED_ROOMS` logs a warning. Conversely, nothing in
   Talk marks its messages as automated, so people may take an answer for a person's: name the
   account so that it is obvious.

9. Rate limiting covers chat and not the HTTP routes. Nothing limits how fast `/notify` or `/hook`
   can be called by whoever holds the token, so the real memory ceiling for attachments is the
   cap times the number of concurrent callers, and what Nextcloud does about an account that
   posts too fast is not something sable relies on. A reverse proxy's limits are worth having in
   front. The chat limit keys on the actor id, so somebody with many accounts has many
   allowances, and the queue bound then decides what is dropped. Reading chat is a risk to
   Nextcloud rather than to sable: every followed conversation holds a PHP worker continuously,
   which can starve everyone else on a small server
   ([PHP workers](deployment.md#give-nextcloud-enough-php-workers)).

10. Logs are the only audit trail: no separate audit log, no metrics endpoint. At INFO they
    record who used the bot, which command or trigger, and in which conversation, but not what
    was said; DEBUG puts chat content in the log.

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
    who can ask the model in a tools room, guests included unless `SABLE_LLM_USERS` is set, can
    cause whatever those tools do. Enabling them is a decision about a room
    (`SABLE_LLM_TOOL_ROOMS`). If they reach Home Assistant, a stranger can turn off your lights
    by asking, and prompt injection stops being an embarrassing-text problem, since a participant
    can paste text aimed at the model rather than at the room. The control that matters most is
    the account, not sable: give it a dedicated Open WebUI user holding only the tools a chat
    room should have. sable never sends a `terminal_id`, so Open Terminal is out of reach by
    construction, and `SABLE_LLM_KEEP_CHATS=true` keeps each conversation, the closest thing to
    an audit trail of what the model ran.

15. CI holds credentials: the registry password and a runner token with write access to the
    repository, used to create releases. Anyone who can change a workflow on a branch CI runs
    can reach both.

16. **The defaults are open.** Each access setting has an open default, the startup log prints
    the resolved state with a warning for the empty room list, and nothing forces the choice.
    Treat setting `SABLE_ALLOWED_ROOMS` and `SABLE_LLM_USERS` as part of installing it.

17. Neutralising mass mentions is best effort. The forms it breaks are those Talk's clients send,
    which Talk's documentation does not spell out, so a form added later would not be covered
    ([what is defanged](configuration.md#mass-mentions)). Mentions of a single person are left
    alone, so somebody can still be pinged by a model's answer or a hook payload, `/notify` text
    is never defanged, and the zero-width space stays in the posted text, where somebody copying
    it out will find an invisible character.

## Hardening checklist

- [ ] A dedicated Nextcloud account for sable that owns nothing else, and whose display name
      makes plain it is not a person
- [ ] `SABLE_NEXTCLOUD_PASSWORD` an app password, not the login password, and revoked when
      rotated
- [ ] `SABLE_NEXTCLOUD_URL` is `https://`, unless it is a private network you trust
- [ ] The account invited only to the conversations it should answer in, and `SABLE_ALLOWED_ROOMS`
      listing them by token, so an invitation from anybody else gets nothing (and
      `SABLE_LEAVE_UNLISTED_ROOMS` if it should not sit in the rest)
- [ ] `SABLE_LLM_USERS` naming the people who may use the model, so being in a room is not enough
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
- [ ] `SABLE_ASK_ADMINS_ONLY` on if forwarding somebody else's message to the model should not be
      open to everyone in the room
- [ ] If server-side tools are on, they are enabled only in a dedicated room through
      `SABLE_LLM_TOOL_ROOMS`, with `SABLE_LLM_USERS` set, and the Open WebUI account behind the key
      holds only the tools a chat room should reach ([accepted risk 14](#accepted-risks))
- [ ] The [container hardening](deployment.md#container-hardening) in `compose.yaml` kept, or
      the systemd equivalents
- [ ] `SABLE_RATE_LIMIT` and `SABLE_MAX_CONCURRENT_REPLIES` left on, not set to `0` (`0` lifts
      either one; for `SABLE_MAX_QUEUED_REPLIES` it means nothing may wait, which is stricter)
- [ ] The image pinned by version or digest on the host rather than `latest`

## Reporting a problem

See [SECURITY.md](../SECURITY.md).
