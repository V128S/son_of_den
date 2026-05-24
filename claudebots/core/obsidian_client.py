"""Obsidian vault integration: append contact messages to per-contact and daily log notes.

Writes two files per interaction:
  Contacts/{safe_name}.md   — per-contact conversation history
  Daily/{YYYY-MM-DD}.md     — daily log across all contacts


Performance notes
-----------------
The public ``log_*`` methods are synchronous and called from inside async
handlers.  Each method used to issue several blocking syscalls — one
``Path.mkdir`` per write (even when the directory had been created
already), one ``Path.exists()`` stat before deciding to write a header,
and two separate ``open()/write()/close()`` cycles per logged message.

The optimised version:

* caches the set of directories already known to exist so ``mkdir`` is
  issued at most once per directory per process lifetime;
* caches the set of files already known to exist so we skip the redundant
  header check after the first message;
* batches header + line into a single ``write()`` call per file.
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
        # Caches — bounded by number of distinct contacts/days, so safe to
        # keep for the lifetime of the process.
        self._known_dirs: set[Path] = set()
        self._known_files: set[Path] = set()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _now(self) -> datetime:
        return datetime.now(self.tz)

    def _ensure_dir(self, directory: Path) -> bool:
        """Create *directory* if needed.  Returns True on success."""
        if directory in self._known_dirs:
            return True
        try:
            directory.mkdir(parents=True, exist_ok=True)
            self._known_dirs.add(directory)
            return True
        except Exception as e:
            logger.warning("Obsidian mkdir failed (%s): %s", directory, e)
            return False

    def _file_exists(self, file_path: Path) -> bool:
        """Has *file_path* ever been seen by this process?

        Uses an in-memory cache to avoid a ``stat`` syscall on every log
        call once we already know the file exists.
        """
        if file_path in self._known_files:
            return True
        if file_path.exists():
            self._known_files.add(file_path)
            return True
        return False

    def _write_block(self, file_path: Path, *blocks: str) -> None:
        """Write the concatenation of *blocks* to *file_path* in one syscall.

        Creates the parent directory once and remembers the file path so we
        skip future existence checks.
        """
        if not blocks:
            return
        if not self._ensure_dir(file_path.parent):
            return
        text = "".join(blocks)
        try:
            with file_path.open("a", encoding="utf-8") as f:
                f.write(text)
        except Exception as e:
            logger.warning("Obsidian write failed (%s): %s", file_path, e)
            return
        self._known_files.add(file_path)

    # Backwards compatible single-write helper (kept for external callers,
    # but the optimised methods below avoid it where possible).
    def _append(self, file_path: Path, text: str) -> None:
        self._write_block(file_path, text)

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

        # --- Per-contact note — batch header (if any) + line into one write ---
        if not self._file_exists(contact_file):
            header = (
                f"# {contact_name}\n"
                f"- **ID:** {contact_id}\n"
                f"- **Первый контакт:** {date_display}\n\n"
                f"---\n\n"
            )
            self._write_block(contact_file, header, line)
        else:
            self._write_block(contact_file, line)

        # --- Daily log — batch header (if any) + entry into one write ---
        daily_line = f"**{safe_name}** {line}"
        if not self._file_exists(daily_file):
            self._write_block(daily_file, f"# Дневник контактов — {date_display}\n\n", daily_line)
        else:
            self._write_block(daily_file, daily_line)

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
        daily_file = self.vault_path / "Daily" / f"{date_str}.md"

        note = (
            f"\U0001f4ca [{time_str}] Перенос прайса: {rows_read} строк прочитано, "
            f"{rows_written} перенесено. Источник: {source_url}\n"
        )

        if not self._file_exists(contact_file):
            header = (
                f"# {contact_name}\n"
                f"- **Первый контакт:** {date_display}\n\n"
                f"---\n\n"
            )
            self._write_block(contact_file, header, note)
        else:
            self._write_block(contact_file, note)

        daily_line = f"**{safe_name}** {note}"
        if not self._file_exists(daily_file):
            self._write_block(daily_file, f"# Дневник контактов — {date_display}\n\n", daily_line)
        else:
            self._write_block(daily_file, daily_line)

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
        daily_file = self.vault_path / "Daily" / f"{date_str}.md"

        link_part = f" [Открыть]({event_link})" if event_link else ""
        note = f"\U0001f4c5 [{time_str}] Событие создано: «{event_summary}»{link_part}\n"

        if not self._file_exists(contact_file):
            header = (
                f"# {contact_name}\n"
                f"- **Первый контакт:** {date_display}\n\n"
                f"---\n\n"
            )
            self._write_block(contact_file, header, note)
        else:
            self._write_block(contact_file, note)

        daily_line = f"**{safe_name}** {note}"
        if not self._file_exists(daily_file):
            self._write_block(daily_file, f"# Дневник контактов — {date_display}\n\n", daily_line)
        else:
            self._write_block(daily_file, daily_line)
