"""Sleep-robust daily scheduling.

Background
----------
The bot runs on a MacBook that sleeps overnight. The legacy schedulers used a
single long ``asyncio.sleep(seconds_until_target)``. On macOS the asyncio event
loop is driven by ``time.monotonic()`` == ``mach_absolute_time()``, which **does
not advance while the system is asleep**. So a timer armed in the evening for
07:45 never elapses on schedule — it only counts the machine's awake time — and
the task (e.g. the morning digest) silently never fires.

Fix
---
Instead of one long sleep, poll the **wall clock** in short ``poll_seconds``
chunks. Each chunk freezes during sleep too, but as soon as the Mac wakes the
next poll observes that the target time has passed and fires. A missed task is
therefore delivered within ``poll_seconds`` of wake-up — late, but delivered —
and at most once per calendar day.

Usage::

    tz = ZoneInfo("Europe/Kyiv")
    async for _ in daily_at("07:45", tz, label="digest", log=logger):
        await do_the_work()
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)


def _next_fire_decision(
    now: datetime,
    hour: int,
    minute: int,
    last_fired_date: date | None,
    poll_seconds: int,
) -> tuple[bool, float]:
    """Pure core: decide whether to fire now, else how long to sleep.

    Returns ``(should_fire, sleep_seconds)``. ``sleep_seconds`` is capped to
    ``poll_seconds`` so the loop re-checks the wall clock soon after a wake-up.
    """
    target_today = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now >= target_today and last_fired_date != now.date():
        return True, 0.0
    if now < target_today:
        wait = (target_today - now).total_seconds()
    else:
        wait = (target_today + timedelta(days=1) - now).total_seconds()
    return False, min(wait, float(poll_seconds))


async def daily_at(
    time_str: str,
    tz: ZoneInfo,
    *,
    poll_seconds: int = 60,
    fire_if_missed_on_start: bool = False,
    label: str = "scheduler",
    log: logging.Logger | None = None,
) -> AsyncIterator[datetime]:
    """Yield once per day shortly after local ``HH:MM``, surviving system sleep.

    Args:
        time_str: target local time, ``"HH:MM"``.
        tz: timezone the target is expressed in (scheduling is wall-clock, not
            dependent on the host's system timezone).
        poll_seconds: maximum sleep between wall-clock checks. Smaller = the task
            fires sooner after a wake-up, at the cost of more wake-ups.
        fire_if_missed_on_start: if the process starts *after* today's target and
            the task has not run today, fire immediately. Default ``False`` keeps
            the legacy behaviour (a manual restart at 14:00 does not retroactively
            emit a 07:45 task). The common case — a process that keeps running
            across an overnight sleep — is caught regardless of this flag, because
            ``last_fired`` stays on the previous day.
        label / log: emit one "next run at ..." info line per target day.
    """
    log = log or logger
    hour, minute = map(int, time_str.split(":"))

    now = datetime.now(tz)
    target_today = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    # Seed last_fired so a late start does not retroactively fire today unless asked.
    last_fired: date | None = (
        None if (now < target_today or fire_if_missed_on_start) else now.date()
    )
    logged_for: date | None = None

    while True:
        now = datetime.now(tz)
        should_fire, sleep_s = _next_fire_decision(now, hour, minute, last_fired, poll_seconds)
        if should_fire:
            last_fired = now.date()
            yield now
            continue

        upcoming = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if now >= upcoming:
            upcoming += timedelta(days=1)
        if logged_for != upcoming.date():
            logged_for = upcoming.date()
            log.info("%s: next run at %s", label, upcoming.strftime("%d.%m %H:%M"))

        await asyncio.sleep(sleep_s)
