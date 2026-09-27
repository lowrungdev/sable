# Branches, versions and releases

## The short version

```bash
git switch dev                              # do your work here
# ... commits ...
git push origin dev

git switch main && git merge --no-ff dev    # integrate when it is green
git push origin main

bash scripts/release.sh 1.1                 # cut the release
```

That last command bumps the version, commits, tags `v1.1`, moves `release` to it, merges
`main` back into `dev`, and pushes all of it — after running the tests and refusing if anything
looks wrong. `--dry-run` shows every step without touching a thing.

Pushing the tag is what publishes: Forgejo Actions builds the image and pushes
`sable:1.1`, `sable:latest` and `sable:build-<n>` to the registry. See
[CI/CD on Forgejo](#cicd-on-forgejo).

## The three branches, and the tags

| Ref | Role |
| --- | --- |
| `dev` | Where work happens. Commit here directly, or merge feature branches into it. Expected to be ahead of `main` most of the time. |
| `main` | The main commit history. `dev` is merged in when it is ready to be integrated; this is the branch a release is cut from. |
| `release` | Always points at the **current** release — the same commit as the newest tag. "What is live" in one ref. |
| `v1.1`, `v1.2`, … | Annotated tags. **These are the point-in-time releases.** |

The distinction that matters: a branch is a moving pointer, so `release` can only ever mean
*the latest* release. Tags are immutable, so they are what let you say "the state of the code on
the day 1.1 went out" a year later. You need both — the branch for convenience, the tags for
history.

```
dev      ──●──●──●───────────────●──●──────────
            \                   /        \
main     ────●─────────────────●──────────●────
                               │          │
                            v1.1       v1.2      ← annotated tags
                               │          │
release  ──────────────────────●──────────●────  ← always the newest release
```

### Why `--no-ff` when merging `dev` into `main`

A fast-forward merge makes `main` and `dev` the same line of history, and "the main commit
history" then has no marker for where an integration happened. `--no-ff` creates a merge commit,
so `git log --first-parent main` reads as a list of integrations rather than every individual
development commit. The release script does not care either way; this is just what makes `main`
worth having as a separate branch.

## Versions

**MAJOR.MINOR only** — `0.1`, `1.0`, `1.1`, `1.2`. No patch segment, no `-rc`, no build
metadata. Bump the minor for anything shippable; bump the major when you break how the thing is
configured or deployed and someone upgrading has to do something about it.

The version lives in **exactly one place**:

```python
# src/sable/__init__.py
__version__ = "0.1"
```

`pyproject.toml` declares it `dynamic` and reads that line, so the package metadata, the
`!version` command and `GET /healthz` all report the same number and cannot drift. Two tests
enforce this: one that the version matches `MAJOR.MINOR`, one that `pyproject.toml` has not
grown a hardcoded copy.

Nothing else needs editing to release — no changelog file to remember, no second version
string, no Docker tag baked into a file.

## Cutting a release

```bash
bash scripts/release.sh 1.1              # the real thing, with a confirmation prompt
bash scripts/release.sh 1.1 --dry-run    # print every step, change nothing
bash scripts/release.sh 1.1 --yes        # skip the prompt (CI)
bash scripts/release.sh 1.1 --no-tests   # skip the test gate, if you verified another way
```

On Windows use Git Bash (`bash scripts/release.sh 1.1`), which ships with Git.

It stops before doing anything if:

- the version is not `MAJOR.MINOR`, is unchanged, or is older than the current one;
- you are not on `main`;
- the working tree is dirty;
- `main` has diverged from `origin/main` (push or pull first);
- the tag already exists locally or on the remote;
- `release` has commits `main` does not;
- the tests fail.

Nothing is pushed until every local step has succeeded, so a failure part-way leaves you with a
local commit and tag you can inspect, fix or delete:

```bash
git tag -d v1.1 && git reset --hard HEAD~1     # undo a local, unpushed release
```

Overrides, if your names differ: `RELEASE_REMOTE`, `RELEASE_TRUNK`, `RELEASE_DEV`,
`RELEASE_BRANCH`.

## CI/CD on Forgejo

Everything runs on Forgejo Actions, in [`.forgejo/workflows/`](../.forgejo/workflows). Both
workflows use the `docker-cli` runner and need nothing but Docker.

| Workflow | Runs on | Does |
| --- | --- | --- |
| [`test.yml`](../.forgejo/workflows/test.yml) | Pushes to `dev`, `main`, `release`; PRs into `dev` or `main` | `docker build --target test .` |
| [`release.yml`](../.forgejo/workflows/release.yml) | Pushes of a `v*` tag | Checks the tag against the packaged version, runs the suite, then builds and pushes the image |

The test suite lives in the **Dockerfile's `test` stage**, so CI does not need a Python
toolchain, a matrix or a cached virtualenv — one `docker build` either passes or fails, and the
same command reproduces CI exactly on your laptop:

```bash
docker build --target test .
```

`release.yml` publishes three tags for one release:

| Image tag | Means |
| --- | --- |
| `forgejo.subversive.link/subversive/sable:1.1` | That release, immutably. What a server should pin to. |
| `…/sable:latest` | The newest release. |
| `…/sable:build-<n>` | The CI run that produced it, for tracing a build back to its logs. |

Before the image is built, the workflow refuses a tag whose number disagrees with
`src/sable/__init__.py` — so a hand-made `git tag v9.9` fails loudly instead of publishing a
mislabelled image. `scripts/release.sh` cannot produce that mismatch.

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

```bash
git fetch --tags
git checkout v1.1              # exactly what went out
```

Or just take the image CI already built:

```bash
docker pull forgejo.subversive.link/subversive/sable:1.1
```

In `compose.yaml`, swapping `build: .` for
`image: forgejo.subversive.link/subversive/sable:1.1` pins a host to that release and skips
building on the server entirely.

On a deployed host, pinning to a tag rather than tracking `main` means a `git pull` cannot
surprise you mid-week. `release` is the alternative if you would rather always be on the newest
release: `git fetch && git reset --hard origin/release`.

Confirm what is actually running:

```bash
curl -fsS https://sable.example.org/healthz    # {"version":"1.1", ...}
```

…or ask it in chat with `!version`.

## Variations you may want later

**Patching an old release.** With a single `release` branch you cannot ship a fix for 1.1 while
1.2 is out — the branch has already moved on. When that day comes, branch from the tag:

```bash
git switch -c release/1.1 v1.1
# fix, then tag v1.1.1 on that branch (the one place a third segment makes sense)
```

**A changelog.** `git log --first-parent main` between two tags is the honest version of one:

```bash
git log --first-parent --oneline v1.1..v1.2
```

If you want a written `CHANGELOG.md`, write it on `dev` as you go and let the release commit
pick it up; do not try to generate it at release time from commit subjects.

**Building `main` too.** If you want a staging image from every integration, add a second job to
`test.yml` (or a third workflow) that pushes `…/sable:main` on pushes to `main`. Keep `latest`
meaning *the latest release* — that is the tag people deploy by accident.

**Signed tags.** `git config tag.gpgSign true`, and the script's `git tag -a` will sign.
