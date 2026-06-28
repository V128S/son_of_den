"""Tests for ObsidianClient.read_contact_context and write_contact_context."""
from pathlib import Path

import pytest

from claudebots.core.obsidian_client import ObsidianClient


@pytest.fixture
def vault(tmp_path: Path) -> ObsidianClient:
    return ObsidianClient(vault_path=tmp_path, timezone_str="Europe/Kyiv")


def test_read_context_returns_none_when_no_file(vault: ObsidianClient) -> None:
    assert vault.read_contact_context("Иван") is None


def test_write_then_read_context(vault: ObsidianClient) -> None:
    vault.write_contact_context("Иван", 123, "Оптовый клиент. Торгуется.")
    result = vault.read_contact_context("Иван")
    assert result == "Оптовый клиент. Торгуется."


def test_write_context_replaces_existing(vault: ObsidianClient) -> None:
    vault.write_contact_context("Иван", 123, "Старый контекст")
    vault.write_contact_context("Иван", 123, "Новый контекст")
    result = vault.read_contact_context("Иван")
    assert result == "Новый контекст"
    assert "Старый контекст" not in result


def test_write_context_creates_file_if_missing(vault: ObsidianClient, tmp_path: Path) -> None:
    vault.write_contact_context("Новый", 456, "Первое упоминание")
    expected = tmp_path / "Contacts" / "Новый.md"
    assert expected.exists()
    assert "Первое упоминание" in expected.read_text(encoding="utf-8")


def test_write_context_preserves_log_lines(vault: ObsidianClient) -> None:
    # First log some messages, then write context — log lines must survive
    vault.log_message("Иван", 123, "Привет", role="contact")
    vault.write_contact_context("Иван", 123, "Новый контекст")
    contact_file = vault.vault_path / "Contacts" / "Иван.md"
    content = contact_file.read_text(encoding="utf-8")
    assert "Привет" in content
    assert "Новый контекст" in content


def test_read_context_returns_none_when_section_missing(vault: ObsidianClient) -> None:
    # File exists but has no ## Контекст section
    vault.log_message("Петр", 789, "Сообщение", role="contact")
    assert vault.read_contact_context("Петр") is None


def test_write_context_with_unsafe_name(vault: ObsidianClient, tmp_path: Path) -> None:
    vault.write_contact_context("Иван/Петров", 123, "Клиент")
    # Slash should be stripped from filename
    expected = tmp_path / "Contacts" / "ИванПетров.md"
    assert expected.exists()
