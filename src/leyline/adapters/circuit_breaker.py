"""A small thread-safe circuit breaker.

After ``failure_threshold`` consecutive failures the circuit opens and calls
are refused immediately for ``reset_seconds``; then one trial call is allowed
(half-open). A success closes the circuit, a failure re-opens it. This keeps a
dead model from tying up every worker thread for the full timeout.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable


class CircuitBreaker:
    def __init__(
        self,
        *,
        failure_threshold: int = 5,
        reset_seconds: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._threshold = failure_threshold
        self._reset = reset_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._failures = 0
        self._opened_at: float | None = None
        self._trial_in_flight = False

    @property
    def is_open(self) -> bool:
        with self._lock:
            return self._opened_at is not None

    def allow(self) -> bool:
        """True if a call may proceed. In half-open state only one caller at a
        time gets the trial."""
        with self._lock:
            if self._opened_at is None:
                return True
            if self._clock() - self._opened_at < self._reset:
                return False
            if self._trial_in_flight:
                return False
            self._trial_in_flight = True
            return True

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._opened_at = None
            self._trial_in_flight = False

    def record_failure(self) -> None:
        with self._lock:
            self._trial_in_flight = False
            self._failures += 1
            if self._opened_at is not None or self._failures >= self._threshold:
                self._opened_at = self._clock()
