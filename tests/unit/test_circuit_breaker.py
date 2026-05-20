import pytest

from claudebots.core.circuit_breaker import CircuitBreaker, CircuitBreakerOpen


def test_closed_breaker_allows_calls():
    cb = CircuitBreaker(threshold=3, window_seconds=60, recovery_seconds=120, now=lambda: 0.0)
    cb.check()  # does not raise


def test_opens_after_threshold_failures():
    t = [0.0]
    cb = CircuitBreaker(threshold=3, window_seconds=60, recovery_seconds=120, now=lambda: t[0])
    cb.record_failure()
    t[0] = 1.0
    cb.record_failure()
    t[0] = 2.0
    cb.record_failure()
    with pytest.raises(CircuitBreakerOpen):
        cb.check()


def test_old_failures_drop_out_of_window():
    t = [0.0]
    cb = CircuitBreaker(threshold=3, window_seconds=60, recovery_seconds=120, now=lambda: t[0])
    cb.record_failure()
    cb.record_failure()
    t[0] = 100.0  # past window
    cb.record_failure()
    cb.check()  # should not raise: only 1 failure within window


def test_recovery_after_timeout_allows_one_probe():
    t = [0.0]
    cb = CircuitBreaker(threshold=2, window_seconds=60, recovery_seconds=30, now=lambda: t[0])
    cb.record_failure()
    cb.record_failure()
    with pytest.raises(CircuitBreakerOpen):
        cb.check()
    t[0] = 31.0
    cb.check()  # half-open: probe allowed


def test_success_in_half_open_closes_breaker():
    t = [0.0]
    cb = CircuitBreaker(threshold=2, window_seconds=60, recovery_seconds=30, now=lambda: t[0])
    cb.record_failure()
    cb.record_failure()
    t[0] = 31.0
    cb.check()
    cb.record_success()
    cb.check()
    cb.check()  # multiple checks still allowed


def test_failure_in_half_open_reopens():
    t = [0.0]
    cb = CircuitBreaker(threshold=2, window_seconds=60, recovery_seconds=30, now=lambda: t[0])
    cb.record_failure()
    cb.record_failure()
    t[0] = 31.0
    cb.check()
    cb.record_failure()
    with pytest.raises(CircuitBreakerOpen):
        cb.check()
