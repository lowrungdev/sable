"""Reading the environment, and refusing it when it cannot work.

Most of these are about catching a mistake at startup rather than at three in
the morning when an alert does not arrive.
"""

from __future__ import annotations

import os
from typing import Callable

import pytest

from sable.config import TOKEN_RE, Config, ConfigError

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


def test_a_token_with_a_trailing_newline_is_not_a_token() -> None:
    """It used to be, and the boundary check is the only thing standing between a
    value somebody supplied and a URL built around it. re's `$` matches before a
    final newline as well as at the end, so a room of 'abcd1234' plus one passed
    the check in POST /notify and reached httpx, which refuses it as an invalid
    URL - an unhandled error, so the caller was told 500 where the same value
    uppercased was correctly told 400. The anchor is `\\Z` now.
    """
    assert TOKEN_RE.match(ROOM + chr(10)) is None
    assert TOKEN_RE.match(ROOM) is not None


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


# --------------------------------------------------------------------------- #
# Rotating the bot secret without a window of 401s
# --------------------------------------------------------------------------- #


def test_there_is_only_one_secret_to_try_by_default(load: Load) -> None:
    config = load()
    assert config.bot_secret_previous == ""
    assert config.inbound_secrets == (SECRET,)


def test_a_rotation_offers_the_current_secret_first(load: Load) -> None:
    """Order is the point: the old secret is a fallback for events signed before
    the reinstall, not something to check first once the new one is live."""
    old = "o" * 40
    config = load(SABLE_BOT_SECRET_PREVIOUS=old)
    assert config.bot_secret_previous == old
    assert config.inbound_secrets == (SECRET, old)


def test_the_previous_secret_is_held_to_the_same_length_rule(load: Load) -> None:
    with pytest.raises(ConfigError, match="SABLE_BOT_SECRET_PREVIOUS must be 40-128"):
        load(SABLE_BOT_SECRET_PREVIOUS="short")


def test_the_previous_secret_repeating_the_current_one_is_refused(load: Load) -> None:
    """Nothing has been rotated, so it can only be a copy-paste of the value above
    it - and accepting it would read as a rotation in progress that is not."""
    with pytest.raises(ConfigError, match="same value as SABLE_BOT_SECRET"):
        load(SABLE_BOT_SECRET_PREVIOUS=SECRET)


# --------------------------------------------------------------------------- #
# Who may ask about a message by reacting to it, and where
# --------------------------------------------------------------------------- #


def test_the_reaction_is_open_to_everyone_by_default(load: Load) -> None:
    assert load().ask_admins_only is False


def test_the_reaction_can_be_restricted_to_the_admins(load: Load) -> None:
    config = load(SABLE_ASK_ADMINS_ONLY="true", SABLE_ADMIN_USERS="maser")
    assert config.ask_admins_only is True
    assert config.is_admin_user("maser")


def test_restricting_the_reaction_without_an_admin_is_refused(load: Load) -> None:
    """The same shape as SABLE_ADMIN_COMMANDS with nobody to run them: a setting
    that would leave the feature usable by no one is a misconfiguration, not a
    very thorough way of turning it off."""
    with pytest.raises(ConfigError, match="SABLE_ASK_ADMINS_ONLY is on"):
        load(SABLE_ASK_ADMINS_ONLY="true")


def test_every_room_is_cached_when_no_rooms_are_named(load: Load) -> None:
    """The asymmetry with SABLE_AI_ROOMS, where empty means none. Empty here has
    to keep meaning all of them, or upgrading would silently stop the reaction
    working in every conversation an existing deployment has."""
    config = load()
    assert config.ask_rooms == []
    assert config.ask_room_allowed(ROOM, "Anything")
    assert not config.ai_room_allowed(ROOM, "Anything")


def test_naming_rooms_confines_the_cache_to_them(load: Load) -> None:
    config = load(SABLE_ASK_ROOMS=f"{ROOM}, Ops ")
    assert config.ask_rooms == [ROOM, "Ops"]
    assert config.ask_room_allowed(ROOM)
    assert config.ask_room_allowed("e5f6g7h8", "ops")
    assert not config.ask_room_allowed("e5f6g7h8", "Random")


def test_a_star_caches_every_room_as_an_empty_list_does(load: Load) -> None:
    assert load(SABLE_ASK_ROOMS="*").ask_room_allowed("e5f6g7h8", "Random")


# --------------------------------------------------------------------------- #
# A ceiling on model calls in flight
# --------------------------------------------------------------------------- #


def test_eight_model_calls_may_be_in_flight_by_default(load: Load) -> None:
    assert load().max_concurrent_replies == 8


def test_the_ceiling_can_be_raised_or_lifted_entirely(load: Load) -> None:
    assert load(SABLE_MAX_CONCURRENT_REPLIES="32").max_concurrent_replies == 32
    assert load(SABLE_MAX_CONCURRENT_REPLIES="0").max_concurrent_replies == 0


def test_a_negative_ceiling_is_refused(load: Load) -> None:
    """0 is already the way to ask for no ceiling, so -1 is a typo rather than an
    emphatic version of it."""
    with pytest.raises(ConfigError, match="SABLE_MAX_CONCURRENT_REPLIES cannot be negative"):
        load(SABLE_MAX_CONCURRENT_REPLIES="-1")


# --------------------------------------------------------------------------- #
# Ignore entries a rename would defeat
# --------------------------------------------------------------------------- #


def test_ids_are_never_reported_as_fragile(load: Load) -> None:
    config = load(SABLE_IGNORE_USERS="alice,users/bob,guests/abc123,noisy-integration")
    assert config.fragile_ignore_users == []


def test_an_entry_with_a_space_in_it_is_reported_as_a_display_name(load: Load) -> None:
    """A user id has no whitespace and a display name usually does, which is as
    far as anything can tell them apart from here. Reported, not refused: a name
    is a legitimate thing to ignore when it is all an operator has."""
    config = load(SABLE_IGNORE_USERS="alice,Alice Anderson,users/bob")
    assert config.fragile_ignore_users == ["Alice Anderson"]
    assert config.is_ignored("users/carol", "Alice Anderson")


# --------------------------------------------------------------------------- #
# Time, and the request body
# --------------------------------------------------------------------------- #


def test_a_time_zone_is_checked_at_startup(load: Load) -> None:
    with pytest.raises(ConfigError, match="not an IANA time zone"):
        load(SABLE_TIMEZONE="EST5EDT/nope")


def test_a_real_time_zone_is_kept(load: Load) -> None:
    assert load(SABLE_TIMEZONE="America/New_York").timezone == "America/New_York"


def test_no_time_zone_means_the_host_clock(load: Load) -> None:
    assert load().timezone == ""


def test_extra_body_may_not_override_the_fields_sable_builds(load: Load) -> None:
    # stream in particular: sable would then be handed SSE and fail to parse it.
    with pytest.raises(ConfigError, match="must not set stream"):
        load(SABLE_LLM_EXTRA_BODY='{"stream": true}')


def test_extra_body_still_takes_provider_specific_fields(load: Load) -> None:
    config = load(SABLE_LLM_EXTRA_BODY='{"tool_ids": ["server:mcp:1"], "top_k": 40}')
    assert config.llm.extra_body == {"tool_ids": ["server:mcp:1"], "top_k": 40}


# --------------------------------------------------------------------------- #
# Which backend, and what it is allowed to reach
# --------------------------------------------------------------------------- #

OWUI = {
    "SABLE_LLM_BACKEND": "openwebui",
    "SABLE_LLM_BASE_URL": "https://ai.example.org/api",
    "SABLE_LLM_API_KEY": "sk-test",
    "SABLE_LLM_MODEL": "gemma-focused",
}


def test_the_plain_openai_backend_is_the_default(load: Load) -> None:
    assert load().llm.backend == "openai"
    assert not load().llm.agentic


def test_the_openwebui_backend_is_selectable(load: Load) -> None:
    config = load(**OWUI, SABLE_LLM_TOOL_IDS="server:mcp:1,server:mcp:2")
    assert config.llm.agentic
    assert config.llm.tool_ids == ["server:mcp:1", "server:mcp:2"]


def test_an_unknown_backend_is_refused(load: Load) -> None:
    with pytest.raises(ConfigError, match="SABLE_LLM_BACKEND must be one of"):
        load(SABLE_LLM_BACKEND="ollama")


def test_the_openwebui_backend_needs_a_key(load: Load) -> None:
    # The key is the account the tools run as, not just authentication.
    env = {**OWUI, "SABLE_LLM_API_KEY": ""}
    with pytest.raises(ConfigError, match="SABLE_LLM_API_KEY is required"):
        load(**env)


def test_a_base_url_without_api_is_doubted_but_allowed(load: Load) -> None:
    # A proxy may rewrite the path, so this address can be right even when it
    # does not look it. Everything hangs off this URL, though, so a mistake here
    # 404s every question - worth saying once.
    env = {**OWUI, "SABLE_LLM_BASE_URL": "https://ai.example.org"}
    config = load(**env)
    assert any("does not end in /api" in warning for warning in config.warnings)


def test_a_base_url_under_api_says_nothing(load: Load) -> None:
    assert load(**OWUI).warnings == ()


def test_an_unknown_builtin_feature_is_refused(load: Load) -> None:
    with pytest.raises(ConfigError, match="SABLE_LLM_FEATURES may name"):
        load(**OWUI, SABLE_LLM_FEATURES="web_search,telepathy")


def test_builtin_features_need_the_openwebui_backend(load: Load) -> None:
    with pytest.raises(ConfigError, match="only applies to the openwebui backend"):
        load(SABLE_LLM_FEATURES="web_search")


def test_builtin_features_need_a_session(load: Load) -> None:
    # Without a session id Open WebUI never offers them, so this would be a
    # setting that silently did nothing.
    with pytest.raises(ConfigError, match="needs SABLE_LLM_BUILTIN_TOOLS on"):
        load(**OWUI, SABLE_LLM_FEATURES="web_search", SABLE_LLM_BUILTIN_TOOLS="false")


def test_a_zero_poll_interval_is_refused(load: Load) -> None:
    with pytest.raises(ConfigError, match="SABLE_LLM_POLL_INTERVAL"):
        load(**OWUI, SABLE_LLM_POLL_INTERVAL="0")
