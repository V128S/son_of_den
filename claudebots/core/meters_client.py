"""Utility meter readings integration with Google Sheets.

Detects meter readings in owner's free-text messages, extracts values via AI,
and appends a new row to the correct sheet tab (Gas / Water / Electricity).

Sheet structure (read from actual table):
  Gas:         Дата | Счетчик | Расход        | $   | Итого
  Water:       Дата | Счетчик | Расход        | $   | Итого
  Electricity: Дата | День    | Ночь | Факт   | Расход | $ | Итого

Formulas are written for calculated columns so Sheets does the math.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

# Sheet tab names (as read from the actual spreadsheet)
SHEET_GAS         = "Gas"
SHEET_WATER       = "Water"
SHEET_ELECTRICITY = "Electricity"

# Quick-detect keywords — check before calling AI
_METER_KEYWORDS = re.compile(
    r"\b(газ|вода|свет|электр|счётчик|счетчик|показани|день|ночь|кубов|квт|kwh|"
    r"gas|water|electr)\b",
    re.IGNORECASE,
)


def looks_like_meter_message(text: str) -> bool:
    """Return True if the text likely contains meter readings."""
    return bool(_METER_KEYWORDS.search(text))


async def extract_meter_readings(text: str, ai_registry: Any) -> dict[str, Any] | None:
    """Ask AI to extract meter values from free-form text.

    Returns a dict with some of these keys (None if not mentioned):
        gas: float | None
        water: float | None
        electricity_day: float | None
        electricity_night: float | None
    Returns None if no readings were found.
    """
    prompt = (
        "Извлеки показания счётчиков из сообщения и верни JSON.\n"
        "Ключи: gas, water, electricity_day, electricity_night.\n"
        "Если показание не упомянуто — ставь null.\n"
        "Числа — только цифры (без единиц).\n"
        "Если в сообщении вообще нет показаний счётчиков — верни только слово: нет\n\n"
        f"Сообщение: {text}"
    )
    try:
        client = ai_registry.get_client("openrouter_gemini")
        raw = await client.complete(
            system="Ты извлекаешь числовые данные из текста. Отвечай строго JSON или словом нет.",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=80,
        )
        raw = raw.strip().strip("`").strip()
        if raw.startswith("json"):
            raw = raw[4:].strip()
        if raw.lower().startswith("нет") or raw.lower() == "no":
            return None
        data = json.loads(raw)
        if isinstance(data, dict) and any(data.get(k) is not None for k in ("gas", "water", "electricity_day", "electricity_night")):
            return data
        return None
    except Exception as e:
        logger.debug("extract_meter_readings failed: %s", e)
        return None


class MetersClient:
    """Write meter readings to Google Sheets."""

    def __init__(
        self,
        service_account_file: Path | None,
        sheet_id: str,
        timezone_str: str = "Europe/Moscow",
    ) -> None:
        self.service_account_file = service_account_file
        self.sheet_id = sheet_id
        try:
            self.tz = ZoneInfo(timezone_str)
        except Exception:
            self.tz = ZoneInfo("Europe/Moscow")
        self._service = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_service(self) -> Any:
        if self._service is not None:
            return self._service
        if not self.service_account_file or not self.service_account_file.exists():
            logger.warning("MetersClient: service account file not found.")
            return None
        try:
            from google.oauth2 import service_account
            from googleapiclient.discovery import build  # type: ignore[import-untyped]
            creds = service_account.Credentials.from_service_account_file(  # type: ignore[no-untyped-call]
                str(self.service_account_file),
                scopes=["https://www.googleapis.com/auth/spreadsheets"],
            )
            self._service = build("sheets", "v4", credentials=creds, cache_discovery=False)
            return self._service
        except Exception as e:
            logger.error("MetersClient: failed to init Sheets service: %s", e)
            return None

    def _get_last_row_num(self, svc: Any, sheet_name: str) -> int:
        """Return the 1-based index of the last row with data (including header)."""
        try:
            result = (
                svc.spreadsheets().values()
                .get(spreadsheetId=self.sheet_id, range=f"'{sheet_name}'!A:A")
                .execute()
            )
            rows = result.get("values", [])
            return max(len(rows), 1)  # at least 1 (header)
        except Exception as e:
            logger.warning("MetersClient: get_last_row failed (%s): %s", sheet_name, e)
            return 1

    def _append_row(self, svc: Any, sheet_name: str, row: list[Any]) -> bool:
        """Append one row to the sheet tab. Returns True on success."""
        try:
            svc.spreadsheets().values().append(
                spreadsheetId=self.sheet_id,
                range=f"'{sheet_name}'!A:A",
                valueInputOption="USER_ENTERED",
                body={"values": [row]},
            ).execute()
            return True
        except Exception as e:
            logger.warning("MetersClient: append failed (%s): %s", sheet_name, e)
            return False

    # ------------------------------------------------------------------
    # Per-meter write methods
    # ------------------------------------------------------------------

    def _write_gas_sync(self, reading: float, date_str: str) -> bool:
        svc = self._get_service()
        if not svc:
            return False
        last = self._get_last_row_num(svc, SHEET_GAS)
        new = last + 1
        row = [
            date_str,
            str(int(reading) if reading == int(reading) else reading),
            f"=B{new}-B{last}",   # Расход = текущий - предыдущий
            f"=D{last}",           # $ = тариф из прошлой строки
            f"=C{new}*D{new}",    # Итого = Расход * $
        ]
        return self._append_row(svc, SHEET_GAS, row)

    def _write_water_sync(self, reading: float, date_str: str) -> bool:
        svc = self._get_service()
        if not svc:
            return False
        last = self._get_last_row_num(svc, SHEET_WATER)
        new = last + 1
        row = [
            date_str,
            str(int(reading) if reading == int(reading) else reading),
            f"=B{new}-B{last}",
            f"=D{last}",
            f"=C{new}*D{new}",
        ]
        return self._append_row(svc, SHEET_WATER, row)

    def _write_electricity_sync(self, day: float, night: float, date_str: str) -> bool:
        svc = self._get_service()
        if not svc:
            return False
        last = self._get_last_row_num(svc, SHEET_ELECTRICITY)
        new = last + 1
        row = [
            date_str,
            str(int(day) if day == int(day) else day),
            str(int(night) if night == int(night) else night),
            f"=B{new}+C{new}",        # Факт = День + Ночь
            f"=D{new}-D{last}",       # Расход = текущий Факт - предыдущий Факт
            f"=F{last}",               # $ = тариф из прошлой строки
            f"=E{new}*F{new}",        # Итого = Расход * $
        ]
        return self._append_row(svc, SHEET_ELECTRICITY, row)

    # ------------------------------------------------------------------
    # Public async API
    # ------------------------------------------------------------------

    async def save_readings(self, readings: dict[str, Any], timeout: float = 15.0) -> dict[str, Any]:
        """Write all non-null readings to their respective sheet tabs.

        Returns a dict: {sheet_name: True/False} for each sheet written.
        """
        loop = asyncio.get_running_loop()
        now = datetime.now(self.tz)
        date_str = now.strftime("%d.%m.%Y")
        results: dict[str, bool | None] = {}

        if readings.get("gas") is not None:
            try:
                ok = await asyncio.wait_for(
                    loop.run_in_executor(None, self._write_gas_sync, float(readings["gas"]), date_str),
                    timeout=timeout,
                )
                results["Gas"] = ok
            except TimeoutError:
                results["Gas"] = False

        if readings.get("water") is not None:
            try:
                ok = await asyncio.wait_for(
                    loop.run_in_executor(None, self._write_water_sync, float(readings["water"]), date_str),
                    timeout=timeout,
                )
                results["Water"] = ok
            except TimeoutError:
                results["Water"] = False

        if readings.get("electricity_day") is not None and readings.get("electricity_night") is not None:
            try:
                ok = await asyncio.wait_for(
                    loop.run_in_executor(
                        None,
                        self._write_electricity_sync,
                        float(readings["electricity_day"]),
                        float(readings["electricity_night"]),
                        date_str,
                    ),
                    timeout=timeout,
                )
                results["Electricity"] = ok
            except TimeoutError:
                results["Electricity"] = False
        elif readings.get("electricity_day") is not None or readings.get("electricity_night") is not None:
            # Only one of the two — can't write incomplete electricity row
            results["Electricity"] = None  # None = skipped, need both

        return results

    def format_confirmation(self, readings: dict[str, Any], results: dict[str, Any]) -> str:
        """Build a human-readable confirmation message."""
        lines = ["✅ Показания записаны в таблицу:"]
        emojis = {"Gas": "🔥", "Water": "💧", "Electricity": "⚡"}
        names  = {"Gas": "Газ", "Water": "Вода", "Electricity": "Электричество"}

        if readings.get("gas") is not None:
            ok = results.get("Gas")
            mark = "✓" if ok else "✗"
            lines.append(f"  {emojis['Gas']} Газ: {readings['gas']} {mark}")

        if readings.get("water") is not None:
            ok = results.get("Water")
            mark = "✓" if ok else "✗"
            lines.append(f"  {emojis['Water']} Вода: {readings['water']} {mark}")

        ed = readings.get("electricity_day")
        en = readings.get("electricity_night")
        if ed is not None and en is not None:
            ok = results.get("Electricity")
            mark = "✓" if ok else "✗"
            lines.append(f"  {emojis['Electricity']} Электро: день {ed}, ночь {en} {mark}")
        elif ed is not None or en is not None:
            lines.append(f"  {emojis['Electricity']} Электро: нужны оба показания (день и ночь)")

        return "\n".join(lines)
