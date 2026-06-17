"""Unit tests for moderator HTML formatting helpers."""

import pytest

from claudebots.routers.panel import (
    _DEBATE_INITIATOR_INSTRUCTION,
    _DEBATE_RESPONDER_INSTRUCTION,
    _MOD_SUMMARY_INSTRUCTION,
    _REVIVAL_INITIATOR_INSTRUCTION,
    _REVIVAL_RESPONDER_INSTRUCTION,
    _SPEAKER_TURN_INSTRUCTION,
    _format_mod_html,
    _parse_mod_sections,
)


# ---------------------------------------------------------------------------
# Every speaker turn instruction must force Russian — the panel model drifts to
# English on English-language topics, and the system prompt alone doesn't hold.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "instruction",
    [
        _SPEAKER_TURN_INSTRUCTION,
        _REVIVAL_INITIATOR_INSTRUCTION,
        _REVIVAL_RESPONDER_INSTRUCTION,
        _DEBATE_INITIATOR_INSTRUCTION,
        _DEBATE_RESPONDER_INSTRUCTION,
    ],
)
def test_speaker_instruction_enforces_russian(instruction):
    assert "русск" in instruction.lower()


def test_speaker_instruction_still_formats_with_name():
    out = _SPEAKER_TURN_INSTRUCTION.format(name="Аналитик")
    assert "Аналитик" in out
    assert "русск" in out.lower()


def test_debate_instructions_still_format():
    assert "Скептик" in _DEBATE_INITIATOR_INSTRUCTION.format(name="Скептик", opponent="Креатив")
    assert "Креатив" in _DEBATE_RESPONDER_INSTRUCTION.format(name="Креатив", initiator="Скептик")


# ---------------------------------------------------------------------------
# Moderator summary instruction — must force Russian output so the синтез
# does not drift to English (and break the Russian-label parser below).
# ---------------------------------------------------------------------------

def test_mod_summary_instruction_enforces_russian():
    assert "русск" in _MOD_SUMMARY_INSTRUCTION.lower()


def test_mod_summary_instruction_keeps_russian_labels():
    # The parser keys on these exact Russian labels — the instruction must ask for them.
    assert "ВЫВОД:" in _MOD_SUMMARY_INSTRUCTION
    assert "ДЕЙСТВИЕ:" in _MOD_SUMMARY_INSTRUCTION
    assert "ПОЗИЦИЯ:" in _MOD_SUMMARY_INSTRUCTION


# ---------------------------------------------------------------------------
# _parse_mod_sections
# ---------------------------------------------------------------------------

def test_parse_all_three_sections():
    text = (
        "ВЫВОД: Рынок перегрет\n"
        "ДЕЙСТВИЕ: Сократить расходы на 20%\n"
        "ПОЗИЦИЯ: Скептик прав"
    )
    s = _parse_mod_sections(text)
    assert s["ВЫВОД"] == "Рынок перегрет"
    assert s["ДЕЙСТВИЕ"] == "Сократить расходы на 20%"
    assert s["ПОЗИЦИЯ"] == "Скептик прав"


def test_parse_case_insensitive_prefix():
    text = "вывод: Нижний регистр\nдействие: Сделать что-то\nпозиция: Консенсус"
    s = _parse_mod_sections(text)
    assert s["ВЫВОД"] == "Нижний регистр"
    assert s["ДЕЙСТВИЕ"] == "Сделать что-то"
    assert s["ПОЗИЦИЯ"] == "Консенсус"


def test_parse_empty_text():
    assert _parse_mod_sections("") == {}


def test_parse_unstructured_text():
    assert _parse_mod_sections("Всё хорошо, молодцы") == {}


def test_parse_strips_whitespace():
    text = "ВЫВОД:  Вывод с пробелами  \nДЕЙСТВИЕ:  Действие  "
    s = _parse_mod_sections(text)
    assert s["ВЫВОД"] == "Вывод с пробелами"
    assert s["ДЕЙСТВИЕ"] == "Действие"


# ---------------------------------------------------------------------------
# _format_mod_html — structured path
# ---------------------------------------------------------------------------

def test_format_structured_contains_blockquote():
    raw = "ВЫВОД: Ключевой вывод\nДЕЙСТВИЕ: Сделать A\nПОЗИЦИЯ: Прав аналитик"
    html = _format_mod_html(raw)
    assert "<blockquote>" in html
    assert "💡 <b>Ключевой вывод</b>" in html
    assert "✅" in html
    assert "Сделать A" in html
    assert "⚖️" in html
    # Verdict is hidden behind a spoiler — reveals on tap.
    assert "<tg-spoiler>Прав аналитик</tg-spoiler>" in html


def test_format_structured_has_expandable_blockquote():
    raw = "ВЫВОД: Вывод\nДЕЙСТВИЕ: Действие\nПОЗИЦИЯ: Разногласие"
    html = _format_mod_html(raw)
    assert "<blockquote expandable>" in html


def test_format_consensus_uses_italic_not_position_header():
    raw = "ВЫВОД: Вывод\nДЕЙСТВИЕ: Действие\nПОЗИЦИЯ: Консенсус достигнут"
    html = _format_mod_html(raw)
    assert "🤝" in html
    assert "<i>Консенсус достигнут</i>" in html
    assert "⚖️" not in html
    # Consensus has no winner → no spoiler verdict.
    assert "<tg-spoiler>" not in html


# ---------------------------------------------------------------------------
# _format_mod_html — new Telegram rich-text formatting
# ---------------------------------------------------------------------------

def test_format_header_is_bold_underlined():
    html = _format_mod_html("ВЫВОД: X\nДЕЙСТВИЕ: Y\nПОЗИЦИЯ: Консенсус")
    assert "<b><u>Итог дискуссии</u></b>" in html


def test_format_conclusion_is_bold():
    html = _format_mod_html("ВЫВОД: Главное\nДЕЙСТВИЕ: Y\nПОЗИЦИЯ: Консенсус")
    assert "<b>Главное</b>" in html


def test_format_action_is_italic():
    html = _format_mod_html("ВЫВОД: X\nДЕЙСТВИЕ: Сделать раз\nПОЗИЦИЯ: Консенсус")
    assert "<i>Сделать раз</i>" in html


def test_format_disagreement_hides_verdict_in_spoiler():
    html = _format_mod_html("ВЫВОД: X\nДЕЙСТВИЕ: Y\nПОЗИЦИЯ: Прав скептик, риск выше выгоды")
    assert "<tg-spoiler>Прав скептик, риск выше выгоды</tg-spoiler>" in html


def test_format_spoiler_content_is_escaped():
    html = _format_mod_html("ВЫВОД: X\nДЕЙСТВИЕ: Y\nПОЗИЦИЯ: A < B & прав <b>он</b>")
    assert "<tg-spoiler>" in html
    assert "&lt;b&gt;" in html          # inner tag escaped, not rendered
    assert "<b>он</b>" not in html


def test_format_contains_header_and_footer():
    raw = "ВЫВОД: X\nДЕЙСТВИЕ: Y\nПОЗИЦИЯ: Консенсус"
    html = _format_mod_html(raw)
    assert "📋 <b><u>Итог дискуссии</u></b>" in html
    assert "🎤" in html
    assert "<i>Жду следующую тему.</i>" in html


def test_format_escapes_html_special_chars():
    raw = "ВЫВОД: <script>alert(1)</script>\nДЕЙСТВИЕ: A&B\nПОЗИЦИЯ: Консенсус"
    html = _format_mod_html(raw)
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    assert "&amp;B" in html


# ---------------------------------------------------------------------------
# _format_mod_html — fallback path (unstructured AI output)
# ---------------------------------------------------------------------------

def test_format_fallback_wraps_in_blockquote():
    raw = "Просто текст без меток"
    html = _format_mod_html(raw)
    assert "<blockquote>" in html
    assert "Просто текст без меток" in html
    assert "📋 <b><u>Итог дискуссии</u></b>" in html


def test_format_fallback_escapes_special_chars():
    raw = "Текст с <b>тегом</b>"
    html = _format_mod_html(raw)
    assert "<b>тегом</b>" not in html
    assert "&lt;b&gt;тегом&lt;/b&gt;" in html
