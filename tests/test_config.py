"""Reading the environment, and refusing it when it cannot work.

Most of these are about catching a mistake at startup rather than at three in
the morning when an alert does not arrive.
"""

from __future__ import annotations

import os
from typing import Callable

import pytest

from sable.config import Config, ConfigError

SECRET = "s" * 40
ROOM = "abcd1234"

Load = Callable[..., Config]


@pytest.fixture
def load(monkeypatch: pytest.MonkeyPatch) -> Load:
    """Build a config from an environment holding only what a test names."""
    for key in list(os.environ):
        if key.startswith("SABLE_"):
            monkeypatch.delenv(key, raising=False)

    def _load(*, nextcloud: bool = True, **env: str) -> Config:
        monkeypatch.setenv("SABLE_BOT_SECRET", SECRET)
        if nextcloud:
            monkeypatch.setenv("SABLE_NEXTCLOUD_URL", "https://cloud.example.org")
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        return Config.from_env()

    return _load


def test_the_minimum_is_a_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in list(os.environ):
        if key.startswith("SABLE_"):
            monkeypatch.delenv(key, raising=False)
    with pytest.raises(ConfigError, match="SABLE_BOT_SECRET is required"):
        Config.from_env()


# --------------------------------------------------------------------------- #
# Conversations, which are named by token and mistaken for names
# --------------------------------------------------------------------------- #


def test_a_hook_takes_a_conversation_token(load: Load) -> None:
    config = load(SABLE_HOOKS=f"komodo={ROOM}", SABLE_HOOK_TOKEN_KOMODO="t")
    assert config.hook_room("komodo") == ROOM


def test_a_hook_can_name_a_notify_alias_instead(load: Load) -> None:
    config = load(
        SABLE_NOTIFY_ROOMS=f"alerts={ROOM}",
        SABLE_HOOKS="komodo=alerts",
        SABLE_HOOK_TOKEN_KOMODO="t",
    )
    assert config.hook_room("komodo") == ROOM


def test_a_room_name_where_a_token_belongs_fails_at_startup(load: Load) -> None:
    # Talk's own routes are lowercase, so "Notice" could never have worked. It
    # used to surface as an OCS 998 at the moment an alert fired.
    with pytest.raises(ConfigError, match="neither a conversation token"):
        load(SABLE_HOOKS="komodo=Notice", SABLE_HOOK_TOKEN_KOMODO="t")


def test_the_error_says_where_to_find_the_token(load: Load) -> None:
    with pytest.raises(ConfigError, match=r"end of the conversation's URL"):
        load(SABLE_HOOKS="komodo=Notice", SABLE_HOOK_TOKEN_KOMODO="t")


def test_a_notify_alias_pointing_at_a_name_fails_too(load: Load) -> None:
    with pytest.raises(ConfigError, match="SABLE_NOTIFY_ROOMS entry"):
        load(SABLE_NOTIFY_ROOMS="alerts=Notice")


def test_an_unknown_hook_has_no_room(load: Load) -> None:
    config = load(SABLE_HOOKS=f"komodo={ROOM}", SABLE_HOOK_TOKEN_KOMODO="t")
    assert config.hook_room("nothere") == ""


# --------------------------------------------------------------------------- #
# Hooks and their tokens have to agree
# --------------------------------------------------------------------------- #


def test_a_hook_without_a_token_is_refused(load: Load) -> None:
    with pytest.raises(ConfigError, match="SABLE_HOOK_TOKEN_KOMODO"):
        load(SABLE_HOOKS=f"komodo={ROOM}")


def test_a_token_without_a_hook_is_refused(load: Load) -> None:
    with pytest.raises(ConfigError, match="no matching entry"):
        load(SABLE_HOOK_TOKEN_KOMODO="t")


def test_a_template_without_a_hook_is_refused(load: Load) -> None:
    with pytest.raises(ConfigError, match="SABLE_HOOK_TEMPLATE_KOMODO"):
        load(SABLE_HOOK_TEMPLATE_KOMODO="{level}")


def test_hooks_need_somewhere_to_post_to(load: Load) -> None:
    with pytest.raises(ConfigError, match="SABLE_NEXTCLOUD_URL is required"):
        load(
            nextcloud=False,
            SABLE_HOOKS=f"komodo={ROOM}",
            SABLE_HOOK_TOKEN_KOMODO="t",
        )


def test_hook_names_are_case_insensitive(load: Load) -> None:
    config = load(SABLE_HOOKS=f"Komodo={ROOM}", SABLE_HOOK_TOKEN_komodo="t")
    assert config.hook_room("KOMODO") == ROOM
    assert config.hook_token("komodo") == "t"


# --------------------------------------------------------------------------- #
# The HTTP surface
# --------------------------------------------------------------------------- #


def test_the_schema_is_off_and_the_probe_open_by_default(load: Load) -> None:
    config = load()
    assert config.api_docs is False
    assert config.health_token == ""
    assert config.health_guarded is False


def test_the_schema_and_the_probe_can_both_be_configured(load: Load) -> None:
    config = load(SABLE_API_DOCS="true", SABLE_HEALTH_TOKEN="  h-token  ")
    assert config.api_docs is True
    assert config.health_token == "h-token"
    assert config.health_guarded is True


def test_a_non_boolean_api_docs_is_refused(load: Load) -> None:
    with pytest.raises(ConfigError, match="SABLE_API_DOCS must be a boolean"):
        load(SABLE_API_DOCS="sometimes")


# --------------------------------------------------------------------------- #
# Which proxies are believed about the client address
# --------------------------------------------------------------------------- #


def test_only_loopback_is_trusted_by_default(load: Load) -> None:
    """uvicorn's own default. A proxy on the same host is the common case."""
    assert load().trusted_proxies == ["127.0.0.1", "::1"]


def test_addresses_and_ranges_are_both_accepted(load: Load) -> None:
    config = load(SABLE_TRUSTED_PROXIES="10.0.0.5, 192.168.1.0/24, ::1")
    assert config.trusted_proxies == ["10.0.0.5", "192.168.1.0/24", "::1"]


def test_a_star_trusts_every_client(load: Load) -> None:
    assert load(SABLE_TRUSTED_PROXIES="*").trusted_proxies == ["*"]


def test_set_to_nothing_trusts_nobody(load: Load) -> None:
    """Distinct from unset: naming no proxy is a choice, and uvicorn honours an
    empty list by believing the headers from nobody at all."""
    assert load(SABLE_TRUSTED_PROXIES="").trusted_proxies == []


def test_a_hostname_is_refused(load: Load) -> None:
    """uvicorn keeps one as a literal that never matches a peer address, so a
    value that cannot work is refused here rather than silently ignored."""
    with pytest.raises(ConfigError, match="not an IP address or a CIDR range"):
        load(SABLE_TRUSTED_PROXIES="proxy.example.org")


def test_a_range_with_host_bits_set_is_refused(load: Load) -> None:
    with pytest.raises(ConfigError, match="no host bits set"):
        load(SABLE_TRUSTED_PROXIES="172.17.0.5/16")


def test_a_star_cannot_be_mixed_with_addresses(load: Load) -> None:
    with pytest.raises(ConfigError, match="already means every client"):
        load(SABLE_TRUSTED_PROXIES="*,127.0.0.1")


# --------------------------------------------------------------------------- #
# Who may run which command
# --------------------------------------------------------------------------- #


def test_every_command_is_open_by_default(load: Load) -> None:
    config = load()
    assert not config.admin_only("reset")
    assert not config.is_admin_user("alice")


def test_an_admin_command_is_closed_to_everybody_else(load: Load) -> None:
    config = load(SABLE_ADMIN_COMMANDS="reset, ai", SABLE_ADMIN_USERS="maser,korren")
    assert config.admin_only("reset")
    assert config.admin_only("ai")
    assert not config.admin_only("ping")
    assert config.is_admin_user("maser")
    assert config.is_admin_user("KORREN")
    assert not config.is_admin_user("alice")


def test_an_admin_may_be_written_as_a_full_actor_id(load: Load) -> None:
    config = load(SABLE_ADMIN_COMMANDS="reset", SABLE_ADMIN_USERS="users/maser")
    assert config.is_admin_user("maser")


def test_nobody_is_an_admin_without_a_user_id(load: Load) -> None:
    """Actor.user_id is empty for guests and for bots, and empty matches nobody."""
    config = load(SABLE_ADMIN_COMMANDS="reset", SABLE_ADMIN_USERS="maser")
    assert not config.is_admin_user("")


def test_a_star_closes_every_command(load: Load) -> None:
    config = load(SABLE_ADMIN_COMMANDS="*", SABLE_ADMIN_USERS="maser")
    assert config.admin_only("reset")
    assert config.admin_only("ping")


def test_normal_commands_are_the_exceptions_to_a_star(load: Load) -> None:
    config = load(
        SABLE_ADMIN_COMMANDS="*",
        SABLE_NORMAL_COMMANDS="help,ping",
        SABLE_ADMIN_USERS="maser",
    )
    assert config.admin_only("reset")
    assert not config.admin_only("help")
    assert not config.admin_only("ping")


def test_naming_an_alias_restricts_the_command_behind_it(load: Load) -> None:
    """admin_only is asked about a command's name and its aliases together."""
    config = load(SABLE_ADMIN_COMMANDS="forget", SABLE_ADMIN_USERS="maser")
    assert config.admin_only("reset", "forget")


def test_admin_commands_without_an_admin_are_refused(load: Load) -> None:
    with pytest.raises(ConfigError, match="SABLE_ADMIN_USERS is empty"):
        load(SABLE_ADMIN_COMMANDS="reset")


def test_a_command_cannot_be_admin_and_open_at_once(load: Load) -> None:
    with pytest.raises(ConfigError, match="reset: in both SABLE_ADMIN_COMMANDS"):
        load(
            SABLE_ADMIN_COMMANDS="reset",
            SABLE_NORMAL_COMMANDS="reset",
            SABLE_ADMIN_USERS="maser",
        )


def test_normal_commands_cannot_be_a_star(load: Load) -> None:
    with pytest.raises(ConfigError, match="SABLE_NORMAL_COMMANDS cannot be"):
        load(SABLE_NORMAL_COMMANDS="*")


def test_admins_without_admin_commands_are_allowed(load: Load) -> None:
    """Nothing is gated, but a custom command can still ask ctx.is_admin."""
    config = load(SABLE_ADMIN_USERS="maser")
    assert config.is_admin_user("maser")
    assert not config.admin_only("reset")
