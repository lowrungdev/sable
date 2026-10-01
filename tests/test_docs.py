"""The documentation, read back as claims about this code.

The docs are the only thing between an operator and a misconfiguration, and
review does not catch a paragraph that was true last release: a startup-log
sample drifted three versions, a compose tag named a version that never existed,
a regex in security.md never matched the code. Like the changelog check in
test_version.py, these exist so the drift shows up on dev rather than in
somebody's deployment.

Only facts a machine can settle are checked here - which variables exist, what
the defaults are, which lines the startup log prints, whether a link goes
anywhere. Whether a sentence is *true* is still on the person writing it.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

import pytest
from conftest import make_config

from sable.app import create_app, tools_summary
from sable.config import Config, LLMConfig

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
ENV_EXAMPLE = ROOT / ".env.example"
COMPOSE = ROOT / "compose.yaml"
CONFIGURATION = DOCS / "configuration.md"
DEPLOYMENT = DOCS / "deployment.md"


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# Every variable the code reads, documented everywhere an operator looks
# --------------------------------------------------------------------------- #


#: The negative lookbehind carries the weight: a bare ``SABLE_[A-Z0-9_]+`` also
#: matches the tail of ``status.HTTP_422_UNPROCESSABLE_CONTENT`` in app.py and
#: invents a SABLE_CONTENT that nothing reads and nothing could document.
VARIABLE = re.compile(r"(?<![A-Z_])SABLE_[A-Z0-9_]+")

#: One variable per hook, collected by prefix scan, so the code carries the bare
#: prefix and the docs a ``<NAME>`` placeholder. Both name the same family.
HOOK_FAMILIES = ("SABLE_HOOK_TOKEN_", "SABLE_HOOK_TEMPLATE_")

#: Read by docker compose, to pick the host directory it bind-mounts as the CA
#: bundle, and never by sable. It belongs in the operator-facing files, so it is
#: documented without the code ever mentioning it.
NOT_READ_BY_SABLE = {"SABLE_HOST_CA_DIR"}

#: Where an operator goes looking for a setting. All three have to list all of
#: them: the .env template, the Compose environment block, and the reference.
OPERATOR_FILES = (ENV_EXAMPLE, COMPOSE, CONFIGURATION)


def variables(text: str) -> set[str]:
    """The ``SABLE_*`` names mentioned, each per-hook family collapsed into one."""
    found: set[str] = set()
    for name in VARIABLE.findall(text):
        for prefix in HOOK_FAMILIES:
            if name.startswith(prefix):
                name = f"{prefix}<NAME>"
                break
        found.add(name)
    return found


def variables_the_code_reads() -> set[str]:
    modules = sorted((ROOT / "src" / "sable").glob("*.py"))
    return set().union(*(variables(read(path)) for path in modules))


def test_the_pattern_does_not_invent_a_variable_out_of_a_constant() -> None:
    # app.py names status.HTTP_422_UNPROCESSABLE_CONTENT eleven times. The whole
    # reason for the lookbehind, and the reason not to keep an exclusion list.
    assert variables("status.HTTP_422_UNPROCESSABLE_CONTENT") == set()
    assert variables("SABLE_NEXTCLOUD_USER") == {"SABLE_NEXTCLOUD_USER"}


@pytest.mark.parametrize("path", OPERATOR_FILES, ids=lambda path: path.name)
def test_every_variable_the_code_reads_is_documented(path: Path) -> None:
    undocumented = sorted(variables_the_code_reads() - variables(read(path)))
    assert not undocumented, (
        f"{path.name} does not mention {', '.join(undocumented)}, which the code "
        f"reads. An operator who only reads that file cannot know the setting "
        f"exists."
    )


@pytest.mark.parametrize("path", OPERATOR_FILES, ids=lambda path: path.name)
def test_no_file_documents_a_variable_the_code_never_reads(path: Path) -> None:
    invented = sorted(variables(read(path)) - variables_the_code_reads() - NOT_READ_BY_SABLE)
    assert not invented, (
        f"{path.name} documents {', '.join(invented)}, which nothing in src/sable "
        f"reads. Either it was renamed in the code and not here, or setting it "
        f"does nothing at all."
    )


# --------------------------------------------------------------------------- #
# The Default column against the real defaults
# --------------------------------------------------------------------------- #


#: The settings configuration.md gives a Default for, mapped to the attribute
#: that default lands in. Not every documented variable: a required one, a
#: secret, and anything documented as *(unset)* have nothing to compare.
DOCUMENTED_DEFAULTS = {
    "SABLE_POLL_TIMEOUT": "poll_timeout",
    "SABLE_ROOM_REFRESH": "room_refresh",
    "SABLE_COMMAND_PREFIX": "command_prefix",
    "SABLE_ALLOWED_ROOMS": "allowed_rooms",
    "SABLE_LEAVE_UNLISTED_ROOMS": "leave_unlisted_rooms",
    "SABLE_AI_ROOMS": "ai_rooms",
    "SABLE_LLM_USERS": "llm_users",
    "SABLE_RATE_LIMIT": "rate_limit",
    "SABLE_MAX_QUEUED_REPLIES": "max_queued_replies",
    "SABLE_ASK_REACTION": "ask_reaction",
    "SABLE_UNKNOWN_COMMAND_HINT": "unknown_command_hint",
    "SABLE_REPORT_ERRORS": "report_errors",
    "SABLE_STARTUP_CHECK": "startup_check",
    "SABLE_MAX_MESSAGE_CHARS": "max_message_chars",
    "SABLE_HISTORY_TURNS": "history_turns",
    "SABLE_HISTORY_TTL": "history_ttl",
    "SABLE_MAX_HOOK_BYTES": "max_hook_bytes",
    "SABLE_UPLOAD_PATH": "upload_path",
    "SABLE_MAX_UPLOAD_BYTES": "max_upload_bytes",
    "SABLE_API_DOCS": "api_docs",
    "SABLE_MAX_CONCURRENT_REPLIES": "max_concurrent_replies",
    "SABLE_ASK_ADMINS_ONLY": "ask_admins_only",
    "SABLE_HEALTH_TOKEN": "health_token",
    "SABLE_TRUSTED_PROXIES": "trusted_proxies",
    "SABLE_HOST": "host",
    "SABLE_PORT": "port",
    "SABLE_LOG_LEVEL": "log_level",
    "SABLE_LOG_HEALTH_CHECKS": "log_health_checks",
}

#: The same, for settings that live on config.llm rather than on config.
DOCUMENTED_LLM_DEFAULTS = {
    "SABLE_LLM_BACKEND": "backend",
    "SABLE_LLM_TOOL_ROOMS": "tool_rooms",
    "SABLE_LLM_BUILTIN_TOOLS": "builtin_tools",
    "SABLE_LLM_POLL_INTERVAL": "poll_interval",
    "SABLE_LLM_KEEP_CHATS": "keep_chats",
    "SABLE_LLM_SHOW_SOURCES": "show_sources",
}


def default_cells() -> dict[str, str]:
    """The Default cell of every ``| Variable | Default | Notes |`` row.

    configuration.md has other tables - value formats, provider recipes - whose
    cells would otherwise be read as defaults, so the header decides.
    """
    cells: dict[str, str] = {}
    inside = False
    for line in read(CONFIGURATION).splitlines():
        if not line.startswith("|"):
            inside = False
            continue
        row = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if row[:3] == ["Variable", "Default", "Notes"]:
            inside = True
        elif inside and len(row) >= 2 and row[0].startswith("`"):
            cells[row[0].strip("`")] = row[1]
    return cells


def documented_value(variable: str, cell: str) -> str:
    """The value a Default cell states: ``262144`` out of "`262144` (256 KiB)"."""
    literal = re.search(r"`([^`]*)`", cell)
    if literal:
        return literal.group(1)
    assert cell == "*(empty)*", f"{variable}: no value to read out of the default {cell!r}"
    return ""


def written_as(value: object) -> str:
    """The setting as configuration.md spells it: a lowercase boolean, a list joined."""
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, list):
        return ",".join(str(entry) for entry in value)
    return str(value)


@pytest.fixture
def default_config(monkeypatch: pytest.MonkeyPatch) -> Config:
    """What an operator gets having set only the three required variables."""
    for key in list(os.environ):
        if key.startswith("SABLE_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SABLE_NEXTCLOUD_URL", "https://cloud.example.org")
    monkeypatch.setenv("SABLE_NEXTCLOUD_USER", "sable")
    monkeypatch.setenv("SABLE_NEXTCLOUD_PASSWORD", "app-password-1234")
    return Config.from_env()


def test_every_setting_with_a_documented_default_still_has_one() -> None:
    # Guards the mapping itself: a variable dropped from the reference, or one
    # whose Default column went away, would otherwise silently stop being checked.
    cells = default_cells()
    missing = sorted(
        variable
        for variable in {**DOCUMENTED_DEFAULTS, **DOCUMENTED_LLM_DEFAULTS}
        if variable not in cells
    )
    assert not missing, (
        f"{', '.join(missing)}: no '| Variable | Default | Notes |' row in "
        f"docs/configuration.md any more"
    )


@pytest.mark.parametrize("variable,attribute", sorted(DOCUMENTED_DEFAULTS.items()))
def test_the_documented_default_is_the_one_the_code_uses(
    variable: str, attribute: str, default_config: Config
) -> None:
    documented = documented_value(variable, default_cells()[variable])
    actual = written_as(getattr(default_config, attribute))
    assert documented == actual, (
        f"docs/configuration.md documents {variable} as defaulting to "
        f"{documented!r}, but an unset environment gives {actual!r}. Whichever is "
        f"wrong, an operator reading the table is being lied to."
    )


# --------------------------------------------------------------------------- #
# The startup-log sample in deployment.md
# --------------------------------------------------------------------------- #


#: An indented ``  label:`` line of the startup banner, in the log and in the
#: sample alike. Non-greedy up to the first colon, because the values hold
#: colons of their own (``http://0.0.0.0:8080``).
LABEL = re.compile(r"^ {2}(\S[^:]*):")


def labels(lines: list[str]) -> list[str]:
    return [match.group(1) for match in map(LABEL.match, lines) if match]


def sample_block() -> list[str]:
    """The fenced block in deployment.md that opens with the starting banner."""
    for body in re.findall(r"```[^\n]*\n(.*?)^```", read(DEPLOYMENT), re.S | re.M):
        first = body.splitlines()[0]
        if first.startswith("sable ") and first.endswith("starting"):
            return body.splitlines()
    raise AssertionError(
        "docs/deployment.md has no fenced block starting with 'sable <version> "
        "starting'; the sample log under 'What the log tells you' is gone"
    )


async def test_the_logged_startup_banner_is_the_one_deployment_md_shows(caplog) -> None:
    # Labels only. The values are whatever the deployment is configured with, and
    # the sample is deliberately somebody else's configuration.
    # receive=False: the lifespan must not start polling a server that is not there.
    app = create_app(make_config(), receive=False)
    with caplog.at_level(logging.INFO):
        async with app.router.lifespan_context(app):
            pass
    logged = labels(caplog.messages)
    documented = labels(sample_block())
    assert logged == documented, (
        f"the startup banner and its sample in docs/deployment.md have drifted.\n"
        f"  logged:     {logged}\n"
        f"  documented: {documented}\n"
        f"Add, remove or reorder the sample's lines to match - an operator checks "
        f"their own log against it line by line."
    )


def sample_tools_line() -> str:
    """The ``tools:`` line of the startup banner shown in deployment.md."""
    for line in read(DEPLOYMENT).splitlines():
        if line.startswith("tools:"):
            return line
    raise AssertionError("docs/deployment.md has no sample 'tools:' startup line")


def test_the_tools_line_in_deployment_md_is_the_one_the_code_builds() -> None:
    # The values are the ones the sample's own .ini block above it configures, so a
    # change to the wording or order of tools_summary shows up here.
    config = make_config(
        llm=LLMConfig(
            model="m",
            api_key="k",
            backend="openwebui",
            base_url="https://ai.example.org/api",
            tool_ids=["server:mcp:1", "server:mcp:2"],
            features=["web_search"],
            tool_rooms=["e5f6g7h8"],
        )
    )
    assert sample_tools_line() == f"tools:          {tools_summary(config)}"


def test_the_removed_echo_command_is_not_documented() -> None:
    # The `!echo` command is gone: nothing an operator reads should still offer it.
    # (The changelog is history and may name it; the removed settings are covered by
    # test_no_file_documents_a_variable_the_code_never_reads.)
    for path in [ENV_EXAMPLE, COMPOSE, *sorted(DOCS.glob("*.md"))]:
        if path.name == "CHANGELOG.md":
            continue
        assert "!echo" not in read(path), f"{path.name} still mentions the removed !echo command"


# --------------------------------------------------------------------------- #
# Links that go somewhere
# --------------------------------------------------------------------------- #


LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
HEADING = re.compile(r"^#{1,6}\s+(.*)$")
FENCE = re.compile(r"^\s*```")

LINKED_FILES = sorted(DOCS.glob("*.md")) + [ENV_EXAMPLE, COMPOSE]


def slug(heading: str) -> str:
    """A heading as a fragment, the way a Markdown renderer spells it."""
    kept = re.sub(r"[^\w\s-]", "", heading.strip().lower())
    return re.sub(r"\s+", "-", kept).strip("-")


def fragments(path: Path) -> set[str]:
    """Every heading in the file, slugified. Fenced blocks are skipped: a shell
    comment is not a heading, and treating one as a target hides a broken link."""
    found: set[str] = set()
    fenced = False
    for line in read(path).splitlines():
        if FENCE.match(line):
            fenced = not fenced
        elif not fenced:
            heading = HEADING.match(line)
            if heading:
                found.add(slug(heading.group(1)))
    return found


@pytest.mark.parametrize("path", LINKED_FILES, ids=lambda path: path.name)
def test_every_internal_link_resolves(path: Path) -> None:
    for target in LINK.findall(read(path)):
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        relative, _, fragment = target.partition("#")
        destination = (path.parent / relative).resolve() if relative else path
        # exists() rather than is_file(): releasing.md points at the workflow
        # directory, which is a reasonable thing to link to.
        assert destination.exists(), (
            f"{path.name} links to {target}, but {relative} does not exist. Something "
            f"was renamed or moved without its inbound links."
        )
        if fragment:
            assert fragment.lower() in fragments(destination), (
                f"{path.name} links to {target}, but no heading in {relative or path.name} "
                f"slugifies to {fragment!r}. The section was renamed or removed."
            )


@pytest.mark.parametrize("variable,attribute", sorted(DOCUMENTED_LLM_DEFAULTS.items()))
def test_the_documented_model_default_is_the_one_the_code_uses(
    variable: str, attribute: str, default_config: Config
) -> None:
    documented = documented_value(variable, default_cells()[variable])
    actual = written_as(getattr(default_config.llm, attribute))
    assert documented == actual, (
        f"docs/configuration.md documents {variable} as defaulting to "
        f"{documented!r}, but an unset environment gives {actual!r}."
    )
