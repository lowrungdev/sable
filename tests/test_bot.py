from __future__ import annotations

import json

import httpx
import respx
from conftest import BACKEND, ROOM, FakeLLM, event, make_config

from sable.bot import Bot
from sable.config import LLMConfig
from sable.llm import LLMError
from sable.talk import API_BASE

MESSAGE_URL = f"{BACKEND}{API_BASE}/bot/{ROOM}/message"
REACTION_URL = f"{BACKEND}{API_BASE}/bot/{ROOM}/reaction/100"


def message_route():
    return respx.post(MESSAGE_URL).mock(
        return_value=httpx.Response(201, json={"ocs": {"data": {"id": 1}}})
    )


def sent(route) -> list[dict]:
    return [json.loads(call.request.content) for call in route.calls]


@respx.mock
async def test_a_command_gets_a_reply(bot: Bot) -> None:
    route = message_route()
    await bot.handle(event("!ping"))
    assert sent(route)[0]["message"] == "pong 🏓"


@respx.mock
async def test_help_lists_the_commands(bot: Bot) -> None:
    route = message_route()
    await bot.handle(event("!help"))
    body = sent(route)[0]["message"]
    assert "`!ping`" in body and "`!ai <question>`" in body
    assert "@sable" in body


@respx.mock
async def test_help_for_one_command(bot: Bot) -> None:
    route = message_route()
    await bot.handle(event("!help echo"))
    body = sent(route)[0]["message"]
    assert "**!echo**" in body and "Usage: `!echo <text>`" in body


@respx.mock
async def test_command_error_is_sent_to_the_room(bot: Bot) -> None:
    route = message_route()
    await bot.handle(event("!echo"))
    assert sent(route)[0]["message"] == "Give me something to echo."


@respx.mock
async def test_unknown_command_hints(bot: Bot) -> None:
    route = message_route()
    await bot.handle(event("!nope"))
    assert "`!help`" in sent(route)[0]["message"]


@respx.mock
async def test_unknown_command_can_stay_silent(llm: FakeLLM) -> None:
    route = message_route()
    bot = Bot(make_config(unknown_command_hint=False), llm=llm)  # type: ignore[arg-type]
    try:
        await bot.handle(event("!nope"))
    finally:
        await bot.aclose()
    assert not route.called


@respx.mock
async def test_messages_from_bots_are_ignored(bot: Bot) -> None:
    route = message_route()
    await bot.handle(
        event("!ping", actor_id="bots/bot-abc", actor_type="Application")
    )
    assert not route.called


@respx.mock
async def test_a_redelivered_event_is_handled_once(bot: Bot) -> None:
    route = message_route()
    incoming = event("!ping", message_id=55)
    await bot.handle(incoming)
    await bot.handle(incoming)
    assert len(route.calls) == 1


@respx.mock
async def test_join_and_leave_are_noted_without_replying(bot: Bot) -> None:
    route = message_route()
    join = {
        "type": "Join",
        "actor": {"type": "Person", "id": "users/alice", "name": "Alice"},
        "object": {"type": "Collection", "id": ROOM, "name": "Team chat"},
    }
    from sable.events import parse_event

    await bot.handle(parse_event(join))
    assert not route.called


@respx.mock
async def test_a_mention_goes_to_the_model(bot: Bot, llm: FakeLLM) -> None:
    route = message_route()
    await bot.handle(event("@sable how are you?"))
    assert sent(route)[0]["message"] == "mock answer"
    assert llm.last_prompt == "Alice: how are you?"
    assert llm.calls[0][0]["role"] == "system"
    assert "Team chat" in llm.calls[0][0]["content"]


@respx.mock
async def test_a_bare_name_counts_as_a_mention(bot: Bot, llm: FakeLLM) -> None:
    message_route()
    await bot.handle(event("Sable: status?"))
    assert llm.last_prompt == "Alice: status?"


@respx.mock
async def test_a_mention_mid_sentence_keeps_the_whole_text(bot: Bot, llm: FakeLLM) -> None:
    message_route()
    await bot.handle(event("hey @sable what is up"))
    assert llm.last_prompt == "Alice: hey @sable what is up"


@respx.mock
async def test_a_similar_word_is_not_a_mention(bot: Bot, llm: FakeLLM) -> None:
    route = message_route()
    await bot.handle(event("sabletooth tigers are extinct"))
    assert not route.called
    assert not llm.calls


@respx.mock
async def test_a_mention_can_still_carry_a_command(bot: Bot, llm: FakeLLM) -> None:
    route = message_route()
    await bot.handle(event("@sable !ping"))
    assert sent(route)[0]["message"] == "pong 🏓"
    assert not llm.calls


@respx.mock
async def test_plain_chatter_is_ignored(bot: Bot, llm: FakeLLM) -> None:
    route = message_route()
    await bot.handle(event("just talking to my colleague"))
    assert not route.called
    assert not llm.calls


@respx.mock
async def test_an_ai_room_answers_everything(llm: FakeLLM) -> None:
    route = message_route()
    bot = Bot(make_config(ai_rooms=["*"]), llm=llm)  # type: ignore[arg-type]
    try:
        await bot.handle(event("no mention needed"))
    finally:
        await bot.aclose()
    assert sent(route)[0]["message"] == "mock answer"


@respx.mock
async def test_ai_rooms_can_be_listed_by_token(llm: FakeLLM) -> None:
    route = message_route()
    bot = Bot(make_config(ai_rooms=["other-room"]), llm=llm)  # type: ignore[arg-type]
    try:
        await bot.handle(event("no mention needed"))
    finally:
        await bot.aclose()
    assert not route.called


@respx.mock
async def test_the_ai_command_works_without_a_mention(bot: Bot, llm: FakeLLM) -> None:
    route = message_route()
    await bot.handle(event("!ai what is 2+2"))
    assert sent(route)[0]["message"] == "mock answer"
    assert llm.last_prompt == "Alice: what is 2+2"


@respx.mock
async def test_history_is_threaded_through_and_can_be_reset(bot: Bot, llm: FakeLLM) -> None:
    message_route()
    await bot.handle(event("@sable first", message_id=1))
    await bot.handle(event("@sable second", message_id=2))

    roles = [m["role"] for m in llm.calls[1]]
    assert roles == ["system", "user", "assistant", "user"]
    assert llm.calls[1][1]["content"] == "Alice: first"
    assert llm.calls[1][2]["content"] == "mock answer"

    await bot.handle(event("!reset", message_id=3))
    assert bot.history.get(ROOM) == []


@respx.mock
async def test_the_model_is_only_asked_when_configured(llm: FakeLLM) -> None:
    route = message_route()
    bot = Bot(make_config(llm=LLMConfig()), llm=llm)  # type: ignore[arg-type]
    try:
        await bot.handle(event("@sable hello"))
    finally:
        await bot.aclose()
    assert not route.called
    assert not llm.calls


@respx.mock
async def test_a_model_failure_is_reported_in_the_room(bot: Bot) -> None:
    route = message_route()
    bot._llm = FakeLLM(error=LLMError("the model exploded"))  # type: ignore[assignment]
    await bot.handle(event("@sable hello"))
    assert sent(route)[0]["message"].startswith("⚠️")
    assert "exploded" in sent(route)[0]["message"]


@respx.mock
async def test_errors_can_be_kept_out_of_the_room(llm: FakeLLM) -> None:
    route = message_route()
    bot = Bot(
        make_config(report_errors=False),
        llm=FakeLLM(error=LLMError("boom")),  # type: ignore[arg-type]
    )
    try:
        await bot.handle(event("@sable hello"))
    finally:
        await bot.aclose()
    assert not route.called


@respx.mock
async def test_the_thinking_reaction_is_added_and_removed(llm: FakeLLM) -> None:
    message_route()
    add = respx.post(REACTION_URL).mock(return_value=httpx.Response(201, json={"ocs": {}}))
    remove = respx.delete(REACTION_URL).mock(return_value=httpx.Response(200, json={"ocs": {}}))
    bot = Bot(make_config(thinking_reaction="👀"), llm=llm)  # type: ignore[arg-type]
    try:
        await bot.handle(event("@sable hello", message_id=100))
    finally:
        await bot.aclose()
    assert add.called and remove.called


@respx.mock
async def test_the_reaction_is_removed_even_when_the_model_fails(llm: FakeLLM) -> None:
    message_route()
    respx.post(REACTION_URL).mock(return_value=httpx.Response(201, json={"ocs": {}}))
    remove = respx.delete(REACTION_URL).mock(
        return_value=httpx.Response(200, json={"ocs": {}})
    )
    bot = Bot(
        make_config(thinking_reaction="👀"),
        llm=FakeLLM(error=LLMError("boom")),  # type: ignore[arg-type]
    )
    try:
        await bot.handle(event("@sable hello", message_id=100))
    finally:
        await bot.aclose()
    assert remove.called


@respx.mock
async def test_replies_can_thread_under_the_question(llm: FakeLLM) -> None:
    route = message_route()
    bot = Bot(make_config(reply_as_reply=True), llm=llm)  # type: ignore[arg-type]
    try:
        await bot.handle(event("!ping", message_id=77))
    finally:
        await bot.aclose()
    assert sent(route)[0]["replyTo"] == 77


@respx.mock
async def test_a_custom_prefix_is_honoured(llm: FakeLLM) -> None:
    route = message_route()
    bot = Bot(make_config(command_prefix="/"), llm=llm)  # type: ignore[arg-type]
    try:
        await bot.handle(event("/ping"))
        await bot.handle(event("!ping", message_id=101))
    finally:
        await bot.aclose()
    assert len(route.calls) == 1
    assert sent(route)[0]["message"] == "pong 🏓"


@respx.mock
async def test_a_crashing_command_is_reported_not_raised(bot: Bot) -> None:
    route = message_route()

    async def boom(ctx):
        raise RuntimeError("unexpected")

    bot.registry.command("boom", hidden=True)(boom)
    try:
        await bot.handle(event("!boom"))
    finally:
        bot.registry._commands.pop("boom")
    assert "crashed" in sent(route)[0]["message"]


@respx.mock
async def test_whoami_describes_the_sender(bot: Bot) -> None:
    route = message_route()
    await bot.handle(event("!whoami"))
    body = sent(route)[0]["message"]
    assert "Alice" in body and "users/alice" in body and ROOM in body


@respx.mock
async def test_empty_messages_do_nothing(bot: Bot) -> None:
    route = message_route()
    await bot.handle(event("   "))
    assert not route.called


@respx.mock
async def test_an_unreachable_nextcloud_does_not_crash_the_handler(bot: Bot) -> None:
    # The webhook was answered long ago; a failed post is a log line, not a raise.
    respx.post(MESSAGE_URL).mock(side_effect=httpx.ConnectError("refused"))
    await bot.handle(event("!ping"))


@respx.mock
async def test_an_unreachable_nextcloud_does_not_crash_the_error_path(llm: FakeLLM) -> None:
    respx.post(MESSAGE_URL).mock(side_effect=httpx.ConnectError("refused"))
    bot = Bot(make_config(), llm=FakeLLM(error=LLMError("boom")))  # type: ignore[arg-type]
    try:
        await bot.handle(event("@sable hello"))
    finally:
        await bot.aclose()


@respx.mock
async def test_two_people_reacting_to_one_message_are_both_seen(bot: Bot) -> None:
    # The event id is the message reacted *to*, so these share it. Keying on the
    # id alone would drop the second as a redelivery.
    from conftest import reaction_event

    alice = reaction_event("👍", message_id=100, actor_id="users/alice")
    bob = reaction_event("😄", message_id=100, actor_id="users/bob")
    assert bot.seen(alice) is False
    assert bot.seen(bob) is False


@respx.mock
async def test_one_person_reacting_twice_with_different_emoji(bot: Bot) -> None:
    from conftest import reaction_event

    first = reaction_event("👍", message_id=100)
    second = reaction_event("🎉", message_id=100)
    assert bot.seen(first) is False
    assert bot.seen(second) is False


@respx.mock
async def test_the_same_reaction_redelivered_is_still_deduplicated(bot: Bot) -> None:
    from conftest import reaction_event

    event_ = reaction_event("👍", message_id=100)
    assert bot.seen(event_) is False
    assert bot.seen(reaction_event("👍", message_id=100)) is True


@respx.mock
async def test_adding_and_removing_a_reaction_are_distinct_events(bot: Bot) -> None:
    from conftest import reaction_event

    added = reaction_event("👍", message_id=100)
    removed = reaction_event("👍", message_id=100, undo=True)
    assert bot.seen(added) is False
    assert bot.seen(removed) is False


@respx.mock
async def test_reactions_draw_no_reply_and_no_model_call(bot: Bot, llm: FakeLLM) -> None:
    from conftest import reaction_event

    route = message_route()
    await bot.handle(reaction_event("👍"))
    await bot.handle(reaction_event("👍", undo=True))
    assert not route.called
    assert not llm.calls


# --------------------------------------------------------------------------- #
# React with the ask emoji to send a message to the model
# --------------------------------------------------------------------------- #

ASK = "⁉️"


@respx.mock
async def test_reacting_with_the_ask_emoji_answers_that_message(
    bot: Bot, llm: FakeLLM
) -> None:
    from conftest import reaction_event

    route = message_route()
    # The bot only knows the text because it saw the message go past.
    await bot.handle(event("what is the capital of Peru?", message_id=100, actor_name="Bob"))
    await bot.handle(reaction_event(ASK, message_id=100, actor_name="Alice"))

    assert sent(route)[0]["message"] == "mock answer"
    # Threaded to the message asked about, not floating free.
    assert sent(route)[0]["replyTo"] == 100
    prompt = llm.last_prompt
    assert "what is the capital of Peru?" in prompt
    assert "Bob" in prompt and "Alice" in prompt


@respx.mock
async def test_the_ask_emoji_matches_without_its_variation_selector(
    bot: Bot, llm: FakeLLM
) -> None:
    from conftest import reaction_event

    message_route()
    await bot.handle(event("explain this", message_id=100))
    await bot.handle(reaction_event("⁉", message_id=100))  # no U+FE0F
    assert llm.calls


@respx.mock
async def test_another_emoji_does_nothing(bot: Bot, llm: FakeLLM) -> None:
    from conftest import reaction_event

    route = message_route()
    await bot.handle(event("hello", message_id=100))
    await bot.handle(reaction_event("👍", message_id=100))
    assert not route.called
    assert not llm.calls


@respx.mock
async def test_removing_the_ask_emoji_does_nothing(bot: Bot, llm: FakeLLM) -> None:
    from conftest import reaction_event

    route = message_route()
    await bot.handle(event("hello", message_id=100))
    await bot.handle(reaction_event(ASK, message_id=100, undo=True))
    assert not route.called
    assert not llm.calls


@respx.mock
async def test_asking_about_a_message_it_never_saw_says_so(bot: Bot, llm: FakeLLM) -> None:
    from conftest import reaction_event

    route = message_route()
    await bot.handle(reaction_event(ASK, message_id=999))
    assert not llm.calls
    body = sent(route)[0]["message"]
    assert "do not have that message" in body
    assert sent(route)[0]["replyTo"] == 999


@respx.mock
async def test_the_ask_emoji_works_on_the_bots_own_answer(bot: Bot, llm: FakeLLM) -> None:
    from conftest import reaction_event

    message_route()
    # A bot message is cached but never acted on, so a follow-up question works.
    await bot.handle(
        event("42 is the answer", message_id=100, actor_id="bots/bot-abc", actor_type="Application")
    )
    await bot.handle(reaction_event(ASK, message_id=100))
    assert llm.calls
    assert "42 is the answer" in llm.last_prompt


@respx.mock
async def test_nothing_is_cached_when_the_feature_is_off(llm: FakeLLM) -> None:
    from conftest import reaction_event

    route = message_route()
    bot = Bot(make_config(ask_reaction=""), llm=llm)  # type: ignore[arg-type]
    try:
        await bot.handle(event("hello", message_id=100))
        assert bot.messages.get(ROOM, 100) is None
        await bot.handle(reaction_event(ASK, message_id=100))
    finally:
        await bot.aclose()
    assert not route.called
    assert not llm.calls


@respx.mock
async def test_the_ask_emoji_is_ignored_with_no_model_configured(llm: FakeLLM) -> None:
    from conftest import reaction_event

    route = message_route()
    bot = Bot(make_config(llm=LLMConfig()), llm=llm)  # type: ignore[arg-type]
    try:
        await bot.handle(event("hello", message_id=100))
        await bot.handle(reaction_event(ASK, message_id=100))
    finally:
        await bot.aclose()
    assert not route.called
    assert not llm.calls


@respx.mock
async def test_a_model_failure_on_an_ask_is_reported(llm: FakeLLM) -> None:
    from conftest import reaction_event

    route = message_route()
    bot = Bot(make_config(), llm=FakeLLM(error=LLMError("boom")))  # type: ignore[arg-type]
    try:
        await bot.handle(event("hello", message_id=100))
        await bot.handle(reaction_event(ASK, message_id=100))
    finally:
        await bot.aclose()
    assert sent(route)[0]["message"].startswith("⚠️")
