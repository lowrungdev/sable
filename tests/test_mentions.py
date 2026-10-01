from __future__ import annotations

import pytest

from sable.mentions import ZWSP, defang_mentions


@pytest.mark.parametrize(
    "text",
    [
        "@all",
        "hey @all, lunch",
        "@ALL",
        '@"all"',
        '@"group/admins"',
        '@"groups/Team Leads"',
        '@"team/42"',
        '@"teams/abc"',
        "ok.\n@all",
        "(@all)",
        "@all.",
    ],
)
def test_mass_mentions_are_broken(text: str) -> None:
    out = defang_mentions(text)
    assert out != text
    assert "@" + ZWSP in out
    assert out.replace(ZWSP, "") == text, "only a zero-width space is added"


@pytest.mark.parametrize(
    "text",
    [
        "hello @alice",
        '@"guest/abcdef" hi',
        '@"federated_user/karl@cloud.example.net"',
        '@"space user"',
        "mail me at bob@all.example.org",
        "bob@all",
        "@all-hands is a user",
        "@allison",
        "@all.hands",
        "no mention here",
        "",
        "@",
    ],
)
def test_ordinary_mentions_and_lookalikes_are_left_alone(text: str) -> None:
    assert defang_mentions(text) == text


def test_every_mass_mention_in_a_text_is_broken() -> None:
    out = defang_mentions('@all and @"group/x" and @all')
    assert out.count(ZWSP) == 3


def test_it_is_idempotent() -> None:
    once = defang_mentions("@all")
    assert defang_mentions(once) == once
