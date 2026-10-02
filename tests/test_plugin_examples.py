"""The plugins in examples/plugins, loaded by the real plugin manager and the real worker.

They are documentation people copy, so they have to keep working: discovered, valid,
inactive as shipped, and doing what docs/plugins.md says once somebody sets rooms.
"""

from __future__ import annotations

import datetime as dt
import re
import shutil
import socket
import ssl
import subprocess
import threading
from collections.abc import Iterator
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
import respx
import yaml

from conftest import ROOM, event
from plugin_helpers import message_route, posix_only, texts
from sable.plugins import Status, default_command

pytestmark = posix_only
needs_openssl = pytest.mark.skipif(
    shutil.which("openssl") is None, reason="the certcheck tests sign certificates with openssl"
)

EXAMPLES = Path(__file__).resolve().parent.parent / "examples" / "plugins"


@pytest.fixture
def plugins(tmp_path: Path) -> Path:
    """A private copy of the examples, so a test may edit the settings files."""
    target = tmp_path / "plugins"
    shutil.copytree(EXAMPLES, target, ignore=shutil.ignore_patterns("__pycache__"))
    return target


def open_rooms(plugins: Path, name: str, **more: Any) -> None:
    """Put a room into an example's settings file, as an operator would, and
    optionally replace its `settings:` block."""
    path = plugins / name / f"{name}_settings.yaml"
    text = path.read_text(encoding="utf-8")
    assert "rooms: []" in text, f"{path.name} no longer ships with its rooms empty"
    text = text.replace("rooms: []", f'rooms: ["{ROOM}"]')
    document = yaml.safe_load(text)
    document.update(more)
    path.write_text(yaml.safe_dump(document), encoding="utf-8")


class Service:
    """A tiny HTTP server on loopback that answers by path and remembers who asked."""

    def __init__(self) -> None:
        seen: list[tuple[str, str | None]] = []
        self.seen = seen

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                seen.append((self.path, self.headers.get("Authorization")))
                self.send_response(503 if self.path == "/bad" else 200)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *args: object) -> None:
                return None

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    @staticmethod
    def refused_url() -> str:
        """A loopback address nothing listens on: bound to learn a free port, then closed."""
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return f"http://127.0.0.1:{sock.getsockname()[1]}/"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@pytest.fixture
def service() -> Iterator[Service]:
    found = Service()
    yield found
    found.close()


# --------------------------------------------------------------------------- #
# As shipped
# --------------------------------------------------------------------------- #


async def test_every_example_is_found_and_ships_inactive(rigs, plugins) -> None:
    rig = await rigs(plugins, command=default_command)
    assert sorted(r.name for r in rig.manager.records) == ["certcheck", "dice", "uptime"]
    for record in rig.manager.records:
        # Parsed, valid Python, settings accepted: and never started, because no rooms.
        assert record.status is Status.INACTIVE, (record.name, record.reason)
        assert record.reason == "no rooms set"
        assert record.worker is None
    assert rig.manager.notes == []


def test_the_examples_directory_holds_exactly_these_files() -> None:
    # Whoever copies a folder copies everything in it, so a stray file would travel.
    shipped = sorted(p.relative_to(EXAMPLES).as_posix() for p in EXAMPLES.rglob("*") if p.is_file())
    assert [name for name in shipped if "__pycache__" not in name] == [
        "README.md",
        "certcheck/certcheck.py",
        "certcheck/certcheck_settings.yaml",
        "dice/dice.py",
        "dice/dice_settings.yaml",
        "uptime/uptime.py",
        "uptime/uptime_settings.yaml",
    ]


# --------------------------------------------------------------------------- #
# dice
# --------------------------------------------------------------------------- #


@respx.mock
async def test_roll_works_through_the_bot_once_rooms_are_set(rigs, plugins) -> None:
    open_rooms(plugins, "dice")
    rig = await rigs(plugins, command=default_command)
    assert rig.record("dice").status is Status.ACTIVE, rig.record("dice").reason
    route = message_route()

    await rig.bot.handle(event("!roll 2d6"))
    await rig.bot.handle(event("!dice d20+3", message_id=101))
    first, second = texts(route)

    found = re.fullmatch(r"Alice rolled \*\*(\d+)\*\* \(2d6: (\d+) \+ (\d+)\)", first)
    assert found, first
    total, one, two = (int(group) for group in found.groups())
    assert 1 <= one <= 6
    assert 1 <= two <= 6
    assert total == one + two

    found = re.fullmatch(r"Alice rolled \*\*(\d+)\*\* \(d20\+3: (\d+) \+ 3\)", second)
    assert found, second
    assert int(found[1]) == int(found[2]) + 3
    assert 1 <= int(found[2]) <= 20


@respx.mock
async def test_roll_says_what_was_wrong_with_the_input(rigs, plugins) -> None:
    open_rooms(plugins, "dice")
    rig = await rigs(plugins, command=default_command)
    route = message_route()
    for number, text in enumerate(["!roll", "!roll banana", "!roll 99d6", "!roll d1", "!roll 0d6"]):
        await rig.bot.handle(event(text, message_id=200 + number))
    assert texts(route) == [
        "Say what to roll, like `2d6` or `d20+3`.",
        "Say what to roll, like `2d6` or `d20+3`.",
        "Roll between 1 and 20 dice at a time.",
        "A die has between 2 and 1000 sides.",
        "Roll between 1 and 20 dice at a time.",
    ]


@respx.mock
async def test_roll_stays_inside_the_rooms_it_was_given(rigs, plugins) -> None:
    open_rooms(plugins, "dice")
    rig = await rigs(plugins, command=default_command)
    route = message_route("efgh5678")
    await rig.bot.handle(event("!roll 2d6", room="efgh5678"))
    # Not one of its rooms: answered like a command that does not exist.
    assert texts(route) == ["I have no `roll` command. Try `!help`."]


# --------------------------------------------------------------------------- #
# uptime
# --------------------------------------------------------------------------- #


async def test_uptime_refuses_its_shipped_placeholders_once_it_would_run(rigs, plugins) -> None:
    open_rooms(plugins, "uptime")
    rig = await rigs(plugins, command=default_command)
    record = rig.record("uptime")
    # check() ran in the worker and said no, in the plugin's own words.
    assert record.status is Status.FAILED
    assert "its check rejected the settings" in record.reason
    assert "placeholder url" in record.reason


async def test_uptime_refuses_the_placeholder_token_too(rigs, plugins, service) -> None:
    open_rooms(
        plugins,
        "uptime",
        settings={"services": [{"name": "a", "url": service.url, "token": "REPLACE-ME"}]},
    )
    rig = await rigs(plugins, command=default_command)
    assert rig.record("uptime").status is Status.FAILED
    assert "placeholder token" in rig.record("uptime").reason


@respx.mock
async def test_uptime_checks_services_with_its_settings(rigs, plugins, service) -> None:
    open_rooms(
        plugins,
        "uptime",
        settings={
            "timeout": 3,
            "services": [
                {"name": "good", "url": f"{service.url}/ok", "token": "s3cret-token"},
                {"name": "bad", "url": f"{service.url}/bad"},
                {"name": "gone", "url": service.refused_url()},
            ],
        },
    )
    rig = await rigs(plugins, command=default_command, call_timeout=10)
    assert rig.record("uptime").status is Status.ACTIVE, rig.record("uptime").reason
    route = message_route()

    await rig.bot.handle(event("!up"))
    await rig.bot.handle(event("!uptime bad", message_id=101))
    await rig.bot.handle(event("!up nope", message_id=102))
    everything, only_bad, unknown = texts(route)

    lines = everything.splitlines()
    assert re.fullmatch(r"- \*\*good\*\*: up \(HTTP 200, \d+ ms\)", lines[0]), lines
    assert re.fullmatch(r"- \*\*bad\*\*: down \(HTTP 503, \d+ ms\)", lines[1]), lines
    assert lines[2] == "- **gone**: down (ConnectError)"
    assert re.fullmatch(r"- \*\*bad\*\*: down \(HTTP 503, \d+ ms\)", only_bad)
    assert unknown == "I only know: bad, gone, good."
    # The token went to the one service it was written for, and nowhere else.
    assert ("/ok", "Bearer s3cret-token") in service.seen
    assert all(auth is None for path, auth in service.seen if path == "/bad")
    # Neither the URL nor the token is in anything said to the room.
    assert "s3cret-token" not in " ".join(texts(route))
    assert service.url not in " ".join(texts(route))


# --------------------------------------------------------------------------- #
# certcheck
# --------------------------------------------------------------------------- #


def _run_openssl(*args: str) -> None:
    # A fixed, trusted argument list built only from this file's own constants and paths
    # under tmp_path - never from a message or anything a plugin wrote.
    subprocess.run(["openssl", *args], check=True, capture_output=True, text=True)  # noqa: S603,S607


def _asn1_time(offset: dt.timedelta) -> str:
    return (dt.datetime.now(dt.UTC) + offset).strftime("%Y%m%d%H%M%SZ")


def make_ca(path: Path) -> None:
    _run_openssl(
        "req",
        "-x509",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-keyout",
        str(path / "ca.key"),
        "-out",
        str(path / "ca.crt"),
        "-days",
        "3650",
        "-subj",
        "/CN=sable-test-ca",
        # Explicit, not just req's x509 default, so OpenSSL's own chain-building
        # verification (what certcheck.py relies on) accepts this as a real CA.
        "-addext",
        "basicConstraints=critical,CA:TRUE",
        "-addext",
        "keyUsage=critical,keyCertSign,cRLSign",
    )


def make_leaf(path: Path, name: str, not_before: dt.timedelta, not_after: dt.timedelta) -> None:
    """A certificate for localhost, signed by the throwaway CA, valid over the given window."""
    _run_openssl(
        "req",
        "-newkey",
        "rsa:2048",
        "-nodes",
        "-keyout",
        str(path / f"{name}.key"),
        "-out",
        str(path / f"{name}.csr"),
        "-subj",
        "/CN=localhost",
    )
    ext = path / f"{name}.ext"
    ext.write_text("subjectAltName=DNS:localhost,IP:127.0.0.1\n", encoding="utf-8")
    _run_openssl(
        "x509",
        "-req",
        "-in",
        str(path / f"{name}.csr"),
        "-CA",
        str(path / "ca.crt"),
        "-CAkey",
        str(path / "ca.key"),
        "-CAcreateserial",
        "-not_before",
        _asn1_time(not_before),
        "-not_after",
        _asn1_time(not_after),
        "-extfile",
        str(ext),
        "-out",
        str(path / f"{name}.crt"),
    )


class TlsServer:
    """A loopback TLS server for one certificate. Only the handshake matters: certcheck
    reads the peer certificate and closes, so nothing needs to be sent or received after."""

    def __init__(self, certfile: Path, keyfile: Path) -> None:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(certfile=str(certfile), keyfile=str(keyfile))
        self._context = context
        self._sock = socket.create_server(("127.0.0.1", 0))
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    @property
    def port(self) -> int:
        return int(self._sock.getsockname()[1])

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            with (
                suppress(ssl.SSLError, OSError),
                self._context.wrap_socket(conn, server_side=True) as tls,
                suppress(OSError),
            ):
                tls.recv(1)

    def close(self) -> None:
        self._sock.close()
        self._thread.join(timeout=5)


@pytest.fixture
def ca(tmp_path: Path) -> Path:
    """A throwaway CA, trusted only by the worker this test spawns (SSL_CERT_FILE), never
    installed anywhere real."""
    root = tmp_path / "ca"
    root.mkdir()
    make_ca(root)
    return root


@needs_openssl
@respx.mock
async def test_cert_warns_about_a_certificate_expiring_soon(rigs, plugins, ca, monkeypatch) -> None:
    make_leaf(ca, "soon", dt.timedelta(days=-1), dt.timedelta(days=10))
    server = TlsServer(ca / "soon.crt", ca / "soon.key")
    monkeypatch.setenv("SSL_CERT_FILE", str(ca / "ca.crt"))
    try:
        open_rooms(plugins, "certcheck")
        rig = await rigs(plugins, command=default_command)
        assert rig.record("certcheck").status is Status.ACTIVE, rig.record("certcheck").reason
        route = message_route()
        await rig.bot.handle(event(f"!cert localhost:{server.port}"))
        (reply,) = texts(route)
        pattern = r"⚠️ \*\*localhost\*\*'s certificate expires in \*\*(\d+) days?\*\* \(.+\)\."
        assert re.fullmatch(pattern, reply), reply
    finally:
        server.close()


@needs_openssl
@respx.mock
async def test_cert_reports_a_healthy_certificate_without_warning(
    rigs, plugins, ca, monkeypatch
) -> None:
    make_leaf(ca, "far", dt.timedelta(days=-1), dt.timedelta(days=90))
    server = TlsServer(ca / "far.crt", ca / "far.key")
    monkeypatch.setenv("SSL_CERT_FILE", str(ca / "ca.crt"))
    try:
        open_rooms(plugins, "certcheck")
        rig = await rigs(plugins, command=default_command)
        route = message_route()
        await rig.bot.handle(event(f"!cert localhost:{server.port}"))
        (reply,) = texts(route)
        assert "⚠️" not in reply
        pattern = r"\*\*localhost\*\*'s certificate is good for \d+ more days \(until .+\)\."
        assert re.fullmatch(pattern, reply), reply
    finally:
        server.close()


@needs_openssl
@respx.mock
async def test_cert_explains_an_already_expired_certificate(rigs, plugins, ca, monkeypatch) -> None:
    make_leaf(ca, "expired", dt.timedelta(days=-30), dt.timedelta(days=-1))
    server = TlsServer(ca / "expired.crt", ca / "expired.key")
    monkeypatch.setenv("SSL_CERT_FILE", str(ca / "ca.crt"))
    try:
        open_rooms(plugins, "certcheck")
        rig = await rigs(plugins, command=default_command)
        route = message_route()
        await rig.bot.handle(event(f"!cert localhost:{server.port}"))
        (reply,) = texts(route)
        assert "does not verify" in reply
        assert "expired" in reply.lower()
    finally:
        server.close()


@respx.mock
async def test_cert_says_what_was_wrong_with_bad_input(rigs, plugins) -> None:
    open_rooms(plugins, "certcheck")
    rig = await rigs(plugins, command=default_command)
    route = message_route()
    for number, text in enumerate(["!cert", "!cert host:notaport", "!cert host:999999"]):
        await rig.bot.handle(event(text, message_id=300 + number))
    first, second, third = texts(route)
    assert "Say which host to check" in first
    assert "is not a port number" in second
    assert "Port must be between" in third


@respx.mock
async def test_cert_stays_inside_the_rooms_it_was_given(rigs, plugins) -> None:
    open_rooms(plugins, "certcheck")
    rig = await rigs(plugins, command=default_command)
    route = message_route("efgh5678")
    await rig.bot.handle(event("!cert example.com", room="efgh5678"))
    assert texts(route) == ["I have no `cert` command. Try `!help`."]
