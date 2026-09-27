# Future changes

Things that will or may need to change, with the constraint that forces each one. Nothing here
is scheduled — this is the list to consult when something starts hurting, so that the reasoning
does not have to be rediscovered.

For what sable deliberately does **not** do and is not trying to become, see
[purpose.md](purpose.md#non-goals). Some of those non-goals appear below as "revisit if", which
is the honest position: they were right for one bot on one server.

## Limitations with a known fix

### Durable conversation history

`History` in [`history.py`](../src/sable/history.py) is an in-process dict of deques with a turn
cap and a TTL. A restart forgets everything, which is fine for a chat bot and wrong for anything
that needs recall.

**Fix:** swap the class for a Redis- or SQLite-backed one. It has three methods (`add`, `get`,
`clear`) and `Bot` takes it as a constructor argument, so nothing else changes.
**Do it when:** people start noticing that a deploy loses context mid-conversation.

### One process only

History and the redelivery de-duplication cache (`SEEN_CACHE = 512` in
[`bot.py`](../src/sable/bot.py)) are per-process. Running `--workers 2` would split both:
replies would forget context depending on which worker answered, and a redelivered webhook could
be handled twice.

**Fix:** move both into a shared store, at which point horizontal scaling is real.
**Do it when:** one process is genuinely saturated — which, for a chat bot handling a few
webhooks a minute, is a long way off. Until then the constraint is documented, not a bug.

### No rate limiting on `/notify`

Nothing stops a misconfigured alertmanager from posting a thousand messages. Talk will start
returning 429 and the bot will log failures, but the noise has already happened.

**Fix:** a token bucket per room in the app, or rate limiting at the proxy.
**Do it when:** something upstream misbehaves once. It will.

### No authorization on commands

Any participant can run any command. Fine for `!ping`, not fine for a command that deploys
something.

**Fix:** a decorator that checks `ctx.event.actor.user_id` against an allowlist, applied per
command rather than globally, since the point is that most commands stay open.
**Do it when:** you add the first command with side effects outside the chat.

## Talk features not yet used

**Threads.** The bot API's `sendMessage` accepts `threadTitle` and `threadId`, which
[`talk.py`](../src/sable/talk.py) does not pass. Replying in a thread rather than inline would
suit the assistant well in busy rooms. The parameters are already there; this is a small change
plus a config switch alongside `SABLE_REPLY_AS_REPLY`.

**Reactions** are handled for one emoji: ⁉️ sends the message it is attached to to the model.
Any other reaction is parsed and ignored, so a second behaviour — an approval workflow where 👍
from the right person does something — is a branch in `Bot.handle` next to the existing one.

**Join and leave** are parsed and only logged. A greeting when the bot is enabled in a
conversation would go in the same place.

**The message cache is the weak point of the ⁉️ feature.** It is in memory, bounded by
`SABLE_MESSAGE_CACHE` and expiring with `SABLE_HISTORY_TTL`, so reacting to anything older gets
"I do not have that message". Making that reliable means persistence — see the note on durable
history above, and the discussion of why a chat *log* is a different and much larger commitment.

**File attachments** are done, in a hybrid shape: the webhook bot still receives, and a separate
Nextcloud user account uploads over WebDAV and shares into the conversation, on the `/notify`
path only. What is *not* done is the other direction — sable cannot read a file somebody posts,
which would need that same account to fetch it. Nothing has asked for it yet.

## Assistant features

- **Tool calling.** The assistant sees the conversation and nothing else, by design. A tool loop
  belongs inside a command, where the blast radius is explicit and you control the authorization.
- **Retrieval.** Same reasoning. If it happens, the interesting question is what corpus — and
  whether Nextcloud Files is it.
- **Per-room models.** `SABLE_LLM_MODEL` is global. A cheap model for chatter and an expensive
  one for a specific room is a small change to `answer_with_llm`, which already takes a `model`
  override.
- **Output filtering.** Model output is posted verbatim. If injection becomes a real concern
  rather than a theoretical one, the place to intervene is between `complete()` and `reply()`.

## Supply chain and image hygiene

These are the natural next steps and none of them exist yet:

| Gap | What it would add |
| --- | --- |
| No image scanning | A `trivy`/`grype` step in CI, failing on high-severity CVEs in the base image |
| No SBOM | `docker buildx --sbom=true`, or `uv export` attached to the release |
| No image signing | `cosign` signatures, so a host can verify the image came from your CI |
| Single architecture | `platforms: linux/amd64,linux/arm64` in the build step, if anything ever runs on ARM |

The lock file already gives reproducibility and hash verification of Python dependencies; these
would extend the same idea to the image.

## Maintenance cadence

**Python.** The image pins `python:3.14.7-slim`. 3.15 is expected in late 2026 — before moving,
check that the compiled dependencies (`uvloop`, `httptools`, `watchfiles`, `pydantic-core`,
`websockets`) publish `cp315` wheels, or the slim image will try to compile them.

**Dependencies.** `uv lock --upgrade` refreshes everything; `uv lock --upgrade-package httpx`
does one. There is no automation — Forgejo has no Dependabot equivalent out of the box, so this
is a deliberate manual step. A scheduled workflow that opens a PR with a refreshed lock is the
obvious improvement.

**uv itself** is pinned twice, in the Dockerfile and in each workflow's test step. Bumping it
means changing both.

## Release mechanics

**Patch releases.** The scheme is `MAJOR.MINOR` with no third segment. A fix to `1.1` after
`1.2` is out means either rolling forward to `1.3` (usually right) or branching from the `v1.1`
tag and publishing `1.1.1` by hand, outside the normal flow.

**The version appears in `uv.lock`.** Bumping the version requires `uv lock` before the release
will build, because `uv sync --locked` refuses a stale lock. That is one extra command per
release, and the guard catches you if you forget. A dynamic version would remove the churn but
would move the version out of `pyproject.toml`, which is where the release trigger reads it.

**Changelog location.** `docs/CHANGELOG.md` follows this project's "docs live in `docs/`" rule
rather than the near-universal root convention. Moving it to the root means updating the awk in
`release.yml` and the test in `tests/test_version.py`.

**Old releases' source archives.** `.gitattributes` trims them, but archives are generated per
tag, so releases published before that landed still contain `tests/` and `.forgejo/`.

## Operations

- **No metrics.** `/healthz` reports liveness and configuration; there is no `/metrics`. If
  someone wants dashboards, a Prometheus endpoint counting commands, model calls, latency and
  failures is a contained addition.
- **No integration test.** The 104 tests cover everything without a network, which is why they
  are fast and reliable — but nothing exercises a real Nextcloud. A compose-based integration
  test against a throwaway Nextcloud would catch API drift that mocks cannot.
- **Logs only.** No audit trail beyond stdout; see [security.md](security.md#accepted-risks).

## Decisions worth revisiting

| Decision | Revisit if |
| --- | --- |
| `uvicorn[standard]` pulls 8 packages, 4 of them compiled, most unused | Image size or Python upgrades become painful. Plain `uvicorn` needs only `click` and `h11`, but `sable --reload` needs `watchfiles` |
| Single `release` branch | You ever need to patch an old release while a newer one is out — then `release/1.x` branches from the tag |
| `:latest` follows the newest release | It is the tag people deploy by accident. Consider not publishing it at all |
| Tests run on the runner's system Python, not 3.14 | It means CI tests a different Python than the image ships. `uv sync --python 3.14` would align them, if uv can fetch that interpreter on your runner |
| Manual builds all publish `:dev` | Two people building different branches overwrite each other's `:dev`. The commit tag distinguishes them; the digest pins them |
