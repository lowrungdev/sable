# Multi-stage: `docker build .` produces the runtime image; CI gates on
# `docker build --target test .`, which fails the build if pytest fails.
#
# With BuildKit (the default since Docker 23, and what buildx uses in CI) the
# test stage is skipped unless it is the target. The classic builder would run
# it either way, which is slower but not wrong.

FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Everything the build backend needs for metadata, including the version, which
# it reads from src/sable/__init__.py.
COPY pyproject.toml ./
COPY docs/README.md docs/LICENSE ./docs/
COPY src ./src

# --------------------------------------------------------------------------- #
# Tests. Never part of the runtime image.
FROM base AS test

COPY tests ./tests
# The changelog test reads it, and the release notes come from it.
COPY docs/CHANGELOG.md ./docs/
RUN pip install --no-cache-dir '.[dev]' \
    && python -m pytest -q

# --------------------------------------------------------------------------- #
# The wheel, for attaching to a Forgejo Release. CI extracts it with
# `docker create` + `docker cp`; nothing else needs this stage.
FROM base AS wheel

RUN pip install --no-cache-dir build \n    && python -m build --wheel --outdir /dist

# --------------------------------------------------------------------------- #
# The image that ships.
FROM base AS runtime

ENV SABLE_HOST=0.0.0.0 \
    SABLE_PORT=8080

RUN pip install --no-cache-dir .

RUN useradd --create-home --uid 10001 sable
USER sable

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,os,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('SABLE_PORT','8080') + '/healthz', timeout=3).status == 200 else 1)"

CMD ["python", "-m", "sable"]
