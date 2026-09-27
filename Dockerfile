FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    SABLE_HOST=0.0.0.0 \
    SABLE_PORT=8080

WORKDIR /app

# Dependencies first, so edits to the source do not bust the layer cache.
COPY pyproject.toml ./
COPY docs/README.md docs/LICENSE ./docs/
COPY src ./src
RUN pip install --no-cache-dir .

RUN useradd --create-home --uid 10001 sable
USER sable

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request,os,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('SABLE_PORT','8080') + '/healthz', timeout=3).status == 200 else 1)"

CMD ["python", "-m", "sable"]
