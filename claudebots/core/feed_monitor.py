"""Feed monitor: poll Telegram channel RSS feeds and auto-trigger panel discussions.

Polls rsshub.app/telegram/channel/<slug> hourly (Atom format), scores entries
with the cheapest available AI provider, and fires a PanelRoundRunner when a
high-quality fresh entry is found — subject to a per-day cap and a minimum
re-trigger interval.
"""
from __future__ import annotations

import asyncio
import html as _html
import logging
import re
import xml.etree.ElementTree as ET
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

from claudebots.core import state as _state

if TYPE_CHECKING:
    from claudebots.core.ai_registry import AIRegistry
    from claudebots.core.alerts import AlertSender
    from claudebots.core.conversation import ConversationStore
    from claudebots.core.personas import PersonaRegistry

logger = logging.getLogger(__name__)

_RSS_BASE = "https://rsshub.app/telegram/channel/{channel}"
_TG_WEB_BASE = "https://t.me/s/{channel}"
_FEED_SEEN_MAX = 500
_ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _strip_html(text: str) -> str:
    """Remove HTML tags and unescape entities; collapse whitespace."""
    text = re.sub(r"<[^>]+>", " ", text)
    text = _html.unescape(text)
    return " ".join(text.split())


def _parse_timestamp(ts_str: str) -> float:
    """Parse an RFC-2822 or ISO-8601 timestamp string into a POSIX float."""
    if not ts_str:
        return 0.0
    try:
        return parsedate_to_datetime(ts_str).timestamp()
    except Exception:
        pass
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def _first_el(entry: ET.Element, *paths: tuple[str, dict[str, str] | None]) -> ET.Element | None:
    """Return first non-None element found by the given (path, namespaces) pairs."""
    for path, ns in paths:
        el = entry.find(path, ns) if ns else entry.find(path)
        if el is not None:
            return el
    return None


def _el_text(el: ET.Element | None) -> str:
    """Extract all text from an XML element, including child element text."""
    if el is None:
        return ""
    return "".join(el.itertext())


def _parse_rss(xml_text: str) -> list[tuple[str, str, str, float]]:
    """Parse Atom or RSS 2.0 XML; return list of (url, title, text, timestamp)."""
    if not xml_text.strip():
        return []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    entries: list[tuple[str, str, str, float]] = []

    # --- Atom ---
    atom_entries = root.findall("atom:entry", _ATOM_NS)
    if not atom_entries:
        atom_entries = root.findall(".//entry")

    for entry in atom_entries:
        link_el = _first_el(
            entry,
            ("atom:link", _ATOM_NS),
            ("link", None),
        )
        title_el = _first_el(
            entry,
            ("atom:title", _ATOM_NS),
            ("title", None),
        )
        content_el = _first_el(
            entry,
            ("atom:content", _ATOM_NS),
            ("atom:summary", _ATOM_NS),
            ("content", None),
            ("summary", None),
        )
        ts_el = _first_el(
            entry,
            ("atom:updated", _ATOM_NS),
            ("atom:published", _ATOM_NS),
            ("updated", None),
            ("published", None),
        )

        url = ""
        if link_el is not None:
            url = link_el.get("href") or link_el.text or ""
        title = _strip_html(_el_text(title_el))
        text = _strip_html(_el_text(content_el))
        ts = _parse_timestamp(ts_el.text or "") if ts_el is not None else 0.0

        if url:
            entries.append((url.strip(), title.strip(), text.strip(), ts))

    if entries:
        return entries

    # --- RSS 2.0 ---
    for item in root.findall(".//item"):
        link_el = item.find("link")
        title_el = item.find("title")
        desc_el = item.find("description")
        pub_el = item.find("pubDate")
        url = (link_el.text or "") if link_el is not None else ""
        title = _strip_html(_el_text(title_el))
        text = _strip_html(_el_text(desc_el))
        ts = _parse_timestamp(pub_el.text or "") if pub_el is not None else 0.0
        if url:
            entries.append((url.strip(), title.strip(), text.strip(), ts))

    return entries


def _parse_tme(html: str, channel: str) -> list[tuple[str, str, str, float]]:
    """Parse t.me/s/<channel> HTML; return list of (url, title, text, timestamp)."""
    entries: list[tuple[str, str, str, float]] = []
    for m in re.finditer(
        r'data-post="([^"]+)".*?'
        r'datetime="([^"]+)".*?'
        r'class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>',
        html,
        re.DOTALL,
    ):
        slug, dt_str, raw = m.group(1), m.group(2), m.group(3)
        url = f"https://t.me/{slug}"
        text = _strip_html(raw)
        ts = _parse_timestamp(dt_str)
        title = text[:80].rstrip()
        if url and text:
            entries.append((url, title, text, ts))
    return entries


# ---------------------------------------------------------------------------
# FeedMonitor
# ---------------------------------------------------------------------------

class FeedMonitor:
    """One-shot feed checker: call run_once() from an asyncio loop."""

    def __init__(
        self,
        *,
        channels: list[str],
        interests: str,
        max_per_day: int,
        min_score: int,
        check_interval_seconds: int,
        min_interval_seconds: int,
        state_path: Path,
        ai_registry: AIRegistry,
        scoring_provider: str,
        bots: dict[str, Any],
        personas: PersonaRegistry,
        conv: ConversationStore,
        alerts: AlertSender,
        panel_chat_id: int,
        search_client: Any = None,
    ) -> None:
        self._channels = channels
        self._interests = interests
        self._max_per_day = max_per_day
        self._min_score = min_score
        self._check_interval = check_interval_seconds
        self._min_interval = min_interval_seconds
        self._state_path = state_path
        self._ai_registry = ai_registry
        self._scoring_provider = scoring_provider
        self._bots = bots
        self._personas = personas
        self._conv = conv
        self._alerts = alerts
        self._panel_chat_id = panel_chat_id
        self._search_client = search_client

    async def run_once(self) -> None:
        """Check feeds and fire a round if a worthy entry is found."""
        if not self._channels:
            return

        data = _state.load(self._state_path)
        feed_seen: list[str] = data.get("feed_seen", [])
        feed_today_count: int = data.get("feed_today_count", 0)
        feed_last_run_ts: float = data.get("feed_last_run_ts", 0.0)
        feed_last_reset_date: str = data.get("feed_last_reset_date", "")

        today = date.today().isoformat()
        if feed_last_reset_date != today:
            feed_today_count = 0
            feed_last_reset_date = today

        if feed_today_count >= self._max_per_day:
            logger.debug("Feed monitor: daily limit reached (%d/%d)", feed_today_count, self._max_per_day)
            return

        now = datetime.now(UTC).timestamp()
        if now - feed_last_run_ts < self._min_interval:
            logger.debug("Feed monitor: min interval not elapsed (%.0f s remaining)",
                         self._min_interval - (now - feed_last_run_ts))
            return

        seen_set = set(feed_seen)
        best_url = best_title = best_text = None
        best_score = -1

        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            for channel in self._channels:
                for url, title, text, pub_ts in await self._fetch_entries(client, channel):
                    if url in seen_set:
                        continue
                    if now - pub_ts > 86_400:  # skip entries older than 24 h
                        continue
                    score = await self._score_entry(title, text)
                    if score > best_score:
                        best_score, best_url, best_title, best_text = score, url, title, text

        if best_url is None or best_title is None or best_text is None or best_score < self._min_score:
            logger.debug("Feed monitor: no worthy entry found (best_score=%d)", best_score)
            # Still update run timestamp so we don't hammer on every call
            _state.update(self._state_path, {
                "feed_last_run_ts": now,
                "feed_last_reset_date": feed_last_reset_date,
            })
            return

        topic = f"Тема из новостей: {best_title}\n\n{best_text[:400]}"
        logger.info("Feed monitor: triggering round (score=%d, url=%s)", best_score, best_url)

        from claudebots.routers.panel import PanelRoundRunner  # late import avoids circular
        runner = PanelRoundRunner(
            bots=self._bots,
            personas=self._personas,
            ai_registry=self._ai_registry,
            conv=self._conv,
            alerts=self._alerts,
            panel_chat_id=self._panel_chat_id,
            thread_id=None,
            search_client=self._search_client,
        )
        _t = asyncio.create_task(runner.run_round(topic))
        _t.add_done_callback(
            lambda t: logger.warning("Feed-triggered round raised: %s", t.exception())
            if not t.cancelled() and t.exception()
            else None
        )

        feed_seen.append(best_url)
        if len(feed_seen) > _FEED_SEEN_MAX:
            feed_seen = feed_seen[-_FEED_SEEN_MAX:]

        _state.update(self._state_path, {
            "feed_seen": feed_seen,
            "feed_today_count": feed_today_count + 1,
            "feed_last_run_ts": now,
            "feed_last_reset_date": feed_last_reset_date,
        })

    async def _fetch_entries(
        self, client: httpx.AsyncClient, channel: str
    ) -> list[tuple[str, str, str, float]]:
        ch = channel.strip()
        # 1. Try rsshub.app (Atom RSS) — works with self-hosted instances
        try:
            r = await client.get(_RSS_BASE.format(channel=ch))
            r.raise_for_status()
            entries = _parse_rss(r.text)
            if entries:
                return entries
            logger.debug("Feed: rsshub returned empty feed for %s, trying t.me/s/", ch)
        except Exception:
            pass  # silent fall-through to t.me/s/
        # 2. Fallback: scrape t.me/s/<channel> (works without external service)
        try:
            r = await client.get(_TG_WEB_BASE.format(channel=ch))
            r.raise_for_status()
            entries = _parse_tme(r.text, ch)
            if entries:
                return entries
            logger.debug("Feed: t.me/s/%s returned no posts", ch)
        except Exception as e:
            logger.warning("Feed: failed to fetch channel=%s: %s", ch, e)
        return []

    async def _score_entry(self, title: str, text: str) -> int:
        """Ask the cheapest AI provider to score the entry 0–10."""
        prompt = (
            f"Оцени от 0 до 10 насколько эта новость достойна панельного обсуждения "
            f"по теме: {self._interests}.\n"
            f"Заголовок: {title}\n"
            f"Текст: {text[:300]}\n"
            f"Ответь только цифрой от 0 до 10."
        )
        try:
            result = await self._ai_registry.get_client(self._scoring_provider).complete(
                messages=[{"role": "user", "content": prompt}],
                system="",
                max_tokens=5,
            )
            return max(0, min(10, int(result.strip().split()[0])))
        except Exception as e:
            logger.warning("Feed: scoring failed: %s", e)
            return 0


# ---------------------------------------------------------------------------
# Background loop
# ---------------------------------------------------------------------------

async def _feed_loop(monitor: FeedMonitor, interval_seconds: int) -> None:
    while True:
        try:
            await monitor.run_once()
        except Exception as e:
            logger.exception("Feed monitor loop error: %s", e)
        await asyncio.sleep(interval_seconds)


async def _fetch_channel_entries_raw(
    channel: str, since_ts: float
) -> list[tuple[str, str, str, float]]:
    """Fetch all feed entries for *channel* newer than *since_ts*."""
    url = _RSS_BASE.format(channel=channel)
    try:
        async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
            resp = await client.get(url)
            if resp.status_code == 200:
                entries = _parse_rss(resp.text)
            else:
                # Fall back to t.me/s scraping
                web_url = _TG_WEB_BASE.format(channel=channel)
                resp2 = await client.get(web_url, headers={"User-Agent": "Mozilla/5.0"})
                entries = _parse_tme(resp2.text, channel) if resp2.status_code == 200 else []
    except Exception as e:
        logger.debug("Digest: fetch failed for %s: %s", channel, e)
        return []
    return [(u, t, tx, ts) for u, t, tx, ts in entries if ts >= since_ts]


async def build_daily_digest(
    *,
    channels: list[str],
    ai_registry: AIRegistry,
    interests: str,
) -> str | None:
    """Fetch all channel entries from the past 24 h and return an AI digest string."""
    if not channels:
        return None
    since = datetime.now(UTC).timestamp() - 86400
    all_entries: dict[str, list[tuple[str, str, str, float]]] = {}
    for ch in channels:
        entries = await _fetch_channel_entries_raw(ch, since)
        if entries:
            all_entries[ch] = entries

    if not all_entries:
        return None

    # Build compact text block for AI
    lines: list[str] = []
    for ch, entries in all_entries.items():
        lines.append(f"=== @{ch} ({len(entries)} posts) ===")
        for _, title, text, _ in entries[:20]:
            snippet = (text or title)[:200].replace("\n", " ")
            lines.append(f"• {snippet}")

    block = "\n".join(lines)[:6000]

    available = list(ai_registry.providers)
    provider = next(
        (p for p in ["openrouter_gemini", "groq", "openrouter_deepseek", "claude"] if p in available),
        available[0] if available else "claude",
    )
    try:
        client = ai_registry.get_client(provider)
        digest = await client.complete(
            system=(
                f"Ты составляешь ежедневный дайджест Telegram-каналов. "
                f"Интересы читателя: {interests}. "
                "Структурируй по каналам, выдели 3-5 самых важных постов, кратко изложи суть каждого. "
                "Формат: канал → краткий список. Язык: русский."
            ),
            messages=[{"role": "user", "content": f"Посты за последние 24 ч:\n\n{block}"}],
            max_tokens=1200,
        )
        return digest.strip() if digest else None
    except Exception as e:
        logger.warning("Digest: AI summarisation failed: %s", e)
        return None


async def _digest_loop(
    *,
    digest_time: str,
    channels: list[str],
    interests: str,
    ai_registry: AIRegistry,
    bot: Any,
    panel_chat_id: int,
    user_timezone: str,
) -> None:
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(user_timezone)
    h, m = map(int, digest_time.split(":"))
    while True:
        now = datetime.now(tz)
        target = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if target <= now:
            target = target.replace(day=target.day + 1)
        wait = (target - now).total_seconds()
        await asyncio.sleep(wait)
        try:
            digest = await build_daily_digest(
                channels=channels,
                ai_registry=ai_registry,
                interests=interests,
            )
            if digest:
                header = f"📰 <b>Дайджест каналов</b> — {datetime.now(tz).strftime('%d.%m %H:%M')}\n\n"
                await bot.send_message(
                    chat_id=panel_chat_id,
                    text=header + digest,
                    parse_mode="HTML",
                )
                logger.info("Daily digest sent to panel chat %d", panel_chat_id)
        except Exception as e:
            logger.warning("Digest loop error: %s", e)


def start_digest_scheduler(
    *,
    digest_time: str,
    channels: list[str],
    interests: str,
    ai_registry: AIRegistry,
    bot: Any,
    panel_chat_id: int,
    user_timezone: str,
) -> asyncio.Task[None]:
    """Start the daily digest background task."""
    task = asyncio.create_task(
        _digest_loop(
            digest_time=digest_time,
            channels=channels,
            interests=interests,
            ai_registry=ai_registry,
            bot=bot,
            panel_chat_id=panel_chat_id,
            user_timezone=user_timezone,
        )
    )
    task.add_done_callback(
        lambda t: logger.warning("Digest scheduler raised: %s", t.exception())
        if not t.cancelled() and t.exception()
        else None
    )
    logger.info("Daily digest scheduler started (time=%s, channels=%s)", digest_time, channels)
    return task


def start_feed_monitor(
    *,
    channels: list[str],
    interests: str,
    max_per_day: int,
    min_score: int,
    check_interval_seconds: int,
    min_interval_seconds: int,
    state_path: Path,
    ai_registry: AIRegistry,
    bots: dict[str, Any],
    personas: PersonaRegistry,
    conv: ConversationStore,
    alerts: AlertSender,
    panel_chat_id: int,
    search_client: Any = None,
) -> asyncio.Task[None]:
    """Create and start the feed monitor background task; return the Task."""
    available = list(ai_registry.providers)
    scoring_provider = next(
        (p for p in ["groq", "openrouter_deepseek", "claude"] if p in available),
        available[0] if available else "claude",
    )
    monitor = FeedMonitor(
        channels=channels,
        interests=interests,
        max_per_day=max_per_day,
        min_score=min_score,
        check_interval_seconds=check_interval_seconds,
        min_interval_seconds=min_interval_seconds,
        state_path=state_path,
        ai_registry=ai_registry,
        scoring_provider=scoring_provider,
        bots=bots,
        personas=personas,
        conv=conv,
        alerts=alerts,
        panel_chat_id=panel_chat_id,
        search_client=search_client,
    )
    task = asyncio.create_task(_feed_loop(monitor, check_interval_seconds))
    task.add_done_callback(
        lambda t: logger.warning("Feed monitor task raised: %s", t.exception())
        if not t.cancelled() and t.exception()
        else None
    )
    logger.info(
        "Feed monitor started: channels=%s, interval=%.0f min, max_per_day=%d",
        channels,
        check_interval_seconds / 60,
        max_per_day,
    )
    return task
