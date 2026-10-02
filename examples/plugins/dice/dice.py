"""!roll - roll dice in the usual notation: `!roll 2d6`, `!roll d20+3`.

The smallest useful plugin: one command, no settings of its own, and a user-facing error
for input it does not understand. Read docs/plugins.md for the rules this follows.
"""

from __future__ import annotations

import re
import secrets

from sable.plugin_api import Context, PluginError, command

# Count and modifier are optional: "d20", "2d6", "3d8-1". Anchored, with every number
# bounded in the pattern itself, so what reaches int() is always short.
NOTATION = re.compile(r"(?P<count>\d{1,2})?d(?P<sides>\d{1,4})(?P<bonus>[+-]\d{1,4})?")

MAX_DICE = 20
MAX_SIDES = 1000


@command(
    "roll",
    aliases=("dice",),
    help="Roll dice, for example 2d6 or d20+3.",
    usage="roll <NdM[+K]>",
)
async def roll(ctx: Context) -> str:
    # Spaces are tolerated ("2d6 + 3") by joining the words before matching.
    notation = "".join(ctx.argv).lower()
    match = NOTATION.fullmatch(notation)
    if match is None:
        # Raised, not returned: the text is for whoever typed the command, so it says
        # what was wrong and what would work.
        raise PluginError("Say what to roll, like `2d6` or `d20+3`.")

    count = int(match["count"] or 1)
    sides = int(match["sides"])
    bonus = int(match["bonus"] or 0)
    if not 1 <= count <= MAX_DICE:
        raise PluginError(f"Roll between 1 and {MAX_DICE} dice at a time.")
    if not 2 <= sides <= MAX_SIDES:
        raise PluginError(f"A die has between 2 and {MAX_SIDES} sides.")

    # `secrets`, not `random`: no reason to make a roll predictable, and it keeps the
    # security linter quiet without a suppression.
    rolls = [secrets.randbelow(sides) + 1 for _ in range(count)]
    total = sum(rolls) + bonus

    # Markdown, since Talk renders it. The reply goes out exactly as written here, apart
    # from the checks sable applies to every plugin's output.
    detail = " + ".join(str(value) for value in rolls)
    if bonus:
        detail += f" {'+' if bonus > 0 else '-'} {abs(bonus)}"
    who = ctx.actor_name or ctx.actor_id
    if count == 1 and not bonus:
        return f"{who} rolled **{total}** (d{sides})"
    return f"{who} rolled **{total}** ({notation}: {detail})"
