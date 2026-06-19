"""Unit tests for claudebots.core.feed_monitor."""
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from claudebots.core.feed_monitor import (
    FeedMonitor,
    _parse_rss,
    _strip_html,
)

# ---------------------------------------------------------------------------
# _strip_html
# ---------------------------------------------------------------------------

def test_strip_html_removes_tags():
    assert _strip_html("<b>hello</b>") == "hello"


def test_strip_html_unescapes_entities():
    assert _strip_html("&amp;") == "&"
    assert _strip_html("&lt;b&gt;") == "<b>"


def test_strip_html_collapses_whitespace():
    assert _strip_html("a  \n  b") == "a b"


def test_strip_html_empty():
    assert _strip_html("") == ""


# ---------------------------------------------------------------------------
# _parse_rss  (Atom + RSS 2.0)
# ---------------------------------------------------------------------------

ATOM_XML = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <link href="https://t.me/testchan/1"/>
    <title>Test title</title>
    <content>Test body</content>
    <updated>2024-01-01T10:00:00Z</updated>
  </entry>
  <entry>
    <link href="https://t.me/testchan/2"/>
    <title>Second</title>
    <content>Body 2</content>
    <updated>2024-01-01T11:00:00Z</updated>
  </entry>
</feed>"""

RSS2_XML = """<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <item>
      <link>https://example.com/1</link>
      <title>RSS Item</title>
      <description>RSS body</description>
      <pubDate>Mon, 01 Jan 2024 12:00:00 +0000</pubDate>
    </item>
  </channel>
</rss>"""


def test_parse_atom_returns_entries():
    entries = _parse_rss(ATOM_XML)
    assert len(entries) == 2
    url, title, text, ts = entries[0]
    assert url == "https://t.me/testchan/1"
    assert title == "Test title"
    assert text == "Test body"
    assert ts > 0


def test_parse_atom_second_entry():
    entries = _parse_rss(ATOM_XML)
    url, title, text, ts = entries[1]
    assert url == "https://t.me/testchan/2"
    assert title == "Second"


def test_parse_rss2_returns_entries():
    entries = _parse_rss(RSS2_XML)
    assert len(entries) == 1
    url, title, text, ts = entries[0]
    assert url == "https://example.com/1"
    assert title == "RSS Item"
    assert text == "RSS body"
    assert ts > 0


def test_parse_rss_empty_string():
    assert _parse_rss("") == []


def test_parse_rss_malformed_xml():
    assert _parse_rss("<not valid xml") == []


def test_parse_rss_no_items():
    xml = '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"></feed>'
    assert _parse_rss(xml) == []


def test_parse_atom_strips_html_in_content():
    xml = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <link href="https://t.me/c/1"/>
    <title><b>Bold</b></title>
    <content>Hello &amp; world</content>
    <updated>2024-01-01T10:00:00Z</updated>
  </entry>
</feed>"""
    entries = _parse_rss(xml)
    assert entries[0][1] == "Bold"
    assert entries[0][2] == "Hello & world"


# ---------------------------------------------------------------------------
# FeedMonitor state logic
# ---------------------------------------------------------------------------

def _make_monitor(tmp_path, *, channels=None, max_per_day=2, min_score=7):

    if channels is None:
        channels = ["testchan"]

    ai_registry = MagicMock()
    ai_client = AsyncMock()
    ai_client.complete = AsyncMock(return_value="9")
    ai_registry.get_client.return_value = ai_client
    ai_registry.providers = ["groq"]

    bots = MagicMock()
    personas = MagicMock()
    conv = MagicMock()
    alerts = MagicMock()

    state_path = tmp_path / "state.json"

    return FeedMonitor(
        channels=channels,
        interests="AI, tech",
        max_per_day=max_per_day,
        min_score=min_score,
        check_interval_seconds=3600,
        min_interval_seconds=14400,
        state_path=state_path,
        ai_registry=ai_registry,
        scoring_provider="groq",
        bots=bots,
        personas=personas,
        conv=conv,
        alerts=alerts,
        panel_chat_id=-100123,
    )


@pytest.mark.asyncio
async def test_daily_limit_prevents_extra_rounds(tmp_path):
    """If feed_today_count >= max_per_day, run_once() returns without starting a round."""
    monitor = _make_monitor(tmp_path, max_per_day=2)
    # Directly set state that already hit the daily limit
    from claudebots.core import state as _state
    _state.update(
        monitor._state_path,
        {
            "feed_today_count": 2,
            "feed_last_reset_date": date.today().isoformat(),
            "feed_last_run_ts": 0.0,
        },
    )
    with patch("claudebots.core.feed_monitor.asyncio.create_task") as mock_create:
        await monitor.run_once()
        mock_create.assert_not_called()


@pytest.mark.asyncio
async def test_min_interval_blocks_run(tmp_path):
    """run_once() does nothing if last run was less than 4 hours ago."""
    monitor = _make_monitor(tmp_path)
    now = datetime.now(UTC).timestamp()
    from claudebots.core import state as _state
    _state.update(
        monitor._state_path,
        {
            "feed_today_count": 0,
            "feed_last_reset_date": date.today().isoformat(),
            "feed_last_run_ts": now - 3600,  # 1 hour ago — under 4h minimum
        },
    )
    with patch("claudebots.core.feed_monitor.asyncio.create_task") as mock_create:
        await monitor.run_once()
        mock_create.assert_not_called()


@pytest.mark.asyncio
async def test_seen_dedup_skips_known_url(tmp_path):
    """Entries whose URL is already in feed_seen are skipped."""
    monitor = _make_monitor(tmp_path)
    from claudebots.core import state as _state
    _state.update(
        monitor._state_path,
        {
            "feed_seen": ["https://t.me/testchan/1"],
            "feed_today_count": 0,
            "feed_last_reset_date": date.today().isoformat(),
            "feed_last_run_ts": 0.0,
        },
    )
    now = datetime.now(UTC).timestamp()
    fake_entries = [("https://t.me/testchan/1", "Title", "Body", now - 100)]
    with (
        patch.object(monitor, "_fetch_entries", return_value=fake_entries),
        patch("claudebots.core.feed_monitor.asyncio.create_task") as mock_create,
    ):
        await monitor.run_once()
        mock_create.assert_not_called()


@pytest.mark.asyncio
async def test_midnight_reset_clears_daily_count(tmp_path):
    """Daily count resets when the reset date doesn't match today."""
    monitor = _make_monitor(tmp_path, max_per_day=2)
    from claudebots.core import state as _state
    _state.update(
        monitor._state_path,
        {
            "feed_today_count": 2,  # yesterday's limit
            "feed_last_reset_date": "2000-01-01",  # stale date
            "feed_last_run_ts": 0.0,
        },
    )
    now = datetime.now(UTC).timestamp()
    fake_entries = [("https://t.me/c/42", "Fresh news", "Some body", now - 300)]
    mock_runner = MagicMock()
    mock_runner.run_round = MagicMock()
    with (
        patch.object(monitor, "_fetch_entries", return_value=fake_entries),
        patch.object(monitor, "_score_entry", return_value=9),
        patch("claudebots.core.feed_monitor.asyncio.create_task") as mock_create,
        patch("claudebots.routers.panel.PanelRoundRunner", return_value=mock_runner),
    ):
        await monitor.run_once()
        # Count was reset, so a round should have started
        mock_create.assert_called_once()


@pytest.mark.asyncio
async def test_old_entry_skipped(tmp_path):
    """Entries older than 24 h are skipped even if unseen."""
    monitor = _make_monitor(tmp_path)
    from claudebots.core import state as _state
    _state.update(monitor._state_path, {"feed_today_count": 0, "feed_last_run_ts": 0.0,
                                         "feed_last_reset_date": date.today().isoformat()})
    now = datetime.now(UTC).timestamp()
    old_ts = now - 90_000  # 25 hours ago
    fake_entries = [("https://t.me/c/99", "Old news", "Body", old_ts)]
    with (
        patch.object(monitor, "_fetch_entries", return_value=fake_entries),
        patch("claudebots.core.feed_monitor.asyncio.create_task") as mock_create,
    ):
        await monitor.run_once()
        mock_create.assert_not_called()


@pytest.mark.asyncio
async def test_low_score_no_round(tmp_path):
    """Entries scoring below min_score don't trigger a round."""
    monitor = _make_monitor(tmp_path, min_score=7)
    from claudebots.core import state as _state
    _state.update(monitor._state_path, {"feed_today_count": 0, "feed_last_run_ts": 0.0,
                                         "feed_last_reset_date": date.today().isoformat()})
    now = datetime.now(UTC).timestamp()
    fake_entries = [("https://t.me/c/5", "Meh news", "Boring", now - 100)]
    with (
        patch.object(monitor, "_fetch_entries", return_value=fake_entries),
        patch.object(monitor, "_score_entry", return_value=3),  # below min_score=7
        patch("claudebots.core.feed_monitor.asyncio.create_task") as mock_create,
    ):
        await monitor.run_once()
        mock_create.assert_not_called()


@pytest.mark.asyncio
async def test_high_score_triggers_round(tmp_path):
    """A high-scoring fresh entry triggers asyncio.create_task."""
    monitor = _make_monitor(tmp_path, min_score=7)
    from claudebots.core import state as _state
    _state.update(monitor._state_path, {"feed_today_count": 0, "feed_last_run_ts": 0.0,
                                         "feed_last_reset_date": date.today().isoformat()})
    now = datetime.now(UTC).timestamp()
    fake_entries = [("https://t.me/c/7", "Hot news", "Great stuff", now - 100)]
    with (
        patch.object(monitor, "_fetch_entries", return_value=fake_entries),
        patch.object(monitor, "_score_entry", return_value=9),
        patch("claudebots.core.feed_monitor.asyncio.create_task") as mock_create,
    ):
        mock_task = MagicMock()
        mock_task.add_done_callback = MagicMock()
        mock_create.return_value = mock_task
        with patch("claudebots.routers.panel.PanelRoundRunner"):
            await monitor.run_once()
        mock_create.assert_called_once()


@pytest.mark.asyncio
async def test_topic_contains_title(tmp_path):
    """The panel topic message contains the entry title."""
    monitor = _make_monitor(tmp_path, min_score=7)
    from claudebots.core import state as _state
    _state.update(monitor._state_path, {"feed_today_count": 0, "feed_last_run_ts": 0.0,
                                         "feed_last_reset_date": date.today().isoformat()})
    now = datetime.now(UTC).timestamp()
    fake_entries = [("https://t.me/c/8", "Unique headline XYZ", "Body text", now - 100)]
    mock_runner = MagicMock()
    mock_runner.run_round = MagicMock()

    with (
        patch.object(monitor, "_fetch_entries", return_value=fake_entries),
        patch.object(monitor, "_score_entry", return_value=9),
        patch("claudebots.core.feed_monitor.asyncio.create_task") as mock_create,
        patch("claudebots.routers.panel.PanelRoundRunner", return_value=mock_runner),
    ):
        mock_task = MagicMock()
        mock_task.add_done_callback = MagicMock()
        mock_create.return_value = mock_task
        await monitor.run_once()

    # The topic passed to run_round should mention the headline
    mock_runner.run_round.assert_called_once()
    called_topic = mock_runner.run_round.call_args[0][0]
    assert "Unique headline XYZ" in called_topic


# ---------------------------------------------------------------------------
# _parse_tme (t.me/s/ HTML scraper)
# ---------------------------------------------------------------------------

TME_HTML = """
<div class="tgme_widget_message_wrap">
  <div data-post="testchan/42">
    <time datetime="2024-01-15T10:00:00+00:00"></time>
    <div class="tgme_widget_message_text js-message_text" dir="auto">
      Hot AI news today!
    </div>
  </div>
</div>
<div class="tgme_widget_message_wrap">
  <div data-post="testchan/43">
    <time datetime="2024-01-15T11:00:00+00:00"></time>
    <div class="tgme_widget_message_text js-message_text" dir="auto">
      <b>Another</b> post with &amp; entities
    </div>
  </div>
</div>
"""


def test_parse_tme_returns_entries():
    from claudebots.core.feed_monitor import _parse_tme
    entries = _parse_tme(TME_HTML, "testchan")
    assert len(entries) == 2
    url, title, text, ts = entries[0]
    assert url == "https://t.me/testchan/42"
    assert "AI news" in text
    assert ts > 0


def test_parse_tme_strips_html():
    from claudebots.core.feed_monitor import _parse_tme
    entries = _parse_tme(TME_HTML, "testchan")
    _, _, text, _ = entries[1]
    assert "<b>" not in text
    assert "Another" in text
    assert "&" in text  # entity decoded


def test_parse_tme_empty_html():
    from claudebots.core.feed_monitor import _parse_tme
    assert _parse_tme("", "testchan") == []


@pytest.mark.asyncio
async def test_empty_channels_no_http(tmp_path):
    """When channels list is empty, no HTTP requests are made."""
    monitor = _make_monitor(tmp_path, channels=[])
    with patch("httpx.AsyncClient") as mock_client:
        await monitor.run_once()
        mock_client.assert_not_called()
