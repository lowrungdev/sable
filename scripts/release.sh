#!/usr/bin/env bash
#
# Cut a release. See docs/releasing.md.
#
#   bash scripts/release.sh 1.1              # the real thing
#   bash scripts/release.sh 1.1 --dry-run    # print every step, change nothing
#   bash scripts/release.sh 1.1 --yes        # no confirmation prompt (for CI)
#
# What it does, in order, stopping at the first problem:
#   1. checks the version, the branch, the working tree and that you are in sync
#   2. runs the tests
#   3. writes the version into src/sable/__init__.py and commits it on main
#   4. tags that commit v<version>
#   5. fast-forwards release to it, and merges main back into dev
#   6. pushes main, release, dev and the tag
#
# Nothing is pushed until every local step has succeeded.

set -euo pipefail

REMOTE="${RELEASE_REMOTE:-origin}"
TRUNK="${RELEASE_TRUNK:-main}"
DEV="${RELEASE_DEV:-dev}"
REL="${RELEASE_BRANCH:-release}"
VERSION_FILE="src/sable/__init__.py"

VERSION=""
DRY_RUN=0
ASSUME_YES=0
RUN_TESTS=1

die() { printf '\nerror: %s\n' "$*" >&2; exit 1; }
step() { printf '\n==> %s\n' "$*"; }
run() {
    if [ "$DRY_RUN" -eq 1 ]; then
        printf '   would run: %s\n' "$*"
    else
        "$@"
    fi
}

for arg in "$@"; do
    case "$arg" in
        --dry-run) DRY_RUN=1 ;;
        --yes|-y) ASSUME_YES=1 ;;
        --no-tests) RUN_TESTS=0 ;;
        -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
        -*) die "unknown option: $arg" ;;
        *)
            [ -z "$VERSION" ] || die "give exactly one version"
            VERSION="$arg"
            ;;
    esac
done

# ---------------------------------------------------------------- checks ----
[ -n "$VERSION" ] || die "usage: bash scripts/release.sh <major.minor> [--dry-run] [--yes] [--no-tests]"

VERSION="${VERSION#v}"
[[ "$VERSION" =~ ^[0-9]+\.[0-9]+$ ]] \
    || die "version must be MAJOR.MINOR, e.g. 1.1 (got '$VERSION')"

cd "$(git rev-parse --show-toplevel)" || die "not in a git repository"
[ -f "$VERSION_FILE" ] || die "$VERSION_FILE not found"

TAG="v$VERSION"
CURRENT="$(sed -nE 's/^__version__ = "(.*)"$/\1/p' "$VERSION_FILE")"
[ -n "$CURRENT" ] || die "could not read __version__ from $VERSION_FILE"

step "sable $CURRENT -> $VERSION  (tag $TAG)"

# Refuse to go backwards or sideways: sort -V puts the smaller version first.
if [ "$VERSION" = "$CURRENT" ]; then
    die "$VERSION is already the current version"
fi
if [ "$(printf '%s\n%s\n' "$CURRENT" "$VERSION" | sort -V | head -1)" != "$CURRENT" ]; then
    die "$VERSION is older than the current $CURRENT"
fi

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
[ "$BRANCH" = "$TRUNK" ] \
    || die "releases are cut from $TRUNK, but you are on $BRANCH. Merge $DEV into $TRUNK first:
    git switch $TRUNK && git merge --no-ff $DEV"

[ -z "$(git status --porcelain)" ] \
    || die "the working tree has uncommitted changes; commit or stash them first"

step "fetching $REMOTE"
git fetch --quiet --tags "$REMOTE"

git rev-parse --quiet --verify "refs/tags/$TAG" >/dev/null \
    && die "tag $TAG already exists locally"
git ls-remote --exit-code --tags "$REMOTE" "refs/tags/$TAG" >/dev/null 2>&1 \
    && die "tag $TAG already exists on $REMOTE"

if git rev-parse --quiet --verify "refs/remotes/$REMOTE/$TRUNK" >/dev/null; then
    [ "$(git rev-parse HEAD)" = "$(git rev-parse "$REMOTE/$TRUNK")" ] \
        || die "$TRUNK and $REMOTE/$TRUNK have diverged; push or pull first"
fi

# release must be an ancestor of trunk, or fast-forwarding it would lose commits.
if git rev-parse --quiet --verify "refs/heads/$REL" >/dev/null; then
    git merge-base --is-ancestor "$REL" HEAD \
        || die "$REL is not an ancestor of $TRUNK; it has commits $TRUNK does not.
    Reconcile them by hand, then re-run."
fi

# ----------------------------------------------------------------- tests ----
if [ "$RUN_TESTS" -eq 1 ]; then
    step "running the tests"
    if command -v pytest >/dev/null 2>&1; then
        run pytest -q
    elif python -c 'import pytest' >/dev/null 2>&1; then
        run python -m pytest -q
    else
        die "pytest is not installed. Run 'pip install -e .[dev]', or pass --no-tests
    if you have verified the build another way."
    fi
else
    step "skipping the tests (--no-tests)"
fi

# --------------------------------------------------------------- confirm ----
cat <<SUMMARY

About to release sable $VERSION:
  bump      $VERSION_FILE  ($CURRENT -> $VERSION)
  commit    on $TRUNK
  tag       $TAG (annotated)
  advance   $REL to $TRUNK
  merge     $TRUNK back into $DEV
  push      $TRUNK, $REL, $DEV and $TAG to $REMOTE
SUMMARY

if [ "$DRY_RUN" -eq 0 ] && [ "$ASSUME_YES" -eq 0 ]; then
    printf '\nProceed? [y/N] '
    read -r reply
    case "$reply" in [yY]|[yY][eE][sS]) ;; *) die "cancelled" ;; esac
fi

# ------------------------------------------------------------------ bump ----
step "bumping the version"
if [ "$DRY_RUN" -eq 0 ]; then
    sed -i -E "s/^__version__ = \".*\"$/__version__ = \"$VERSION\"/" "$VERSION_FILE"
    [ "$(sed -nE 's/^__version__ = "(.*)"$/\1/p' "$VERSION_FILE")" = "$VERSION" ] \
        || die "failed to write the version into $VERSION_FILE"
    git add "$VERSION_FILE"
else
    printf '   would set __version__ = "%s"\n' "$VERSION"
fi

step "committing and tagging"
run git commit -m "Release $VERSION"
run git tag -a "$TAG" -m "sable $VERSION"

# --------------------------------------------------------------- branches ---
step "advancing $REL"
run git branch -f "$REL" "$TRUNK"

step "merging $TRUNK back into $DEV"
if git rev-parse --quiet --verify "refs/heads/$DEV" >/dev/null; then
    if [ "$DRY_RUN" -eq 0 ]; then
        git switch --quiet "$DEV"
        if ! git merge --no-edit "$TRUNK"; then
            git merge --abort || true
            git switch --quiet "$TRUNK"
            die "merging $TRUNK into $DEV conflicts. The release is committed and
    tagged locally but nothing has been pushed. Resolve it by hand:
        git switch $DEV && git merge $TRUNK
    then push: git push $REMOTE $TRUNK $REL $DEV && git push $REMOTE $TAG"
        fi
        git switch --quiet "$TRUNK"
    else
        printf '   would merge %s into %s\n' "$TRUNK" "$DEV"
    fi
else
    printf '   %s does not exist locally; skipping\n' "$DEV"
    DEV=""
fi

# ------------------------------------------------------------------ push ----
step "pushing to $REMOTE"
# shellcheck disable=SC2086 # DEV is deliberately unquoted: it may be empty.
run git push "$REMOTE" "$TRUNK" "$REL" $DEV
run git push "$REMOTE" "$TAG"

cat <<DONE

Released sable $VERSION.

  See what went out:      git show $TAG
  Build its image:        docker build -t sable:$VERSION .
  Back to development:    git switch ${DEV:-$TRUNK}

To deploy this exact release, on the server:
  git fetch --tags && git checkout $TAG
DONE
