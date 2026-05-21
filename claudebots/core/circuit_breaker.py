import logging
import time
from collections import deque
from collections.abc import Callable
from enum import Enum

logger = logging.getLogger(__name__)


class CircuitBreakerOpen(Exception):
    pass


class _State(Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    def __init__(
        self,
        threshold: int,
        window_seconds: float,
        recovery_seconds: float,
        now: Callable[[], float] | None = None,
    ) -> None:
        self._threshold = threshold
        self._window = window_seconds
        self._recovery = recovery_seconds
        self._now = now or time.monotonic
        self._failures: deque[float] = deque()
        self._state = _State.CLOSED
        self._opened_at: float = 0.0

    def check(self) -> None:
        now = self._now()
        if self._state == _State.OPEN:
            if now - self._opened_at >= self._recovery:
                logger.info("Circuit breaker entering HALF_OPEN after %.1fs", self._recovery)
                self._state = _State.HALF_OPEN
                return
            raise CircuitBreakerOpen("Circuit breaker is open")

    def record_failure(self) -> None:
        now = self._now()
        self._failures.append(now)
        self._prune(now)
        if self._state == _State.HALF_OPEN:
            self._open(now)
            return
        if len(self._failures) >= self._threshold:
            self._open(now)

    def record_success(self) -> None:
        if self._state == _State.HALF_OPEN:
            logger.info("Circuit breaker CLOSED after successful probe")
            self._state = _State.CLOSED
            self._failures.clear()

    def _prune(self, now: float) -> None:
        while self._failures and now - self._failures[0] > self._window:
            self._failures.popleft()

    def _open(self, now: float) -> None:
        logger.warning(
            "Circuit breaker OPEN — %d failures in %.1fs window",
            len(self._failures),
            self._window,
        )
        self._state = _State.OPEN
        self._opened_at = now
