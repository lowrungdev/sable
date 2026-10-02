"""!cert - how many days until a site's TLS certificate expires, or why it already failed.

Connects as a real TLS client (hostname and chain both checked, against this container's
own trust store - the same one an internal CA has to be installed into for anything else
sable does). An expired, self-signed or mismatched certificate fails verification before
the handshake finishes, so there is no certificate to read a date from; this plugin's own
value is in naming *why* for those, not in dating them. Read docs/plugins.md for the rules
this follows.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import socket
import ssl

from sable.plugin_api import Context, PluginError, command

DEFAULT_PORT = 443
CONNECT_TIMEOUT = 5.0
MAX_HOST_CHARS = 253  # the longest a DNS name can be
WARN_DAYS_DEFAULT = 14


def parse_target(raw: str) -> tuple[str, int]:
    """Split "host" or "host:port" into (host, port), with a user-facing error for either."""
    host, _, port_text = raw.strip().partition(":")
    host = host.strip().rstrip(".")
    if not host or len(host) > MAX_HOST_CHARS:
        raise PluginError("Say which host to check, like `example.com` or `example.com:8443`.")
    if not port_text:
        return host, DEFAULT_PORT
    try:
        port = int(port_text)
    except ValueError:
        raise PluginError(f"`{port_text}` is not a port number.") from None
    if not 1 <= port <= 65535:
        raise PluginError("Port must be between 1 and 65535.")
    return host, port


def parse_cert_time(value: str) -> dt.datetime:
    """Parse the format ssl's getpeercert() gives for notBefore/notAfter: 'Jun  1 12:00:00 2026 GMT'.

    Always UTC in practice (OpenSSL only ever reports GMT here), so the trailing zone name
    is dropped rather than trusted to a libc-dependent %Z match.
    """
    without_zone = value.rsplit(" ", 1)[0]
    return dt.datetime.strptime(without_zone, "%b %d %H:%M:%S %Y").replace(tzinfo=dt.UTC)


def fetch_expiry(host: str, port: int) -> dt.datetime:
    """Blocking: a verified TLS handshake, then the leaf certificate's notAfter.

    Blocking because there is no verified-TLS-with-a-timeout primitive in asyncio's own
    API; run this off the event loop (see `cert`, which uses asyncio.to_thread).
    """
    context = ssl.create_default_context()
    with socket.create_connection((host, port), timeout=CONNECT_TIMEOUT) as sock:
        with context.wrap_socket(sock, server_hostname=host) as tls:
            cert = tls.getpeercert()
    # Only reachable with CERT_REQUIRED (create_default_context's own default), which is
    # exactly when getpeercert() is documented to return the decoded fields rather than {}.
    return parse_cert_time(cert["notAfter"])


@command(
    "cert",
    aliases=("ssl", "tls"),
    help="Report a site's certificate expiry, or why it failed.",
    usage="cert <host[:port]>",
)
async def cert(ctx: Context) -> str:
    host, port = parse_target(ctx.args)
    warn_days = ctx.settings.get("warn_days", WARN_DAYS_DEFAULT)

    try:
        expiry = await asyncio.to_thread(fetch_expiry, host, port)
    except ssl.SSLCertVerificationError as exc:
        # OpenSSL's own reason ("certificate has expired", "self-signed certificate",
        # "Hostname mismatch", "unable to get local issuer certificate" for an internal CA
        # this container does not trust yet) is more useful here than anything built from
        # scratch, and it is exactly what a verified connection is for: this plugin never
        # has to parse a certificate it would not otherwise trust.
        raise PluginError(f"{host}:{port}'s certificate does not verify: {exc.verify_message}")
    except TimeoutError:
        raise PluginError(f"{host}:{port} did not answer within {CONNECT_TIMEOUT:.0f}s.") from None
    except socket.gaierror as exc:
        raise PluginError(f"Could not resolve `{host}`: {exc.strerror}.") from None
    except (ConnectionError, OSError) as exc:
        raise PluginError(f"Could not reach {host}:{port}: {exc}.") from None
    except ssl.SSLError as exc:
        raise PluginError(f"{host}:{port} did not speak TLS: {exc}.") from None

    days = (expiry - dt.datetime.now(dt.UTC)).days
    when = expiry.date().isoformat()
    if days <= warn_days:
        return f"⚠️ **{host}**'s certificate expires in **{days} day{'s' if days != 1 else ''}** ({when})."
    return f"**{host}**'s certificate is good for {days} more days (until {when})."
