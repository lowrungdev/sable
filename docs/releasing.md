# Branches, versions and releases

## The short version

Work on `dev`. Keep `main` as the history. Push to `release` to publish.

```bash
# 1. work, and write the changelog entry as you go
git switch dev
$EDITOR docs/CHANGELOG.md          # add notes under ## Unreleased
git commit -am "..." && git push origin dev

# 2. when you are ready to release, name the version
$EDITOR pyproject.toml             # version = "0.5"
$EDITOR docs/CHANGELOG.md          # rename ## Unreleased to ## 0.5
uv lock                            # the lock records the project version too
git commit -am "Release 0.5" && git push origin dev

# 3. integrate into the history
git switch main && git merge --no-ff dev && git push origin main

# 4. publish
git switch release && git merge --ff-only main && git push origin release
```

Step 4 is the release. Forgejo runs the tests, pushes `sable:0.5`, `sable:latest` and a tag
named after the commit, and creates a Forgejo Release with the tag, the changelog notes and the
wheel attached, which is what puts it in the repository sidebar.

Do not skip the `uv lock`. The lock file records the project's own version, so `uv sync
--locked` refuses a stale one and the release fails before it builds.

## The three branches

| Branch | Role | Builds |
| --- | --- | --- |
| `dev` | Where work happens. Commit here, or merge feature branches into it. | **Never automatically.** Tests run on every push; images only when you press the button. |
| `main` | The full commit history. Nothing publishes from here. | Tests only. |
| `release` | What is published. Advance it when you want a release to go out. | **Automatically**, on every push — plus a button. |

```
dev      ──●──●──●────────────●──●────────   button → sable:dev
            \                /       \
main     ────●──────────────●─────────●───   tests only
                            \         \
release  ────────────────────●─────────●──   push → publish
                             │         │
                            v0.4     v0.5    ← Releases, in the sidebar
                             │         │
registry            sable:0.4   sable:0.5, sable:latest
```

### Why `--no-ff` into `main` but `--ff-only` into `release`

`main` should read as a list of integrations, so each merge from `dev` gets its own merge commit
and `git log --first-parent main` stays useful. `release` should only ever be a point `main`
already passed through, so a fast-forward is the honest operation there. If `--ff-only` refuses,
something has been committed straight to `release` and wants looking at.

## Versions

The scheme is MAJOR.MINOR and nothing else: `0.4`, `1.0`, `1.1`. No patch segment, no release
candidates. Bump the minor for anything shippable, and the major when upgrading requires someone
to do something.

Set in **one place**, as an ordinary field:

```toml
# pyproject.toml
[project]
version = "0.1"
```

`sable.__version__` reads the installed package metadata, which the build backend generates
from that field, so `!version` and `GET /healthz` cannot disagree with it. An editable install
caches the metadata, so run `pip install -e '.[dev]'` again if you want a local bump reflected;
built images are always fresh.

The version is the release number, so **the version is what decides whether a push to
`release` publishes anything.** Push `release` without changing it and the workflow ends green
having done nothing — you cannot republish a version that already exists.

## The changelog is required

[`docs/CHANGELOG.md`](CHANGELOG.md) holds one section per release, and the section matching the
current version becomes the release body:

```markdown
## 0.2

- Thread replies under the triggering message
- Fix reaction cleanup when the model times out
```

A missing or empty section fails the test suite as well as the release. That is deliberate: a
release with no notes is not worth publishing, and the test means you find out on `dev` rather
than at publish time. Write notes under `## Unreleased` as you work, then rename that heading to
the version when you release.

## CI/CD on Forgejo

Three workflows in [`.forgejo/workflows/`](../.forgejo/workflows), all on the `docker-cli`
runner.

| Workflow | Runs on | Does |
| --- | --- | --- |
| [`test.yml`](../.forgejo/workflows/test.yml) | Pushes to `dev`, `main`, `release`; PRs into `dev` or `main` | Runs pytest |
| [`build.yml`](../.forgejo/workflows/build.yml) | **Button only** (`workflow_dispatch`) | Tests, then pushes `sable:dev` and `sable:<commit>` |
| [`release.yml`](../.forgejo/workflows/release.yml) | Pushes to `release`, **plus a button** | Tests, pushes the versioned image, creates the Forgejo Release |

Every workflow that builds an image runs the suite first, from the lock file:

```bash
uv sync --locked --extra dev
uv run --locked pytest -q
```

`--locked` asserts that `uv.lock` still matches `pyproject.toml` and fails if it does not, so a
dependency change without a re-lock cannot slip through. The same two commands reproduce CI on
your own machine.

The [`Dockerfile`](../Dockerfile) has nothing to do with testing — it is one stage that installs
sable from `uv.lock` and runs it, and `docker build .` builds exactly the image that ships.

### The buttons

**Actions → Manual build → Run workflow**, then pick a branch. That is how you get an image out
of `dev`, which never builds on its own. Everything built this way is pushed as `sable:dev` —
never `:latest`, never a version number, and no Release — so nothing you press there can be
mistaken for a release. `:dev` moves with each run; the summary prints the digest if you need to
pin a particular one.

**Actions → Release → Run workflow** rebuilds and re-pushes the image for whatever is on
`release` right now. It refuses to run on any other branch. Use it when a run failed halfway,
or when the registry lost an image. If the version already has a Release, it rebuilds the image
and leaves that Release untouched.

### What a release run decides

| Situation | What happens |
| --- | --- |
| The version has no Release yet | Tests run, the image is pushed, and a Release is created with the notes and the wheel |
| The version is already released and this was a push | Ends green, publishes nothing |
| The version is already released and this was the button | The image is rebuilt and re-pushed; the Release is left alone |
| The version is behind the latest Release | Fails before publishing |
| The version is not MAJOR.MINOR, or has no changelog section | Fails before publishing |

### What gets published

| Artifact | Where |
| --- | --- |
| `…/sable:0.5` | Packages: that release. What a server should pin to. |
| `…/sable:latest` | Packages: the newest release. |
| `…/sable:<commit>` | Packages: the full commit sha it was built from, so any image maps back to its source. |
| `…/sable@sha256:…` | Packages: the digest, printed in the run summary. Immutable, and the only way to pin one exact build. |
| Release `v0.5` and its git tag | Releases, in the repository sidebar |
| `sable-0.5-py3-none-any.whl` | Attached to that Release, when the wheel builds |

The wheel is archival only, so its step is `continue-on-error`: if it fails, the run warns, the
release is still created, and it simply has no attachment. Nothing about the release depends on
it.

### What it needs configured

| Secret | Used for |
| --- | --- |
| `SUBVERSIVE_ROOT` | Internal root CA, into the runner's trust store |
| `SUBVERSIVE_INTERMEDIATE` | Internal intermediate CA |
| `RUNDECK_KEY_VALUE` | Registry password for `rundeck_automation` |

Creating the Release uses the **runner's own token** — no secret to manage — read from
`FORGEJO_TOKEN`, falling back to `GITHUB_TOKEN`. It needs write access to the repository; if
Forgejo hands the runner a read-only token, the workflow fails with a message saying so, and the
fix is either the repository's Actions settings or adding a personal access token as a
`FORGEJO_TOKEN` secret. Nothing else changes if you go that route.

Also: Actions enabled for the repository, and a runner registered with the `docker-cli` label.

Publishing under a different name means editing the `tags:`/`labels:` blocks, the `username:`,
and `IMAGE` in `release.yml` and `build.yml`.

## Using a release

```bash
docker pull forgejo.subversive.link/subversive/sable:0.5
```

In `compose.yaml`, replacing `build: .` with
`image: forgejo.subversive.link/subversive/sable:0.5` pins a host to that release. Pin the
version rather than `latest`, so a `docker compose pull` cannot move you unintentionally.

Confirm what is running:

```bash
curl -fsS https://sable.example.org/healthz    # {"version":"0.5", ...}
```

…or ask it in chat with `!version`. To get from an image back to its source, the workflow stamps
the commit in:

```bash
docker image inspect forgejo.subversive.link/subversive/sable:0.5 --format '{{index .Config.Labels "org.opencontainers.image.revision"}}'
```

Or go the other way: every image is also tagged with the commit it was built from, so the tag
list in Packages tells you the source directly.

```bash
docker pull forgejo.subversive.link/subversive/sable:0937a9479cccec9ea4375de1621eb5e124d2c009
git show 0937a9479cccec9ea4375de1621eb5e124d2c009
```

Or use the git tag the Release created: `git checkout v0.5`.

## Variations you may want later

The version scheme has no third segment, so a fix to `1.1` after `1.2` is out means either
rolling forward to `1.3`, which is usually right, or branching from the `v1.1` tag and
publishing `1.1.1` by hand.

Forgejo has a PyPI registry, so uploading the wheel with twine would make `pip install sable`
work against your instance. That is left out because nothing consumes sable as a library; the
wheel is attached to each Release for archival only.

`release.yml` creates releases published rather than draft. Flipping that would make them appear
but stay unpublished until somebody presses publish, which is worth doing if you want a human
check on the notes before they go out.

The Manual build button already covers `main` when you want an image from it. If you would
rather that were automatic, add a `push` trigger for `main` to `build.yml` — but keep `latest`
meaning the latest release, since that is the tag people deploy by accident.
