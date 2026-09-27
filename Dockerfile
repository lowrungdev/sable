# The image that ships. One stage, one job: install sable and run it.
#
# Dependencies come from uv.lock, so the same commit always installs the same
# 31 packages, verified by hash. Tests and the release wheel are built by CI,
# not here. See .forgejo/workflows/.

FROM python:3.14.7-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_NO_CACHE=1 \
    UV_LINK_MODE=copy \
    PATH="/app/.venv/bin:$PATH" \
    SABLE_HOST=0.0.0.0 \
    SABLE_PORT=8080

RUN useradd --create-home --uid 10001 sable

WORKDIR /app

# uv.lock pins everything; pyproject.toml points at the two docs files, so the
# project install needs them present.
COPY pyproject.toml uv.lock ./
COPY docs/README.md docs/LICENSE ./docs/
COPY src ./src

# --locked asserts uv.lock still matches pyproject.toml: if it does not, the
# build fails instead of quietly installing something else. uv is uninstalled in
# the same layer, since the image only needs the environment it produced.
RUN pip install uv==0.12.19 \
    && uv sync --locked --no-dev --no-editable \
    && pip uninstall -y -q uv

USER sable

EXPOSE 8080

# urlopen raises on a connection failure or a 4xx/5xx, which is a non-zero exit.
HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('SABLE_PORT', '8080') + '/healthz', timeout=3)"]

CMD ["python", "-m", "sable"]
