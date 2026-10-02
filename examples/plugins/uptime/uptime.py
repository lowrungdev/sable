"""!up - ask a list of services whether they answer, and how fast.

A plugin that needs settings: the services come from `settings:` in uptime_settings.yaml,
which a plugin reads as `ctx.settings` (read-only, nested values included). It also shows
`check()`, which sable runs once when it loads the plugin so that a settings file that
still holds the shipped placeholders fails loudly at startup instead of in the chat.
Read docs/plugins.md for the rules this follows.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit

import httpx

from sable.plugin_api import Context, PluginError, command

PLACEHOLDER_TOKEN = "REPLACE-ME"  # noqa: S105 - the shipped placeholder, not a credential
# Hosts the shipped settings file uses. check() refuses them, so nobody runs the example
# unedited and sends a stranger's server a request on behalf of the room.
PLACEHOLDER_DOMAINS = ("example.org", "example.com", "example.net")
MAX_SERVICES = 10


def is_placeholder(host: str) -> bool:
    return any(host == domain or host.endswith("." + domain) for domain in PLACEHOLDER_DOMAINS)


def check(settings: Mapping[str, Any]) -> str | None:
    """Return why the settings are unusable, or None. Runs once, when sable loads this."""
    services = settings.get("services")
    if not isinstance(services, tuple | list) or not services:
        return "settings.services must be a list with at least one service"
    if len(services) > MAX_SERVICES:
        return f"settings.services may hold at most {MAX_SERVICES} services"
    timeout = settings.get("timeout", 5)
    if isinstance(timeout, bool) or not isinstance(timeout, int | float) or not 1 <= timeout <= 20:
        return "settings.timeout must be a number of seconds from 1 to 20"

    # The messages below say "service 2", not the service's name: sable blanks every string
    # of four characters or more from `settings:` out of what a plugin reports, since it
    # might be a secret, so a quoted name would show up as ***.
    names: set[str] = set()
    for number, service in enumerate(services, start=1):
        if not isinstance(service, Mapping):
            return f"service {number} must be a mapping with a name and a url"
        name, url = service.get("name"), service.get("url")
        if not isinstance(name, str) or not name.strip() or name in names:
            return f"service {number} needs a name of its own"
        names.add(name)
        parts = urlsplit(url) if isinstance(url, str) else None
        if parts is None or parts.scheme not in ("http", "https") or not parts.hostname:
            return f"service {number} needs an http(s) url"
        if is_placeholder(parts.hostname):
            return f"service {number} still has the placeholder url: replace it with your own"
        token = service.get("token")
        if token is not None and (not isinstance(token, str) or token == PLACEHOLDER_TOKEN):
            return f"service {number} still has the placeholder token: replace it or delete it"
    return None


@command(
    "up",
    aliases=("uptime",),
    help="Check whether the configured services answer.",
    usage="up [service]",
)
async def up(ctx: Context) -> str:
    services = {service["name"]: service for service in ctx.settings["services"]}
    if ctx.args:
        if ctx.args not in services:
            raise PluginError(f"I only know: {', '.join(sorted(services))}.")
        services = {ctx.args: services[ctx.args]}

    timeout = float(ctx.settings.get("timeout", 5))
    # No redirects followed: a service that redirects is answering, and following would
    # let a misconfigured one send this request somewhere that was never listed.
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        lines = await asyncio.gather(*(probe(client, name, s) for name, s in services.items()))

    ctx.log.info("checked %d service(s) for %s", len(lines), ctx.actor_id)
    return "\n".join(lines)


async def probe(client: httpx.AsyncClient, name: str, service: Mapping[str, Any]) -> str:
    headers = {"Authorization": f"Bearer {service['token']}"} if service.get("token") else {}
    started = time.monotonic()
    try:
        response = await client.get(service["url"], headers=headers)
    except httpx.HTTPError as exc:
        # The class name only: an httpx message can quote the URL, and a plugin's
        # output goes to the whole room.
        return f"- **{name}**: down ({type(exc).__name__})"
    millis = round((time.monotonic() - started) * 1000)
    # Any answer below 500 means the service is there; a 401 is still "up".
    state = "up" if response.status_code < 500 else "down"
    return f"- **{name}**: {state} (HTTP {response.status_code}, {millis} ms)"
