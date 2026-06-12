"""Integration-test-specific fixtures.

Zeros all panel-router timing constants so tests run instantly —
no real asyncio.sleep() calls in test runs.
"""
import pytest


@pytest.fixture(autouse=True)
def zero_panel_delays(monkeypatch):
    """Patch every delay constant in the panel router to 0."""
    for attr in (
        "SILENT_DELAY_MIN",
        "SILENT_DELAY_MAX",
        "REVIVAL_DELAY_MIN",
        "REVIVAL_DELAY_MAX",
        "TYPING_DELAY_MIN",
        "TYPING_DELAY_MAX",
    ):
        monkeypatch.setattr(f"claudebots.routers.panel.{attr}", 0.0)
