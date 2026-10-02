# Future changes

Things that will or may need to change, each with the constraint that forces it. Nothing here is
scheduled. It is the list to consult when something starts hurting, so the reasoning does not
have to be worked out twice. What sable deliberately does not do is in
[purpose.md](purpose.md); some of it appears below as "revisit if", which is the honest position:
they were the right call for one bot on one server.

## Limitations with a known fix

**Durable conversation history.** `History` in [`history.py`](../src/sable/history.py) is an
in-process dictionary of deques, so a restart forgets everything. The fix is to swap the class
for one backed by Redis or SQLite; it has three methods and `Bot` takes it as a constructor
argument, so nothing else changes. Worth doing when people start noticing that a deploy loses
context mid-conversation.

**One process only.** History, the rate limiter and the position in each conversation live in
memory, which is why [deployment.md](deployment.md#upgrading-rotating-backing-up) says to run one
process per account. Moving that state into a shared store, and deciding which worker owns which
conversation, makes horizontal scaling real, which for a chat assistant is a long way off.
`SABLE_MAX_CONCURRENT_REPLIES` raises the ceiling within one process without splitting anything.

**No rate limiting on `/notify` and `/hook`.** Nothing stops a misconfigured alertmanager posting
a thousand messages ([accepted risk 9](security.md#accepted-risks)); their only protection is the
size cap on each body. Nextcloud may eventually answer 429 and the bot will log failures, but the
noise has already happened. A token bucket per room, or rate limiting at the proxy, fixes it.

**Command authorization is global, not per room.** The admin and model-user lists and the allowed
rooms say who may do what across every conversation the bot is in. What they cannot say is "maser
may deploy, but only from the ops room", or defer to Talk's own notion of a moderator, which is
the thing an operator reaches for next. sable does not know the sender's participant type: the
chat messages and reaction events it reads are reported not to carry it (not re-checked against a
live server), so `!whoami` says only whether the sender is a user, a guest or a bot. A rule
against the moderator role would need a participant-list lookup per decision, which is not a small
change and is one more thing to get wrong for an assistant whose admin list is usually two names.
Revisit when the first per-room rule is actually wanted.

## Talk features not yet used

**Adding a reaction is confirmed; taking one back is not.** The ⁉️ feature was tried against a
live Nextcloud on 2026-10-01 and works: Talk delivers the reaction as a `reaction` system message
through the same chat poll as everything else, with the reacted-to message in `parent`. The Talk
documentation had suggested that message might be replaced before a poll saw it, and that its
text might be a `{reaction}` placeholder; neither got in the way. What has not been exercised is
removal. A reaction a person takes back is documented as `reaction_deleted` and a moderator
removing someone else's as `reaction_revoked`; whether either arrives through the poll is not
known. Neither is parsed: `parse_message` in [`events.py`](../src/sable/events.py) keeps the
`reaction` system message and ignores every other system message, so nothing acts on a removal.

**Whether the context call includes the message itself.** The ⁉️ feature reads the message back
through the context endpoint ([how](purpose.md#the-talk-calls-it-makes)). The Talk documentation
does not say whether the answer includes the message, so `TalkClient.message` asks for three
neighbours each way (`CONTEXT_LIMIT`), picks the entry whose id matches and treats none as "not
found". If a server answers with the neighbours only, every reaction would get "I cannot find
that message", and the fix is a larger `limit` in that one call.

**Other reactions, joins and threads.** Reactions are handled for one emoji; any other is parsed
and ignored, so a second behaviour (an approval flow where a thumbs-up from the right person does
something) is a branch in `Bot._route` next to the existing one. Joining and leaving a
conversation are not parsed at all; a greeting when sable is invited would hang off
`Poller.scan`, which is where it learns of a new conversation. Message posting accepts a thread
id, which [`talk.py`](../src/sable/talk.py) does not pass; replying in a thread rather than inline
would suit the assistant in busy rooms.

**Reading files.** Attachments go out (WebDAV upload, then a share, on the `/notify` path). The
other direction is not done: sable cannot read a file somebody posts, which the account could now
do, and nothing has asked for it yet.

**A file posted to a locked conversation fails with a misleading error.** A read-only (locked)
conversation makes the share step of `/notify` answer 404 `Conversation not found`, apparently
because Talk's file-share handler treats it as missing rather than refusing with 403. sable
uploads the file, gets the 404, deletes the upload and returns 400, with nothing in the message
that points at the lock. Nothing in `src/` reads the room's `readOnly` field. The fix is to look
the room up with `GET /api/v4/room/{token}` before uploading and answer with a clear
"conversation is read-only", at the cost of one extra call per file post; failing that, the 404
message could name a lock as a likely cause. Not done because the failure is rare and obvious
once you know it. The link to the lock is inferred from the symptom, not checked against Talk's
source.

**Messages sent while sable is down are not answered.** Positions in each conversation are kept in
memory, and a conversation is followed from its newest message when sable starts, so anything
said during a restart or an outage is skipped rather than caught up on. Persisting the last
message id per conversation would close it, at the price of a state file and of deciding how old
a missed question may be before answering it would be strange.

**Long-poll load on Nextcloud.** One held request per conversation is the cost of the
user-account model and the thing to watch on a small server (a
[raised PHP pool](deployment.md#give-nextcloud-enough-php-workers) fixed it once). If it still
hurts, the options are a longer `SABLE_POLL_TIMEOUT`, fewer conversations, or a different shape.

The shape worth building is polling the conversation list instead of holding a request per
conversation: `GET /api/v4/room?modifiedSince=…` every few seconds returns only conversations
with newer activity, last message included, and a one-second chat poll then fetches what is new
from just those. That holds nothing open, at the cost of a few seconds of latency and one cheap
request per interval, and it would replace `SABLE_POLL_TIMEOUT` and `SABLE_ROOM_REFRESH` with a
single interval. It is **not** implemented, and it rests on something not yet verified: whether
a reaction moves a conversation's `lastMessage`, which the ⁉️ feature would need (it works today
because every conversation has its own poll, which sees the reaction directly).

## Assistant features

Tool calling is borrowed, not implemented: `SABLE_LLM_BACKEND=openwebui` hands the loop to Open
WebUI, which owns the tool registry and the credentials. Running its own loop would mean sable
becoming an MCP client and holding those credentials, a different project with a much larger
blast radius. The gap that borrowing leaves is authorization: Open WebUI decides what the account
may reach, and sable cannot say "only maser may call this one". It can say where
(`SABLE_LLM_TOOL_ROOMS`) and who may ask at all (`SABLE_LLM_USERS`), which is coarser: every tool
configured is on in every tools room. If finer control becomes a real need, an allowlist of tool
names checked against `ctx.is_admin` before the question is sent is the smaller half of the fix;
the other half is that the model, not sable, chooses the tool.

Retrieval is untouched, and the interesting question there is what corpus, and whether Nextcloud
Files is it.

`SABLE_LLM_MODEL` is global. A cheap model for chatter and an expensive one for a particular
room is a small change to `answer_with_llm`: both clients' `complete()` already take a model
override, but `answer_with_llm` does not pass one.

Model output is posted verbatim. If prompt injection becomes a real concern rather than a
theoretical one, the place to intervene is between the completion and the reply.

## Plugins

What [plugins](plugins.md) do not do yet, and what is known about doing it. The isolation items
are the gaps listed in [security.md](security.md#plugins-and-the-process-boundary); each fix below
is an idea that has not been tried here, and each would be Linux-specific.

**Reload.** A changed plugin or settings file takes effect when sable restarts. The fix is to
discover again and replace the workers, which has to settle what happens to commands registered
by the old set, to calls in flight, and to a name that now clashes. Worth doing when restarting
the bot to tweak a plugin hurts: a restart costs a few seconds in which nothing is read and
anything said is not answered ([messages sent while sable is down](#talk-features-not-yet-used)).

**A writable place for plugins.** In the shipped container, read-only root plus the one `/tmp`
mount in `compose.yaml`, a plugin can write `/tmp` and nothing else, which is gone on restart and
is shared with the `/notify` upload spool. That is a property of the container, not of plugins
themselves: on a bare-metal or systemd install nothing makes the rest of the filesystem read-only,
so a plugin can write anything the service user can
([what that costs](security.md#plugins-and-the-process-boundary)). A plugin that needs to remember
something has to keep it elsewhere, in a service of its own. The fix is a directory per plugin,
writable, outside the code directory, with a size cap; the cost is state that now has to be backed
up, and a place a plugin can fill.

**Pattern triggers.** Nothing in a plugin matches a message against a regular expression. A
pattern an author wrote is untrusted input to the matcher: a pathological one can hold a thread
for as long as it likes. The fixes are to run the match inside the worker, where the timeout
already kills it, or to use a matching engine that cannot backtrack. Until one of those, there is
no regex.

**Asking the model.** A plugin cannot make sable call its language model. The right shape is a
method on `Context` that goes through `answer_with_llm`, so that `SABLE_LLM_USERS` is asked of the
person who triggered the call, the history rules apply, and the plugin never holds the API key.
The open question is tools: a plugin in a room that is not a tools room must not be a way to use
them.

**Keeping a plugin out of files it does not need.** Plugins read whatever the sable user can, which
includes other plugins' settings files, although a worker is handed its own settings over the pipe
and never needs the file. Landlock (Linux 5.13 and later) lets a process give up access to paths
for itself and its children without privileges, so a worker could be confined to its plugin
directory, the interpreter and `/tmp`, which also closes the readable `.env`. It is called through
`ctypes`, there is no standard library wrapper, and whether the container runtime's default
seccomp profile lets it through is not something this was checked against. A newer kernel (6.7)
adds rules for TCP connections, which would be the egress policy; before that, the network is the
container's. A user per plugin would separate them more completely, and needs root at start or user
namespaces, which the image deliberately does without.

**Reaping without an init.** Orphans of a killed worker (what a plugin forked into a session of
its own) reparent to PID 1, which is sable - that part needs nothing extra: being PID 1 already
makes them sable's own children directly, with no `PR_SET_CHILD_SUBREAPER` required (that flag is
for a process that wants orphans to come to *it* instead of PID 1; sable already is PID 1). What is
actually missing is a reap loop: nothing in sable calls `waitpid` on a child it did not itself
start with `asyncio.create_subprocess_exec` (a worker, which it already reaps), so one of these
orphans exits but stays a zombie until the container restarts. An init in front of sable would reap
them, but it would hold the container's environment in a process a plugin can read
([security.md](security.md#plugins-and-the-process-boundary)), so `compose.yaml` has none. The fix
that keeps PID 1 non-dumpable is a small loop reaping any PID, not just the ones sable started
itself (`os.waitpid(-1, os.WNOHANG)` on a `SIGCHLD` handler, or polled), careful not to reap what
asyncio's own child-watching is waiting for. Not tried here.

## Supply chain and image hygiene

The lock file already gives reproducibility and hash verification for Python dependencies. These
would extend the same idea to the image, and none of them exist yet.

| Gap | What it would add |
| --- | --- |
| No image scanning | A `trivy` or `grype` step in CI, failing on high-severity CVEs in the base image |
| No SBOM | `docker buildx --sbom=true`, or `uv export` attached to the release |
| No image signing | `cosign` signatures, so a host can verify an image came from your CI |
| Single architecture | `platforms: linux/amd64,linux/arm64` in the build step, if anything ever runs on ARM |

## Maintenance cadence

The image pins a specific Python patch release. Before moving to 3.15, check that the compiled
dependencies — `uvloop`, `httptools`, `watchfiles`, `pydantic-core`, `websockets` — publish
`cp315` wheels, or the slim image will try to compile them from source.

`uv lock --upgrade` refreshes every dependency and `uv lock --upgrade-package httpx` does one.
There is no automation, since Forgejo has no Dependabot equivalent out of the box, so this is a
deliberate manual step. A scheduled workflow that opens a pull request with a refreshed lock is
the obvious improvement. uv itself is pinned twice, in the Dockerfile and in each workflow's
test step, so bumping it means changing both.

## Release mechanics

The version appears in `uv.lock` as well as `pyproject.toml`, so every release needs a
`uv lock`. A dynamic version would remove that churn but would move the version out of
`pyproject.toml`, which is where the release trigger reads it. Source archives are generated per
tag, so releases published before `.gitattributes` landed still contain `tests/` and `.forgejo/`.
Patching an old release is in [releasing.md](releasing.md#variations-you-may-want-later).

## Operations

There are no metrics: `/healthz` reports liveness and a little configuration, and there is no
`/metrics` endpoint. A Prometheus endpoint counting commands, model calls, latency and failures
is a contained addition if anyone wants dashboards.

There is no integration test. The suite covers everything without a network, which is why it is
fast and reliable, but nothing exercises a real Nextcloud. A compose-based test against a
throwaway instance would catch API drift that mocks cannot.

`sable --check` prints less than the startup block does, and the gap keeps widening: eight
settings (and a line per plugin, when plugins are on) against the block's twenty-one (twenty-two
with the tools line). It has never named
attachments, hooks or the ignore list, and now also misses the allowed rooms, who may use the
model, the rate limit, the concurrency ceiling and queue, the ask reaction, the API docs, the
health check, the proxy trust and every tool the model can reach — most of what somebody runs
`--check` to confirm before deploying, and none of the access settings that matter most. Either
it grows to match the block or it stops claiming to show the resolved configuration; feeding both
from the same summary helpers would keep them from drifting again, and `tests/test_docs.py`
already pins the block's shape.

## Decisions worth revisiting

| Decision | Revisit if |
| --- | --- |
| `uvicorn[standard]` pulls in eight packages, four of them compiled and most unused | Image size or Python upgrades become painful. Plain `uvicorn` needs only `click` and `h11`, but `sable --reload` needs `watchfiles` |
| A single `release` branch | You need to patch an old release while a newer one is out, which wants `release/1.x` branched from the tag |
| `latest` follows the newest release | It is the tag people deploy by accident. Consider not publishing it at all |
| Tests run on the runner's system Python rather than the version the image ships | `uv sync --python 3.14` would align them, if uv can fetch that interpreter on your runner |
| Manual builds all publish `dev` | Two people building different branches overwrite each other's. The commit tag distinguishes them and the digest pins them |
| Attachments are buffered in memory before upload | Large files become routine. Streaming straight through to WebDAV keeps memory flat regardless of size |
