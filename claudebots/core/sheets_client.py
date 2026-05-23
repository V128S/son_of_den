"""Google Sheets integration: read external price sheets, write to personal sheet with markup.

Uses the same Google Service Account credentials as the Calendar client.
Requires the service account to have Editor access to the personal sheet,
and View access to any external sheet shared with it.
"""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# Regex to extract a Google Sheets spreadsheet ID from any Sheets URL.
_SHEETS_URL_RE = re.compile(
    r"https://docs\.google\.com/spreadsheets/d/([A-Za-z0-9_-]+)"
)


def extract_sheet_id(text: str) -> str | None:
    """Return the first Google Sheets ID found in *text*, or None."""
    m = _SHEETS_URL_RE.search(text)
    return m.group(1) if m else None


class GoogleSheetsClient:
    """Read an external Google Sheet and append rows to the owner's personal sheet.

    Price markup is applied to every cell in the rightmost numeric-looking column.
    """

    def __init__(
        self,
        service_account_file: Path | None,
        personal_sheet_id: str = "",
        markup_percent: float = 20.0,
    ) -> None:
        self.service_account_file = service_account_file
        self.personal_sheet_id = personal_sheet_id
        self.markup_percent = markup_percent
        self._service = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_service(self):
        """Lazy-load the Sheets API service (cached after first call)."""
        if self._service is not None:
            return self._service
        if not self.service_account_file or not self.service_account_file.exists():
            logger.warning(
                "Sheets: service account file '%s' not found; integration disabled.",
                self.service_account_file,
            )
            return None
        try:
            from google.oauth2 import service_account
            from googleapiclient.discovery import build

            scopes = ["https://www.googleapis.com/auth/spreadsheets"]
            creds = service_account.Credentials.from_service_account_file(
                str(self.service_account_file), scopes=scopes
            )
            self._service = build("sheets", "v4", credentials=creds, cache_discovery=False)
            logger.info("Google Sheets service initialized.")
            return self._service
        except Exception as e:
            logger.error("Failed to initialize Google Sheets API client: %s", e)
            return None

    def _read_sheet_sync(self, sheet_id: str, range_: str = "A:ZZ") -> list[list[str]]:
        """Synchronously read all values from a sheet."""
        svc = self._get_service()
        if not svc:
            return []
        try:
            result = (
                svc.spreadsheets()
                .values()
                .get(spreadsheetId=sheet_id, range=range_)
                .execute()
            )
            return result.get("values", [])
        except Exception as e:
            logger.warning("Sheets: read failed for %s: %s", sheet_id, e)
            return []

    def _apply_markup(self, rows: list[list[str]]) -> list[list[str]]:
        """Return rows with numeric price column multiplied by (1 + markup/100).

        Looks for the last column that has numeric values in the majority of data rows.
        Falls back to marking up the last cell of every row if no price column is found.
        """
        if not rows:
            return rows

        factor = 1.0 + self.markup_percent / 100.0

        def _to_float(val: str) -> float | None:
            cleaned = val.strip().replace(",", ".").replace("\xa0", "").replace(" ", "")
            try:
                return float(cleaned)
            except ValueError:
                return None

        # Find the price column index: rightmost column where >50% of data rows are numeric
        data_rows = rows[1:] if len(rows) > 1 else rows  # skip potential header
        max_cols = max((len(r) for r in data_rows), default=0)
        price_col: int | None = None
        for col_idx in range(max_cols - 1, -1, -1):
            numeric_count = sum(
                1 for row in data_rows
                if col_idx < len(row) and _to_float(row[col_idx]) is not None
            )
            if numeric_count > len(data_rows) * 0.5:
                price_col = col_idx
                break

        out: list[list[str]] = []
        for i, row in enumerate(rows):
            new_row = list(row)
            if price_col is not None and price_col < len(new_row):
                # Skip header row (index 0 when rows > 1)
                if i == 0 and len(rows) > 1:
                    out.append(new_row)
                    continue
                val = _to_float(new_row[price_col])
                if val is not None:
                    marked = round(val * factor, 2)
                    # Preserve integer appearance if result has no decimal part
                    new_row[price_col] = str(int(marked)) if marked == int(marked) else str(marked)
            out.append(new_row)
        return out

    def _write_rows_sync(self, rows: list[list[str]]) -> int:
        """Append *rows* to the personal sheet. Returns number of rows written."""
        if not self.personal_sheet_id:
            logger.warning("Sheets: SHEETS_PERSONAL_ID not configured; skipping write.")
            return 0
        svc = self._get_service()
        if not svc:
            return 0
        try:
            body = {"values": rows}
            result = (
                svc.spreadsheets()
                .values()
                .append(
                    spreadsheetId=self.personal_sheet_id,
                    range="A:ZZ",
                    valueInputOption="USER_ENTERED",
                    body=body,
                )
                .execute()
            )
            updates = result.get("updates", {})
            written = updates.get("updatedRows", len(rows))
            logger.info("Sheets: wrote %d rows to personal sheet.", written)
            return written
        except Exception as e:
            logger.warning("Sheets: write failed: %s", e)
            return 0

    # ------------------------------------------------------------------
    # Public async API
    # ------------------------------------------------------------------

    async def transfer_prices(
        self, source_sheet_id: str, timeout: float = 20.0
    ) -> tuple[int, int]:
        """Read *source_sheet_id*, apply price markup, append to personal sheet.

        Returns ``(rows_read, rows_written)``.
        """
        loop = asyncio.get_running_loop()
        try:
            rows: list[list[str]] = await asyncio.wait_for(
                loop.run_in_executor(None, self._read_sheet_sync, source_sheet_id),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.warning("Sheets: read timed out for %s", source_sheet_id)
            return 0, 0

        if not rows:
            return 0, 0

        marked_rows = self._apply_markup(rows)

        try:
            written: int = await asyncio.wait_for(
                loop.run_in_executor(None, self._write_rows_sync, marked_rows),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            logger.warning("Sheets: write timed out")
            return len(rows), 0

        return len(rows), written
