"""Obsidian vault integration: append contact messages to per-contact and daily log notes.

Writes two files per interaction:
  Contacts/{safe_name}.md   — per-contact conversation history
  Daily/{YYYY-MM-DD}.md     — daily log across all contacts
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)


def _safe_filename(name: str) -> str:
    """Convert a display name to a filesystem-safe filename (keep Cyrillic, Latin, digits)."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name).strip()
    return name[:100] or "Контакт"


class ObsidianClient:
    """Append contact messages to Obsidian Markdown notes inside a local vault."""

    def __init__(self, vault_path: Path | str, timezone_str: str = "Europe/Moscow") -> None:
        self.vault_path = Path(vault_path)
        try:
            self.tz = ZoneInfo(timezone_str)
        except Exception:
            self.tz = ZoneInfo("Europe/Moscow")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _now(self) -> datetime:
        return datetime.now(self.tz)

    def _append(self, file_path: Path, text: str) -> None:
        """Create parent directories if needed, then append text to file."""
        try:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            with file_path.open("a", encoding="utf-8") as f:
                f.write(text)
        except Exception as e:
            logger.warning("Obsidian write failed (%s): %s", file_path, e)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def log_message(
        self,
        contact_name: str,
        contact_id: int,
        message_text: str,
        role: str = "contact",  # "contact" | "assistant"
    ) -> None:
        """Append one message to both the per-contact note and the daily log."""
        now = self._now()
        time_str = now.strftime("%H:%M")
        date_str = now.strftime("%Y-%m-%d")
        date_display = now.strftime("%d.%m.%Y")

        safe_name = _safe_filename(contact_name)
        contact_file = self.vault_path / "Contacts" / f"{safe_name}.md"
        daily_file = self.vault_path / "Daily" / f"{date_str}.md"

        prefix = "\U0001f4e9" if role == "contact" else "\U0001f916"
        label = contact_name if role == "contact" else "Бот"
        snippet = message_text[:500].replace("\n", " ")
        line = f"{prefix} [{time_str}] {label}: {snippet}\n"

        # --- Per-contact note ---
        if not contact_file.exists():
            header = (
                f"# {contact_name}\n"
                f"- **ID:** {contact_id}\n"
                f"- **Первый контакт:** {date_display}\n\n"
                f"---\n\n"
            )
            self._append(contact_file, header)
        self._append(contact_file, line)

        # --- Daily log ---
        if not daily_file.exists():
            self._append(daily_file, f"# Дневник контактов — {date_display}\n\n")
        self._append(daily_file, f"**{safe_name}** {line}")

        logger.debug("Obsidian: logged %s message for %s", role, contact_name)

    def log_sheets_transfer(
        self,
        contact_name: str,
        rows_read: int,
        rows_written: int,
        source_url: str,
    ) -> None:
        """Append a note about a Sheets transfer to the contact's note."""
        now = self._now()
        date_str = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%H:%M")
        date_display = now.strftime("%d.%m.%Y")

        safe_name = _safe_filename(contact_name)
        contact_file = self.vault_path / "Contacts" / f"{safe_name}.md"

        if not contact_file.exists():
            header = (
                f"# {contact_name}\n"
                f"- **Первый контакт:** {date_display}\n\n"
                f"---\n\n"
            )
            self._append(contact_file, header)

        note = (
            f"\U0001f4ca [{time_str}] Перенос прайса: {rows_read} строк прочитано, "
            f"{rows_written} перенесено. Источник: {source_url}\n"
        )
        self._append(contact_file, note)

        daily_file = self.vault_path / "Daily" / f"{date_str}.md"
        if not daily_file.exists():
            self._append(daily_file, f"# Дневник контактов — {date_display}\n\n")
        self._append(daily_file, f"**{safe_name}** {note}")

        logger.debug("Obsidian: logged sheets transfer for %s", contact_name)

    def log_calendar_event(
        self,
        contact_name: str,
        event_summary: str,
        event_link: str | None,
    ) -> None:
        """Append a note about a created calendar event to the contact's note."""
        now = self._now()
        date_str = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%H:%M")
        date_display = now.strftime("%d.%m.%Y")

        safe_name = _safe_filename(contact_name)
        contact_file = self.vault_path / "Contacts" / f"{safe_name}.md"

        if not contact_file.exists():
            header = (
                f"# {contact_name}\n"
                f"- **Первый контакт:** {date_display}\n\n"
                f"---\n\n"
            )
            self._append(contact_file, header)

        link_part = f" [Открыть]({event_link})" if event_link else ""
        note = f"\U0001f4c5 [{time_str}] Событие создано: «{event_summary}»{link_part}\n"
        self._append(contact_file, note)

        daily_file = self.vault_path / "Daily" / f"{date_str}.md"
        if not daily_file.exists():
            self._append(daily_file, f"# Дневник контактов — {date_display}\n\n")
        self._append(daily_file, f"**{safe_name}** {note}")

    def read_contact_context(self, contact_name: str) -> str | None:
        """Read the ## Контекст section from a contact's note. Returns None if absent."""
        safe_name = _safe_filename(contact_name)
        contact_file = self.vault_path / "Contacts" / f"{safe_name}.md"
        if not contact_file.exists():
            return None
        content = contact_file.read_text(encoding="utf-8")
        match = re.search(r"## Контекст\n(.*?)(?=\n##|\Z)", content, re.DOTALL)
        if not match:
            return None
        text = match.group(1).strip()
        return text or None

    def write_contact_context(
        self, contact_name: str, contact_id: int, context_text: str
    ) -> None:
        """Write/replace the ## Контекст section in a contact's note.

        Preserves all other content (header, log lines). Creates the file if missing.
        """
        safe_name = _safe_filename(contact_name)
        contact_file = self.vault_path / "Contacts" / f"{safe_name}.md"
        context_block = f"## Контекст\n{context_text}\n"

        if contact_file.exists():
            content = contact_file.read_text(encoding="utf-8")
            if "## Контекст" in content:
                # Replace the existing section (everything up to next ## or end-of-file)
                content = re.sub(
                    r"## Контекст\n.*?(?=\n##|\Z)",
                    context_block,
                    content,
                    flags=re.DOTALL,
                )
            else:
                # Insert the section after the --- separator (before log lines)
                if "---\n" in content:
                    head, tail = content.split("---\n", 1)
                    content = f"{head}---\n\n{context_block}\n{tail}"
                else:
                    content = content + f"\n{context_block}"
            try:
                contact_file.write_text(content, encoding="utf-8")
            except Exception as e:
                logger.warning("Obsidian write_contact_context failed (%s): %s", contact_file, e)
        else:
            now = self._now()
            date_display = now.strftime("%d.%m.%Y")
            header = (
                f"# {contact_name}\n"
                f"- **ID:** {contact_id}\n"
                f"- **Первый контакт:** {date_display}\n\n"
                f"---\n\n"
                f"{context_block}\n"
            )
            try:
                contact_file.parent.mkdir(parents=True, exist_ok=True)
                contact_file.write_text(header, encoding="utf-8")
            except Exception as e:
                logger.warning("Obsidian write_contact_context failed (%s): %s", contact_file, e)
