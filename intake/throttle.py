"""Shared rate control for all workers in one run: adaptive pacing and a provider circuit breaker."""
from __future__ import annotations

import threading
import time

# Failure kinds that say "the provider is struggling or unusable", not "this message is bad".
PROVIDER_KINDS = {"rate-limit", "timeout", "connection", "provider-unavailable", "auth"}


class Throttle:
    """Spaces calls at least `interval` apart across threads.

    A 429 (honouring Retry-After) pushes every worker's next call out and doubles the spacing, so workers
    back off together instead of each retrying alone; successes decay the spacing back to `base`.
    """

    def __init__(self, base_interval: float = 0.0, max_interval: float = 8.0,
                 clock=time.monotonic, sleep=time.sleep):
        self.base, self.max, self.interval = base_interval, max_interval, base_interval
        self._clock, self._sleep = clock, sleep
        self._next = 0.0
        self._lock = threading.Lock()
        self.penalties = 0

    def wait(self) -> None:
        with self._lock:
            now = self._clock()
            start = max(now, self._next)
            self._next = start + self.interval
            delay = start - now
        if delay > 0:
            self._sleep(delay)

    def penalize(self, retry_after: float) -> None:
        with self._lock:
            self.penalties += 1
            self._next = max(self._next, self._clock() + retry_after)
            self.interval = min(self.max, max(self.interval * 2, 0.25))

    def success(self) -> None:
        with self._lock:
            self.interval = max(self.base, self.interval * 0.9)
            if self.interval < 0.01:
                self.interval = self.base


class Breaker:
    """Opens after `threshold` consecutive provider-level failures so an outage does not make every
    remaining document wait through its own retries. Remaining documents are recorded as errors."""

    def __init__(self, threshold: int):
        self.threshold, self._n, self._lock = threshold, 0, threading.Lock()

    @property
    def open(self) -> bool:
        return self.threshold > 0 and self._n >= self.threshold

    def failure(self) -> None:
        with self._lock:
            self._n += 1

    def success(self) -> None:
        with self._lock:
            self._n = 0
