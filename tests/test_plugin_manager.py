"""The plugin manager against a fake worker: validation, access, and the chat flow."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import pytest
import respx

from conftest import ACTOR_SHAPES, ROOM, actor_event, as_bot, event, make_config
from plugin_helpers import (
    builtin_names,
    fake_command,
    message_route,
    posix_only,
    texts,
    write_plugin,
)
from sable.__main__ import main
from sable.app import create_app
from sable.commands import Access, Registry, registry
from sable.plugins import MAX_PLUGINS, PluginManager, PluginStartupError, Status

pytestmark = posix_only

OTHER = "efgh5678"


def declare(*commands: dict[str, Any]) -> dict[str, Any]:
    return {"declare": {"commands": list(commands), "phrases": [], "schedules": []}}


def cmd(name: str, *aliases: str, help: str = "", usage: str = "") -> dict[str, Any]:
    return {"name": name, "aliases": list(aliases), "help": help, "usage": usage}


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #


async def test_a_plugin_that_loads_is_active_and_registers_its_commands(rigs, tmp_path) -> None:
    before = builtin_names()
    write_plugin(tmp_path, "greeter")
    rig = await rigs(tmp_path)
    record = rig.record("greeter")
    assert record.status is Status.ACTIVE
    assert record.declared is not None
    assert record.declared.command_names == ["hi", "yo"]
    command = rig.bot.registry.get("yo")
    assert command is not None
    assert command.name == "hi"
    assert command.plugin == "greeter"
    assert command.help == "say hi"
    # Nothing leaked into the module-level registry of built-ins.
    assert builtin_names() == before
    assert registry.get("hi") is None
    assert registry.get("yo") is None


async def test_a_plugin_with_no_rooms_is_inactive_and_never_started(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "quiet", rooms=None)
    rig = await rigs(tmp_path)
    record = rig.record("quiet")
    assert record.status is Status.INACTIVE
    assert record.state == "inactive: no rooms set"
    assert record.worker is None
    assert rig.bot.registry.get("hi") is None


async def test_an_empty_rooms_list_is_inactive_too(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "quiet", rooms=[])
    rig = await rigs(tmp_path)
    assert rig.record("quiet").status is Status.INACTIVE


async def test_a_disabled_plugin_is_listed_and_never_started(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "off", enabled=False)
    rig = await rigs(tmp_path)
    record = rig.record("off")
    assert record.status is Status.DISABLED
    assert record.state == "disabled"
    assert record.worker is None


async def test_an_inactive_plugin_is_still_validated(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "quiet", rooms=None, source="def broken(:\n")
    rig = await rigs(tmp_path)
    assert rig.record("quiet").status is Status.FAILED
    assert "syntax error" in rig.record("quiet").reason


async def test_a_syntax_error_fails_the_plugin_before_any_worker_starts(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "oops", source="x = = 1\n")
    rig = await rigs(tmp_path)
    record = rig.record("oops")
    assert record.status is Status.FAILED
    assert "line 1" in record.reason
    assert record.worker is None


async def test_a_bad_settings_file_fails_only_that_plugin(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "bad", raw_yaml="surprise: true\n")
    write_plugin(tmp_path, "good")
    rig = await rigs(tmp_path)
    assert rig.record("bad").status is Status.FAILED
    assert rig.record("good").status is Status.ACTIVE


async def test_an_import_error_in_the_worker_fails_the_plugin(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "broken", settings={"load": "error"})
    rig = await rigs(tmp_path)
    record = rig.record("broken")
    assert record.status is Status.FAILED
    assert "no module named nothing" in record.reason
    assert record.worker is None


async def test_a_worker_that_never_answers_the_load_fails_the_plugin(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "slow", settings={"load": "hang"})
    rig = await rigs(tmp_path, load_timeout=0.25)
    record = rig.record("slow")
    assert record.status is Status.FAILED
    assert "did not answer within 0.25 seconds" in record.reason


async def test_a_worker_that_speaks_garbage_while_loading_fails_the_plugin(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "noisy", settings={"load": "garbage"})
    rig = await rigs(tmp_path)
    record = rig.record("noisy")
    assert record.status is Status.FAILED
    assert "protocol violation" in record.reason


async def test_a_worker_that_dies_while_loading_fails_the_plugin(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "dies", settings={"load": "crash"})
    rig = await rigs(tmp_path)
    record = rig.record("dies")
    assert record.status is Status.FAILED
    assert "exited with status 7" in record.reason


@pytest.mark.parametrize(
    ("declared", "why"),
    [
        (declare(cmd("Bad Name")), "legal command name"),
        (declare(cmd("ok", "Bad Alias")), "legal command name"),
        (declare(cmd("ok", help="h" * 201)), "help"),
        (declare(cmd("ok", usage="u" * 101)), "usage"),
        (declare(), "declares no commands"),
        (declare(cmd("a", "a")), "declared twice"),
        (declare(cmd("a"), cmd("b", "a")), "declared twice"),
        (declare(*[cmd(f"c{i:02d}") for i in range(33)]), "at most 32"),
        ({"declare": "nonsense"}, "did not say what it declares"),
        ({"declare": {"commands": "nope"}}, "invalid declaration"),
        ({"declare": {"commands": [{"help": "no name"}]}}, "name"),
    ],
)
async def test_an_invalid_declaration_fails_the_plugin(rigs, tmp_path, declared, why) -> None:
    write_plugin(tmp_path, "lies", settings=declared)
    rig = await rigs(tmp_path)
    record = rig.record("lies")
    assert record.status is Status.FAILED, record.state
    assert why in record.reason


async def test_help_text_from_a_plugin_is_flattened(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "rude", settings=declare(cmd("go", help="line one\nline two\x1b[31m")))
    rig = await rigs(tmp_path)
    assert rig.bot.registry.get("go").help == "line one line two [31m"  # type: ignore[union-attr]


async def test_phrases_are_listed_and_schedules_still_only_parsed(rigs, tmp_path) -> None:
    declared = {
        "declare": {
            "commands": [cmd("go")],
            "phrases": [{"id": "greet", "any": ["gm"], "whole_words": True, "cooldown": 3600}],
            "schedules": [{"id": "standup", "cron": "0 8 * * 1-5", "every": None}],
        }
    }
    write_plugin(tmp_path, "later", settings=declared)
    rig = await rigs(tmp_path)
    record = rig.record("later")
    assert record.status is Status.ACTIVE
    assert record.declared is not None
    assert len(record.declared.phrases) == 1
    assert len(record.declared.schedules) == 1
    assert "phrases: `greet`" in rig.manager.report()
    assert "1 schedule(s)" in rig.manager.report()


async def test_a_check_that_rejects_fails_the_plugin_with_its_words(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "picky", settings={"check": "reject"})
    rig = await rigs(tmp_path)
    record = rig.record("picky")
    assert record.status is Status.FAILED
    assert "the api url is wrong" in record.reason


async def test_a_check_that_passes_leaves_the_plugin_active(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "fine", settings={"check": "ok"})
    rig = await rigs(tmp_path)
    assert rig.record("fine").status is Status.ACTIVE


async def test_a_check_that_hangs_fails_the_plugin(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "stuck", settings={"check": "hang"})
    rig = await rigs(tmp_path, load_timeout=0.25)
    assert rig.record("stuck").status is Status.FAILED


# --------------------------------------------------------------------------- #
# Collisions
# --------------------------------------------------------------------------- #


BUILTIN_NAMES = ["ping", "help", "whoami", "ai", "reset", "version", "plugins"]
BUILTIN_ALIASES = ["?", "commands", "ask", "forget"]


async def test_a_command_or_alias_named_like_a_builtin_fails_that_plugin(rigs, tmp_path) -> None:
    # One plugin per name, all started together.
    for name in BUILTIN_NAMES:
        write_plugin(tmp_path, f"x{name}", settings=declare(cmd(name)))
    for number, alias in enumerate(BUILTIN_ALIASES):
        write_plugin(tmp_path, f"a{number}", settings=declare(cmd(f"mine{number}", alias)))
    rig = await rigs(tmp_path)
    for name in BUILTIN_NAMES:
        record = rig.record(f"x{name}")
        assert record.status is Status.FAILED, name
        assert "built-in" in record.reason
        # The built-in is still the built-in.
        assert rig.bot.registry.get(name).plugin == ""  # type: ignore[union-attr]
    for number in range(len(BUILTIN_ALIASES)):
        assert rig.record(f"a{number}").status is Status.FAILED
        assert rig.bot.registry.get(f"mine{number}") is None


async def test_the_later_plugin_loses_a_collision_and_names_the_earlier(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "first", directory="a", settings=declare(cmd("dup")))
    write_plugin(tmp_path, "second", directory="b", settings=declare(cmd("dup")))
    rig = await rigs(tmp_path)
    assert rig.record("first").status is Status.ACTIVE
    loser = rig.record("second")
    assert loser.status is Status.FAILED
    assert "first" in loser.reason
    assert rig.bot.registry.get("dup").plugin == "first"  # type: ignore[union-attr]
    assert loser.worker is None


async def test_an_alias_collides_with_another_plugins_command(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "first", directory="a", settings=declare(cmd("x")))
    write_plugin(tmp_path, "second", directory="b", settings=declare(cmd("y", "x")))
    rig = await rigs(tmp_path)
    assert rig.record("second").status is Status.FAILED


async def test_an_inactive_plugin_does_not_claim_its_names(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "sleeper", directory="a", rooms=None, settings=declare(cmd("dup")))
    write_plugin(tmp_path, "worker", directory="b", settings=declare(cmd("dup")))
    rig = await rigs(tmp_path)
    assert rig.record("worker").status is Status.ACTIVE


# --------------------------------------------------------------------------- #
# Access
# --------------------------------------------------------------------------- #

ALICE = "users/alice"
MASER = "users/maser"


ACCESS_CASES = [
    # rooms, users, admins_only, room, actor, expected
    ([ROOM], None, None, ROOM, ALICE, Access.OK),
    ([ROOM], None, None, OTHER, ALICE, Access.NOT_HERE),
    (["*"], None, None, OTHER, ALICE, Access.OK),
    ([ROOM, OTHER], None, None, OTHER, ALICE, Access.OK),
    ([ROOM], ["alice"], None, ROOM, ALICE, Access.OK),
    ([ROOM], ["Alice"], None, ROOM, ALICE, Access.OK),
    ([ROOM], ["users/alice"], None, ROOM, ALICE, Access.OK),
    ([ROOM], ["bob"], None, ROOM, ALICE, Access.NOT_YOU),
    ([ROOM], ["bob"], None, OTHER, ALICE, Access.NOT_HERE),
    ([ROOM], None, True, ROOM, ALICE, Access.NOT_YOU),
    ([ROOM], None, True, ROOM, MASER, Access.OK),
    # An administrator is not implicitly on the list of users.
    ([ROOM], ["bob"], True, ROOM, MASER, Access.NOT_YOU),
    ([ROOM], ["maser"], True, ROOM, MASER, Access.OK),
    ([ROOM], ["maser"], True, ROOM, ALICE, Access.NOT_YOU),
    ([ROOM], None, True, OTHER, MASER, Access.NOT_HERE),
]


async def test_the_access_decision(rigs, tmp_path) -> None:
    # One plugin per case, all started together: a worker per parametrised case would
    # make this the slowest test in the suite for no extra coverage.
    for number, (rooms, users, admins_only, _, _, _) in enumerate(ACCESS_CASES):
        write_plugin(
            tmp_path,
            f"gate{number}",
            settings=declare(cmd(f"c{number}")),
            rooms=rooms,
            users=users,
            admins_only=admins_only,
        )
    rig = await rigs(tmp_path, admin_users=["maser"])
    wrong = []
    for number, (rooms, users, admins_only, room, actor, expected) in enumerate(ACCESS_CASES):
        got = rig.manager.allows(f"gate{number}", event("!hi", room=room, actor_id=actor))
        if got is not expected:
            wrong.append(
                f"{rooms} {users} {admins_only} in {room} by {actor}: {got}, not {expected}"
            )
    assert not wrong, wrong


async def test_a_room_outside_allowed_rooms_is_never_served(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "gate", rooms=["*"])
    rig = await rigs(tmp_path, allowed_rooms=[ROOM])
    assert rig.manager.allows("gate", event("!hi", room=ROOM)) is Access.OK
    assert rig.manager.allows("gate", event("!hi", room=OTHER)) is Access.NOT_HERE


async def test_a_listed_room_outside_allowed_rooms_warns_and_is_not_served(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "gate", rooms=[OTHER])
    rig = await rigs(tmp_path, allowed_rooms=[ROOM])
    assert any("SABLE_ALLOWED_ROOMS" in w for w in rig.record("gate").warnings)
    assert rig.manager.allows("gate", event("!hi", room=OTHER)) is Access.NOT_HERE


async def test_who_may_use_an_open_plugin_and_who_may_be_listed(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "open", settings=declare(cmd("o")))
    write_plugin(
        tmp_path,
        "listed",
        settings=declare(cmd("l")),
        users=["alice", "7f3c9a2b", "karl@cloud.example.net"],
    )
    rig = await rigs(tmp_path)
    for shape in ACTOR_SHAPES:
        # Users, guests and federated users are "anyone in those rooms"; a bot never is.
        got = rig.manager.allows("open", actor_event(shape, "!hi"))
        assert got is (Access.NOT_HERE if shape.is_bot else Access.OK), shape.label
        # Only a user has an id that can be on the list; a guest's or a federated
        # user's, even when written there, is not one.
        got = rig.manager.allows("listed", actor_event(shape, "!hi"))
        expected = {"a_user": Access.OK, "a_bot": Access.NOT_HERE}.get(shape.label, Access.NOT_YOU)
        assert got is expected, shape.label


async def test_a_bot_is_refused_by_the_decision_itself(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "open")
    rig = await rigs(tmp_path)
    assert rig.manager.allows("open", event("!hi", actor_type="bots")) is Access.NOT_HERE
    assert rig.manager.allows("open", as_bot(event("!hi"))) is Access.NOT_HERE


async def test_a_bot_that_carries_an_administrators_id_is_not_one(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "admin", admins_only=True)
    rig = await rigs(tmp_path, admin_users=["maser"])
    assert rig.manager.allows("admin", as_bot(event("!hi", actor_id=MASER))) is Access.NOT_HERE


@pytest.mark.parametrize("name", ["quiet", "off", "ghost"])
async def test_an_inactive_disabled_or_unknown_plugin_is_not_here(rigs, tmp_path, name) -> None:
    write_plugin(tmp_path, "quiet", rooms=None)
    write_plugin(tmp_path, "off", enabled=False)
    rig = await rigs(tmp_path)
    assert rig.manager.allows(name, event("!hi")) is Access.NOT_HERE


# --------------------------------------------------------------------------- #
# Through the bot
# --------------------------------------------------------------------------- #


@respx.mock
async def test_a_plugin_command_runs_and_replies(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "greeter")
    rig = await rigs(tmp_path)
    route = message_route()
    await rig.bot.handle(event("!hi echo hello there"))
    assert texts(route) == ["hello there"]


@respx.mock
async def test_an_alias_runs_the_command_under_its_own_name(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "greeter")
    rig = await rigs(tmp_path)
    route = message_route()
    await rig.bot.handle(event("!yo ctx"))
    import json

    ctx = json.loads(texts(route)[0])
    assert ctx["name"] == "hi"
    assert ctx["trigger"] == "command"


@respx.mock
async def test_the_call_context_carries_only_what_the_event_says(rigs, tmp_path) -> None:
    import json

    write_plugin(tmp_path, "greeter", settings={"api_key": "SECRET-VALUE-123"})
    rig = await rigs(tmp_path, admin_users=["maser"])
    route = message_route()
    await rig.bot.handle(event('!hi ctx "two words" x', message_id=77, actor_id=MASER))
    ctx = json.loads(texts(route)[0])
    assert ctx["plugin"] == "greeter"
    assert ctx["args"] == 'ctx "two words" x'
    assert ctx["argv"] == ["ctx", "two words", "x"]
    assert ctx["room"] == ROOM
    assert ctx["actor_id"] == MASER
    assert ctx["user_id"] == "maser"
    assert ctx["actor_name"] == "Alice"
    assert ctx["is_admin"] is True
    assert ctx["message_id"] == 77
    assert ctx["text"].startswith("!hi ctx")
    assert "SECRET-VALUE-123" not in str(ctx)


@respx.mock
async def test_a_plugin_command_in_a_room_it_does_not_serve_is_an_unknown_command(
    rigs, tmp_path
) -> None:
    write_plugin(tmp_path, "greeter")
    rig = await rigs(tmp_path)
    route = message_route(OTHER)
    await rig.bot.handle(event("!hi echo x", room=OTHER))
    await rig.bot.handle(event("!nothing echo x", room=OTHER, message_id=101))
    hidden, truly_unknown = texts(route)
    assert hidden == "I have no `hi` command. Try `!help`."
    assert truly_unknown == hidden.replace("`hi`", "`nothing`")


@respx.mock
async def test_the_unknown_command_hint_setting_covers_a_hidden_plugin_command(
    rigs, tmp_path
) -> None:
    write_plugin(tmp_path, "greeter")
    rig = await rigs(tmp_path, unknown_command_hint=False)
    route = message_route(OTHER)
    await rig.bot.handle(event("!hi echo x", room=OTHER))
    assert route.call_count == 0


@respx.mock
async def test_an_inactive_plugins_command_does_not_exist(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "quiet", rooms=None)
    rig = await rigs(tmp_path)
    route = message_route()
    await rig.bot.handle(event("!hi echo x"))
    assert texts(route) == ["I have no `hi` command. Try `!help`."]


@respx.mock
async def test_somebody_not_on_the_users_list_is_told_it_is_not_for_them(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "greeter", users=["bob"])
    rig = await rigs(tmp_path)
    route = message_route()
    await rig.bot.handle(event("!hi echo x"))
    assert texts(route) == ["`!hi` is not available to you."]


@respx.mock
async def test_an_admins_only_plugin_refuses_everybody_else(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "greeter", admins_only=True)
    rig = await rigs(tmp_path, admin_users=["maser"])
    route = message_route()
    await rig.bot.handle(event("!hi echo x"))
    await rig.bot.handle(event("!hi echo admin here", actor_id=MASER, message_id=101))
    assert texts(route) == ["`!hi` is not available to you.", "admin here"]


@respx.mock
async def test_sable_admin_commands_still_applies_to_a_plugin_command(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "greeter")
    rig = await rigs(tmp_path, admin_commands=["hi"], admin_users=["maser"])
    route = message_route()
    await rig.bot.handle(event("!hi echo x"))
    await rig.bot.handle(event("!yo echo ok", actor_id=MASER, message_id=101))
    assert texts(route) == ["`!hi` is for administrators only.", "ok"]


@respx.mock
async def test_a_plugin_command_counts_as_a_trigger_for_the_poller(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "greeter")
    rig = await rigs(tmp_path)
    assert rig.bot.would_handle(event("!hi echo x")) is True
    assert rig.bot.would_handle(event("just chatting")) is False


@respx.mock
async def test_a_plugins_own_error_is_said_in_the_room_as_it_wrote_it(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "greeter")
    rig = await rigs(tmp_path)
    route = message_route()
    await rig.bot.handle(event("!hi pluginerror Which city did you mean?"))
    assert texts(route) == ["Which city did you mean?"]


@respx.mock
async def test_any_other_failure_is_only_reported_as_a_crash(rigs, tmp_path, caplog) -> None:
    write_plugin(tmp_path, "greeter")
    rig = await rigs(tmp_path)
    route = message_route()
    with caplog.at_level(logging.WARNING):
        await rig.bot.handle(event("!hi exception"))
    body = texts(route)[0]
    assert "the `greeter` plugin crashed" in body
    assert "boom" not in body
    assert "ValueError: boom" in caplog.text


@respx.mock
async def test_crash_reports_respect_report_errors(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "greeter")
    rig = await rigs(tmp_path, report_errors=False)
    route = message_route()
    await rig.bot.handle(event("!hi exception"))
    assert route.call_count == 0


@respx.mock
async def test_nothing_is_said_for_a_none_reply(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "greeter")
    rig = await rigs(tmp_path)
    route = message_route()
    await rig.bot.handle(event("!hi none"))
    assert route.call_count == 0


# --------------------------------------------------------------------------- #
# !help
# --------------------------------------------------------------------------- #


def help_declaration() -> dict[str, Any]:
    return declare(cmd("hi", "yo", help="say hi", usage="hi <text>"))


@respx.mock
async def test_help_marks_plugin_commands_only_where_they_can_be_run(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "greeter", settings=help_declaration())
    rig = await rigs(tmp_path)
    route = message_route()
    route_other = message_route(OTHER)
    await rig.bot.handle(event("!help"))
    await rig.bot.handle(event("!help", room=OTHER))
    here, there = texts(route)[0], texts(route_other)[0]
    assert "- `!hi <text>` - say hi _(plugin)_" in here
    assert "`!hi" not in there
    assert "_(plugin)_" not in there


@respx.mock
async def test_help_leaves_out_what_the_reader_could_not_run(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "greeter", settings=help_declaration(), users=["bob"])
    write_plugin(tmp_path, "boss", settings=declare(cmd("boss", help="bossy")), admins_only=True)
    rig = await rigs(tmp_path, admin_users=["maser"])
    route = message_route()
    await rig.bot.handle(event("!help"))
    await rig.bot.handle(event("!help", actor_id="users/bob", message_id=101))
    await rig.bot.handle(event("!help", actor_id=MASER, message_id=102))
    alice, bob, maser = texts(route)
    assert "`!hi" not in alice
    assert "`!boss" not in alice
    assert "`!hi <text>`" in bob
    assert "`!boss" not in bob
    assert "`!boss` - bossy _(admin)_ _(plugin)_" in maser
    # The administrator is not on greeter's list of users, so it is not offered.
    assert "`!hi" not in maser


@respx.mock
async def test_help_for_a_plugin_command(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "greeter", settings=help_declaration())
    rig = await rigs(tmp_path)
    route = message_route()
    await rig.bot.handle(event("!help yo"))
    body = texts(route)[0]
    assert "**!hi** - say hi _(plugin)_" in body
    assert "Usage: `!hi <text>`" in body
    assert "Aliases: `!yo`" in body


@respx.mock
async def test_help_describes_a_hidden_plugin_command_as_unknown(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "greeter", settings=help_declaration())
    rig = await rigs(tmp_path)
    route = message_route(OTHER)
    await rig.bot.handle(event("!help hi", room=OTHER))
    await rig.bot.handle(event("!help nothing", room=OTHER, message_id=101))
    hidden, unknown = texts(route)
    assert hidden == "I have no `hi` command."
    assert unknown == hidden.replace("`hi`", "`nothing`")


@respx.mock
async def test_help_lists_plugins_to_administrators_only(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "greeter")
    rig = await rigs(tmp_path, admin_users=["maser"])
    route = message_route()
    await rig.bot.handle(event("!help"))
    await rig.bot.handle(event("!help", actor_id=MASER, message_id=101))
    plain, admin = texts(route)
    assert "!plugins" not in plain
    assert "`!plugins [name]` - List the plugins and how they are doing. _(admin)_" in admin


# --------------------------------------------------------------------------- #
# !plugins
# --------------------------------------------------------------------------- #


@respx.mock
async def test_plugins_is_for_administrators(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "greeter")
    rig = await rigs(tmp_path, admin_users=["maser"])
    route = message_route()
    await rig.bot.handle(event("!plugins"))
    assert texts(route) == ["`!plugins` is for administrators only."]


@respx.mock
async def test_plugins_stays_admin_only_whatever_normal_commands_says(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "greeter")
    rig = await rigs(
        tmp_path, admin_users=["maser"], admin_commands=["reset"], normal_commands=["plugins"]
    )
    route = message_route()
    await rig.bot.handle(event("!plugins"))
    assert texts(route) == ["`!plugins` is for administrators only."]


async def test_plugins_is_refused_inside_its_handler_too(rigs, tmp_path) -> None:
    from sable.commands import CommandError, Context, registry

    write_plugin(tmp_path, "greeter")
    rig = await rigs(tmp_path, admin_users=["maser"])
    command = registry.get("plugins")
    assert command is not None
    with pytest.raises(CommandError, match="administrators"):
        await command.handler(Context(rig.bot, event("!plugins"), "plugins", ""))


@respx.mock
async def test_plugins_lists_every_plugin_with_its_status(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "greeter", rooms=[ROOM, OTHER])
    write_plugin(tmp_path, "quiet", rooms=None)
    write_plugin(tmp_path, "off", enabled=False)
    write_plugin(tmp_path, "broken", settings={"load": "error"})
    rig = await rigs(tmp_path, admin_users=["maser"])
    route = message_route()
    await rig.bot.handle(event("!plugins", actor_id=MASER))
    body = texts(route)[0]
    assert "1 active, 1 inactive, 1 disabled, 1 failed" in body
    assert f"`greeter` - active - `!hi` - rooms: {ROOM}, {OTHER}" in body
    assert "`quiet` - inactive: no rooms set" in body
    assert "`off` - disabled" in body
    assert "`broken` - failed: import failed: no module named nothing" in body


@respx.mock
async def test_plugins_with_a_name_describes_one(rigs, tmp_path) -> None:
    write_plugin(
        tmp_path,
        "greeter",
        settings=help_declaration(),
        users=["alice"],
        admins_only=False,
    )
    rig = await rigs(tmp_path, admin_users=["maser"])
    route = message_route()
    await rig.bot.handle(event("!plugins greeter", actor_id=MASER))
    await rig.bot.handle(event("!plugins nothing", actor_id=MASER, message_id=101))
    detail, missing = texts(route)
    assert "**greeter** - active" in detail
    assert "File: `greeter/greeter.py`" in detail
    assert f"Rooms: {ROOM}" in detail
    assert "Users: alice" in detail
    assert "Command `!hi <text>` - say hi (aliases: `!yo`)" in detail
    assert missing == "No plugin called `nothing`."


@respx.mock
async def test_plugins_never_prints_a_settings_value(rigs, tmp_path) -> None:
    secret = "hunter2-SECRET-VALUE"
    write_plugin(tmp_path, "greeter", settings={"api_key": secret, "nested": {"token": secret}})
    write_plugin(tmp_path, "failed", settings={"api_key": secret, "load": "error"})
    write_plugin(tmp_path, "mangled", raw_yaml=f"settings: {{token: {secret}\n")
    write_plugin(tmp_path, "unknown", raw_yaml=f"surprise: {secret}\n")
    write_plugin(tmp_path, "checked", settings={"api_key": secret, "check": "reject"})
    rig = await rigs(tmp_path, admin_users=["maser"])
    route = message_route()
    await rig.bot.handle(event("!plugins", actor_id=MASER))
    for number, name in enumerate(["greeter", "failed", "mangled", "unknown", "checked"], 1):
        await rig.bot.handle(event(f"!plugins {name}", actor_id=MASER, message_id=100 + number))
    for body in texts(route):
        assert secret not in body
    assert route.call_count == 6
    for line in rig.manager.check_lines():
        assert secret not in line


@respx.mock
async def test_plugins_says_so_when_the_feature_is_off(http_client) -> None:
    route = message_route()
    admin = make_config(admin_users=["maser"])
    from sable.bot import Bot

    off = Bot(admin, http_client=http_client)
    await off.handle(event("!plugins", actor_id=MASER))
    assert "Plugins are off" in texts(route)[0]


@respx.mock
async def test_plugins_is_never_reached_by_a_bot(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "greeter")
    rig = await rigs(tmp_path, admin_users=["maser"])
    route = message_route()
    await rig.bot.handle(as_bot(event("!plugins", actor_id=MASER)))
    assert route.call_count == 0


# --------------------------------------------------------------------------- #
# Start-up and shutdown through the app
# --------------------------------------------------------------------------- #


def structural_only(root: Path) -> None:
    """Plugins that need no worker: inactive, disabled and structurally broken."""
    write_plugin(root, "quiet", rooms=None)
    write_plugin(root, "off", enabled=False)
    (root / "orphan").mkdir()
    (root / "orphan" / "orphan_settings.yaml").write_text("enabled: true\n")


async def test_the_banner_counts_the_plugins_and_each_failure_is_named(tmp_path, caplog) -> None:
    structural_only(tmp_path)
    config = make_config(plugins_dir=str(tmp_path))
    app = create_app(config, receive=False)
    with caplog.at_level(logging.INFO):
        async with app.router.lifespan_context(app):
            pass
    banner = [m for m in caplog.messages if m.startswith("  plugins:")]
    assert banner == [f"  plugins:        1 inactive, 1 disabled, 1 failed ({tmp_path})"]
    assert any(
        r.levelno == logging.WARNING and "plugin orphan failed" in r.getMessage()
        for r in caplog.records
    )
    assert any(
        r.levelno == logging.INFO and "plugin quiet is inactive" in r.getMessage()
        for r in caplog.records
    )


async def test_the_banner_says_when_plugins_are_off(caplog) -> None:
    app = create_app(make_config(), receive=False)
    with caplog.at_level(logging.INFO):
        async with app.router.lifespan_context(app):
            pass
    assert "  plugins:        off (SABLE_PLUGINS_DIR is empty)" in caplog.messages
    assert app.state.plugins is None


async def test_a_failing_plugin_does_not_stop_the_bot(tmp_path) -> None:
    structural_only(tmp_path)
    app = create_app(make_config(plugins_dir=str(tmp_path)), receive=False)
    async with app.router.lifespan_context(app):
        assert app.state.bot.plugins is app.state.plugins


async def test_strict_mode_refuses_to_start_and_names_every_failure(tmp_path, caplog) -> None:
    structural_only(tmp_path)
    (tmp_path / "worse").mkdir()
    (tmp_path / "worse" / "worse_settings.yaml").write_text("enabled: true\n")
    app = create_app(make_config(plugins_dir=str(tmp_path), plugins_strict=True), receive=False)
    with caplog.at_level(logging.ERROR), pytest.raises(PluginStartupError) as caught:
        async with app.router.lifespan_context(app):
            pytest.fail("the app started")
    assert "refusing to start" in caplog.text
    message = str(caught.value)
    assert "SABLE_PLUGINS_STRICT" in message
    assert "orphan" in message
    assert "worse" in message


async def test_strict_mode_starts_when_nothing_failed(tmp_path) -> None:
    write_plugin(tmp_path, "quiet", rooms=None)
    app = create_app(make_config(plugins_dir=str(tmp_path), plugins_strict=True), receive=False)
    async with app.router.lifespan_context(app):
        pass


async def test_shutdown_closes_the_manager(tmp_path, monkeypatch) -> None:
    write_plugin(tmp_path, "quiet", rooms=None)
    closed: list[bool] = []
    original = PluginManager.aclose

    async def recording(self) -> None:
        closed.append(True)
        await original(self)

    monkeypatch.setattr(PluginManager, "aclose", recording)
    app = create_app(make_config(plugins_dir=str(tmp_path)), receive=False)
    async with app.router.lifespan_context(app):
        assert app.state.plugins is not None
        assert closed == []
    assert closed


# --------------------------------------------------------------------------- #
# python -m sable --check
# --------------------------------------------------------------------------- #


@pytest.fixture
def check_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    for key in list(os.environ):
        if key.startswith("SABLE_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SABLE_NEXTCLOUD_URL", "https://cloud.example.org")
    monkeypatch.setenv("SABLE_NEXTCLOUD_USER", "sable")
    monkeypatch.setenv("SABLE_NEXTCLOUD_PASSWORD", "app-password-1234")
    monkeypatch.setenv("SABLE_ALLOWED_ROOMS", ROOM)
    root = tmp_path / "plugins"
    root.mkdir()
    monkeypatch.setenv("SABLE_PLUGINS_DIR", str(root))
    return root


def test_check_reports_each_plugin(check_env, capsys, tmp_path) -> None:
    structural_only(check_env)
    assert main(["--check", "--env-file", str(tmp_path / "none.env")]) == 0
    out = capsys.readouterr().out
    assert "plugins:" in out
    assert "1 inactive, 1 disabled, 1 failed" in out
    assert "quiet: inactive: no rooms set" in out
    assert "off: disabled" in out
    assert "orphan: failed:" in out


def test_check_exits_non_zero_only_in_strict_mode(check_env, monkeypatch, capsys, tmp_path) -> None:
    structural_only(check_env)
    assert main(["--check", "--env-file", str(tmp_path / "none.env")]) == 0
    capsys.readouterr()
    monkeypatch.setenv("SABLE_PLUGINS_STRICT", "true")
    assert main(["--check", "--env-file", str(tmp_path / "none.env")]) == 2
    assert "orphan: failed" in capsys.readouterr().out


def test_strict_mode_stops_the_server_before_it_starts(
    check_env, monkeypatch, capsys, tmp_path
) -> None:
    structural_only(check_env)
    monkeypatch.setenv("SABLE_PLUGINS_STRICT", "true")
    started: list[object] = []
    monkeypatch.setattr("uvicorn.run", lambda *a, **k: started.append(1))
    assert main(["--env-file", str(tmp_path / "none.env")]) == 2
    assert started == []
    err = capsys.readouterr().err
    assert "SABLE_PLUGINS_STRICT" in err
    assert "orphan" in err


def test_check_without_plugins_prints_no_plugin_lines(monkeypatch, capsys, tmp_path) -> None:
    for key in list(os.environ):
        if key.startswith("SABLE_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SABLE_NEXTCLOUD_URL", "https://cloud.example.org")
    monkeypatch.setenv("SABLE_NEXTCLOUD_USER", "sable")
    monkeypatch.setenv("SABLE_NEXTCLOUD_PASSWORD", "app-password-1234")
    assert main(["--check", "--env-file", str(tmp_path / "none.env")]) == 0
    assert "plugins:" not in capsys.readouterr().out


@respx.mock
async def test_the_call_context_has_no_user_id_for_a_guest(rigs, tmp_path) -> None:
    import json

    write_plugin(tmp_path, "greeter")
    rig = await rigs(tmp_path)
    route = message_route()
    await rig.bot.handle(event("!hi ctx", actor_id="guests/7f3c9a2b"))
    ctx = json.loads(texts(route)[0])
    assert ctx["actor_id"] == "guests/7f3c9a2b"
    assert ctx["user_id"] == ""


@respx.mock
async def test_a_command_with_no_help_has_no_dangling_dash(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "terse", settings=declare(cmd("up")))
    rig = await rigs(tmp_path)
    route = message_route()
    await rig.bot.handle(event("!help"))
    assert "- `!up` _(plugin)_" in texts(route)[0]
    assert "-  _(plugin)_" not in texts(route)[0]


async def test_a_surprise_in_one_plugins_files_fails_only_that_plugin(
    rigs, tmp_path, monkeypatch
) -> None:
    from sable import plugins as module

    write_plugin(tmp_path, "good")
    write_plugin(tmp_path, "unlucky", settings=declare(cmd("lucky")))
    real = module.load_settings

    def surprising(path):
        if "unlucky" in str(path):
            raise RuntimeError("something nobody planned for: SECRET-IN-MESSAGE")
        return real(path)

    monkeypatch.setattr(module, "load_settings", surprising)
    rig = await rigs(tmp_path)
    assert rig.record("good").status is Status.ACTIVE
    unlucky = rig.record("unlucky")
    assert unlucky.status is Status.FAILED
    assert unlucky.reason == "unexpected RuntimeError while checking it"
    assert "SECRET-IN-MESSAGE" not in unlucky.reason


async def test_a_settings_file_that_cannot_exist_fails_even_a_disabled_plugin_quietly(
    rigs, tmp_path
) -> None:
    write_plugin(
        tmp_path, "off", enabled=False, raw_yaml="enabled: false\nsettings: {when: 2025-02-30}\n"
    )
    write_plugin(tmp_path, "big", raw_yaml="settings: {n: " + "9" * 5000 + "}\n")
    write_plugin(tmp_path, "zone", raw_yaml="settings: {t: 2001-12-14t21:59:43.10-99:99}\n")
    write_plugin(tmp_path, "fine")
    rig = await rigs(tmp_path)
    for name in ("off", "big", "zone"):
        assert rig.record(name).status is Status.FAILED, name
        assert "out of range or cannot exist" in rig.record(name).reason
    assert rig.record("fine").status is Status.ACTIVE


async def test_a_pathological_settings_file_is_refused_fast(rigs, tmp_path) -> None:
    import time

    write_plugin(tmp_path, "deep", raw_yaml="settings: " + "[" * 30000 + "]" * 30000 + "\n")
    write_plugin(tmp_path, "dashes", raw_yaml="- " * 30000 + "x\n")
    started = time.monotonic()
    rig = await rigs(tmp_path)
    assert time.monotonic() - started < 3
    for name in ("deep", "dashes"):
        assert rig.record(name).status is Status.FAILED
        assert "levels deep" in rig.record(name).reason


async def test_a_relative_plugins_directory_works(rigs, tmp_path, monkeypatch) -> None:
    write_plugin(tmp_path / "plugins", "greeter")
    monkeypatch.chdir(tmp_path)
    cfg = make_config(plugins_dir="plugins")
    manager = PluginManager(cfg, command=fake_command)
    try:
        await manager.load_all(Registry())
        record = manager.records[0]
        assert record.status is Status.ACTIVE, record.reason
        assert record.entry is not None
        assert record.entry.is_absolute()
        assert manager.root.is_absolute()
    finally:
        await manager.aclose()


# --------------------------------------------------------------------------- #
# Two plugins with one name
# --------------------------------------------------------------------------- #


@respx.mock
async def test_a_duplicate_name_never_shadows_the_plugin_that_holds_it(rigs, tmp_path) -> None:
    write_plugin(
        tmp_path,
        "dup",
        directory="d1",
        settings={
            "declare": {
                "commands": [{"name": "hi"}],
                "phrases": [{"id": "greet", "any": ["gm"], "cooldown": 0}],
                "schedules": [],
            }
        },
    )
    write_plugin(
        tmp_path,
        "dup",
        directory="d2",
        settings={
            "declare": {
                "commands": [{"name": "other"}],
                "phrases": [{"id": "later", "any": ["hello"], "cooldown": 0}],
                "schedules": [],
            }
        },
    )
    rig = await rigs(tmp_path, admin_users=["maser"])
    winner, loser = rig.manager.records
    assert winner.status is Status.ACTIVE
    assert loser.status is Status.FAILED
    assert loser.duplicate
    assert "'dup' is already used by d1/dup.py" in loser.reason
    assert loser.worker is None

    # Everything keyed by name is the winner's.
    assert rig.manager.allows("dup", event("!hi", actor_id=ALICE)) is Access.OK
    assert rig.manager.admins_only("dup") is False
    assert rig.bot.registry.get("hi").plugin == "dup"  # type: ignore[union-attr]
    assert rig.bot.registry.get("other") is None
    route = message_route()
    await rig.bot.handle(event("!hi echo works", message_id=1))
    await rig.bot.handle(event("gm", message_id=2))
    await rig.bot.handle(event("hello", message_id=3))  # the loser's phrase: nobody listens
    assert texts(route) == ["works", "dup/greet matched gm"]
    outcome = await rig.manager.call(
        "dup",
        "command:hi",
        {
            "plugin": "dup",
            "trigger": "command",
            "name": "hi",
            "args": "echo again",
            "argv": [],
            "room": ROOM,
            "actor_id": ALICE,
            "user_id": "alice",
            "actor_name": "Alice",
            "is_admin": False,
            "message_id": 5,
            "text": "",
            "match": "",
        },
        lambda action, args: None,  # type: ignore[arg-type,return-value]
    )
    assert outcome.reply == "again"

    # !plugins <name> is the winner; the loser is listed, with where it lives.
    await rig.bot.handle(event("!plugins dup", actor_id=MASER, message_id=6))
    await rig.bot.handle(event("!plugins", actor_id=MASER, message_id=7))
    detail, listing = texts(route)[2:]
    assert detail.startswith("**dup** - active")
    assert "File: `d1/dup.py`" in detail
    assert "- `dup` - active" in listing
    assert "- `dup (d2/dup.py)` - failed: the name 'dup' is already used by d1/dup.py" in listing
    assert "1 active, 1 failed" in listing


async def test_a_duplicate_of_a_plugin_that_failed_does_not_resurrect_it(rigs, tmp_path) -> None:
    write_plugin(tmp_path, "dup", directory="d1", settings={"load": "error"})
    write_plugin(tmp_path, "dup", directory="d2")
    rig = await rigs(tmp_path)
    first, second = rig.manager.records
    assert first.status is Status.FAILED
    assert second.status is Status.FAILED
    assert second.duplicate
    assert rig.manager.allows("dup", event("!hi")) is Access.NOT_HERE
    assert rig.bot.registry.get("hi") is None


async def test_strict_mode_counts_a_duplicate_as_a_failure(tmp_path) -> None:
    write_plugin(tmp_path, "dup", directory="d1", rooms=None)
    write_plugin(tmp_path, "dup", directory="d2", rooms=None)
    app = create_app(make_config(plugins_dir=str(tmp_path), plugins_strict=True), receive=False)
    with pytest.raises(PluginStartupError) as caught:
        async with app.router.lifespan_context(app):
            pytest.fail("the app started")
    assert "dup (d2/dup.py)" in str(caught.value)


# --------------------------------------------------------------------------- #
# The 64-plugin cap only counts plugins that actually loaded (L7)
# --------------------------------------------------------------------------- #


async def test_the_cap_only_counts_plugins_that_loaded_successfully(rigs, tmp_path) -> None:
    for number in range(MAX_PLUGINS + 3):
        write_plugin(tmp_path, f"p{number:03d}", settings=declare(cmd(f"c{number:03d}")))
    rig = await rigs(tmp_path)
    states = [r.status for r in rig.manager.records]
    assert states[:MAX_PLUGINS] == [Status.ACTIVE] * MAX_PLUGINS
    assert states[MAX_PLUGINS:] == [Status.FAILED] * 3
    reason = rig.manager.records[-1].reason
    assert "not loaded" in reason
    assert f"at most {MAX_PLUGINS}" in reason
    assert "loaded successfully" in reason
    # The message says what is actually true: only successful loads count.
    assert "inactive, disabled or broken" not in reason


async def test_plugins_that_fail_the_handshake_do_not_count_against_the_cap(rigs, tmp_path) -> None:
    """L7's exact repro: 64 plugins that fail at the import/handshake stage, followed
    by 3 good ones - every one of the 3 good ones must load. Before the fix, the cap
    was enforced before the handshake ran, so these 64 (which never actually
    started) held 64 places and the 3 good ones, sorted after them, never got a
    turn."""
    for number in range(MAX_PLUGINS):
        write_plugin(tmp_path, f"bad{number:03d}", settings={"load": "error"})
    for number in range(3):
        write_plugin(tmp_path, f"good{number}", settings=declare(cmd(f"c{number}")))
    rig = await rigs(tmp_path)
    bad = [r for r in rig.manager.records if r.name.startswith("bad")]
    good = [r for r in rig.manager.records if r.name.startswith("good")]
    assert len(bad) == MAX_PLUGINS
    assert len(good) == 3
    assert all(r.status is Status.FAILED for r in bad)
    assert all("no module named nothing" in r.reason for r in bad)
    assert all(r.status is Status.ACTIVE for r in good), [(r.name, r.state) for r in good]


async def test_a_structural_failure_also_does_not_count_against_the_cap(rigs, tmp_path) -> None:
    for number in range(MAX_PLUGINS):
        (tmp_path / f"bad{number:03d}").mkdir()
        (tmp_path / f"bad{number:03d}" / f"bad{number:03d}_settings.yaml").write_text(
            "access: {rooms: [abcd1234]}\n"
        )
    for number in range(3):
        write_plugin(tmp_path, f"good{number}", settings=declare(cmd(f"c{number}")))
    rig = await rigs(tmp_path)
    good = [r for r in rig.manager.records if r.name.startswith("good")]
    assert len(good) == 3
    assert all(r.status is Status.ACTIVE for r in good), [(r.name, r.state) for r in good]
