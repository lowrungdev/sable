# Plugins

Giving sable a new command, or a phrase it listens for, without changing sable: a Python file and
a settings file in a directory, run in a process of its own. This page is everything about them:
the layout, the settings file, the author API, who may trigger a plugin, what happens when one
fails, and how to run them. The threat model is in [security.md](security.md#plugins-and-the-process-boundary); the
three environment variables are in [configuration.md](configuration.md#plugins); mounting the
directory is in [deployment.md](deployment.md#running-plugins-optional). Two worked plugins, ready
to copy, are in [`examples/plugins`](../examples/plugins).

## What a plugin is

A plugin is a file `<name>.py` that declares one or more commands, and a file
`<name>_settings.yaml` beside it that says where and for whom they run. sable finds both when it
starts. Nothing is installed, nothing is imported into sable, and nothing runs until the settings
file names at least one conversation.

Each plugin runs in a **worker**: a separate Python process that sable starts for it, talks to over
a pipe, and kills if it hangs. The reason is that a plugin is somebody else's code running on your
host, as the same user as sable, and sable's one credential is an app password that reaches the
whole account. A plugin in sable's own process could read it. In a worker of its own it gets an
environment with no `SABLE_*` in it, is held to a time and memory limit, can crash without taking
the bot with it, and can only reach chat through a narrow API that sable checks on the other end
([what that does and does not protect](security.md#plugins-and-the-process-boundary)). It is a
containment, not a sandbox: read the code of every plugin before you mount it.

Plugins are off until `SABLE_PLUGINS_DIR` is set. With it empty nothing is discovered and no
worker process ever starts.

## Layout on disk

```
/plugins/                          SABLE_PLUGINS_DIR
  weather/                         any directory name, except one starting with . or _
    weather.py                     the plugin: has a settings file beside it
    weather_settings.yaml          paired with weather.py by its name
    helpers.py                     anything else is yours: the plugin may import it
  ops/
    deploy.py                      one directory may hold several plugins
    deploy_settings.yaml
    backup.py
    backup_settings.yaml
```

- **A plugin is a pair**: `<name>_settings.yaml` and `<name>.py` in the same directory. The pair is
  found by the settings file. The plugin's name is the file stem, lowercased, and must match
  `^[a-z][a-z0-9_-]{0,31}$`: it is what `!plugins` and the log call it.
- **Names are unique across the whole directory.** When two plugins share a name the one that
  comes first, in sorted path order, keeps it: its commands, its phrases and `!plugins <name>` are
  all the first one's. The other fails and says whose name it clashed with, is listed in `!plugins`
  as `name (its/path.py)` so it is clear which is which, and never shadows the first even if the
  first later fails to load for some other reason.
- **Only the directories directly under `SABLE_PLUGINS_DIR` are looked at.** A pair at the top
  level, or one nested a level deeper, is not found, and nothing says so.
- **A directory whose name starts with `.` or `_` is skipped** (`_disabled/`, `.git/`), which is
  the way to park a plugin without deleting it. So is a symlink to a directory, with a note in the
  log. A plugin file or settings file that is itself a symlink is refused, so a plugin cannot be
  pointed at something outside the directory. Symlinks are never followed.
- **Size caps**: the plugin file may be at most 256 KiB, the settings file 64 KiB. Only the first
  256 directories, and the first 256 settings files within each, in name order, are even looked at;
  the rest are named in a note and never read.
- **At most 64 plugins actually start.** The cap is counted only after a plugin has loaded:
  passed its settings and its syntax, had its worker import it and answer what it declares, and -
  if it has one - passed its `check()`. A plugin that fails earlier (a bad name, bad settings, a
  syntax error, a clash of names) never takes one of the 64 places, however many come before it in
  path order. The 65th plugin that would otherwise have loaded fails instead, with "not loaded:
  sable starts at most 64 plugins, and 64 that loaded successfully, in path order, already are".
- **Helper files** are anything else in the plugin's directory. The directory is first on the
  plugin's import path, so `from helpers import shout` works, and so does vendoring a pure-Python
  library into the folder. A helper named like a standard library module (`json.py`) will shadow
  it. Two plugins in one directory share the helpers on disk but not a process, so they share no
  state.

### What is not a plugin

- A `.py` file with no `_settings.yaml` beside it is a helper, and is never run by itself.
- A `<name>_settings.yaml` with no `<name>.py` is a plugin that failed: `!plugins` says
  `weather_settings.yaml has no weather.py next to it`.
- Nothing is executed during discovery. The plugin file is parsed (never run) so that a syntax
  error is reported against that plugin before anything starts.

### File names that look right and are not found

The settings file has to be called exactly `<name>_settings.yaml`, lowercase. The near misses,
which would otherwise make a plugin silently disappear, are named in the log and in `--check`:

```
misc/weather_settings.yml looks like a settings file but is not called <name>_settings.yaml (that exact spelling, lowercase), so it is ignored
```

That covers `.yml`, `_setting.yaml` and `Weather_Settings.yaml`. A plugin file called
`Weather.py` beside `Weather_settings.yaml` is fine: the name is lowercased, so it is `weather`.

## The settings file

One file per plugin, parsed with PyYAML's `safe_load`. The schema is strict: a key that is not
listed here is an error, so a typo (`room:` for `rooms:`) fails the plugin instead of leaving it
silently inactive.

```yaml
# plugins/weather/weather_settings.yaml

# false: validated and listed in !plugins as disabled, never started.
enabled: true

access:
  # Conversation tokens, the last part of a conversation's URL. QUOTE them: a token made of
  # digits is a number to YAML and is refused. Rooms are matched by token, never by name.
  # Empty, or this key missing, leaves the plugin INACTIVE: validated and never run.
  rooms:
    - "a1b2c3d4"
    - "e5f6g7h8"
  # "*" instead means every conversation sable follows (quoted: a bare * is a YAML alias).
  #   rooms: ["*"]

  # Nextcloud user ids who may use it. Empty: anyone in those rooms. Administrators are not
  # added to this list automatically.
  users: [alice, bob]

  # true: only SABLE_ADMIN_USERS. With both set, a person must be on the list and an administrator.
  admins_only: false

# Anything the plugin needs, handed to it read-only as ctx.settings. Plain YAML values only
# (text, numbers, true/false, null, lists, mappings with text keys). The plugin's check() may
# insist on what it needs.
settings:
  units: metric
  default_city: Berlin
  api_key: "<your key>"
```

How the pieces behave:

- **`rooms` decides whether the plugin runs at all.** A plugin with no rooms is **inactive**:
  its file and settings are checked, and its code is never imported. That is what a freshly copied
  plugin should be, and the examples ship that way.
- **A room is also subject to `SABLE_ALLOWED_ROOMS`.** If that is set, a plugin room that is not
  in it never sees a message (sable does not read that conversation), and startup warns about the
  entry. `"*"` means every conversation sable follows, so with `SABLE_ALLOWED_ROOMS` empty that is
  every conversation the account is in ([how chat is received](configuration.md#how-chat-is-received)).
- **`users`** takes user ids, matched on the id only and without regard to case; `users/alice`
  works as well as `alice`. A guest, a federated user and a bot have no user id and never match a
  non-empty list. Quote an id that YAML would read as something else (`"yes"`, `"1234"`).
- **`settings` is JSON-shaped.** Dates and times are YAML types a plugin cannot receive: write
  them as quoted text. It may nest at most 16 levels and hold at most 10,000 values.
- **Anchors and aliases (`&a`, `*a`) are refused.** A few kilobytes of them expand into gigabytes,
  and nothing here needs them. A file that nests brackets or list items more than 100 levels deep
  is refused too, and so is a file that is not UTF-8.
- **An error message names the field and never quotes the file**, because the file holds secrets:
  `access.rooms: 12345678 is a number, not text: quote it ...`.
- **The file is read when sable starts.** Changing it needs a restart.

## Triggers

A plugin declares what it answers to. Two kinds so far.

### Commands

A plugin command works like a built-in one: the person types the prefix and the name
(`!weather Berlin`), and the plugin answers. It also answers to its aliases, and appears in
`!help` for the people who may run it, marked _(plugin)_, with its `help` text and its `usage`.

A command name or alias may not clash with a built-in command, nor with one from a plugin that
loaded earlier. Built-ins always win: the plugin that clashes fails and says which name.

### Phrases

A plugin can also answer to a phrase inside an ordinary message, with nobody typing a command:

```python
from sable.plugin_api import Context, on_phrase


@on_phrase(any=["good morning", "gm"], whole_words=True, cooldown="1h")
async def greet(ctx: Context) -> str | None:
    return f"Good morning, {ctx.actor_name}!"
```

`@on_phrase(any=[...], *, whole_words=True, cooldown=30)` declares the handler. `any` is 1 to 20
phrases, each 2 to 100 characters after stripping; two that fold to the same text (see below) are
one phrase, kept under the first spelling. The handler's id - for `!plugins`, the log and the
cooldown table - is its function name: letters, digits and `_`, starting with a letter or `_`,
unique across every kind of trigger in the plugin. All of this is checked when the file is
imported, like `@command`: a bad declaration fails the plugin with a reason, never surprises
somebody at the first matching message.

**Matching is literal, never a pattern a plugin wrote.** No regular expression, no wildcard: the
phrase is compared to the message as plain text. Both sides are folded the same way first -
Unicode-normalised (NFKC) and casefolded - so `Straße`, `STRASSE` and `strasse` are one phrase,
and so are full-width and ordinary letters. A plain-ASCII message is matched in full. A message
holding any non-ASCII character has only its first 4,000 characters (`MAX_MATCH_TEXT` in
[`plugins.py`](../src/sable/plugins.py)) folded and searched; a phrase that only appears later in
a long non-ASCII message is never found. The cap exists because normalising a long, adversarial
run of combining marks is slow enough to matter - Talk itself allows a message up to 32,000
characters.

**`whole_words` (the default, `True`) wants a phrase that stands on its own.** It is found only
where it is not touched by a word character on a side where its own edge is one: `gm` matches
"gm!" and "say gm" but not "gmail" or "2gm", while `:)` or `c++` have no word character at either
edge and match anywhere, `c++` inside "love c++ a lot" included. With `whole_words=False` any
substring matches, `gm` inside "telegram" included.

This has a real weak spot: in a script written with no spaces between words - Han, Thai and the
like - almost every character counts as a "word" character, so `whole_words=True` finds almost
nothing there: there is no useful boundary to sit at. For those scripts, use `whole_words=False`,
or choose a phrase long and specific enough that a plain substring match is still meaningful.

**A short phrase without word boundaries is refused.** `whole_words=False` with a phrase under 3
characters is a declaration error: a 1- or 2-character substring would match inside almost any
longer word, which is not a trigger, it is close to reading every message in the plugin's rooms.
A phrase that short must use `whole_words=True`.

**`cooldown`** is how long a handler stays quiet in a room after it fires there: an integer number
of seconds, or text like `"30s"`, `"5m"`, `"1h"` or `"1d"` (ASCII digits only - a fullwidth or
Arabic-indic digit is not understood), from `0` (no cooldown) to `604800` (a week). The default is
30 seconds. It is tracked per `(plugin, handler, room)`, never per person: once a handler has
fired in a room, everyone in it shares the same quiet period, whoever says the phrase next.

**Fairness.** At most 3 phrase handlers fire for one message (`MAX_PHRASE_FIRES_PER_MESSAGE`),
round-robined one per plugin before any plugin gets a second, so a plugin with several handlers on
a broad, cooldown-0 phrase cannot fill every slot itself and starve every other plugin's handler
for the same phrase. A handler that loses out because the cap was already reached starts no
cooldown, so it gets its turn on the next matching message instead.

**When a phrase is even considered.** Only for a plain message: never a `!command`, and never one
addressed to sable (a mention, or its name at the start of the line). In a conversation listed in
`SABLE_AI_ROOMS` the model answers every message as it always has, and a phrase handler may fire
on that same message alongside it - two separate decisions about one event. Matching itself costs
nothing: an ambient phrase match, whether it fires, is cooling down, or simply is not there, never
takes a rate-limit token (`SABLE_RATE_LIMIT`); only a command, a mention, an AI-room message or
the ⁉️ reaction does ([the layers](configuration.md#how-the-access-layers-combine)).

**Who may trigger a phrase handler** is decided by the same first two checks as
[who may run a plugin command](#who-may-run-a-plugin-command): the plugin has to be active, with
this room among its `access.rooms` (inactive, with no rooms set, like any freshly copied plugin),
and the sender has to pass its `users` and `admins_only`. There is no third layer:
`SABLE_ADMIN_COMMANDS` and `SABLE_NORMAL_COMMANDS` name commands, and a phrase handler has no
command name for them to match. Refused the same way too, silently: nothing starts, and the
worker is never told the message existed.

**When a handler fails** - it crashes, raises an exception it did not mean to, times out, or its
worker is switched off by the breaker - nobody asked it anything, so nothing is posted: only the
log hears about it, with the plugin named. A handler's own `raise PluginError("text")` is treated
the same way, for the same reason: nobody asked, so it never reaches the room, only the log
(unlike a command, where a `PluginError` *is* the answer). A successful reply - the returned
string, or an awaited `ctx.reply` - is posted normally, mass mentions defanged and all, exactly
like a command's.

> **A phrase handler sees the full text of every message it matches, in every room it is enabled
> in** - mentioned display names and shared file names included. A plugin with broad, short,
> cooldown-0 phrases amounts to standing read access to that room's traffic. Review a plugin's
> declared phrases the way you would review its permissions before enabling it: `!plugins <name>`
> shows them for exactly this reason.

## Writing a plugin

A complete plugin, as small as it gets:

```python
from sable.plugin_api import Context, PluginError, command


@command("weather", aliases=("wx",), help="Forecast for a city", usage="weather <city>")
async def weather(ctx: Context) -> str | None:
    city = ctx.args or ctx.settings.get("default_city")
    if not city:
        raise PluginError("Which city?")
    return f"Sunny in {city}."
```

[`examples/plugins`](../examples/plugins) holds two that do real work: `!roll` (input checking,
`PluginError`) and `!up` (settings, `check()`, outbound HTTP).

`sable.plugin_api` is the only part of sable a plugin is written against. It needs nothing but the
standard library. A plugin does not need to be installed or built: the file is imported from where
it lies.

### `@command`

`@command(name, *, aliases=(), help="", usage="")` declares a handler. The handler is an
`async def` that takes one argument, the `Context`, and returns Markdown text or `None`. The
declaration is checked when the file is imported, and a bad one stops the plugin loading with a
message saying what is wrong:

- `name` and every alias: lowercase letters, digits, `-` and `_`, starting with a letter, at most
  32 characters. The decorator needs its brackets: `@command("x")`, not `@command`.
- `help`: at most 200 characters; `usage`: at most 100; both single-line plain text. `usage` is
  written without the prefix (`weather <city>`); `!help` adds it.
- A name or alias may be declared only once in a plugin, and a plugin may declare at most 32
  handlers.

### What a handler is given

`Context` is read-only, filled in by sable for each call.

| Attribute | What it holds |
| --- | --- |
| `plugin` | This plugin's name. |
| `trigger` | `"command"` or `"phrase"`. |
| `name` | The command's own name (also when it was called by an alias), or a phrase handler's id. |
| `args` | Everything after the command, as typed; empty for a phrase. |
| `argv` | The same, split like a shell would (`"two words"` stays one); a message with unbalanced quotes is split on spaces instead. Empty for a phrase. |
| `match` | For a phrase handler, the declared phrase that matched - as written in `any=`, not as typed in the message. Empty for a command. |
| `room` | The token of the conversation the message was written in. |
| `actor_id` | Who wrote it, as Talk names them: `users/alice`, `guests/7f3c9a2b…`, `federated_users/…`. |
| `user_id` | The bare Nextcloud user id, `alice`. **Empty for a guest or a federated user.** Use this, not `actor_id`, to compare against a list of people. |
| `actor_name` | The display name. Anybody can set theirs to anything, so never decide access on it. |
| `is_admin` | Whether the sender is in `SABLE_ADMIN_USERS`. |
| `message_id`, `text` | The triggering message's id and its full text, prefix included. |
| `settings` | The plugin's `settings:` block, read-only, nested mappings and lists included (lists arrive as tuples). |
| `log` | A `logging.Logger` named `sable.plugin.<name>`. What it writes lands in sable's log, see [operating](#operating). |

And three methods, all `async` and all to be awaited:

- `await ctx.reply(text, *, silent=False)`: post into the conversation that triggered the call.
  `silent=True` posts without a notification. Call it as often as the limits allow.
- `await ctx.send(room, text, *, silent=False)`: post into another conversation, which has to be
  one of the plugin's own `access.rooms` (any room sable follows, with `"*"`).
- `await ctx.react(emoji)`: react to the message that triggered the call.

A refusal from sable (a `send` to a room the plugin may not use, a reaction Talk would not take,
a call over its limits) raises `PluginActionError` in the handler, importable from
`sable.plugin_api` like `PluginError`. It is not a `PluginError`: unless the handler catches it,
the call counts as a crash.

### Answering: return, reply, or raise

- **Return a string** and it is posted as one final reply. It is the same as a last `ctx.reply`.
  `None`, or text that is only whitespace, says nothing. Anything else (a number, a dict) is a bug
  in the plugin and is reported as a crash.
- **`raise PluginError("text")`** answers with that text, as the plugin's own error. It is shown in
  the conversation as written (control characters replaced, 2,000 characters at most), like a
  built-in command's usage error, so write it for the person who typed the command. It is not
  limited by `SABLE_REPORT_ERRORS`.
- **Any other exception is a crash.** The traceback goes to the log under the plugin's name, and
  the conversation sees only ``⚠️ Sorry - the `weather` plugin crashed`` (nothing, with
  `SABLE_REPORT_ERRORS=false`). A crash in one call does not stop the worker.
- **Posted text is checked on the way out.** Mass mentions are defanged
  ([what that covers](configuration.md#mass-mentions)), and what goes to Talk is clipped at
  `SABLE_MAX_MESSAGE_CHARS`. One call may do at most 10 actions (replies, sends, reactions; the
  returned string counts as one) and post at most 20,000 characters in all; text over what is left
  is cut to fit.

### The handler has to finish what it started

`reply`, `send` and `react` work only while the handler is running. The moment it returns, the
call is over: a later `ctx.reply` raises `PluginActionError`, and any task the handler started
with `asyncio.create_task` and did not await is cancelled. **Await every action before
returning**, and await (or `gather`) the tasks you start. A plugin that needs to say something
slowly (a long report) should do the work in the handler, up to the timeout, and return.

### `check(settings)`

An optional function in the plugin file, sync or `async`, that sable runs once, right after the
plugin is imported, with the same read-only `settings`. It returns `None` when the settings are
usable and a string saying what is wrong when they are not (raising `PluginError` with that
text does the same). The plugin then fails to load with that text, in the log, in `!plugins` and
in `--check`, rather than failing in front of a user later. It has 10 seconds. It is not run again
when the worker restarts, and it never runs for a plugin that is inactive or disabled.

Quote nothing from `settings` in the message: sable blanks every string of four characters or more
from the settings out of what a plugin reports (an error message, a log line, the last error
`!plugins` shows), in case it is a secret, so a quoted value reads `***`. The shipped
[`uptime`](../examples/plugins/uptime/uptime.py) example says "service 2" for that reason. What the
plugin writes into the conversation itself is not touched.

### The environment a plugin runs in

- **The interpreter and libraries are sable's own.** The standard library, `httpx` and PyYAML can
  be relied on: they are sable's dependencies. Other packages in sable's environment (FastAPI,
  pydantic and so on) happen to be importable too, but they are sable's, not a promise to plugins.
  Nothing can be installed from inside a plugin; ship what you need as files in the plugin's
  directory.
- **The environment holds no `SABLE_*` and no other secret.** What it does hold: a fixed `PATH`
  (`/usr/local/bin:/usr/bin:/bin`), `LANG=C.UTF-8`, `TZ` (`SABLE_TIMEZONE`, or `UTC` when that is
  unset), the two variables that name a CA bundle (`SSL_CERT_FILE`, `SSL_CERT_DIR`) when sable has
  them, so an internal CA is trusted by `httpx` in a plugin as it is in sable, and two interpreter
  flags, `PYTHONDONTWRITEBYTECODE` and `PYTHONUNBUFFERED`, which the worker's `python -I` ignores
  anyway (set regardless, since nothing relies on `-I` for that). **A plugin receives its secrets
  through its settings file**, not through the environment.
- **The working directory is the plugin's own directory**, which is read-only when mounted as
  [recommended](deployment.md#running-plugins-optional). To write a file, use `/tmp`, which is
  gone when the container restarts.
- **Standard output goes to the log.** `print()` and anything a child process writes to stdout
  cannot disturb sable: they are redirected to the log, as is standard error. Standard input is
  empty.
- **Each plugin has a process of its own, for as long as sable runs.** Module-level state
  survives from one call to the next, until the worker is killed (after a timeout or a crash),
  when it starts empty again. Do not keep anything there that cannot be rebuilt.
- **Calls run concurrently**, up to four at a time per plugin, in one event loop: do not block it.
  A call that spends its time in a synchronous HTTP library holds the other three up.
- **No model access.** A plugin cannot make sable call its language model.

## Who may run a plugin command

A plugin command passes the same layers as every other command, and then its plugin's own. In
order, after the room, ignore and bot checks and the per-person rate limit
([the table](configuration.md#how-the-access-layers-combine)):

1. **Is the plugin here?** It has to be active (loaded, not switched off) and the message has to
   come from one of its `access.rooms` that `SABLE_ALLOWED_ROOMS` also serves. If not, the answer
   is exactly what an unknown command gets, hint included
   (`SABLE_UNKNOWN_COMMAND_HINT`): nothing says the plugin exists. That also hides it from `!help`.
2. **May this person?** If the plugin is `admins_only`, the sender must be in `SABLE_ADMIN_USERS`.
   If `users` is not empty, the sender's user id must be on it. Failing either gets
   "`!weather` is not available to you.": the command exists, in the right room, but not for them.
3. **Then the built-in command layer**: `SABLE_ADMIN_COMMANDS`, `SABLE_NORMAL_COMMANDS` and
   `SABLE_ADMIN_USERS` work on plugin command names and aliases like on any other (`*` closes the
   plugins' commands too, and `SABLE_NORMAL_COMMANDS` reopens named ones). A command refused there
   answers "`!weather` is for administrators only."

The rate limit counts a plugin command as a trigger before any of this is asked, so somebody
refused still uses up their allowance. A bot is refused in the decision itself, whatever id it
carries. `admins_only: true` with `SABLE_ADMIN_USERS` empty means nobody may run the command, and
sable does not warn about it.

The worker is not started, and is not told anything, for an event the plugin may not serve.

A [phrase handler](#phrases) is decided by steps 1 and 2 only - there is no command name for step
3 to apply to - and, unlike a command, never counts against the rate limit.

`!plugins` is not a plugin command. It is a built-in that only administrators can use, whatever
`SABLE_ADMIN_COMMANDS` says, and it is left out of `!help` for everybody else.

## When a plugin fails

### At startup

A plugin passes these in order, and the first one it fails ends its loading, with a reason:

1. **Structure**: a legal name, a pair of files, regular files within the size caps.
2. **Settings**: valid YAML, in the schema above.
3. **Syntax**: the plugin file parses as Python.
4. **Load**, for a plugin that is enabled and has rooms: its worker starts, imports the file
   (10 seconds), and answers what it declares; the declaration is validated again by sable, since
   the worker is not trusted. Then `check()` runs.
5. **Names**: no clash with a built-in command or an earlier plugin.
6. **The cap**: only the first 64 plugins to reach this point, in path order, are kept; a 65th
   fails here, however early its files sort, with nothing wrong in its own settings or code
   ([the exact wording](#layout-on-disk)).

An **inactive** or **disabled** plugin goes through steps 1 to 3 only. Its code is never imported,
its `check()` never runs, and it does not claim any command names or a place in the cap, so a bad
`check()` or a clash shows up on the day somebody sets its rooms. Steps 4 to 6 are why `--check` is
worth running after every change to the settings.

A plugin that fails is skipped. It is named in the log as a WARNING with its reason, counted in
the startup block, and listed by `!plugins` as `failed: <reason>`. It never stops another plugin
or the bot.

### `SABLE_PLUGINS_STRICT`

With `SABLE_PLUGINS_STRICT=true` a plugin that fails to load stops startup instead: exit status 2,
every failure named. Use it where a plugin silently not running is worse than the bot not
starting. Startup with it loads every plugin twice, once before the server starts so that the exit
status is a clean one, and again inside the server, so what a plugin does at import runs twice.
An inactive or disabled plugin is not a failure, and neither is a note about a file that was
skipped.

### While running

- **A call that takes too long.** `SABLE_PLUGINS_TIMEOUT` (30 seconds) is per call. When it runs
  out, the worker's whole process group is killed, the person is told the plugin took too long
  (with `SABLE_REPORT_ERRORS` on), and the next call starts a new worker. Any other call in
  flight on that worker is told it was restarted; the incident counts once.
- **A worker that dies**, on its own or killed (out of memory, a signal), is noticed at once and
  restarted by the next call, not before. The log says how it died, by signal name where there
  is one.
- **A worker that breaks the protocol** (a line that is not JSON, one over 1 MiB, a reply to
  nothing, a flood of actions) is killed and counted as a crash.
- **The circuit breaker.** A plugin may be restarted three times in five minutes. The next time
  it needs a restart inside that window it is **switched off**. For five minutes after, its commands
  answer like commands that do not exist, `!plugins` shows `switched off, retrying in 4 min`, and
  the startup count calls it failed. After the five minutes one call is let through: if it works
  the plugin is whole again and its past is forgiven, and if it fails it is off for another five
  minutes. So a plugin cannot be kept off for good by whoever can crash it, and one that is
  broken costs a worker start every five minutes, not one per message.
- **A restart that declares something different** (different commands, a different `help`) is a
  different plugin, and is switched off until sable restarts.

Every failure leaves a last error that `!plugins <name>` shows (with the plugin's settings strings
blanked out).

### Limits

All of these are constants in [`plugins.py`](../src/sable/plugins.py), apart from the timeout.

| Limit | Value |
| --- | --- |
| One call | `SABLE_PLUGINS_TIMEOUT`, 30 seconds by default (1 to 600), then the worker is killed |
| Importing the plugin, and its `check()` | 10 seconds each |
| Calls in flight per plugin | 4; the rest wait |
| Actions and text per call | 10 actions, 20,000 characters |
| Restarts before it is switched off | 3 in 300 seconds; off for 300 seconds, then one trial call |
| Address space of a worker | 1 GiB (`RLIMIT_AS`: virtual size, not resident memory) |
| Open files in a worker | 64 |
| Core dumps | none |
| CPU time in one call | 10 times the timeout, in CPU seconds (a backstop for a loop the clock cannot interrupt; renewed for every call) |
| A line from a worker | 1 MiB |
| A worker's output to stderr in the log | 1,000 characters a line, 200 lines per 10 seconds |
| Shutdown | a worker is asked to exit and has 2 seconds before it is killed |
| Plugins / handlers per plugin | 64 / 32 |
| Plugin file / settings file | 256 KiB / 64 KiB |
| Phrases per handler, and their length after stripping | 1 to 20 phrases, 2 to 100 characters each |
| A phrase's cooldown | 0 (none) to 604,800 seconds (a week); 30 by default |
| Phrase handlers fired per message | 3, round-robined across plugins |
| Cooldowns remembered at once | 10,000, oldest-to-expire evicted first |
| Of a non-ASCII message, what is searched for a phrase | its first 4,000 characters |

A worker also asks the kernel to prefer it as the victim when memory runs out
(`oom_score_adj`), best effort. There is no limit on all the workers together other than the
container's: see [what to size](deployment.md#running-plugins-optional).

## Operating

- **Mount the directory read-only**, and restart sable after changing anything in it. A plugin
  that could change the files it is loaded from could persist across restarts.
  [How](deployment.md#running-plugins-optional).
- **`!plugins`** (administrators) lists every plugin with its status, triggers and rooms:

  ```
  **Plugins** - 2 active, 1 inactive, 2 failed (/plugins)
  - `deploy` - failed: its check rejected the settings: ... - rooms: a1b2c3d4
  - `dice` - active - `!roll` - rooms: a1b2c3d4
  - `greeter` - switched off, retrying in 4 min - rooms: a1b2c3d4
  - `uptime` - inactive: no rooms set
  - `weather` - active - `!weather` - phrases: `greet` - rooms: a1b2c3d4, e5f6g7h8
  `!plugins <name>` for one in detail.
  ```

  A `rooms:` line is shown whenever the plugin's settings parsed (so most failures show it; a bad
  name or malformed YAML does not, since there is no room list to show). A plugin that is failed
  or switched off shows no commands or phrases, whether or not it declared any: they would not
  answer. `greeter`'s breaker is open, which the tally above counts as one of the 2 failed, even
  though its own line reads "switched off": the summary line is a count of what currently does not
  work, not a count of the literal word "failed".

  `!plugins weather` shows one: the file, rooms, users, each command with its usage and aliases,
  each phrase handler with its phrases and cooldown, restarts since sable started, and the last
  error. Neither shows a plugin's `settings`. The statuses are `active`, `restarting` (the worker
  died and the next call starts a new one), `inactive: no rooms set`, `disabled`, `failed: <reason>`,
  `switched off, retrying in N min`, `switched off, retrying on the next use` (the five minutes are
  up; the next call is the trial), and plain `switched off` (it declared something different after
  a restart, and stays off until sable itself restarts).
- **`sable --check`** does the whole load without starting the server: every enabled plugin with
  rooms is started, asked what it declares, has its `check()` run, and is shut down again. It
  prints one line per plugin and exits 0 unless `SABLE_PLUGINS_STRICT` is on and one failed
  (exit 2). Run it against the real settings before restarting:

  ```
    plugins:   2 active, 1 failed (/plugins)
      dice: active (commands: roll, dice)
      greeter: active (commands: none; phrases: greet)
      uptime: failed: its check rejected the settings: service 1 still has the placeholder url: replace it with your own
  ```
- **The log.** Each plugin is named in it as `plugin <name>`. What a worker writes to stderr
  (`ctx.log`, `print`, a traceback) is logged at INFO as `plugin <name>: <line>`, one line each,
  control characters removed. A failed plugin, a crash, a timeout, a restart and the breaker
  opening or closing are log lines naming it, at WARNING (ERROR when the breaker opens). At
  `--check` the worker's output is kept out of the report.
- **Debugging a plugin** is `ctx.log.info(...)` and `--check`. A plugin that works in `--check`
  and not in chat is usually the room (`!plugins <name>` shows which), the user list, or
  `SABLE_ALLOWED_ROOMS`, which a plugin room must also be in.
- **Not on Linux**: `SABLE_PLUGINS_DIR` is a startup error where `os.name` is not `posix`
  (Windows), because the isolation relies on process groups and resource limits. On another POSIX
  system, macOS for one, it runs, with a warning that sable's own memory stays readable to
  plugins, which only Linux can prevent. The plugin tests are POSIX-only for the same reason; on
  Windows run them in WSL.
