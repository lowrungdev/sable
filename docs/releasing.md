# Branches, versions and releases

## The short version

Work on `dev`. Keep `main` as the history. Push to `release` to publish.

```bash
# 1. work, and write the changelog entry as you go
git switch dev
$EDITOR docs/CHANGELOG.md          # add bullets under ## Unreleased
git commit -am "..." && git push origin dev

# 2. when you are ready to release, name the version
$EDITOR pyproject.toml             # version = "0.2"
$EDITOR docs/CHANGELOG.md          # rename ## Unreleased to ## 0.2
git commit -am "Release 0.2" && git push origin dev

# 3. integrate into the history
git switch main && git merge --no-ff dev && git push origin main

# 4. publish
git switch release && git merge --ff-only main && git push origin release
```

Step 4 is the release. Forgejo then runs the tests, pushes
`sable:0.2` and `sable:latest` to the registry, and creates a **Forgejo Release**
`v0.2` — tag included, notes taken from the changelog, wheel attached — which is what
puts it in the repository sidebar.

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
                            v0.1     v0.2    ← Releases, in the sidebar
                             │         │
registry            sable:0.1   sable:0.2, sable:latest
```

### Why `--no-ff` into `main` but `--ff-only` into `release`

`main` should read as a list of integrations, so each merge from `dev` gets its own merge
commit (`git log --first-parent main`). `release` should only ever be a point that `main`
already passed through, so a fast-forward is the honest operation — if `--ff-only` refuses,
something has been committed straight to `release` and wants looking at.

## Versions

**MAJOR.MINOR only** — `0.1`, `1.0`, `1.1`. No patch segment, no `-rc`. Bump the minor for
anything shippable; bump the major when upgrading requires someone to do something.

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

A missing or empty section fails the test suite *and* the release. That is deliberate: a
release with no notes is not worth publishing, and the test means you find out on `dev` rather
than at publish time. Write bullets under `## Unreleased` as you work, then rename that heading
to the version when you release.

## CI/CD on Forgejo

Three workflows in [`.forgejo/workflows/`](../.forgejo/workflows), all on the `docker-cli`
runner.

| Workflow | Runs on | Does |
| --- | --- | --- |
| [`test.yml`](../.forgejo/workflows/test.yml) | Pushes to `dev`, `main`, `release`; PRs into `dev` or `main` | `docker build --target test .` |
| [`build.yml`](../.forgejo/workflows/build.yml) | **Button only** (`workflow_dispatch`) | Tests, then pushes `sable:<branch>` and `sable:<branch>-<sha>` |
| [`release.yml`](../.forgejo/workflows/release.yml) | Pushes to `release`, **plus a button** | Tests, pushes the versioned image, creates the Forgejo Release |

The suite lives in the Dockerfile's `test` stage, so CI needs nothing but Docker, and the same
command reproduces it exactly on your laptop:

```bash
docker build --target test .
```

### The buttons

**Actions → Manual build → Run workflow**, then pick a branch. That is how you get an image out
of `dev`, which never builds on its own. It pushes tags named after the branch — `sable:dev`,
`sable:dev-4344645` — and never `:latest`, never a version, and no Release. Nothing you press
there can be mistaken for a release.

**Actions → Release → Run workflow** rebuilds and re-pushes the image for whatever is on
`release` right now. It refuses to run on any other branch. Use it when a run failed halfway,
or when the registry lost an image. If the version already has a Release, it rebuilds the image
and leaves that Release untouched.

### What a release run decides

| Situation | What happens |
| --- | --- |
| Version has no Release yet | Tests, image pushed, Release created with notes and the wheel |
| Version already released, pushed to `release` | Ends green, publishes nothing |
| Version already released, started from the button | Image rebuilt and re-pushed; Release left alone |
| Version is behind the latest Release (`1.0` out, this says `0.9`) | Fails before publishing |
| Version is not `MAJOR.MINOR`, or has no changelog section | Fails before publishing |

### What gets published

| Artifact | Where |
| --- | --- |
| `…/sable:0.2` | Packages — that release, immutably. What a server should pin to. |
| `…/sable:latest` | Packages — the newest release. |
| `…/sable:build-<n>` | Packages — the CI run that made it, for tracing back to logs. |
| Release `v0.2` + git tag | **Releases**, in the repository sidebar |
| `sable-0.2-py3-none-any.whl` | Attached to that Release, when the wheel builds |

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
docker pull forgejo.subversive.link/subversive/sable:0.2
```

In `compose.yaml`, replacing `build: .` with
`image: forgejo.subversive.link/subversive/sable:0.2` pins a host to that release. Pin the
version rather than `latest` so a `docker compose pull` cannot move you unintentionally.

Confirm what is running:

```bash
curl -fsS https://sable.example.org/healthz    # {"version":"0.2", ...}
```

…or ask it in chat with `!version`. To get from an image back to its source, the workflow stamps
the commit in:

```bash
docker image inspect forgejo.subversive.link/subversive/sable:0.2 \
  --format '{{index .Config.Labels "org.opencontainers.image.revision"}}'
```

Or use the git tag the Release created: `git checkout v0.2`.

## Variations you may want later

**Patch releases.** The scheme has no third segment, so a fix to `1.1` after `1.2` is out means
either `1.3` (roll forward, usually right) or branching from the `v1.1` tag and publishing
`1.1.1` by hand.

**Publishing the wheel to a package registry.** Forgejo has a PyPI registry, so
`twine upload --repository-url .../api/packages/subversive/pypi dist/*` would make
`pip install sable` work against your instance. Left out because nothing consumes sable as a
library — the wheel is attached to the Release for archival only.

**Draft releases.** `release.yml` sets `draft: false`. Flipping it means releases appear but stay
unpublished until you press publish in the UI, which is useful if you want a human check on the
notes.

**Building `main` on every push.** The Manual build button already covers `main` when you want
it. If you would rather it be automatic, add a `push: branches: [main]` trigger to `build.yml` —
keep `latest` meaning *the latest release*, since that is the tag people deploy by accident.
