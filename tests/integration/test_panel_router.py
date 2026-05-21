from unittest.mock import AsyncMock, MagicMock

import pytest

from claudebots.routers.panel import PanelRoundRunner


async def test_full_round_calls_each_persona_in_order(
    personas, conv, ai_registry_mock, bot_mocks, alerts_mock, monkeypatch
):
    monkeypatch.setattr("claudebots.routers.panel.MAX_DISCUSSION_MESSAGES", 4)
    monkeypatch.setattr("claudebots.routers.panel.DELAY_BETWEEN_MESSAGES", 0)
    runner = PanelRoundRunner(
        bots=bot_mocks,
        personas=personas,
        ai_registry=ai_registry_mock,
        conv=conv,
        alerts=alerts_mock,
        panel_chat_id=-1001,
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
    monkeypatch.setattr("claudebots.routers.panel.DELAY_BETWEEN_MESSAGES", 0)
    runner = PanelRoundRunner(
        bots=bot_mocks,
        personas=personas,
        ai_registry=ai_registry_mock,
        conv=conv,
        alerts=alerts_mock,
        panel_chat_id=-1001,
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
    monkeypatch.setattr("claudebots.routers.panel.DELAY_BETWEEN_MESSAGES", 0)
    runner = PanelRoundRunner(
        bots=bot_mocks,
        personas=personas,
        ai_registry=ai_registry_mock,
        conv=conv,
        alerts=alerts_mock,
        panel_chat_id=-1001,
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
    monkeypatch.setattr("claudebots.routers.panel.DELAY_BETWEEN_MESSAGES", 0)

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
    monkeypatch.setattr("claudebots.routers.panel.DELAY_BETWEEN_MESSAGES", 0)

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
    )

    await runner.run_round("topic")

    # Find the closing message and check it contains mod fallback
    closing_call = bot_mocks["moderator"].send_message.await_args_list[-1]
    sent_text = closing_call.args[1] if len(closing_call.args) > 1 else closing_call.kwargs["text"]
    assert "mod fallback" in sent_text


# ---------------------------------------------------------------------------
# Revival tests
# ---------------------------------------------------------------------------

async def test_revival_skipped_when_no_history(
    personas, conv, ai_registry_mock, bot_mocks, alerts_mock, monkeypatch
):
    """run_revival() does nothing when conversation history is empty."""
    monkeypatch.setattr("claudebots.routers.panel.DELAY_BETWEEN_MESSAGES", 0)
    runner = PanelRoundRunner(
        bots=bot_mocks,
        personas=personas,
        ai_registry=ai_registry_mock,
        conv=conv,
        alerts=alerts_mock,
        panel_chat_id=-1001,
        thread_id=42,
    )

    await runner.run_revival()

    # No bot should have sent anything
    for name in ("analyst", "skeptic", "creative", "pragmatist", "moderator"):
        bot_mocks[name].send_message.assert_not_called()


async def test_revival_sends_2_or_3_messages(
    personas, conv, ai_registry_mock, bot_mocks, alerts_mock, monkeypatch
):
    """run_revival() sends 2 or 3 short messages; no moderator summary."""
    monkeypatch.setattr("claudebots.routers.panel.DELAY_BETWEEN_MESSAGES", 0)

    # Seed conversation history so revival has something to work with
    conv.add("panel:-1001", "user", "Тема: рост продаж в Q4")
    conv.add("panel:-1001", "assistant", "[Analyst]: нужно увеличить рекламный бюджет")

    runner = PanelRoundRunner(
        bots=bot_mocks,
        personas=personas,
        ai_registry=ai_registry_mock,
        conv=conv,
        alerts=alerts_mock,
        panel_chat_id=-1001,
        thread_id=42,
    )

    await runner.run_revival()

    # Count how many panel speaker bots sent messages (not moderator)
    speaker_sends = sum(
        bot_mocks[name].send_message.await_count
        for name in ("analyst", "skeptic", "creative", "pragmatist")
    )
    assert speaker_sends in (2, 3), f"Expected 2 or 3 revival messages, got {speaker_sends}"

    # Moderator must NOT send anything during revival
    bot_mocks["moderator"].send_message.assert_not_called()


async def test_revival_uses_shorter_max_tokens(
    personas, conv, ai_registry_mock, bot_mocks, alerts_mock, monkeypatch
):
    """Revival calls complete() with max_tokens=120 (shorter than regular turns)."""
    monkeypatch.setattr("claudebots.routers.panel.DELAY_BETWEEN_MESSAGES", 0)

    conv.add("panel:-1001", "user", "Тема: оптимизация процессов")
    conv.add("panel:-1001", "assistant", "[Creative]: попробуй автоматизацию")

    client = ai_registry_mock.get_client("claude")

    runner = PanelRoundRunner(
        bots=bot_mocks,
        personas=personas,
        ai_registry=ai_registry_mock,
        conv=conv,
        alerts=alerts_mock,
        panel_chat_id=-1001,
        thread_id=99,
    )

    await runner.run_revival()

    # All revival calls must use max_tokens=120
    for call in client.complete.await_args_list:
        assert call.kwargs.get("max_tokens") == 120, (
            f"Expected max_tokens=120 in revival call, got {call.kwargs.get('max_tokens')}"
        )


async def test_revival_messages_go_to_correct_thread(
    personas, conv, ai_registry_mock, bot_mocks, alerts_mock, monkeypatch
):
    """Revival messages are sent to the thread_id specified on the runner."""
    monkeypatch.setattr("claudebots.routers.panel.DELAY_BETWEEN_MESSAGES", 0)

    conv.add("panel:-1001", "user", "Тема: маркетинг")

    runner = PanelRoundRunner(
        bots=bot_mocks,
        personas=personas,
        ai_registry=ai_registry_mock,
        conv=conv,
        alerts=alerts_mock,
        panel_chat_id=-1001,
        thread_id=777,
    )

    await runner.run_revival()

    # Every send_message call must target thread 777
    for name in ("analyst", "skeptic", "creative", "pragmatist"):
        for call in bot_mocks[name].send_message.await_args_list:
            assert call.kwargs.get("message_thread_id") == 777, (
                f"Bot {name} sent to wrong thread: {call.kwargs}"
            )


async def test_revival_history_updated_after_messages(
    personas, conv, ai_registry_mock, bot_mocks, alerts_mock, monkeypatch
):
    """Conversation history grows by exactly the number of revival messages sent."""
    monkeypatch.setattr("claudebots.routers.panel.DELAY_BETWEEN_MESSAGES", 0)

    conv.add("panel:-1001", "user", "Тема: инвестиции")
    initial_len = len(conv.get("panel:-1001"))

    runner = PanelRoundRunner(
        bots=bot_mocks,
        personas=personas,
        ai_registry=ai_registry_mock,
        conv=conv,
        alerts=alerts_mock,
        panel_chat_id=-1001,
        thread_id=1,
    )

    await runner.run_revival()

    history = conv.get("panel:-1001")
    added = len(history) - initial_len
    # Only assistant messages should be added (no user prompt injected)
    assert added in (2, 3), f"Expected 2 or 3 new history entries, got {added}"
    for msg in history[initial_len:]:
        assert msg["role"] == "assistant"
