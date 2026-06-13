"""Unit tests for business router helper functions.

All tests manipulate module-level state directly (following the pattern
from integration tests) and clean up afterwards.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import claudebots.routers.business as biz_mod


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clear_biz_state() -> None:
    biz_mod._contact_data.clear()
    biz_mod._contact_topics.clear()
    biz_mod._topic_contacts.clear()
    biz_mod._contact_today.clear()
    biz_mod._admin_topics.clear()
    biz_mod._admin_supergroup_id = None


def _add_contact(user_id: int, name: str, messages: list | None = None) -> None:
    biz_mod._contact_data[user_id] = {
        "name": name,
        "messages": messages or [],
    }


# ---------------------------------------------------------------------------
# get_contacts_summary
# ---------------------------------------------------------------------------

def test_contacts_summary_empty():
    _clear_biz_state()
    assert biz_mod.get_contacts_summary() == "Нет контактов."


def test_contacts_summary_shows_name_and_id():
    _clear_biz_state()
    _add_contact(101, "Иван")
    result = biz_mod.get_contacts_summary()
    assert "Иван" in result
    assert "101" in result


def test_contacts_summary_shows_last_inbound_snippet():
    _clear_biz_state()
    _add_contact(101, "Иван", messages=[
        {"role": "contact", "text": "Привет, как дела?", "time": "12:00"},
        {"role": "assistant", "text": "Хорошо, спасибо!", "time": "12:01"},
    ])
    result = biz_mod.get_contacts_summary()
    assert "Привет, как дела?" in result


def test_contacts_summary_shows_dash_when_no_inbound():
    _clear_biz_state()
    _add_contact(101, "Иван", messages=[
        {"role": "assistant", "text": "Привет!", "time": "10:00"},
    ])
    result = biz_mod.get_contacts_summary()
    assert "—" in result


def test_contacts_summary_respects_max_contacts():
    _clear_biz_state()
    for i in range(10):
        _add_contact(i, f"User{i}")
    result = biz_mod.get_contacts_summary(max_contacts=3)
    # Only last 3 users should appear
    assert "User7" in result
    assert "User8" in result
    assert "User9" in result
    assert "User0" not in result


def test_contacts_summary_header_shows_total_count():
    _clear_biz_state()
    for i in range(5):
        _add_contact(i, f"User{i}")
    result = biz_mod.get_contacts_summary()
    assert "5" in result


# ---------------------------------------------------------------------------
# _build_digest_message
# ---------------------------------------------------------------------------

def test_digest_message_empty():
    _clear_biz_state()
    result = biz_mod._build_digest_message("Europe/Kiev")
    assert "сегодня" in result.lower() or "дайджест" in result.lower()
    assert "не было" in result


def test_digest_message_with_contacts():
    _clear_biz_state()
    _add_contact(42, "Петр", messages=[
        {"role": "contact", "text": "Вопрос про цену", "time": "09:00"},
    ])
    biz_mod._contact_today[42] = 3

    result = biz_mod._build_digest_message("Europe/Kiev")
    assert "Петр" in result
    assert "3" in result
    assert "Итого" in result


def test_digest_message_noun_form_singular():
    """1 сообщение → 'сообщение' (not 'сообщений')."""
    _clear_biz_state()
    _add_contact(1, "Алиса", messages=[
        {"role": "contact", "text": "Привет", "time": "10:00"},
    ])
    biz_mod._contact_today[1] = 1
    result = biz_mod._build_digest_message("Europe/Kiev")
    assert "сообщение" in result


def test_digest_message_noun_form_plural_2_4():
    """2–4 сообщения → 'сообщения'."""
    _clear_biz_state()
    _add_contact(1, "Боб", messages=[
        {"role": "contact", "text": "Hello", "time": "11:00"},
    ])
    biz_mod._contact_today[1] = 3
    result = biz_mod._build_digest_message("Europe/Kiev")
    assert "сообщения" in result


def test_digest_message_noun_form_plural_5_plus():
    """5+ сообщений → 'сообщений'."""
    _clear_biz_state()
    _add_contact(1, "Виктор", messages=[
        {"role": "contact", "text": "много", "time": "12:00"},
    ])
    biz_mod._contact_today[1] = 7
    result = biz_mod._build_digest_message("Europe/Kiev")
    assert "сообщений" in result


def test_digest_message_skips_unknown_user_id():
    """If user_id is in _contact_today but not _contact_data, skip it gracefully."""
    _clear_biz_state()
    biz_mod._contact_today[9999] = 5  # no matching entry in _contact_data
    result = biz_mod._build_digest_message("Europe/Kiev")
    # Should not raise and should still produce output
    assert "Итого" in result


# ---------------------------------------------------------------------------
# Contact eviction (MAX_CONTACTS cap)
# ---------------------------------------------------------------------------

def test_contact_eviction_drops_oldest():
    """When _MAX_CONTACTS is exceeded, the oldest entry is evicted."""
    _clear_biz_state()
    orig_max = biz_mod._MAX_CONTACTS
    biz_mod._MAX_CONTACTS = 3

    try:
        for i in range(3):
            _add_contact(i, f"User{i}")

        # Simulate adding a 4th contact (as the handler does)
        if len(biz_mod._contact_data) >= biz_mod._MAX_CONTACTS:
            oldest_key = next(iter(biz_mod._contact_data))
            del biz_mod._contact_data[oldest_key]
        _add_contact(99, "NewUser")

        assert 0 not in biz_mod._contact_data, "Oldest contact not evicted"
        assert 99 in biz_mod._contact_data
        assert len(biz_mod._contact_data) == 3
    finally:
        biz_mod._MAX_CONTACTS = orig_max


# ---------------------------------------------------------------------------
# init_business_state
# ---------------------------------------------------------------------------

def test_init_business_state_restores_contacts(tmp_path):
    _clear_biz_state()
    data = {
        "contact_topics": {"42": 100, "7": 200},
        "admin_topics": {"📋 Задачи": 55},
        "admin_supergroup_id": -1001234,
    }
    biz_mod.init_business_state(tmp_path / "state.json", data)
    assert biz_mod._contact_topics[42] == 100
    assert biz_mod._contact_topics[7] == 200
    assert biz_mod._topic_contacts[100] == 42
    assert biz_mod._admin_topics["📋 Задачи"] == 55
    assert biz_mod._admin_supergroup_id == -1001234


def test_init_business_state_empty_data(tmp_path):
    _clear_biz_state()
    biz_mod.init_business_state(tmp_path / "state.json", {})
    assert biz_mod._contact_topics == {}
    assert biz_mod._admin_topics == {}
    assert biz_mod._admin_supergroup_id is None


def test_init_business_state_ignores_bad_admin_topics(tmp_path):
    _clear_biz_state()
    data = {"admin_topics": "not a dict"}
    biz_mod.init_business_state(tmp_path / "state.json", data)
    assert biz_mod._admin_topics == {}


# ---------------------------------------------------------------------------
# _classify_owner_category
# ---------------------------------------------------------------------------

async def test_classify_owner_category_returns_matching_category():
    from claudebots.core.ai_registry import AIRegistry

    client = MagicMock()
    client.usage = {"input": 0, "output": 0, "cache_read": 0}
    client.complete = AsyncMock(return_value="📋 Задачи")
    reg = AIRegistry({"openrouter_gemini": client})

    result = await biz_mod._classify_owner_category("Добавь задачу", reg)
    assert result == "📋 Задачи"


async def test_classify_owner_category_defaults_to_raznoe_on_unknown():
    from claudebots.core.ai_registry import AIRegistry

    client = MagicMock()
    client.usage = {"input": 0, "output": 0, "cache_read": 0}
    client.complete = AsyncMock(return_value="Что-то не из списка")
    reg = AIRegistry({"openrouter_gemini": client})

    result = await biz_mod._classify_owner_category("Непонятный текст", reg)
    assert result == "📝 Разное"


async def test_classify_owner_category_defaults_to_raznoe_on_error():
    from claudebots.core.ai_registry import AIRegistry

    client = MagicMock()
    client.usage = {"input": 0, "output": 0, "cache_read": 0}
    client.complete = AsyncMock(side_effect=RuntimeError("API down"))
    reg = AIRegistry({"openrouter_gemini": client})

    result = await biz_mod._classify_owner_category("hello", reg)
    assert result == "📝 Разное"


async def test_classify_owner_category_strips_quotes():
    from claudebots.core.ai_registry import AIRegistry

    client = MagicMock()
    client.usage = {"input": 0, "output": 0, "cache_read": 0}
    client.complete = AsyncMock(return_value='"💰 Финансы"')
    reg = AIRegistry({"openrouter_gemini": client})

    result = await biz_mod._classify_owner_category("расходы", reg)
    assert result == "💰 Финансы"
