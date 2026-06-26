"""Tests for the sleep-robust daily scheduler helper.

The legacy schedulers used one long ``asyncio.sleep(seconds_until_target)``. On
macOS ``asyncio`` runs on ``mach_absolute_time()``, which FREEZES while the system
sleeps, so an overnight sleep across the target time silently never fires. The
new helper polls the wall clock in short chunks and fires on the next poll after
the target passes — so a missed task is delivered as soon as the Mac wakes.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from claudebots.core.scheduling import _next_fire_decision, daily_at

TZ = ZoneInfo("Europe/Kyiv")


def _at(h: int, m: int, *, day: int = 26) -> datetime:
    return datetime(2026, 6, day, h, m, 0, tzinfo=TZ)


# --------------------------------------------------------------------------- #
# Pure decision function
# --------------------------------------------------------------------------- #


def test_before_target_waits_capped_to_poll():
    # 06:00, target 07:45, far away -> don't fire, sleep is capped to poll_seconds
    fire, sleep_s = _next_fire_decision(_at(6, 0), 7, 45, None, poll_seconds=60)
    assert fire is False
    assert sleep_s == 60


def test_before_target_short_remaining_waits_exact():
    # 07:44:30 -> 30 s left, less than poll -> sleep the exact remaining time
    now = _at(7, 44).replace(second=30)
    fire, sleep_s = _next_fire_decision(now, 7, 45, None, poll_seconds=60)
    assert fire is False
    assert sleep_s == pytest.approx(30.0)


def test_at_or_past_target_not_fired_today_fires():
    # 08:36 (woke from sleep), target 07:45, not fired today -> FIRE (catch-up)
    fire, sleep_s = _next_fire_decision(_at(8, 36), 7, 45, None, poll_seconds=60)
    assert fire is True


def test_already_fired_today_waits_until_tomorrow():
    # Past target but already fired today -> no fire, wait toward tomorrow (capped)
    today = _at(9, 0).date()
    fire, sleep_s = _next_fire_decision(_at(9, 0), 7, 45, today, poll_seconds=60)
    assert fire is False
    assert sleep_s == 60


def test_exactly_at_target_fires():
    fire, _ = _next_fire_decision(_at(7, 45), 7, 45, None, poll_seconds=60)
    assert fire is True


# --------------------------------------------------------------------------- #
# Async generator — the sleep-survival behaviour
# --------------------------------------------------------------------------- #


async def test_daily_at_fires_once_after_target(monkeypatch):
    """Simulate a process that started before the target, the clock crossing it
    (as if the Mac woke past 07:45), and assert exactly one fire that day."""
    import claudebots.core.scheduling as sched

    # Wall-clock timeline the fake `datetime.now` walks through.
    timeline = [
        _at(7, 30),  # init: before target -> last_fired = None
        _at(7, 30),  # loop 1: before target -> sleep
        _at(8, 36),  # loop 2: past target, not fired -> FIRE
        _at(8, 37),  # loop 3: past target, already fired -> wait tomorrow
    ]
    idx = {"i": 0}

    class _FakeDateTime:
        @staticmethod
        def now(tz=None):
            i = min(idx["i"], len(timeline) - 1)
            return timeline[i]

    monkeypatch.setattr(sched, "datetime", _FakeDateTime)

    async def _fake_sleep(_seconds):
        idx["i"] += 1
        if idx["i"] >= len(timeline):
            raise _StopLoop

    class _StopLoop(Exception):
        pass

    monkeypatch.setattr(sched.asyncio, "sleep", _fake_sleep)

    fired_at: list[datetime] = []
    gen = daily_at("07:45", TZ, poll_seconds=60)
    try:
        async for moment in gen:
            fired_at.append(moment)
    except _StopLoop:
        pass

    assert len(fired_at) == 1
    assert fired_at[0] == _at(8, 36)


async def test_daily_at_no_retroactive_fire_on_late_start(monkeypatch):
    """If the process starts AFTER today's target, it must not retroactively fire
    today (default behaviour) — only schedule for tomorrow."""
    import claudebots.core.scheduling as sched

    timeline = [
        _at(14, 0),  # init: past target -> last_fired = today (seeded)
        _at(14, 0),  # loop 1: past target but already 'fired' -> sleep
    ]
    idx = {"i": 0}

    class _FakeDateTime:
        @staticmethod
        def now(tz=None):
            return timeline[min(idx["i"], len(timeline) - 1)]

    class _StopLoop(Exception):
        pass

    async def _fake_sleep(_seconds):
        raise _StopLoop

    monkeypatch.setattr(sched, "datetime", _FakeDateTime)
    monkeypatch.setattr(sched.asyncio, "sleep", _fake_sleep)

    fired_at: list[datetime] = []
    try:
        async for moment in daily_at("07:45", TZ, poll_seconds=60):
            fired_at.append(moment)
    except _StopLoop:
        pass

    assert fired_at == []
