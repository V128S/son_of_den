"""Unit tests for moderator HTML formatting helpers."""

import pytest

from claudebots.routers.panel import _format_mod_html, _parse_mod_sections


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
    assert "💡 Ключевой вывод" in html
    assert "✅" in html
    assert "Сделать A" in html
    assert "⚖️" in html
    assert "Прав аналитик" in html


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


def test_format_contains_header_and_footer():
    raw = "ВЫВОД: X\nДЕЙСТВИЕ: Y\nПОЗИЦИЯ: Консенсус"
    html = _format_mod_html(raw)
    assert "📋 <b>Итог дискуссии</b>" in html
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
    assert "📋 <b>Итог дискуссии</b>" in html


def test_format_fallback_escapes_special_chars():
    raw = "Текст с <b>тегом</b>"
    html = _format_mod_html(raw)
    assert "<b>тегом</b>" not in html
    assert "&lt;b&gt;тегом&lt;/b&gt;" in html
