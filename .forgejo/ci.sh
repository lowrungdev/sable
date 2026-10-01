#!/bin/sh
# CI only: the checks every workflow runs before it is allowed to build or
# publish anything. test.yml, build.yml and release.yml each call this right
# after checking out the code, from the repository root, so the three can never
# drift apart. It is not a developer tool: contributors run the commands listed
# in CONTRIBUTING.md.
set -eu

# The one place CI pins uv.
UV_VERSION=0.12.19

python3 --version
python3 -m venv /tmp/uv
/tmp/uv/bin/pip install --quiet "uv==${UV_VERSION}"
UV=/tmp/uv/bin/uv
"$UV" --version

echo "== install the locked dependencies"
# --locked installs exactly what uv.lock pins, and fails if the lock no longer
# matches pyproject.toml. The dev extra carries ruff, mypy and pytest.
"$UV" sync --locked --extra dev

echo "== ruff check"
"$UV" run --locked ruff check .

echo "== ruff format --check"
"$UV" run --locked ruff format --check .

echo "== mypy"
"$UV" run --locked mypy

echo "== pytest"
"$UV" run --locked pytest -q
