from unittest.mock import AsyncMock, MagicMock

from claudebots.routers.panel import PanelRoundRunner


async def test_full_round_calls_each_persona_in_order(
    personas, conv, ai_registry_mock, bot_mocks, alerts_mock, monkeypatch
):
    monkeypatch.setattr("claudebots.routers.panel.MAX_DISCUSSION_MESSAGES", 4)
    runner = PanelRoundRunner(
        bots=bot_mocks,
        personas=personas,
        ai_registry=ai_registry_mock,
        conv=conv,
        alerts=alerts_mock,
        panel_chat_id=-1001,
        typing_sleep_seconds=0,
    )

    await runner.run_round("Тема: что делать с инфляцией?")

    # 4 speakers + 1 moderator = 5 client calls
    # Get the mock client from the registry
    client = ai_registry_mock.get_client("claude")
    assert client.complete.await_count == 5

    # Speakers got their own system prompt in order
    systems = [c.kwargs["system"] for c in client.complete.await_args_list]
    assert systems == [
        "analyst system",
        "skeptic system",
        "creative system",
        "pragmatist system",
        "mod system",
    ]


async def test_each_bot_sends_one_message(personas, conv, ai_registry_mock, bot_mocks, alerts_mock, monkeypatch):
    monkeypatch.setattr("claudebots.routers.panel.MAX_DISCUSSION_MESSAGES", 4)
    runner = PanelRoundRunner(
        bots=bot_mocks,
        personas=personas,
        ai_registry=ai_registry_mock,
        conv=conv,
        alerts=alerts_mock,
        panel_chat_id=-1001,
        typing_sleep_seconds=0,
    )

    await runner.run_round("topic")

    # Moderator sends opening + closing summary
    assert bot_mocks["moderator"].send_message.await_count == 2
    # Each speaker sends one message
    for name in ("analyst", "skeptic", "creative", "pragmatist"):
        assert bot_mocks[name].send_message.await_count == 1
        bot_mocks[name].send_message.assert_any_await(-1001, "canned response", message_thread_id=None)


async def test_history_carries_speaker_labels(personas, conv, ai_registry_mock, bot_mocks, alerts_mock, monkeypatch):
    monkeypatch.setattr("claudebots.routers.panel.MAX_DISCUSSION_MESSAGES", 4)
    runner = PanelRoundRunner(
        bots=bot_mocks,
        personas=personas,
        ai_registry=ai_registry_mock,
        conv=conv,
        alerts=alerts_mock,
        panel_chat_id=-1001,
        typing_sleep_seconds=0,
    )

    await runner.run_round("topic")

    history = conv.get("panel:-1001")
    # First entry: user topic. Then 4 assistant entries with labels.
    assert history[0]["role"] == "user"
    assert "topic" in history[0]["content"]
    assert history[1]["content"].startswith("[Analyst]:")
    assert history[2]["content"].startswith("[Skeptic]:")
    assert history[3]["content"].startswith("[Creative]:")
    assert history[4]["content"].startswith("[Pragmatist]:")


async def test_one_speaker_failure_does_not_kill_round(personas, conv, bot_mocks, alerts_mock, monkeypatch):
    from claudebots.core.ai_registry import AIRegistry
    monkeypatch.setattr("claudebots.routers.panel.MAX_DISCUSSION_MESSAGES", 4)

    client = MagicMock()
    side_effects = [
        "ok1",
        RuntimeError("skeptic fail"),
        "ok3",
        "ok4",
        "summary",
    ]
    client.complete = AsyncMock(side_effect=side_effects)
    client.usage = {"input": 0, "output": 0, "cache_read": 0}

    ai_registry = AIRegistry({"claude": client})

    runner = PanelRoundRunner(
        bots=bot_mocks,
        personas=personas,
        ai_registry=ai_registry,
        conv=conv,
        alerts=alerts_mock,
        panel_chat_id=-1001,
        typing_sleep_seconds=0,
    )

    await runner.run_round("topic")

    # Skeptic did NOT send a message
    bot_mocks["skeptic"].send_message.assert_not_called()
    # Others did
    assert bot_mocks["analyst"].send_message.await_count >= 1
    bot_mocks["creative"].send_message.assert_awaited_once()
    bot_mocks["pragmatist"].send_message.assert_awaited_once()
    # Moderator still produced summary
    assert bot_mocks["moderator"].send_message.await_count == 2
    # Alert fired for skeptic
    alerts_mock.send.assert_any_await("panel_skeptic", "RuntimeError: skeptic fail")


async def test_moderator_failure_sends_fallback(personas, conv, bot_mocks, alerts_mock, monkeypatch):
    from claudebots.core.ai_registry import AIRegistry
    monkeypatch.setattr("claudebots.routers.panel.MAX_DISCUSSION_MESSAGES", 4)

    client = MagicMock()
    side_effects = ["ok1", "ok2", "ok3", "ok4", RuntimeError("mod fail")]
    client.complete = AsyncMock(side_effect=side_effects)
    client.usage = {"input": 0, "output": 0, "cache_read": 0}

    ai_registry = AIRegistry({"claude": client})

    runner = PanelRoundRunner(
        bots=bot_mocks,
        personas=personas,
        ai_registry=ai_registry,
        conv=conv,
        alerts=alerts_mock,
        panel_chat_id=-1001,
        typing_sleep_seconds=0,
    )

    await runner.run_round("topic")

    # Find the closing message and check it contains mod fallback
    closing_call = bot_mocks["moderator"].send_message.await_args_list[-1]
    sent_text = closing_call.args[1] if len(closing_call.args) > 1 else closing_call.kwargs["text"]
    assert "mod fallback" in sent_text
