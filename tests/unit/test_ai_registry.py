from unittest.mock import AsyncMock, MagicMock

from claudebots.core.ai_registry import AIRegistry, FallbackClient


def _make_client(inp: int = 0, out: int = 0, cache: int = 0) -> MagicMock:
    c = MagicMock()
    c.usage = {"input": inp, "output": out, "cache_read": cache}
    return c


def test_snapshot_usage_captures_current_values():
    reg = AIRegistry({"a": _make_client(10, 20, 5), "b": _make_client(3, 7, 0)})
    snap = reg.snapshot_usage()
    assert snap["a"] == {"input": 10, "output": 20, "cache_read": 5}
    assert snap["b"] == {"input": 3, "output": 7, "cache_read": 0}


def test_restore_usage_adds_to_existing():
    ca = _make_client(100, 200, 0)
    reg = AIRegistry({"a": ca})
    reg.restore_usage({"a": {"input": 50, "output": 75, "cache_read": 10}})
    assert ca.usage["input"] == 150
    assert ca.usage["output"] == 275
    assert ca.usage["cache_read"] == 10


def test_restore_usage_skips_unknown_providers():
    reg = AIRegistry({"a": _make_client()})
    reg.restore_usage({"unknown_provider": {"input": 999, "output": 999, "cache_read": 0}})
    # should not raise


def test_restore_usage_skips_non_dict_entries():
    ca = _make_client()
    reg = AIRegistry({"a": ca})
    reg.restore_usage({"a": "not a dict"})
    assert ca.usage["input"] == 0


def test_restore_usage_missing_fields_default_to_zero():
    ca = _make_client()
    reg = AIRegistry({"a": ca})
    reg.restore_usage({"a": {}})
    assert ca.usage == {"input": 0, "output": 0, "cache_read": 0}


def test_daily_usage_starts_at_zero_after_reset():
    reg = AIRegistry({"a": _make_client(100, 200, 5)})
    reg.reset_daily_usage()
    daily = reg.get_daily_usage_by_provider()
    assert daily["a"] == {"input": 0, "output": 0, "cache_read": 0}


def test_daily_usage_tracks_new_usage_after_reset():
    ca = _make_client(100, 200, 5)
    reg = AIRegistry({"a": ca})
    reg.reset_daily_usage()
    # Simulate new usage added after reset
    ca.usage["input"] += 30
    ca.usage["output"] += 50
    daily = reg.get_daily_usage_by_provider()
    assert daily["a"]["input"] == 30
    assert daily["a"]["output"] == 50


def test_daily_total_sums_all_providers():
    ca = _make_client()
    cb = _make_client()
    reg = AIRegistry({"a": ca, "b": cb})
    reg.reset_daily_usage()
    ca.usage["input"] += 10
    cb.usage["output"] += 20
    total = reg.get_daily_total_usage()
    assert total["input"] == 10
    assert total["output"] == 20


def test_daily_usage_never_goes_negative():
    """If baseline exceeds current (e.g. restore_usage ran after reset), floor at 0."""
    ca = _make_client(50, 50, 0)
    reg = AIRegistry({"a": ca})
    reg.reset_daily_usage()
    # Simulate a case where baseline > current (shouldn't happen in practice)
    ca.usage["input"] = 30
    daily = reg.get_daily_usage_by_provider()
    assert daily["a"]["input"] == 0


# ---------------------------------------------------------------------------
# FallbackClient tests
# ---------------------------------------------------------------------------

def _make_async_client(return_value: str = "ok", fail: bool = False) -> MagicMock:
    c = MagicMock()
    c.usage = {"input": 0, "output": 0, "cache_read": 0}
    if fail:
        c.complete = AsyncMock(side_effect=RuntimeError("service down"))
    else:
        c.complete = AsyncMock(return_value=return_value)
    return c


async def test_fallback_client_uses_primary_when_healthy():
    primary = _make_async_client("primary result")
    fallback = _make_async_client("fallback result")
    fc = FallbackClient(primary, fallback, name="test")
    result = await fc.complete("sys", [{"role": "user", "content": "hi"}])
    assert result == "primary result"
    primary.complete.assert_awaited_once()
    fallback.complete.assert_not_awaited()


async def test_fallback_client_uses_fallback_on_primary_failure():
    primary = _make_async_client(fail=True)
    fallback = _make_async_client("fallback result")
    fc = FallbackClient(primary, fallback, name="test")
    result = await fc.complete("sys", [{"role": "user", "content": "hi"}])
    assert result == "fallback result"
    fallback.complete.assert_awaited_once()


async def test_fallback_client_usage_mirrors_primary():
    primary = _make_async_client()
    primary.usage["input"] = 100
    fallback = _make_async_client()
    fc = FallbackClient(primary, fallback, name="test")
    assert fc.usage is primary.usage
    primary.usage["input"] += 50
    assert fc.usage["input"] == 150
