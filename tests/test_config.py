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
