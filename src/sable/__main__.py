"""``python -m sable`` / ``sable`` - load the environment and serve."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path

from . import __version__
from .config import Config, ConfigError


def load_dotenv(path: Path) -> int:
    """Load ``KEY=value`` lines from a file. Existing env vars win."""
    if not path.is_file():
        return 0
    loaded = 0
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key.startswith("export "):
            key = key[len("export ") :].strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key and key not in os.environ:
            os.environ[key] = value
            loaded += 1
    return loaded


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sable", description="A Nextcloud Talk bot.")
    parser.add_argument("--env-file", default=".env", type=Path, help="defaults to ./.env")
    parser.add_argument("--host", help="overrides SABLE_HOST")
    parser.add_argument("--port", type=int, help="overrides SABLE_PORT")
    parser.add_argument("--reload", action="store_true", help="auto-reload on edits")
    parser.add_argument("--check", action="store_true", help="validate config and exit")
    parser.add_argument("--version", action="version", version=f"sable {__version__}")
    args = parser.parse_args(argv)

    load_dotenv(args.env_file)

    try:
        config = Config.from_env()
    except ConfigError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    logging.basicConfig(
        level=config.log_level,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    # httpx logs a line per request at INFO, which says less than our own line
    # about the same call and buries it. Let it through only at DEBUG.
    logging.getLogger("httpx").setLevel(
        logging.DEBUG if config.log_level == "DEBUG" else logging.WARNING
    )

    if args.check:
        print(
            f"sable {__version__} config OK\n"
            f"  bot name:   {config.bot_name}\n"
            f"  nextcloud:  {config.nextcloud_url or '(from webhook header)'}\n"
            f"  prefix:     {config.command_prefix}\n"
            f"  model:      {config.llm.model or '(disabled)'} @ {config.llm.base_url}\n"
            f"  ai rooms:   {', '.join(config.ai_rooms) or '(mentions only)'}\n"
            f"  notify:     {'enabled' if config.notify_enabled else 'disabled'}"
            f"{' aliases: ' + ', '.join(config.notify_rooms) if config.notify_rooms else ''}"
        )
        return 0

    import uvicorn

    uvicorn.run(
        "sable.app:create_app",
        factory=True,
        host=args.host or config.host,
        port=args.port or config.port,
        reload=args.reload,
        log_level=config.log_level.lower(),
        # Talk sends the signature over the raw body; never let a proxy rewrite it.
        proxy_headers=True,
        forwarded_allow_ips="*",
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
