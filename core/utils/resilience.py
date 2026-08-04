"""
Generic retry-with-backoff and circuit-breaker helpers for wrapping
flaky outbound calls (Groww broker/market-data transports today, any
future integration tomorrow). Kept dependency-free and provider-agnostic
in ``core/utils`` so ``core/broker`` and ``core/market_data`` can both
use the same building blocks without either depending on the other.

Retry and circuit-breaking are deliberately separate primitives, not one
combined decorator: a caller decides per call whether retrying is even
safe (a read is always safe to retry; a write like ``place_order`` is
not, unless the provider guarantees idempotent submission - retrying it
blindly risks a duplicate order). The circuit breaker is safe to wrap
around every call, read or write, since it never re-attempts anything
itself - it only fails fast once a dependency is clearly down.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class RetryPolicy:
    """Exponential backoff: delay doubles each attempt, capped at ``max_delay_seconds``."""

    max_attempts: int = 3
    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 8.0


def retry_with_backoff(
    fn: Callable[[], T],
    *,
    policy: RetryPolicy,
    is_retryable: Callable[[Exception], bool],
    sleep: Callable[[float], None] = time.sleep,
) -> T:
    """
    Call ``fn`` up to ``policy.max_attempts`` times, sleeping with
    doubling backoff between attempts. Only exceptions for which
    ``is_retryable(exc)`` is true are retried; anything else - or the
    final attempt's failure - propagates immediately. ``sleep`` is
    injectable so tests don't actually wait.
    """
    for attempt in range(policy.max_attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - re-raised as-is when not retryable/exhausted
            if not is_retryable(exc) or attempt == policy.max_attempts - 1:
                raise
            delay = min(policy.base_delay_seconds * (2**attempt), policy.max_delay_seconds)
            sleep(delay)
    raise AssertionError("unreachable: retry_with_backoff always returns or raises above")


class CircuitOpenError(RuntimeError):
    """Raised instead of attempting a call while a ``CircuitBreaker`` is open."""


@dataclass
class CircuitBreaker:
    """
    Trips open after ``failure_threshold`` consecutive failures and fails
    fast (raising ``CircuitOpenError`` without attempting the call) for
    ``cooldown_seconds`` - so a genuinely down dependency gets left alone
    to recover instead of being hammered on every bar of a session. One
    successful call closes the circuit and resets the failure count.
    """

    failure_threshold: int = 5
    cooldown_seconds: float = 30.0
    clock: Callable[[], float] = field(default=time.monotonic, repr=False)
    _consecutive_failures: int = field(default=0, init=False)
    _opened_at: float | None = field(default=None, init=False)

    def call(self, fn: Callable[[], T]) -> T:
        if self._opened_at is not None:
            if self.clock() - self._opened_at < self.cooldown_seconds:
                raise CircuitOpenError(
                    f"circuit open after {self._consecutive_failures} consecutive "
                    f"failures; retry after {self.cooldown_seconds}s cooldown"
                )
            self._opened_at = None  # cooldown elapsed - let one trial call through

        try:
            result = fn()
        except Exception:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self.failure_threshold:
                self._opened_at = self.clock()
            raise
        else:
            self._consecutive_failures = 0
            return result

    @property
    def is_open(self) -> bool:
        """
        Whether this circuit is currently open (failing fast), accounting
        for cooldown elapsing - the same check ``call()`` makes, exposed
        read-only for status/health reporting. Does NOT mutate state (a
        real ``call()`` after cooldown elapses clears ``_opened_at`` to
        let one trial call through; this getter just answers "is it open
        right now" without side effects).
        """
        if self._opened_at is None:
            return False
        return self.clock() - self._opened_at < self.cooldown_seconds

    @property
    def consecutive_failures(self) -> int:
        return self._consecutive_failures
