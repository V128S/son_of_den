from unittest.mock import MagicMock

from claudebots.core.ai_registry import AIRegistry


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
