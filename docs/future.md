# Future changes

Things that will or may need to change, each with the constraint that forces it. Nothing here is
scheduled. It is the list to consult when something starts hurting, so the reasoning does not
have to be worked out twice.

For what sable deliberately does not do and is not trying to become, see
[purpose.md](purpose.md). Some of those appear below as "revisit if", which is the honest
position: they were the right call for one bot on one server.

## Limitations with a known fix

**Durable conversation history.** `History` in [`history.py`](../src/sable/history.py) is an
in-process dictionary of deques with a turn cap and a time limit, so a restart forgets
everything. That is fine for a chat bot and wrong for anything that needs recall. The fix is to
swap the class for one backed by Redis or SQLite; it has three methods and `Bot` takes it as a
constructor argument, so nothing else changes. Worth doing when people start noticing that a
deploy loses context mid-conversation.

**One process only.** History, the message cache, the redelivery cache and the position in each
conversation all live in memory, so running two workers would split them: replies would forget
context depending on which worker answered. Two processes signed in as the same account are
worse, since each long-polls every conversation and answers every message, so a question gets two
replies. Moving that state into a shared store, and deciding which worker owns which
conversation, makes horizontal scaling real. For a chat assistant that is a long way off, so the
constraint is documented rather than treated as a bug. It is also the reason not to reach for two workers as a
throughput fix: `SABLE_MAX_CONCURRENT_REPLIES` raises the ceiling within one process without
splitting anything.

**No rate limiting on `/notify`.** Nothing stops a misconfigured alertmanager posting a thousand
messages. Talk will start returning 429 and the bot will log failures, but the noise has already
happened. A token bucket per room, or rate limiting at the proxy, fixes it. Something upstream
will misbehave eventually. Note what this is *not*: `SABLE_MAX_CONCURRENT_REPLIES` caps how many
model calls run at once, which bounds the resources a flood consumes, but it queues the work
rather than shedding it. A thousand alerts still become a thousand messages, just more slowly.

**Command authorization is global, not per room.** `SABLE_ADMIN_COMMANDS` and `SABLE_ADMIN_USERS`
say who may run what across every conversation the bot is in. What they cannot say is "maser may
deploy, but only from the ops room", or defer to Talk's own notion of a moderator — which is
the thing an operator reaches for next. The participant type already arrives on the event
(`event.actor.participant_type`), so a rule expressed against it is a small change; it is not
there yet because it is one more thing to get wrong for a bot whose admin list is usually two
names. Revisit when the first per-room rule is actually wanted.

## Talk features not yet used

**Reactions rely on an assumption that has not been checked.** The ⁉️ feature works only if Talk
delivers reactions as `reaction` system messages through the same chat poll that delivers
everything else, with the reacted-to message in `parent`. That is how the code reads them, and
the tests feed it exactly that shape, but it has **not been verified against a live Nextcloud**.
The Talk documentation says the `reaction` system message is replaced after the action
completes, so a poll may never see it, and that its text is a `{reaction}` placeholder rather than
the emoji; `_reaction()` reads the emoji from `message` and from `messageParameters`, and neither
is confirmed. A reaction a person removes themselves arrives as `reaction_deleted`, which is not
parsed, while `reaction_revoked` is a moderator removing someone else's. Until it has been
checked, treat the reaction as unproven: if it does nothing, `SABLE_LOG_LEVEL=DEBUG` will show
no `received Like` line, and the fix is in `parse_message` in
[`events.py`](../src/sable/events.py) or in how the poller asks for messages. Checking it is the
first thing to do against a real server.

Reactions are handled for one emoji: ⁉️ sends the message it is attached to to the model. Any
other reaction is parsed and ignored, so a second behaviour — an approval flow where a thumbs-up
from the right person does something — is a branch in `Bot.handle` next to the existing one.
Joining and leaving a conversation are not parsed at all; a greeting when sable is invited would
hang off `Poller.scan`, which is where it learns of a new conversation.

The chat API's message posting accepts a thread id, which [`talk.py`](../src/sable/talk.py) does
not pass. Replying in a thread rather than inline would suit the assistant in busy rooms.

The message cache is the weak point of the ⁉️ feature. It is in memory, bounded by
`SABLE_MESSAGE_CACHE` and expiring with `SABLE_HISTORY_TTL`, so reacting to anything older gets
"I do not have that message". Now that sable is a user it could fetch the message by id instead,
which would make the cache unnecessary, and that is the obvious fix. It is not done because the
cache also bounds what chat content sits in memory, and because fetching needs a call per
reaction.

File attachments are done, as the same account that posts: it uploads over WebDAV and shares into
the conversation, on the `/notify` path. The other direction is not done — sable cannot read a
file somebody posts, which the account could now do, and nothing has asked for it yet.

**Messages sent while sable is down are not answered.** Positions in each conversation are kept in
memory, and a conversation is followed from its newest message when sable starts, so anything
said during a restart or an outage is skipped rather than caught up on. Persisting the last
message id per conversation would close it, at the price of a state file and of deciding how old
a missed question may be before answering it would be strange.

**Long-poll load on Nextcloud.** One held request per conversation, up to 50 of them, is the cost of
the user-account model, and it is the thing to watch on a small server. If it hurts, the options
are a longer `SABLE_POLL_TIMEOUT`, fewer conversations, or a different shape: Talk can push to
a bot over a webhook, which costs an idle server nothing, at the price of everything the
[user-account model](purpose.md#what-it-deliberately-doesnt-do) was chosen to avoid.

## Assistant features

Tool calling is borrowed, not implemented: `SABLE_LLM_BACKEND=openwebui` hands the loop to Open
WebUI, which owns the tool registry and the credentials. sable running its own loop would mean
becoming an MCP client and holding those credentials here, which is a different project and a
much larger blast radius. The gap that borrowing leaves is authorization — Open WebUI decides
what the account may reach, and sable cannot say "only maser may call this one". If that
becomes a real need, an allowlist of tool names checked against `ctx.is_admin` before the
question is sent is the smaller half of the fix; the other half is that the model, not sable,
chooses the tool.

Retrieval is untouched, and the interesting question there is what corpus, and whether Nextcloud
Files is it.

`SABLE_LLM_MODEL` is global. A cheap model for chatter and an expensive one for a particular
room is a small change to `answer_with_llm`, which already takes a model override.

Model output is posted verbatim. If prompt injection becomes a real concern rather than a
theoretical one, the place to intervene is between the completion and the reply.

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

The version scheme has no third segment, so a fix to `1.1` after `1.2` is out means either
rolling forward to `1.3`, which is usually right, or branching from the `v1.1` tag and
publishing `1.1.1` by hand, outside the normal flow.

The project version also appears in `uv.lock`, so bumping it requires running `uv lock` before
the release will build — `uv sync --locked` refuses a stale lock. That is one extra command per
release and the guard catches you if you forget. A dynamic version would remove the churn but
would move the version out of `pyproject.toml`, which is where the release trigger reads it.

`docs/CHANGELOG.md` follows this project's rule that docs live in `docs/` rather than the
near-universal root convention. Moving it would mean updating the extraction in `release.yml`
and the test in `tests/test_version.py`.

Source archives are generated per tag, so releases published before `.gitattributes` landed
still contain `tests/` and `.forgejo/`.

## Operations

There are no metrics. `/healthz` reports liveness and a little configuration, optionally behind
`SABLE_HEALTH_TOKEN`; there is no `/metrics` endpoint. A Prometheus endpoint counting commands,
model calls, latency and failures is a contained addition if anyone wants dashboards.

There is no integration test. The suite covers everything without a network, which is why it is
fast and reliable, but nothing exercises a real Nextcloud. A compose-based test against a
throwaway instance would catch API drift that mocks cannot.

Logs are the only audit trail; see [security.md](security.md#accepted-risks).

`sable --check` prints less than the startup block does, and the gap keeps widening: eight
settings against the block's eighteen. It has never named attachments, hooks or the ignore list,
and now also misses the concurrency ceiling, the cached rooms, the API docs, the health check,
the proxy trust, the time zone and every tool the model can reach — most of
what somebody runs `--check` to confirm before deploying. Either it grows to match the block or
it stops claiming to show the resolved configuration; feeding both from the same summary helpers
would keep them from drifting again, and `tests/test_docs.py` already pins the block's shape.

## Decisions worth revisiting

| Decision | Revisit if |
| --- | --- |
| `uvicorn[standard]` pulls in eight packages, four of them compiled and most unused | Image size or Python upgrades become painful. Plain `uvicorn` needs only `click` and `h11`, but `sable --reload` needs `watchfiles` |
| A single `release` branch | You need to patch an old release while a newer one is out, which wants `release/1.x` branched from the tag |
| `latest` follows the newest release | It is the tag people deploy by accident. Consider not publishing it at all |
| Tests run on the runner's system Python rather than the version the image ships | `uv sync --python 3.14` would align them, if uv can fetch that interpreter on your runner |
| Manual builds all publish `dev` | Two people building different branches overwrite each other's. The commit tag distinguishes them and the digest pins them |
| Attachments are buffered in memory before upload | Large files become routine. Streaming straight through to WebDAV keeps memory flat regardless of size |
