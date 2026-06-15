"""Unit tests for admin router commands."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from claudebots.routers.admin import handle_cost, handle_ping, handle_reset
import claudebots.routers.panel as panel_mod


def _make_message(user_id: int = 42, chat_id: int = 42, text: str = "/ping") -> MagicMock:
    msg = MagicMock()
    msg.from_user = MagicMock()
    msg.from_user.id = user_id
    msg.chat.id = chat_id
    msg.chat.type = "private"
    msg.message_thread_id = None
    msg.text = text
    msg.answer = AsyncMock()
    return msg


def _make_settings(admin_id: int = 42) -> MagicMock:
    s = MagicMock()
    s.admin_user_id = admin_id
    s.panel_chat_id = -1001
    return s


async def test_ping_responds_to_admin():
    msg = _make_message(user_id=42)
    await handle_ping(msg, _make_settings(admin_id=42))
    msg.answer.assert_awaited_once_with("pong")


async def test_ping_ignores_non_admin():
    msg = _make_message(user_id=999)
    await handle_ping(msg, _make_settings(admin_id=42))
    msg.answer.assert_not_awaited()


async def test_reset_clears_private_conv_key(conv):
    msg = _make_message(user_id=42, chat_id=42)
    msg.business_connection_id = None  # ensure not treated as biz message
    conv.add("private:42:0", "user", "hello")
    await handle_reset(msg, conv, _make_settings(admin_id=42))
    assert conv.get("private:42:0") == []
    msg.answer.assert_awaited_once()


async def test_panelfind_returns_no_results_for_missing_query():
    from claudebots.routers.admin import _panelfind

    panel_mod._panel_memories.clear()
    panel_mod._panel_memories.append({"text": "О криптовалюте и биткоине", "topic": "Крипто", "ts": 1.0})

    msg = _make_message(user_id=42, text="/panelfind квантовый")
    msg.text = "/panelfind квантовый"
    await _panelfind(msg, _make_settings(admin_id=42))
    reply = msg.answer.call_args[0][0]
    assert "Ничего не найдено" in reply


async def test_panelfind_returns_hits():
    from claudebots.routers.admin import _panelfind

    panel_mod._panel_memories.clear()
    panel_mod._panel_memories.append({"text": "Биткоин вырастет до миллиона", "topic": "Крипто", "ts": 1.0})
    panel_mod._panel_memories.append({"text": "Квантовые компьютеры угрожают безопасности", "topic": "Технологии", "ts": 2.0})

    msg = _make_message(user_id=42, text="/panelfind биткоин")
    msg.text = "/panelfind биткоин"
    await _panelfind(msg, _make_settings(admin_id=42))
    reply = msg.answer.call_args[0][0]
    assert "1" in reply
    assert "Биткоин" in reply or "биткоин" in reply.lower()


async def test_panelfind_non_admin_ignored():
    from claudebots.routers.admin import _panelfind

    msg = _make_message(user_id=999, text="/panelfind что-то")
    msg.text = "/panelfind что-то"
    await _panelfind(msg, _make_settings(admin_id=42))
    msg.answer.assert_not_awaited()


async def test_panelfind_empty_query_shows_usage():
    from claudebots.routers.admin import _panelfind

    msg = _make_message(user_id=42, text="/panelfind")
    msg.text = "/panelfind"
    await _panelfind(msg, _make_settings(admin_id=42))
    reply = msg.answer.call_args[0][0]
    assert "Использование" in reply


async def test_cost_shows_daily_and_alltime():
    from claudebots.core.ai_registry import AIRegistry
    from unittest.mock import MagicMock

    client = MagicMock()
    client.usage = {"input": 5000, "output": 2000, "cache_read": 1000}
    reg = AIRegistry({"groq": client})
    reg.reset_daily_usage()
    # Simulate some usage after reset
    client.usage["input"] += 100
    client.usage["output"] += 50

    msg = _make_message(user_id=42)
    await handle_cost(msg, reg, _make_settings(admin_id=42))

    call_args = msg.answer.call_args[0][0]
    assert "сегодня" in call_args.lower() or "Токены" in call_args
    assert "всего" in call_args.lower() or "ИТОГО" in call_args


# ---------------------------------------------------------------------------
# /panelbest and /panelworst
# ---------------------------------------------------------------------------

async def test_panelbest_no_ratings_shows_empty_message():
    from claudebots.routers.admin import _panelbest
    panel_mod._panel_memories.clear()
    panel_mod._state_path = None  # no state file → get_rated_rounds returns []

    msg = _make_message(user_id=42, text="/panelbest")
    await _panelbest(msg, _make_settings(admin_id=42))
    reply = msg.answer.call_args[0][0]
    assert "нет" in reply.lower() or "👍" in reply


async def test_panelbest_non_admin_ignored():
    from claudebots.routers.admin import _panelbest

    msg = _make_message(user_id=999, text="/panelbest")
    await _panelbest(msg, _make_settings(admin_id=42))
    msg.answer.assert_not_awaited()


async def test_panelworst_non_admin_ignored():
    from claudebots.routers.admin import _panelworst

    msg = _make_message(user_id=999, text="/panelworst")
    await _panelworst(msg, _make_settings(admin_id=42))
    msg.answer.assert_not_awaited()


async def test_panelbest_shows_rounds_with_memory(tmp_path, monkeypatch):
    from claudebots.routers.admin import _panelbest
    import time

    ts = time.time()
    panel_mod._panel_memories.clear()
    panel_mod._panel_memories.append({"text": "ИИ победит всех", "topic": "Технологии", "ts": ts})

    state_file = tmp_path / "state.json"
    import json
    state_file.write_text(json.dumps({"panel_ratings": [
        {"round_id": "r1", "rating": "good", "topic": "Технологии", "ts": ts},
    ]}))
    monkeypatch.setattr(panel_mod, "_state_path", state_file)

    msg = _make_message(user_id=42, text="/panelbest")
    await _panelbest(msg, _make_settings(admin_id=42))
    reply = msg.answer.call_args[0][0]
    assert "Технологии" in reply
    assert "ИИ победит" in reply


async def test_panelworst_shows_bad_rounds(tmp_path, monkeypatch):
    from claudebots.routers.admin import _panelworst
    import time, json

    ts = time.time()
    panel_mod._panel_memories.clear()
    panel_mod._panel_memories.append({"text": "Скучная дискуссия", "topic": "Финансы", "ts": ts})

    state_file = tmp_path / "state.json"
    state_file.write_text(json.dumps({"panel_ratings": [
        {"round_id": "r2", "rating": "bad", "topic": "Финансы", "ts": ts},
    ]}))
    monkeypatch.setattr(panel_mod, "_state_path", state_file)

    msg = _make_message(user_id=42, text="/panelworst")
    await _panelworst(msg, _make_settings(admin_id=42))
    reply = msg.answer.call_args[0][0]
    assert "Финансы" in reply


# ---------------------------------------------------------------------------
# /panelschedule and /panelcancel
# ---------------------------------------------------------------------------

async def test_panelschedule_non_admin_ignored():
    from claudebots.routers.admin import _panelschedule

    msg = _make_message(user_id=999, text="/panelschedule 15:00 Тема")
    settings = _make_settings(admin_id=42)
    settings.user_timezone = "Europe/Moscow"
    settings.panel_chat_id = -1001
    await _panelschedule(msg, {}, MagicMock(), MagicMock(), MagicMock(), MagicMock(), settings)
    msg.answer.assert_not_awaited()


async def test_panelschedule_missing_topic_shows_usage():
    from claudebots.routers.admin import _panelschedule

    msg = _make_message(user_id=42, text="/panelschedule")
    settings = _make_settings(admin_id=42)
    settings.user_timezone = "Europe/Moscow"
    settings.panel_chat_id = -1001
    await _panelschedule(msg, {}, MagicMock(), MagicMock(), MagicMock(), MagicMock(), settings)
    reply = msg.answer.call_args[0][0]
    assert "Использование" in reply or "HH:MM" in reply


async def test_panelschedule_bad_time_format():
    from claudebots.routers.admin import _panelschedule

    msg = _make_message(user_id=42, text="/panelschedule 25:99 Тема")
    settings = _make_settings(admin_id=42)
    settings.user_timezone = "Europe/Moscow"
    settings.panel_chat_id = -1001
    await _panelschedule(msg, {}, MagicMock(), MagicMock(), MagicMock(), MagicMock(), settings)
    reply = msg.answer.call_args[0][0]
    assert "формат" in reply.lower() or "HH:MM" in reply


async def test_panelschedule_creates_task(monkeypatch):
    from claudebots.routers.admin import _panelschedule
    import claudebots.routers.panel as panel_mod

    scheduled_calls: list = []

    def fake_schedule(delay, bots, personas, ai_registry, conv, alerts, chat_id, topic, **kw):
        scheduled_calls.append({"delay": delay, "topic": topic})
        # Also set _scheduled_info so get_scheduled_panel works
        panel_mod._scheduled_info = {"topic": topic, "fire_at": kw.get("fire_at_str", "")}

    monkeypatch.setattr(panel_mod, "schedule_panel_round", fake_schedule)
    monkeypatch.setattr(panel_mod, "_last_thread_id", None)

    msg = _make_message(user_id=42, text="/panelschedule 23:59 Будущее ИИ")
    settings = _make_settings(admin_id=42)
    settings.user_timezone = "Europe/Moscow"
    settings.panel_chat_id = -1001
    await _panelschedule(msg, {}, MagicMock(), MagicMock(), MagicMock(), MagicMock(), settings)

    assert len(scheduled_calls) == 1
    assert scheduled_calls[0]["topic"] == "Будущее ИИ"
    assert scheduled_calls[0]["delay"] > 0
    reply = msg.answer.call_args[0][0]
    assert "23:59" in reply or "запланирован" in reply.lower()


async def test_panelcancel_when_no_schedule():
    from claudebots.routers.admin import _panelcancel
    import claudebots.routers.panel as panel_mod

    panel_mod._scheduled_task = None
    panel_mod._scheduled_info = None

    msg = _make_message(user_id=42, text="/panelcancel")
    await _panelcancel(msg, _make_settings(admin_id=42))
    reply = msg.answer.call_args[0][0]
    assert "нет" in reply.lower()


async def test_panelcancel_cancels_existing(monkeypatch):
    from claudebots.routers.admin import _panelcancel
    import claudebots.routers.panel as panel_mod

    cancelled = [False]

    def fake_cancel():
        cancelled[0] = True
        return True

    def fake_get():
        return {"topic": "Тест отмены", "fire_at": "15:00"}

    monkeypatch.setattr(panel_mod, "cancel_scheduled_panel", fake_cancel)
    monkeypatch.setattr(panel_mod, "get_scheduled_panel", fake_get)

    msg = _make_message(user_id=42, text="/panelcancel")
    await _panelcancel(msg, _make_settings(admin_id=42))

    assert cancelled[0]
    reply = msg.answer.call_args[0][0]
    assert "отменён" in reply.lower() or "Тест отмены" in reply
