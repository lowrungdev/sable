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

**One process only.** History, the message cache and the redelivery cache all live in memory, so
running two workers would split them: replies would forget context depending on which worker
answered, and a redelivered webhook could be handled twice. Moving that state into a shared
store makes horizontal scaling real. For a bot handling a few webhooks a minute that is a long
way off, so the constraint is documented rather than treated as a bug.

**No rate limiting on `/notify`.** Nothing stops a misconfigured alertmanager posting a thousand
messages. Talk will start returning 429 and the bot will log failures, but the noise has already
happened. A token bucket per room, or rate limiting at the proxy, fixes it. Something upstream
will misbehave eventually.

**No authorization on commands.** Any participant can run any command, which is fine for `!ping`
and not fine for a command that deploys something. A decorator checking
`ctx.event.actor.user_id` against an allowlist, applied per command rather than globally, is the
shape — the point is that most commands stay open. Do it when you add the first command with
side effects outside the chat.

## Talk features not yet used

Reactions are handled for one emoji: ⁉️ sends the message it is attached to to the model. Any
other reaction is parsed and ignored, so a second behaviour — an approval flow where a thumbs-up
from the right person does something — is a branch in `Bot.handle` next to the existing one.
Joining and leaving a conversation are parsed and only logged; a greeting when the bot is
enabled would go in the same place.

The bot API's `sendMessage` accepts `threadTitle` and `threadId`, which
[`talk.py`](../src/sable/talk.py) does not pass. Replying in a thread rather than inline would
suit the assistant in busy rooms, and the parameters are already there.

The message cache is the weak point of the ⁉️ feature. It is in memory, bounded by
`SABLE_MESSAGE_CACHE` and expiring with `SABLE_HISTORY_TTL`, so reacting to anything older gets
"I do not have that message". Making it reliable means persistence, which is the same question
as durable history above.

File attachments are done, in a hybrid shape: the webhook bot still receives, and a separate
Nextcloud user account uploads over WebDAV and shares into the conversation, on the `/notify`
path only. The other direction is not done — sable cannot read a file somebody posts, which
would need that same account to fetch it, and nothing has asked for it yet.

## Assistant features

The assistant sees the conversation and nothing else, by design. Tool calling belongs inside a
command, where the blast radius is explicit and you control the authorization; retrieval is the
same argument, and the interesting question there is what corpus, and whether Nextcloud Files is
it.

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

There are no metrics. `/healthz` reports liveness and configuration; there is no `/metrics`
endpoint. A Prometheus endpoint counting commands, model calls, latency and failures is a
contained addition if anyone wants dashboards.

There is no integration test. The suite covers everything without a network, which is why it is
fast and reliable, but nothing exercises a real Nextcloud. A compose-based test against a
throwaway instance would catch API drift that mocks cannot.

Logs are the only audit trail; see [security.md](security.md#accepted-risks).

`sable --check` prints less than the startup block does — it predates the attachment and ignore
settings, and has not been brought back into line.

## Decisions worth revisiting

| Decision | Revisit if |
| --- | --- |
| `uvicorn[standard]` pulls in eight packages, four of them compiled and most unused | Image size or Python upgrades become painful. Plain `uvicorn` needs only `click` and `h11`, but `sable --reload` needs `watchfiles` |
| A single `release` branch | You need to patch an old release while a newer one is out, which wants `release/1.x` branched from the tag |
| `latest` follows the newest release | It is the tag people deploy by accident. Consider not publishing it at all |
| Tests run on the runner's system Python rather than the version the image ships | `uv sync --python 3.14` would align them, if uv can fetch that interpreter on your runner |
| Manual builds all publish `dev` | Two people building different branches overwrite each other's. The commit tag distinguishes them and the digest pins them |
| Attachments are buffered in memory before upload | Large files become routine. Streaming straight through to WebDAV keeps memory flat regardless of size |
