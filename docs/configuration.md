# Configuration

Every setting is an environment variable prefixed `SABLE_`. There is no configuration file
format to learn. [`.env.example`](../.env.example) is a copy-ready version of the defaults below.

## How settings are loaded

sable reads `./.env` if it is there, or whatever `--env-file` points at, and silently skips a
path that does not exist. Real environment variables always win over the file, which is what
makes Compose overrides and a one-off `SABLE_LOG_LEVEL=DEBUG sable` work. `--host` and `--port`
on the command line override their variables in turn.

Under Docker Compose, [`compose.yaml`](../compose.yaml) carries the same list in its
`environment:` block, every variable commented out with its default and only the three required
Nextcloud account settings active. Those entries override the `.env` file, which is optional
there; secrets are written as `${VAR}` lookups so their values stay out of the committed file.

Check the result before deploying. This validates everything and exits without starting a
server:

```bash
sable --check
```

```
sable 0.8 config OK
  nextcloud:  https://cloud.example.org
  account:    sable (password set)
  polling:    30s long polls, rooms rescanned every 60s
  prefix:     !
  model:      gpt-4o-mini @ https://api.openai.com/v1
  ai rooms:   *
  admin cmds: reset for maser
  notify:     enabled aliases: alerts, deploys
warning: SABLE_ALLOWED_ROOMS is empty, so sable follows every conversation it is in, and any user who can invite the account into one can use it. List the conversation tokens it should serve.
```

The `warning:` lines are the [startup warnings](#startup-warnings), printed here too so a deploy
check shows them; there are none when the configuration raises no doubts.

A bad value exits with status 2 and a message naming the variable. Configuration errors are
fatal at startup by design: a failed deploy is better than a bot that silently ignores half its
settings. The startup log then repeats the resolved configuration, in more detail than `--check`
covers (it shows the allowed rooms, who may use the model, the rate limit and where tools are
on, none of which `--check` does), so the running process tells you what it actually believes.
Settings that are probably a mistake but that sable cannot rule out are not errors; they are
[warnings](#startup-warnings), logged once after that block.

A variable that sable no longer reads is ignored without a word, so an old `.env` keeps
starting. The message cache and its two settings were removed that way (the ⁉️ reaction now
[reads the message back from Talk](#asking-about-a-message-by-reacting-to-it)).

### Value formats

| Kind | Accepted |
| --- | --- |
| Boolean | `1`, `true`, `yes`, `on`, or `0`, `false`, `no`, `off`, case-insensitive. Anything else is an error. |
| Number | Plain integer or decimal. Empty means "use the default". |
| List | Comma-separated; whitespace around entries is trimmed. |
| Map | `alias=value,other=value2`, or a JSON object: `{"alias": "value"}`. |
| JSON | A JSON **object**, e.g. `{"top_k": 40}`. |

Values are trimmed and URLs have trailing slashes stripped, so a stray space or slash in a
`.env` file will not break anything.

## The Nextcloud account

sable is an ordinary Nextcloud user. It reads chat by long-polling the Talk chat API as that
user, and posts, reacts and uploads files as the same user. There is no bot to register, no
webhook for Nextcloud to call and no shared secret: all it needs is an account, an app password,
and an invitation to the conversations it should be in.

| Variable | Default | Notes |
| --- | --- | --- |
| `SABLE_NEXTCLOUD_URL` | **required** | Your Nextcloud base URL, no trailing slash, e.g. `https://cloud.example.org`. Plain `http://` to a host that is not local is accepted with a startup warning, because the password crosses the network on every request. |
| `SABLE_NEXTCLOUD_USER` | **required** | The account's user id. It is also the name people `@`-mention to address sable. |
| `SABLE_NEXTCLOUD_PASSWORD` | **required** | An **app password** for that user, from Settings → Security → Devices & sessions. Not the login password: an app password can be revoked on its own without locking the account. |
| `SABLE_POLL_TIMEOUT` | `30` | Seconds each long poll for new messages may wait. Talk holds one for at most 60, so a larger value is clamped to 60 with a startup warning. Less than 1 is a startup error. |
| `SABLE_ROOM_REFRESH` | `60` | Seconds between looks at which conversations the account is in. Less than 5 is a startup error. |

Give it an account of its own that owns nothing else. An app password cannot be scoped to chat:
it reaches everything that user can, across Files, Contacts and Calendar. [security.md](security.md)
has the rest.

### How chat is received

Talk has no single feed of new messages across conversations, so sable keeps one long poll open
per conversation the account is in — `GET /chat/{token}?lookIntoFuture=1` — and hands each new
message to the same handler every trigger goes through. A poll that finds nothing returns after
`SABLE_POLL_TIMEOUT` seconds and is asked again.

The conversation list is fetched every `SABLE_ROOM_REFRESH` seconds. A conversation the account
was invited to is followed from its newest message on, so nothing said before sable noticed it is
replayed; one it left, or that was deleted, is dropped. Expect up to that long between being
invited and being heard. Conversations nobody addresses a bot in are skipped, since each one
would cost a held request for nothing: the "Talk updates" changelog, a former one-to-one whose
other person has gone, the account's own "Note to self", and Talk's "Let's get started!"
sample conversation. So is every conversation outside [`SABLE_ALLOWED_ROOMS`](#rooms-are-named-by-token),
when that is set: it costs no held request and nothing in it is read.

**The cost is a held request.** Each long poll occupies a request slot on your Nextcloud server
for up to `SABLE_POLL_TIMEOUT` seconds, and there is one per conversation. On PHP-FPM that is a
worker kept busy doing nothing, and the stock PHP-FPM pool allows only **five** of them at
once. Raise `pm.max_children` first; [deployment.md](deployment.md#give-nextcloud-enough-php-workers)
says how. sable therefore follows at most **50** conversations, the most
recently active, and logs a warning once when an account is in more. A dedicated account that is
only in the rooms it needs stays far below that. Raising `SABLE_POLL_TIMEOUT` towards 60 means
fewer, longer requests; lowering it means more, shorter ones. Neither changes how quickly a
message is seen, since a poll returns the moment one arrives.

What that buys is direction: sable needs to reach Nextcloud, and Nextcloud never needs to reach
sable.

#### The Talk calls it makes

All of them are checked against the [Nextcloud Talk API documentation](https://nextcloud-talk.readthedocs.io/en/latest/),
and `Agents/API/talk-user-api-reference.md` in the repository keeps the details and the list of
what is still unverified. Note that **conversations and chat live under different API versions**.

| Call | Endpoint | Documented at |
| --- | --- | --- |
| Who am I | `GET /ocs/v2.php/cloud/user` | Nextcloud OCS |
| List conversations | `GET /ocs/v2.php/apps/spreed/api/v4/room` (`noStatusUpdate=1`) | [conversation](https://nextcloud-talk.readthedocs.io/en/latest/conversation/) |
| Wait for messages | `GET /ocs/v2.php/apps/spreed/api/v1/chat/{token}` with `lookIntoFuture=1`, `timeout`, `lastKnownMessageId`, `setReadMarker=0`, `noStatusUpdate=1`; 304 means nothing new | [chat](https://nextcloud-talk.readthedocs.io/en/latest/chat/) |
| Post | `POST .../api/v1/chat/{token}` | [chat](https://nextcloud-talk.readthedocs.io/en/latest/chat/) |
| React, un-react | `POST` and `DELETE .../api/v1/reaction/{token}/{messageId}`, emoji in the body | [reaction](https://nextcloud-talk.readthedocs.io/en/latest/reaction/) |
| Read one message back (the ⁉️ reaction) | `GET .../api/v1/chat/{token}/{messageId}/context` with `limit=1`; needs the `chat-get-context` capability. Talk has no single-message endpoint, so the message is picked out of the answer by id | [chat](https://nextcloud-talk.readthedocs.io/en/latest/chat/) |
| Leave a conversation (`SABLE_LEAVE_UNLISTED_ROOMS`) | `DELETE /ocs/v2.php/apps/spreed/api/v4/room/{token}/participants/self` | [participant](https://nextcloud-talk.readthedocs.io/en/latest/participant/) |
| Attach a file | WebDAV `PUT`, then `POST /ocs/v2.php/apps/files_sharing/api/v1/shares` with `shareType=10` | [chat](https://nextcloud-talk.readthedocs.io/en/latest/chat/) |

`noStatusUpdate=1` keeps sable's polling from marking its account as online, and `setReadMarker=0`
keeps it from marking conversations as read. Reacting needs the account to hold the reaction
permission in that conversation, or Talk answers 403; a failed reaction is logged and ignored.

## Chat behaviour

| Variable | Default | Notes |
| --- | --- | --- |
| `SABLE_COMMAND_PREFIX` | `!` | Any string. `/` is a reasonable alternative; note Talk itself uses `/` for some client-side commands. |
| `SABLE_ALLOWED_ROOMS` | *(empty)* | The conversations sable serves, as comma-separated conversation **tokens**. **Empty means every conversation the account is in**, with a startup warning, because then anybody who can invite the account can use it. Rooms not listed are never polled or answered in. `*` and anything that is not a token is a startup error. See [below](#rooms-are-named-by-token). |
| `SABLE_LEAVE_UNLISTED_ROOMS` | `false` | With `SABLE_ALLOWED_ROOMS` set, leave the group and public conversations that are neither listed nor a `/notify` or `/hook` destination. Has no effect (and warns) without an allow-list. See [below](#leaving-the-rooms-nobody-listed). |
| `SABLE_AI_ROOMS` | *(empty)* | Conversations where **every** message goes to the model, no mention needed. Comma-separated conversation **tokens**, or `*` for every allowed room. A display name is a startup error. Empty means mentions and `!ai` only. |
| `SABLE_LLM_USERS` | *(empty)* | Nextcloud user ids allowed to make the model answer; the `SABLE_ADMIN_USERS` are always included. **Empty means everyone.** See [below](#who-may-use-the-model). |
| `SABLE_RATE_LIMIT` | `20` | Triggers one person may set off per minute; the rest are ignored. `0` turns it off, a negative value is a startup error. See [below](#rate-limit). |
| `SABLE_REPLY_AS_REPLY` | `false` | Post answers as threaded replies to the triggering message instead of plain messages. |
| `SABLE_THINKING_REACTION` | *(empty)* | A single emoji stuck on the triggering message while the model works, then removed — e.g. `👀`. Empty disables it, which saves two API calls per answer. Failures here are ignored; a reaction is never load-bearing. |
| `SABLE_ASK_REACTION` | `⁉️` | React to any message with this and the bot sends that message to the model, answering in a reply threaded under it. The message is read back from Talk, so nothing is remembered. Empty disables the feature. |
| `SABLE_ASK_ADMINS_ONLY` | `false` | Restrict that reaction to `SABLE_ADMIN_USERS`. On with an empty `SABLE_ADMIN_USERS` is a startup error, since nobody could then use it. See [below](#restricting-the-reaction). |
| `SABLE_UNKNOWN_COMMAND_HINT` | `true` | Reply "I have no `!foo` command" on an unknown command. Turn off in busy rooms where people use other bots with the same prefix. |
| `SABLE_REPORT_ERRORS` | `true` | Post failures into the conversation as well as logging them; the reply is prefixed with a warning sign. Off means failures are logged only and the room stays quiet. |
| `SABLE_STARTUP_CHECK` | `true` | Sign in to Nextcloud at startup (`cloud/user`) and log who it says the account is, so a wrong URL, an untrusted certificate or a rejected app password shows up at boot. Never fatal. |
| `SABLE_IGNORE_USERS` | *(empty)* | Users to ignore completely. Comma-separated; each entry matches a bare user id (`alice`), a full actor id (`users/alice`), or a display name. See [below](#ignoring-people). |
| `SABLE_ADMIN_COMMANDS` | *(empty)* | Commands only `SABLE_ADMIN_USERS` may run. Comma-separated, or `*` for all of them. Empty means every command is open to everyone. See [below](#who-may-run-which-command). |
| `SABLE_NORMAL_COMMANDS` | *(empty)* | The exceptions to `SABLE_ADMIN_COMMANDS=*`. Redundant otherwise, since anything not named as an admin command is open already. |
| `SABLE_ADMIN_USERS` | *(empty)* | Nextcloud user ids that may run the admin commands, comma-separated. **Required** once `SABLE_ADMIN_COMMANDS` is set, or nobody could run them. |
| `SABLE_MAX_MESSAGE_CHARS` | `30000` | Replies longer than this are clipped with a `_[truncated]_` marker. Talk hard-rejects anything over 32000 with HTTP 413, which is the real ceiling. |
| `SABLE_MAX_CONCURRENT_REPLIES` | `8` | Model calls allowed to be in flight at once; `0` lifts the ceiling. A negative value is a startup error. See [below](#how-many-model-calls-at-once). |
| `SABLE_MAX_QUEUED_REPLIES` | `20` | Replies allowed to wait for a slot once the ceiling is reached; beyond that new work is dropped. `0` lets none wait. A negative value is a startup error. See [below](#how-many-model-calls-at-once). |

### How the access layers combine

Several settings decide who gets what out of the bot. They are independent, each narrows the one
before it, and every default is on the open side: install sable, invite it, and everyone in the
conversation can run every command and ask the model anything, in every room it is in, with
tools off. An event meets them in this order:

| # | Question | Setting | Default |
| --- | --- | --- | --- |
| 1 | Is this a conversation sable serves? | `SABLE_ALLOWED_ROOMS` | Every room it is in (warns). Outside the list nothing is read at all |
| 2 | Is the sender ignored? | `SABLE_IGNORE_USERS` | Nobody |
| 3 | Is it addressed to sable? | The prefix, a mention, `SABLE_AI_ROOMS`, the ⁉️ reaction | Ordinary chatter is not, and costs nothing |
| 4 | Has the sender set off too many triggers? | `SABLE_RATE_LIMIT` | 20 a minute each, administrators included |
| 5 | May they run this command? | `SABLE_ADMIN_COMMANDS`, `SABLE_ADMIN_USERS` | Every command is open |
| 6 | May they make the model answer? | `SABLE_LLM_USERS` | Everyone |
| 7 | Is this a room where the model may use tools? | `SABLE_LLM_TOOL_ROOMS` | Nowhere |

What each one does not do is as important. Row 5 gates commands and nothing else: the model
answers a mention, an AI room message or a reaction whatever it says, which is row 6's job. Row 6
gates the model and nothing else: `!ping` still works for somebody it refuses. Row 7 is about the
room, not the person, so on its own it lets anyone in a tools room set the tools off; pair it with
row 6 (the startup warning says so). And `SABLE_ASK_ADMINS_ONLY` narrows the ⁉️ reaction further
still, to the administrators.

A trigger counts against row 4 before rows 5 and 6 are asked, so somebody refused by either
still uses up their allowance.

### When does the assistant answer?

| The message | Answers? |
| --- | --- |
| `!ping` | Command, always (unless named in `SABLE_ADMIN_COMMANDS`) |
| `@sable how are you`, picked from Talk's mention list | Assistant. A real mention of the account's user id is what counts |
| `sable: how are you`, or the id or display name typed at the start | Assistant |
| `hey @sable look at this` (mention mid-sentence) | Assistant, with the whole message as the prompt |
| `!ai how are you` | Assistant, no mention needed |
| `sabletooth tigers` | No — mention matching respects word boundaries |
| `just chatting` | Only in a conversation listed in `SABLE_AI_ROOMS` |
| Anything from sable itself, or from another bot | Never |
| A ⁉️ reaction on any message | Assistant, answering that message |
| Any of the above, in a conversation outside `SABLE_ALLOWED_ROOMS` | Never: it is not read |
| Any of the above that asks the model, from somebody outside `SABLE_LLM_USERS` | No: "You are not allowed to use the assistant." to a mention or `!ai`, silence to the rest |

### Rooms are named by token

`SABLE_ALLOWED_ROOMS`, `SABLE_AI_ROOMS` and `SABLE_LLM_TOOL_ROOMS` all take conversation
**tokens**, and `SABLE_AI_ROOMS` and `SABLE_LLM_TOOL_ROOMS` also take `*` for every allowed room:

```ini
SABLE_ALLOWED_ROOMS=a1b2c3d4,e5f6g7h8     # the rooms sable serves
SABLE_AI_ROOMS=a1b2c3d4                   # one of them answers everything
SABLE_AI_ROOMS=*                          # or all of them do
```

The token is the last segment of the conversation's URL —
`https://cloud.example.org/call/a1b2c3d4` → `a1b2c3d4` — and it is lowercase letters and digits.

Names are refused, with a startup error that says why: anybody who can create a conversation can
call it whatever they like, so a name that matched would let a stranger invite the account to a
room named after yours and have every message in it answered, tools included. A token cannot be
chosen by the other party. (`SABLE_NOTIFY_ROOMS` aliases are a different thing: they are names
your own callers use for tokens you set, and are unchanged.)

An allow-list is the one setting here with a cost to leaving it empty, so an empty one logs a
warning at startup. Entries in `SABLE_AI_ROOMS` or `SABLE_LLM_TOOL_ROOMS` that the allow-list does
not contain log another, because sable never reads those conversations and the entry does nothing.
`/notify` and `/hook` destinations need not be listed: they post without following the
conversation.

`SABLE_LOG_LEVEL=DEBUG` prints both identifiers for every message sable decided to ignore, so you
can find the token of a room you want:

```
message in a1b2c3d4 ('AI') was not for me - no prefix, no mention, and not an AI room
```

#### Leaving the rooms nobody listed

`SABLE_ALLOWED_ROOMS` stops sable acting in a room, but the account is still *in* it, still
visible in its participant list and still a member somebody could mistake for a person.
`SABLE_LEAVE_UNLISTED_ROOMS=true` makes the account leave: each scan, up to five group or public
conversations that are neither allowed nor a `/notify` or `/hook` destination. One-to-one
conversations and other types are never left. Leaving is `DELETE .../participants/self`, and it
cannot be undone from here: sable has to be invited back.

Nothing is left in a scan unless at least one allowed room is in the list Talk returned, so a
mistyped token in `SABLE_ALLOWED_ROOMS` cannot make the account walk out of every other group.
When that happens leaving is suspended and the log says so once; it resumes by itself when an
allowed room is visible again.

If Talk refuses (400 or 403, usually because the account is the room's only moderator or owner)
that is logged once and not retried; a 404 means it was already gone. Without an allow-list the
setting does nothing and warns, since there is nothing to call unlisted. Turn it on only after
checking the list: a mistyped token costs the rooms it should have contained.

### Asking about a message by reacting to it

React with `SABLE_ASK_REACTION` (⁉️ by default) and the bot answers the message you reacted to,
in a reply threaded under it. It works on anyone's message, the bot's own answers included, which
makes it a quick way to ask a follow-up.

A reaction arrives as a system message that names the message reacted to by id and does not carry
its text, so sable reads that message back from Talk with
`GET /chat/{token}/{messageId}/context` (capability `chat-get-context`). Nothing is remembered
between reactions: the feature works on old messages and across restarts, and there is no cache
of chat content. The cost is one call per reaction. The reaction replies, threaded under the
message, are:

| What sable finds | What it does |
| --- | --- |
| The message, with text | Sends it to the model, naming the author and who asked |
| Talk's 404: the message is gone, or never was | "I cannot find that message - it may have been deleted." |
| A message that was deleted | "That message has been deleted." |
| A message with no text | "That message has no text for me to read." |
| A system message (a join, a rename) | Nothing |
| The author is in `SABLE_IGNORE_USERS` | Nothing, so ignored people stay unreadable by somebody else's reaction |
| The call fails | Logged, and reported to the room unless `SABLE_REPORT_ERRORS` is off |

The asker has to pass the same checks as any other trigger: the room allow-list, the rate limit,
`SABLE_ASK_ADMINS_ONLY` if it is on, and `SABLE_LLM_USERS`. Refusals here are logged and say
nothing in the room, since the message belongs to somebody who did nothing.

This relies on Talk delivering reaction events through the same chat poll as messages, which
it does: the feature has been confirmed against a live server. Two things have not been
exercised: a reaction being taken back (see [future.md](future.md#talk-features-not-yet-used)),
and whether the context call with `limit=1` includes the message itself, which the Talk
documentation does not say outright; sable picks the message out of the answer by id and treats a
missing one as not found.

### Restricting the reaction

`SABLE_ASK_ADMINS_ONLY=true` lets only `SABLE_ADMIN_USERS` trigger it. The reaction works on
anybody's message, which is what makes it useful and also means one participant can forward
another person's words to your model backend without saying anything in the room — [accepted risk
7](security.md#accepted-risks). Restricting it to the people you already trust with the admin
commands is the answer where that matters. Turning it on with an empty `SABLE_ADMIN_USERS` is a
startup error, the same as `SABLE_ADMIN_COMMANDS` with nobody to run them: it would leave the
feature usable by no one at all, which is a misconfiguration rather than a thorough way of
switching it off. To switch it off, empty `SABLE_ASK_REACTION`. To let only some people use it
without making them administrators, use [`SABLE_LLM_USERS`](#who-may-use-the-model).

### Who may use the model

`SABLE_LLM_USERS` lists the Nextcloud user ids that may make the model answer. The
`SABLE_ADMIN_USERS` are always included, and **empty means everyone**.

```ini
SABLE_LLM_USERS=alice,bob       # these two and the administrators
```

It covers every way to the model: a mention, a message in an AI room, `!ai`, the ⁉️ reaction, and
any custom command that calls the model. Matching is on the user id only, never a display name
(anybody can set theirs to yours), and `users/alice` works as well as `alice`. A guest, a
federated user or a bot has no user id and so never matches once the list is set.

What somebody outside the list sees depends on whether they spoke to sable:

- a mention, `!ai`, or a custom command that reaches the model: "You are not allowed to use the
  assistant.", once, in the room;
- a plain message in an AI room, or a ⁉️ reaction: nothing, only an INFO line in the log. They did
  not address sable, so a refusal to every line they typed in that room would be sable talking over
  them.

The rest of the bot is unaffected for them: they can still run `!ping`.

When tools are on in some rooms (see [below](#letting-the-model-use-tools)) and this list is
empty, sable warns at startup that anybody in those rooms can set them off.

### How many model calls at once

`SABLE_MAX_CONCURRENT_REPLIES` caps how many completions may be in flight together. Each trigger
— a mention, an AI room, `!ai`, a reaction — is handled in a background task, and those tasks have
no ceiling of their own, so a busy conversation or a burst of messages produces as
many simultaneous model calls as there were events, each holding `SABLE_LLM_TIMEOUT` seconds open.
The default of `8` is a ceiling rather than a target, and most deployments never reach it.

```ini
SABLE_MAX_CONCURRENT_REPLIES=1     # a local model that serves one request at a time
SABLE_MAX_CONCURRENT_REPLIES=32    # a hosted backend with headroom
SABLE_MAX_CONCURRENT_REPLIES=0     # no ceiling, which is how it behaved before this setting
```

Past the ceiling a reply **waits for a slot**, but only so many may wait:
`SABLE_MAX_QUEUED_REPLIES` (default `20`). Only messages that would actually do something take a
slot or a place in the queue: ordinary chatter, the account's own messages and other bots are
filtered out first, so they cannot crowd out a real question. When that many are already running
or waiting, new work
is **dropped**: nobody is told in the room, and the log carries a warning at most once every 30
seconds with a running count. Waiting is bounded because an unbounded pile of parked tasks is the
same flood the ceiling was meant to stop, only cheaper. `0` means nothing may wait, so with every
slot busy the next reply is dropped; the setting is irrelevant when the ceiling is `0`. A
negative value for either is a startup error.

Raising `SABLE_LLM_TIMEOUT` and lowering the ceiling at the same time can leave somebody waiting a
long while, and then a full queue drops what comes after.

### Rate limit

`SABLE_RATE_LIMIT` caps how many triggers one person may set off per minute, 20 by default. A
trigger is a command (an unknown one included), a mention, a message in an AI room, or a ⁉️
reaction; ordinary chatter that is none of these does not count. It is a sliding 60-second window
per actor, in memory, and administrators are counted like anybody else.

Over the limit a trigger is ignored, with one WARNING per person per window naming who and the
setting. A refused trigger does not extend the lockout: somebody who stops is let back in a window
after their last accepted one. `0` turns it off and a negative value is a startup error.

It limits people, not the HTTP surface: `/notify` and `/hook` have their own tokens and no rate
limit at all ([accepted risk 9](security.md#accepted-risks)).

### Mass mentions

Anything sable posts in answer to chat — a model's answer, a command's reply, an error report —
and everything a `/hook` renders has `@all`, `@"group/..."` and `@"team/..."` defanged: a
zero-width space goes after the `@`, so the text reads the same and no longer notifies the whole
room or a whole group. Mentions of one person are left alone, and so is `/notify`, whose caller
is trusted and may mean it. This follows the forms Talk's clients send; Talk's documentation does
not spell out `@all` or the team form.

## Ignoring people

`SABLE_IGNORE_USERS` drops everything from the listed actors: commands, mentions and reactions
alike. Ignore means ignore, so their words do not reach the model even when somebody else asks
about them: a ⁉️ on an ignored person's message is answered with nothing.

```ini
SABLE_IGNORE_USERS=alice                    # bare user id
SABLE_IGNORE_USERS=users/alice              # the full actor id, as the log prints it
SABLE_IGNORE_USERS=Alice                    # display name, case-insensitive
SABLE_IGNORE_USERS=noisy-integration,users/bob,guests/abc123
```

Prefer ids here too. A display name can be changed by the person themselves, which would quietly
stop them being ignored, the opposite of what you configured.

## Who may run which command

By default every command is open to everybody in the conversation, guests included. That is right
for `!ping` and wrong for anything that touches something outside the chat, so commands can be
moved behind a list of administrators.

```ini
SABLE_ADMIN_COMMANDS=reset,ai      # these two need an admin
SABLE_ADMIN_USERS=maser,korren     # and these are the admins
```

Everything not named stays open. The other direction is `*`, which closes everything and lets
`SABLE_NORMAL_COMMANDS` name what stays open — the safer shape if you are adding commands with
side effects and would rather forget to open one than forget to close one:

```ini
SABLE_ADMIN_COMMANDS=*
SABLE_NORMAL_COMMANDS=help,ping,whoami
SABLE_ADMIN_USERS=maser
```

An admin is matched on their **user id** and nothing else. `maser` and `users/maser` both work,
case-insensitively. A display name is deliberately not accepted, unlike `SABLE_IGNORE_USERS`:
anybody who can join a conversation can set their display name to yours, and for an ignore list
that costs you an ignore while here it would cost you the commands. Guests and bots have no user
id, so they are never administrators.

Restricting a command restricts its aliases too — `reset` covers `!forget` — and naming an
alias restricts the command behind it. `!help` lists only what the asker can run (so `!ai` and
the "mention me" line are left out for somebody outside `SABLE_LLM_USERS`), marking the rest
`(admin)` for those who can; `!help reset` says who it is for, and running a command you may not
answers "`!reset` is for administrators only." and logs a warning naming you. Nothing is hidden,
in other words; the list is just tailored.

Two combinations are refused at startup: a command in both lists, since who may run it is then
undecided, and `SABLE_ADMIN_COMMANDS` with an empty `SABLE_ADMIN_USERS`, which would leave those
commands runnable by nobody at all.

One thing this does **not** do: restricting `ai` restricts the `!ai` command and nothing else.
Mentioning the bot, a conversation listed in `SABLE_AI_ROOMS`, and `SABLE_ASK_REACTION` all still
reach the model, because none of them is a command. To keep people away from the model itself,
that is [`SABLE_LLM_USERS`](#who-may-use-the-model), which covers every way to it, or
`SABLE_IGNORE_USERS`, an empty `SABLE_LLM_MODEL`, or not putting the bot in the conversation.

Setting `SABLE_ADMIN_USERS` alone is allowed and gates nothing — useful because a custom command
can ask `ctx.is_admin` for itself, which is the hook for anything these two lists cannot express.

## Conversation memory

| Variable | Default | Notes |
| --- | --- | --- |
| `SABLE_HISTORY_TURNS` | `12` | Turns kept per conversation; a turn is a question and its answer, so this holds 24 messages. |
| `SABLE_HISTORY_TTL` | `3600` | Seconds before a message ages out, so a room picked up tomorrow does not resume yesterday's thread. `0` disables expiry. |

History is per-conversation, in-process and lost on restart, and `!reset` clears one
conversation. It is a cache, not a record. Raising `SABLE_HISTORY_TURNS` costs tokens on every
request, since the whole window is sent each time.

## The model

Any endpoint that implements OpenAI's `POST /chat/completions` works.

| Variable | Default | Notes |
| --- | --- | --- |
| `SABLE_LLM_BASE_URL` | `https://api.openai.com/v1` | Base URL **including** the version segment; `/chat/completions` is appended. |
| `SABLE_LLM_API_KEY` | *(empty)* | Sent as `Authorization: Bearer …`. Omit for a local backend that wants no auth — the header is then not sent at all. |
| `SABLE_LLM_MODEL` | *(empty)* | **Empty disables the assistant entirely**: commands still work, no model is ever called, and mentions are ignored. |
| `SABLE_LLM_SYSTEM_PROMPT` | *(a short default)* | The system message. The conversation's name and the current date and time are appended automatically. |
| `SABLE_LLM_TEMPERATURE` | *(unset)* | Omitted from the request when unset, letting the backend's own default apply. Some newer models reject an explicit temperature. |
| `SABLE_LLM_MAX_TOKENS` | *(unset)* | Sent as `max_tokens`. Also omitted when unset. |
| `SABLE_LLM_TIMEOUT` | `120` | Seconds to wait for a completion. On timeout the room gets an error message (if `SABLE_REPORT_ERRORS` is on) rather than silence. |
| `SABLE_LLM_EXTRA_BODY` | `{}` | A JSON object merged into the request body **last**, so it overrides everything above. The escape hatch for provider-specific fields. |

### Provider recipes

| Backend | `SABLE_LLM_BASE_URL` | `SABLE_LLM_MODEL` | Key |
| --- | --- | --- | --- |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` | required |
| Ollama (local) | `http://localhost:11434/v1` | `llama3.1:8b` | not needed |
| vLLM | `http://localhost:8000/v1` | the served model name | as configured |
| llama.cpp server | `http://localhost:8080/v1` | any | not needed |
| LiteLLM proxy | `http://localhost:4000/v1` | whatever the proxy routes | proxy key |
| OpenRouter | `https://openrouter.ai/api/v1` | `anthropic/claude-sonnet-4.5` | required |
| Groq | `https://api.groq.com/openai/v1` | `llama-3.3-70b-versatile` | required |
| Together | `https://api.together.xyz/v1` | `meta-llama/Llama-3.3-70B-Instruct-Turbo` | required |

Azure OpenAI does not use the same URL shape; put LiteLLM (or any gateway) in front of it and
point sable at the gateway.

Reasoning models that spend their whole budget before answering would otherwise return an empty
message; sable falls back to `reasoning_content` when a provider supplies it, and reports a
clear error naming `finish_reason` when there is nothing at all. If answers come back
truncated, raise `SABLE_LLM_MAX_TOKENS` or lower the reasoning effort via
`SABLE_LLM_EXTRA_BODY`.

`SABLE_LLM_EXTRA_BODY` may not set `stream` or `messages`: sable builds both, and overriding
`stream` leaves the client parsing an event stream as JSON. It may not set `tool_ids`, `features`,
`tool_servers`, `terminal_id` or `session_id` either; see [below](#letting-the-model-use-tools).
Each is a startup error. The check is on top-level keys only: the rest of that object is yours,
so do not put anything there that switches tools on in a nested field.

## Letting the model use tools

A model offered tools does not run them. It replies asking for one to be called, and something
has to execute it and hand the result back. sable makes one request and posts one answer, so a
reply carrying only a tool call becomes an error naming the tool nobody ran.

`SABLE_LLM_BACKEND=openwebui` hands the loop to Open WebUI, which executes tools itself.

| Variable | Default | Notes |
| --- | --- | --- |
| `SABLE_LLM_BACKEND` | `openai` | `openai` is one request against anything speaking chat completions. `openwebui` runs Open WebUI's agentic loop. |
| `SABLE_LLM_TOOL_IDS` | *(empty)* | Workspace tools and MCP servers, as Open WebUI names them: `server:mcp:1,my_tool`. `GET /api/v1/tools/` lists the workspace ones. |
| `SABLE_LLM_FEATURES` | *(empty)* | Open WebUI's built-ins: any of `web_search`, `code_interpreter`, `image_generation`, `memory`. |
| `SABLE_LLM_TOOL_ROOMS` | *(empty)* | The conversations (tokens, or `*`) where the model may be offered the tools and features above. **Empty means nowhere**: tools are off everywhere until a room is named. |
| `SABLE_LLM_BUILTIN_TOOLS` | `true` | Sends a session id, which is what makes those built-ins available. `false` blocks on one request instead of polling, and gets no built-ins. |
| `SABLE_LLM_POLL_INTERVAL` | `2.0` | Seconds between checks while the loop runs. |
| `SABLE_LLM_KEEP_CHATS` | `false` | Keep the conversation each question creates, instead of deleting it. |
| `SABLE_LLM_SHOW_SOURCES` | `false` | Append what the answer cited — how you notice it came from an encyclopaedia rather than today's market. |

`SABLE_LLM_API_KEY` is required here, because the key is the account the tools run as. It, the
backend name and every feature name are checked at startup. `SABLE_LLM_BASE_URL` should end in
`/api`; one that does not gets a warning rather than a refusal, since a proxy may be rewriting
the path.

Open WebUI's loop lives in the code that streams events into a chat, so it runs only for a
request naming a chat and a message inside it, with `stream: true`, and writes the answer there
rather than returning it. Each question is therefore four calls — create a conversation, start
the completion, wait for the tasks to drain, read the message — and sable deletes the
conversation afterwards. Your history is unaffected; sable keeps that itself.

Expect it to be slower, since a tool round is a second model call with the results in the
prompt. Raise `SABLE_LLM_TIMEOUT` to 300 or so and set `SABLE_THINKING_REACTION`.

Three things in Open WebUI decide whether it works at all. The model needs **Native** function
calling. Its *Stream Chat Response* parameter must not be off, because it overrides the request
and then nothing runs — which sable reports as a loop that finished without an answer. And an
OAuth-protected MCP server has to be authorised once in the browser as that user.

**Tools are a decision about a room.** `SABLE_LLM_TOOL_IDS` and `SABLE_LLM_FEATURES` say what is
available; `SABLE_LLM_TOOL_ROOMS` says where. Anywhere else, in every conversation not listed,
the model is called with no tools and no built-ins at all, and a question there is answered from
what the model knows. Such a room uses the same blocking request as
`SABLE_LLM_BUILTIN_TOOLS=false`, with no session id, because a session is what lets Open WebUI
offer its built-ins (knowledge, files, notes, channels, calendar) that need no flag; a room
outside `SABLE_LLM_TOOL_ROOMS` therefore never gets them either. Empty means nowhere, so configuring tools and forgetting the rooms leaves
them off, and sable warns about exactly that at startup. Put the tools in a dedicated room whose
membership you control, not in every room the account happens to be in.

```ini
SABLE_ALLOWED_ROOMS=a1b2c3d4,e5f6g7h8
SABLE_LLM_TOOL_IDS=server:mcp:1
SABLE_LLM_TOOL_ROOMS=e5f6g7h8       # tools here only; a1b2c3d4 answers from the model alone
SABLE_LLM_USERS=alice,bob           # and only these two (and the admins) can ask
```

`SABLE_LLM_EXTRA_BODY` is merged into the request last, which would let it hand tools to every
room past that gate, so it may not set `tool_ids`, `features`, `tool_servers`, `terminal_id` or
`session_id` (the last one would turn a room's tool-free blocking request back into a session
with built-ins): that is a startup error naming the right settings. The startup block's `tools:` line shows the rooms
(`in rooms: ...`, or `none (off everywhere)`).

**Anyone in a tools room can set these tools off**, unless `SABLE_LLM_USERS` says otherwise.
Asking the assistant a question is not a command, so `SABLE_ADMIN_COMMANDS` does not gate it, and
the model chooses which tool to call. If the tools reach Home Assistant, so does a guest. Give
sable its own Open WebUI account holding only what a chat room should have: those permissions are
enforced there, not here. See [security.md](security.md#accepted-risks).

## Alerting endpoint

| Variable | Default | Notes |
| --- | --- | --- |
| `SABLE_NOTIFY_TOKEN` | *(empty)* | Bearer token for `POST /notify`. **Empty disables the route**, which then answers 404 to everyone. Unrelated to the app password — generate a separate value. |
| `SABLE_NOTIFY_ROOMS` | *(empty)* | Aliases so callers need not know conversation tokens: `alerts=a1b2c3d4,deploys=e5f6g7h8`. An unrecognised name is treated as a raw token and must look like one, otherwise the request is a 400. |

The account has to be in the conversation it posts to: Talk answers a post from anybody else with
a 404, which `/notify` reports as a 400. The destination does **not** have to be in
`SABLE_ALLOWED_ROOMS`: posting is not following, so an alerts room can be one nobody talks to
the bot in. A destination (an alias's token, or a `/hook` conversation) is also never left by
`SABLE_LEAVE_UNLISTED_ROOMS`.

A body larger than the [request size cap](#request-size-caps) is a `413`, refused before the token
is even looked at. After the token: invalid JSON, invalid UTF-8 or absurd nesting is a `400`, and
a body that is not a JSON object, or is missing what it needs, is a `422` (the full list is in
[deployment.md](deployment.md#6-verify-end-to-end)). Text sent through `/notify` is posted as
written, mass mentions included, because its caller is trusted and may mean them; `/hook` text
is not (see [mass mentions](#mass-mentions)).

A conversation is named by its *token*, not by its name. The token is the lowercase string at
the end of the conversation's URL — in `https://cloud.example.org/call/a1b2c3d4` it is
`a1b2c3d4`. Talk's own routes only match lowercase, so a room name put where a token belongs
cannot work, and both `SABLE_NOTIFY_ROOMS` and `SABLE_HOOKS` are checked for it at startup.

## File attachments

`/notify` can carry a file. It is uploaded over WebDAV as the account above and shared into the
conversation, so there is no second credential and nothing to switch on: attachments are always
available.

| Variable | Default | Notes |
| --- | --- | --- |
| `SABLE_UPLOAD_PATH` | `/sable` | Folder inside the account's own Files where attachments are put before sharing. Created on first use. |
| `SABLE_MAX_UPLOAD_BYTES` | `26214400` (25 MiB) | Largest attachment `/notify` accepts. Bigger ones get a `413`. It also sets the largest `/notify` request body sable will read: see [request size caps](#request-size-caps). |

The share produces a chat message from the account itself. sable ignores its own messages, so it
does not answer them. [security.md](security.md#accepted-risks) covers what the account's
credential reaches.

## Webhooks from other services

`/notify` expects sable's own shape, which most services cannot send, and many of them cannot
set an `Authorization` header either. `/hook/{name}` takes whatever JSON they do send and
renders it into a message.

| Variable | Default | Notes |
| --- | --- | --- |
| `SABLE_HOOKS` | *(empty)* | Hook name to conversation, as `komodo=a1b2c3d4,grafana=e5f6g7h8`. The conversation is a token or a `SABLE_NOTIFY_ROOMS` alias, checked at startup. Empty means every `/hook/...` answers 404. |
| `SABLE_HOOK_TOKEN_<NAME>` | *(required per hook)* | That hook's own token, one variable each so a secret store can inject them separately. |
| `SABLE_HOOK_TEMPLATE_<NAME>` | *(empty)* | Optional format string. Without one the payload is rendered generically. |
| `SABLE_MAX_HOOK_BYTES` | `262144` (256 KiB) | Largest payload accepted. Alerts are small; this is a cap on abuse. It sets the cap on the request body too: see [request size caps](#request-size-caps). |

A hook with no token, a token with no hook, or a conversation that is neither an alias nor a
token is a startup error rather than something you discover when an alert goes missing.

### What a payload turns into

The renderer flattens the payload to dotted paths, so nesting stops mattering, then looks for a
severity among `level`, `severity`, `status`, `state`, `priority` and `urgency`; a title among
`title`, `subject`, `summary`, `alertname`, `event`, `name` and `type`; and a body among
`message`, `text`, `description`, `details`, `body`, `reason` and `error`. Whatever is left is
shown as key and value pairs.

Identifiers and timestamps are dropped, because a chat message already has its own time and the
ids mean nothing in a room. Repeated name and value pairs are shown once — Alertmanager sends
`alertname` and `severity` three times over. URLs are kept, since a link back to the dashboard
that fired is usually the most useful part. Long payloads are capped, with a count of what was
left out.

A Komodo alert arrives as:

```
**CRITICAL** sable
resolved: false · target.type: Stack · data.type: StackStateChange · server_name: prod-1 · from: Running · to: Unhealthy
```

Anything that is not JSON is posted as text rather than rejected, on the grounds that an alert
that arrives slightly wrong beats one that does not arrive. That includes bodies that are not
valid UTF-8 or are nested too deeply to walk. The rendered text has `@all` and group or team
mentions defanged, since the payload is somebody else's words (see
[mass mentions](#mass-mentions)).

### Format strings

Set `SABLE_HOOK_TEMPLATE_<NAME>` to take control of the wording. `{dotted.path}` is substituted
from the same flattened payload:

```ini
SABLE_HOOK_TEMPLATE_KOMODO=**{level}** {data.type}: {data.data.name} on {data.data.server_name} went {data.data.from} to {data.data.to}
```

```
**CRITICAL** StackStateChange: sable on prod-1 went Running to Unhealthy
```

Paths reach into lists as well, so `{alerts.0.labels.instance}` works, and naming a whole object
gives you its JSON. Substitution is all it does: there are no expressions, no conditionals and
nothing that can run. A path the payload does not have renders as `?` and logs a warning, so a
template that drifts out of date still delivers the alert.

Doubled braces are literal, so `{{like this}}` renders as `{like this}`.

### The token in the URL

Services that cannot set headers can pass `?token=...` instead, which is the only way Komodo can
authenticate. That puts a credential in a URL, where proxies and access logs will record it,
which is why each hook has its own token: one exposed in a log costs you that hook rather than
everything `/notify` can reach. It is listed among the
[accepted risks](security.md#accepted-risks).

## The HTTP surface itself

Three settings about the service as an HTTP endpoint, rather than about the account. Chat does not
come in over it: its only callers are your alerting systems and your health probe.

| Variable | Default | Notes |
| --- | --- | --- |
| `SABLE_API_DOCS` | `false` | Serve FastAPI's generated schema at `/openapi.json` and the doc pages at `/docs` and `/redoc`. Off removes the routes entirely, so they answer 404 rather than 401. |
| `SABLE_HEALTH_TOKEN` | *(empty)* | Require this value in an `X-Health-Token` header on `GET /healthz`. Empty leaves the probe open. |
| `SABLE_TRUSTED_PROXIES` | `127.0.0.1,::1` | Proxies whose `X-Forwarded-For` and `X-Forwarded-Proto` are believed. IP addresses and CIDR ranges, or `*` for any client. Set it to nothing to trust nobody. |

### Request size caps

sable limits request bodies itself, whatever sits in front of it, and refuses an oversized one
with a `413` before it checks a token and before it parses a byte. The limit is enforced on the
declared `Content-Length` and again on the bytes actually read, so a chunked body, or one whose
length header lies, is stopped as well.

| Route | Largest body |
| --- | --- |
| `POST /notify` | `ceil(SABLE_MAX_UPLOAD_BYTES × 4 / 3)` + 64 KiB: a file of that size as base64 in JSON, plus room for the other fields. Multipart is smaller than that |
| `POST /hook/{name}` | `SABLE_MAX_HOOK_BYTES` + 1 KiB, so a payload just over the setting still reaches the handler and gets its own, more specific, `413` |
| Everything else | 64 KiB |

Those two settings therefore do double duty. A very small `SABLE_MAX_UPLOAD_BYTES` leaves little
room for the message text of a `/notify` that carries a file, as the 64 KiB is all there is for
it, and raising it raises the memory an upload can take and the `/tmp` space a multipart upload
spools to ([deployment.md](deployment.md#container-hardening)). A proxy limit in front is still
good defence in depth, and nginx's default of 1 MB would refuse an attachment before sable saw it.

### The schema and its doc pages

`SABLE_API_DOCS` is off because whatever your proxy exposes is what an unauthenticated caller can
read — and the schema describes every route, every header and every body shape in one request.
Nothing at runtime needs it: your alerting callers can be written against
[the README](README.md#alerting) and [deployment.md](deployment.md#6-verify-end-to-end). Turn it on while writing a caller, then turn it off again.

Off means the routes do not exist. `GET /docs` answers 404, the same as any unrouted path, so
turning it off does not advertise that there was ever something there.

### Guarding the health probe

`GET /healthz` answers with the version, the account's user id (as `user`), the configured model, whether alerting is
on, and whether Nextcloud was reachable the last time sable called it — `true`, `false`, or
`null` before anything has been tried. The status stays `ok` and the code stays 200 even when
Nextcloud is down: a liveness probe that fails because a dependency failed gets a healthy process
restarted for no reason. Read the field and decide for yourself. It is **open by default**, which is deliberate: a container healthcheck and a Kubernetes
probe both call it without credentials, and liveness that needs a secret is liveness that fails
for the wrong reasons.

Set `SABLE_HEALTH_TOKEN` and the same answer needs the token:

```bash
curl -fsS -H "X-Health-Token: $SABLE_HEALTH_TOKEN" https://sable.example.org/healthz
```

A header rather than a query parameter, so the value stays out of proxy and access logs. Anything
missing or wrong is a 401 and a logged warning. The container image's healthcheck reads
`SABLE_HEALTH_TOKEN` from its own environment and sends the header when it is set, so guarding
the probe does not fail the container it is checking.

### Which proxies are believed

`X-Forwarded-For` and `X-Forwarded-Proto` are believed only from the addresses in
`SABLE_TRUSTED_PROXIES`, and the default is loopback — right for a proxy on the same host,
reaching sable through a published port or a localhost bind.

```ini
SABLE_TRUSTED_PROXIES=127.0.0.1,::1      # the default: a proxy on this host
SABLE_TRUSTED_PROXIES=172.17.0.0/16      # a proxy in another container
SABLE_TRUSTED_PROXIES=10.0.0.5           # one specific proxy
SABLE_TRUSTED_PROXIES=                   # nobody: ignore both headers
SABLE_TRUSTED_PROXIES=*                  # any client, which is the thing to avoid
```

Nothing in sable reads the client address — the chat rate limit counts people, not addresses,
and there is no allowlist and no authorization decision — so this decides whether the access log tells the truth, not who gets in. That
still matters: with `*`, any client can put what it likes in `X-Forwarded-For` and the log records
that instead of where the request came from.

Two kinds of value are refused at startup rather than accepted and ignored. A hostname cannot
work, since the comparison is against the peer's IP address, and a range with host bits set
(`172.17.0.5/16`) is not a range. uvicorn keeps either as a literal string that never matches
anything, so it would fail silently; sable names it and exits 2 instead.

**In Docker, the proxy is not loopback.** Another container reaches sable over the shared network,
so the peer address belongs to that network and the default ignores its headers. Name the subnet
— `docker network inspect <name>` prints it — or the proxy's own address.

## Time

| Variable | Default | Notes |
| --- | --- | --- |
| `SABLE_TIMEZONE` | *(the host clock)* | An IANA zone name such as `America/New_York`. A wrong name is a startup error rather than a silent fallback. |

The system prompt always ends with the current date and time. A model with no clock answers
"what is gold worth right now" from whatever was true when its training data stopped, and
cannot tell that the figure is years old. In a container the host clock is usually UTC, so set
this if local working hours matter.

## Process

| Variable | Default | Notes |
| --- | --- | --- |
| `SABLE_HOST` | `0.0.0.0` | Bind address. Use `127.0.0.1` when a reverse proxy on the same host is the only client. |
| `SABLE_PORT` | `8080` | |
| `SABLE_LOG_HEALTH_CHECKS` | `false` | Log an access line for every successful `GET /healthz`. The container healthcheck asks every thirty seconds — roughly 2,900 identical lines a day, which hide everything else. A probe that *fails* is logged either way, which is the part worth seeing. |
| `SABLE_LOG_LEVEL` | `INFO` | `INFO` logs the lifecycle, the resolved configuration, and who used what. `DEBUG` adds message text, prompts, command arguments, every outbound HTTP call, and why a message was *not* acted on — the fastest way to debug mention and prefix matching, but it puts chat content in the log. See [deployment.md](deployment.md#what-the-log-tells-you). |

## TLS trust, for an internal or self-signed Nextcloud

There is no `SABLE_` setting for this, and no way to disable certificate verification. Trust is
configured the standard way, with the variable OpenSSL and httpx already understand:

| Variable | Notes |
| --- | --- |
| `SSL_CERT_FILE` | Path to a CA bundle **inside the container**. `compose.yaml` mounts the host's `/etc/ssl/certs` read-only and sets this to `/etc/ssl/certs/ca-certificates.crt`. |

It replaces the trust store rather than adding to it, so the file must be the complete bundle:
public roots as well as your internal CA. Pointing it at a file holding only your CA makes the
internal Nextcloud verify while every public HTTPS call fails. `SSL_CERT_DIR` is a trap here,
because OpenSSL only finds certificates in such a directory by hashed filename, so a plain folder
of `.crt` files trusts nothing while still replacing the store. See
[deployment.md](deployment.md#if-your-nextcloud-uses-an-internal-or-self-signed-certificate).

## Worked examples

### Commands only, no model

```ini
SABLE_NEXTCLOUD_URL=https://cloud.example.org
SABLE_NEXTCLOUD_USER=sable
SABLE_NEXTCLOUD_PASSWORD=<an app password>
SABLE_ALLOWED_ROOMS=a1b2c3d4
```

Nothing else is needed. Mentions are ignored, `!help` works, and only the one room is served. (Leave
`SABLE_ALLOWED_ROOMS` out and it follows every room it is in, with a startup warning.)

### Assistant on a local model, answering everything in two rooms

```ini
SABLE_NEXTCLOUD_URL=https://cloud.example.org
SABLE_NEXTCLOUD_USER=sable
SABLE_NEXTCLOUD_PASSWORD=<an app password>
SABLE_ALLOWED_ROOMS=a1b2c3d4,e5f6g7h8
SABLE_LLM_BASE_URL=http://localhost:11434/v1
SABLE_LLM_MODEL=llama3.1:8b
SABLE_LLM_TIMEOUT=300
SABLE_AI_ROOMS=a1b2c3d4,e5f6g7h8
SABLE_MAX_CONCURRENT_REPLIES=1
SABLE_THINKING_REACTION=👀
```

A local model on modest hardware is slow, hence the longer timeout, the reaction so people can
see it is working, and one reply at a time: with `SABLE_MAX_QUEUED_REPLIES` at its default of 20,
up to twenty more wait their turn and any beyond that are dropped.

### Alerting only

```ini
SABLE_NEXTCLOUD_URL=https://cloud.example.org
SABLE_NEXTCLOUD_USER=sable
SABLE_NEXTCLOUD_PASSWORD=<an app password>
SABLE_NOTIFY_TOKEN=<a different random value>
SABLE_NOTIFY_ROOMS=alerts=a1b2c3d4,deploys=e5f6g7h8
SABLE_UNKNOWN_COMMAND_HINT=false
```

The bot still answers `!ping`, but its job is to relay what CI posts to `/notify`.

### A locked-down team assistant

Two rooms served, the rest left, and only named people may use the model:

```ini
SABLE_NEXTCLOUD_URL=https://cloud.example.org
SABLE_NEXTCLOUD_USER=sable
SABLE_NEXTCLOUD_PASSWORD=<an app password>
SABLE_ALLOWED_ROOMS=a1b2c3d4,e5f6g7h8
SABLE_LEAVE_UNLISTED_ROOMS=true
SABLE_LLM_BASE_URL=https://api.openai.com/v1
SABLE_LLM_API_KEY=sk-...
SABLE_LLM_MODEL=gpt-4o-mini
SABLE_LLM_USERS=alice,bob
SABLE_ADMIN_USERS=alice
SABLE_ADMIN_COMMANDS=reset
SABLE_RATE_LIMIT=10
SABLE_NOTIFY_TOKEN=<a different random value>
SABLE_NOTIFY_ROOMS=alerts=i9j0k1l2
```

The alerts room is not in `SABLE_ALLOWED_ROOMS`, so nobody can talk to the bot there, and it is
not left either, because it is a `/notify` destination.

### Everything, hosted model, quiet about its own failures

```ini
SABLE_NEXTCLOUD_URL=https://cloud.example.org
SABLE_NEXTCLOUD_USER=sable
SABLE_NEXTCLOUD_PASSWORD=<an app password>
SABLE_ALLOWED_ROOMS=a1b2c3d4
SABLE_COMMAND_PREFIX=!
SABLE_LLM_BASE_URL=https://api.openai.com/v1
SABLE_LLM_API_KEY=sk-...
SABLE_LLM_MODEL=gpt-4o-mini
SABLE_LLM_MAX_TOKENS=800
SABLE_LLM_SYSTEM_PROMPT=You are sable, the ops assistant. Be terse. Prefer bullet points.
SABLE_HISTORY_TURNS=20
SABLE_REPLY_AS_REPLY=true
SABLE_REPORT_ERRORS=false
SABLE_NOTIFY_TOKEN=<a different random value>
SABLE_NOTIFY_ROOMS=alerts=i9j0k1l2
SABLE_LOG_LEVEL=INFO
```

### An assistant that can search and reach Home Assistant

Tools run as the Open WebUI account behind the key, and anybody who can talk to the model in a
tools room can prompt it into calling one. Give it an account of its own, put the tools in a room
of their own, and say who may ask.

```ini
SABLE_NEXTCLOUD_URL=https://cloud.example.org
SABLE_NEXTCLOUD_USER=sable
SABLE_NEXTCLOUD_PASSWORD=<an app password>
SABLE_ALLOWED_ROOMS=a1b2c3d4,e5f6g7h8
SABLE_LLM_BACKEND=openwebui
SABLE_LLM_BASE_URL=https://ai.example.org/api
SABLE_LLM_API_KEY=sk-<the sable account's key>
SABLE_LLM_MODEL=gemma-focused
SABLE_LLM_TOOL_IDS=server:mcp:1,server:mcp:2
SABLE_LLM_FEATURES=web_search
SABLE_LLM_TOOL_ROOMS=e5f6g7h8
SABLE_LLM_USERS=alice,bob
SABLE_LLM_SHOW_SOURCES=true
SABLE_LLM_TIMEOUT=300
SABLE_THINKING_REACTION=⏳
SABLE_TIMEZONE=America/New_York
SABLE_AI_ROOMS=e5f6g7h8
```

Here `e5f6g7h8` is the tools room: every message in it goes to the model with the tools on, from
alice, bob and the administrators. `a1b2c3d4` is served too, and a mention there is answered
without any tools.

## Startup warnings

Settings sable cannot rule out but doubts are logged once, after the startup block, and do not stop
it. Each says what to change.

| Warning | Meaning |
| --- | --- |
| `SABLE_ALLOWED_ROOMS is empty, so sable follows every conversation it is in` | Anybody who can invite the account into a room can use it. List the tokens. |
| `SABLE_LEAVE_UNLISTED_ROOMS has no effect while SABLE_ALLOWED_ROOMS is empty` | Nothing is unlisted without a list. |
| `SABLE_AI_ROOMS lists …, which SABLE_ALLOWED_ROOMS does not` | Also for `SABLE_LLM_TOOL_ROOMS`: sable never reads those conversations, so the entry does nothing. |
| `SABLE_LLM_TOOL_IDS or SABLE_LLM_FEATURES is set, but SABLE_LLM_TOOL_ROOMS is empty` | Tools are configured and no conversation may use them. |
| `the model has tools in some conversations and SABLE_LLM_USERS is empty` | Anybody in those rooms can set the tools off. |
| `SABLE_NEXTCLOUD_URL is plain http://` | The password crosses the network unencrypted. |
| `SABLE_POLL_TIMEOUT is …, but Talk holds a long poll for at most 60 seconds` | The value is clamped. |
| `SABLE_LLM_BASE_URL … does not end in /api` | For the `openwebui` backend; every question will 404 unless a proxy rewrites the path. |
| `SABLE_IGNORE_USERS holds whitespace in …` | That can only match a display name, which the person can change. Prefer the user id. |

## Startup errors and what they mean

| Message | Fix |
| --- | --- |
| `… required: sable signs in to Nextcloud as an ordinary user` | One or more of `SABLE_NEXTCLOUD_URL`, `SABLE_NEXTCLOUD_USER`, `SABLE_NEXTCLOUD_PASSWORD` is missing; the message names which. Create a user, give it an app password, and set all three. |
| `SABLE_NEXTCLOUD_URL must start with http:// or https://` | The URL has no scheme, or is not a URL. |
| `SABLE_POLL_TIMEOUT must be at least 1 second` | Use a positive number of seconds; values over 60 are clamped rather than refused. |
| `SABLE_ROOM_REFRESH must be at least 5 seconds` | It is how often the conversation list is fetched. |
| `… must be a boolean` / `… must be an integer` / `… must be a number` | A typo in the value; see [value formats](#value-formats). |
| `… is not valid JSON` / `must be a JSON object` | `SABLE_LLM_EXTRA_BODY` needs an object: `{"top_k": 40}`. Quote it in a shell. |
| `… entries must look like alias=token` | `SABLE_NOTIFY_ROOMS` wants `name=token` pairs or a JSON object. |
| `… is neither a conversation token nor a SABLE_NOTIFY_ROOMS alias` | A `SABLE_HOOKS` entry names a room instead of its token. Take the token from the conversation's URL. |
| `… is not a conversation token` | The same, for a `SABLE_NOTIFY_ROOMS` entry. |
| `every hook needs its own token` | Add `SABLE_HOOK_TOKEN_<NAME>` for each hook in `SABLE_HOOKS`. |
| `has no matching entry in SABLE_HOOKS` | A `SABLE_HOOK_TOKEN_<NAME>` or `SABLE_HOOK_TEMPLATE_<NAME>` for a hook that is not in `SABLE_HOOKS`. |
| `SABLE_ADMIN_COMMANDS is set but SABLE_ADMIN_USERS is empty` | Name the administrators, or drop the commands from the list to leave them open. |
| `SABLE_ASK_ADMINS_ONLY is on but SABLE_ADMIN_USERS is empty` | The same shape: name the administrators, or turn it off to leave the reaction open. To disable the reaction, empty `SABLE_ASK_REACTION`. |
| `SABLE_MAX_CONCURRENT_REPLIES cannot be negative` | `0` is already how you ask for no ceiling. |
| `SABLE_MAX_QUEUED_REPLIES cannot be negative` | `0` already means no reply may wait for a slot. |
| `SABLE_RATE_LIMIT cannot be negative` | `0` already means no limit. |
| `SABLE_ALLOWED_ROOMS entry … is not a conversation token` | Also for `SABLE_AI_ROOMS` and `SABLE_LLM_TOOL_ROOMS`. A display name was put where a token belongs; take the token from the conversation's URL. Names are refused because anybody can name their own conversation after yours. |
| `SABLE_ALLOWED_ROOMS does not take '*'` | Leave it empty to follow every conversation, or list the tokens. |
| `so who may run them is not decided` | A command appears in both `SABLE_ADMIN_COMMANDS` and `SABLE_NORMAL_COMMANDS`. Pick one list. |
| `SABLE_NORMAL_COMMANDS cannot be '*'` | Everything not named as an admin command is open already; use the list for the exceptions to `SABLE_ADMIN_COMMANDS=*`. |
| `is not an IP address or a CIDR range` | A `SABLE_TRUSTED_PROXIES` entry is a hostname, a typo, or a range with host bits set (`172.17.0.0/16`, not `172.17.0.5/16`). |
| `SABLE_TRUSTED_PROXIES lists '*' alongside other entries` | `*` already means every client. Keep one or the other. |
| `SABLE_LLM_BACKEND must be one of` | Only `openai` and `openwebui` exist. |
| `SABLE_LLM_API_KEY is required for the openwebui backend` | The key is the account whose tools the model runs. |
| `SABLE_LLM_FEATURES may name` | One of `web_search`, `code_interpreter`, `image_generation`, `memory`, and only with `SABLE_LLM_BACKEND=openwebui`. |
| `SABLE_LLM_FEATURES needs SABLE_LLM_BUILTIN_TOOLS on` | Without a session id Open WebUI never offers the built-ins, so the setting would do nothing. |
| `SABLE_LLM_EXTRA_BODY must not set` | `stream` and `messages` are built by sable; overriding `stream` leaves it parsing an event stream as JSON. For `tool_ids`, `features`, `tool_servers`, `terminal_id` or `session_id` the message points at `SABLE_LLM_TOOL_IDS`, `SABLE_LLM_FEATURES` and `SABLE_LLM_TOOL_ROOMS`: move them there, or they would reach every room. |
| `SABLE_LLM_POLL_INTERVAL must be greater than zero` | For the `openwebui` backend; a zero would busy-loop against the task endpoint. |
| `SABLE_MAX_HOOK_BYTES must be greater than zero` / `SABLE_MAX_UPLOAD_BYTES must be greater than zero` | Both also size the request body caps. |
| `SABLE_TIMEZONE … is not an IANA time zone` | A name like `America/New_York`, not an abbreviation. |

Runtime problems — 401s, 403s, silence — are in
[deployment.md](deployment.md#troubleshooting).
