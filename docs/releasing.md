# Branches, versions and releases

## The short version

```bash
git switch dev                              # do your work here
# ... commits ...
git push origin dev

git switch main && git merge --no-ff dev    # integrate when it is green
git push origin main
```

To release, bump one line and push it to `main`:

```toml
# pyproject.toml
version = "0.2"
```

Forgejo notices that the version changed and publishes
`forgejo.subversive.link/subversive/sable:0.2`, plus `:latest` and
`:build-<n>`. Nothing else to run — see [CI/CD on Forgejo](#cicd-on-forgejo).

## The three branches

| Branch | Role |
| --- | --- |
| `dev` | Where work happens. Commit here directly, or merge feature branches into it. Expected to be ahead of `main` most of the time. |
| `main` | The main commit history. `dev` is merged in when it is ready to be integrated. **A version change landing here is what publishes a release.** |
| `release` | Optional pointer at what is currently released. Nothing moves it automatically; see [below](#the-release-branch). |

```
dev      ──●──●──●───────────────●──●──────────
            \                   /        \
main     ────●─────────────────●──────────●────
                               │          │
                            0.2        0.3      ← version bumps, each one a build
                               │          │
registry     sable:0.2 ────────●          │
             sable:0.3 ───────────────────●     ← immutable, one per release
             sable:latest ────────────────●
```

### Why `--no-ff` when merging `dev` into `main`

A fast-forward merge makes `main` and `dev` the same line of history, and "the main commit
history" then has no marker for where an integration happened. `--no-ff` creates a merge commit,
so `git log --first-parent main` reads as a list of integrations rather than every individual
development commit.

A merge commit is also perfectly fine as the trigger: the workflow compares against the merge's
first parent, which is the previous tip of `main`.

## Versions

**MAJOR.MINOR only** — `0.1`, `1.0`, `1.1`, `1.2`. No patch segment, no `-rc`, no build
metadata. Bump the minor for anything shippable; bump the major when you break how the thing is
configured or deployed and someone upgrading has to do something about it.

It is set in **one place**, as an ordinary field:

```toml
# pyproject.toml
[project]
name = "sable"
version = "0.1"
```

Nothing else holds a copy. `sable.__version__` reads the installed package metadata, which the
build backend generates from that field, so the number reported by the `!version` command and by
`GET /healthz` cannot disagree with it. Three tests enforce the scheme: the field is
`MAJOR.MINOR`, the package reports a real version, and no `dynamic`/`[tool.hatch.version]`
indirection has crept back in.

One local wrinkle: an editable install caches the metadata at install time, so after bumping the
version run `pip install -e '.[dev]'` again if you want `!version` to be accurate on your own
machine. Built images are always correct, since every build installs fresh.

## Cutting a release

1. Bump `version` in `pyproject.toml` on `dev` and commit it — `Release 0.2` is a fine message.
2. Merge `dev` into `main` and push.
3. Watch the **Release** workflow in Forgejo's Actions tab.

That is the whole procedure. There is no release script, no tagging step and no second file to
keep in step.

Bumping the version directly on `main` works too, if you would rather not round-trip through
`dev`.

## CI/CD on Forgejo

Everything runs on Forgejo Actions, in [`.forgejo/workflows/`](../.forgejo/workflows). Both
workflows use the `docker-cli` runner and need nothing but Docker.

| Workflow | Runs on | Does |
| --- | --- | --- |
| [`test.yml`](../.forgejo/workflows/test.yml) | Pushes to `dev`, `main`, `release`; PRs into `dev` or `main` | `docker build --target test .` |
| [`release.yml`](../.forgejo/workflows/release.yml) | Pushes to `main` that touch `pyproject.toml` | Publishes, **if the version actually changed** |

The test suite lives in the **Dockerfile's `test` stage**, so CI needs no Python toolchain, no
matrix and no cached virtualenv — one `docker build` either passes or fails, and the same command
reproduces CI exactly on your laptop:

```bash
docker build --target test .
```

### How the release trigger decides

`pyproject.toml` changes for all sorts of reasons — a new dependency, a pytest setting — and none
of those should republish an image. So the first step reads the version from the current commit
and from its first parent, and:

| Situation | What happens |
| --- | --- |
| Version changed (`0.1` → `0.2`) | Tests run, image is built and pushed |
| `pyproject.toml` changed, version did not | Job ends green, having done nothing |
| Version went backwards (`1.0` → `0.9`) | Fails, before anything is published |
| Version is not `MAJOR.MINOR` (`1.0.1`) | Fails, before anything is published |

So a release is never published twice, and an unrelated edit never overwrites a published image.

### What gets published

| Image tag | Means |
| --- | --- |
| `…/sable:0.2` | That release, immutably. What a server should pin to. |
| `…/sable:latest` | The newest release. |
| `…/sable:build-<n>` | The CI run that produced it, for tracing a build back to its logs. |

### What it needs configured

Repository (or org) secrets, the same ones the other Subversive builds use:

| Secret | Used for |
| --- | --- |
| `SUBVERSIVE_ROOT` | Internal root CA, installed into the runner's trust store |
| `SUBVERSIVE_INTERMEDIATE` | Internal intermediate CA |
| `RUNDECK_KEY_VALUE` | Registry password for the `rundeck_automation` user |

Also: Actions enabled for the repository, and a runner registered with the `docker-cli` label.

If you publish under a different name or user, the three places to change are the `tags:` and
`labels:` blocks and the `username:` in `release.yml`.

## Using a release

Take the image CI built:

```bash
docker pull forgejo.subversive.link/subversive/sable:0.2
```

In `compose.yaml`, replacing `build: .` with
`image: forgejo.subversive.link/subversive/sable:0.2` pins a host to that release and skips
building on the server entirely. Pinning to a version rather than `latest` means a `docker
compose pull` cannot move you to a new release unintentionally.

Confirm what is actually running:

```bash
curl -fsS https://sable.example.org/healthz    # {"version":"0.2", ...}
```

…or ask it in chat with `!version`.

To find the source a release was built from, the workflow stamps the commit into the image:

```bash
docker image inspect forgejo.subversive.link/subversive/sable:0.2 \
  --format '{{index .Config.Labels "org.opencontainers.image.revision"}}'
```

### The `release` branch

Nothing moves it automatically, because that would mean giving the CI runner permission to push
to the repository. Move it by hand when you want a git ref that says "this is what is released":

```bash
git branch -f release main && git push origin release
```

Or ignore it — the registry already holds an immutable image per release, and `test.yml` will
keep running on it either way.

## Variations you may want later

**Git tags for releases.** There are none right now: a release is identified by its commit on
`main` and by its image tag. Without a git tag, finding the source of a release means searching:

```bash
git log --oneline -S 'version = "0.2"' -- pyproject.toml
```

If that gets old, `release.yml` can create the tag (and a Forgejo Release) itself after a
successful build. That needs a push-capable token as a secret, and on some forges a tag pushed by
a workflow does not trigger other workflows — worth checking before relying on it.

**Patch releases.** The scheme has no third segment, so a fix to `1.1` after `1.2` is out means
either `1.3` (roll forward, usually right) or branching from the release commit and tagging
`1.1.1` by hand.

**A changelog.** `git log --first-parent main` between two release commits is the honest version
of one. If you want a written `CHANGELOG.md`, write it on `dev` as you go and let the release
commit carry it.

**Building `main` on every push.** If you want a staging image from every integration, add a job
that pushes `…/sable:main`. Keep `latest` meaning *the latest release* — that is the tag people
deploy by accident.
