"""Unit tests for core.utils.resilience."""
from __future__ import annotations

import unittest

from core.utils.resilience import CircuitBreaker, CircuitOpenError, RetryPolicy, retry_with_backoff


class RetryWithBackoffTests(unittest.TestCase):
    def setUp(self) -> None:
        self.sleeps: list[float] = []
        self.policy = RetryPolicy(max_attempts=3, base_delay_seconds=1.0, max_delay_seconds=10.0)

    def _sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    def test_returns_result_on_first_success(self) -> None:
        result = retry_with_backoff(
            lambda: 42, policy=self.policy, is_retryable=lambda exc: True, sleep=self._sleep
        )
        self.assertEqual(result, 42)
        self.assertEqual(self.sleeps, [])

    def test_retries_retryable_failures_then_succeeds(self) -> None:
        attempts = {"count": 0}

        def flaky() -> str:
            attempts["count"] += 1
            if attempts["count"] < 3:
                raise ValueError("transient")
            return "ok"

        result = retry_with_backoff(
            flaky, policy=self.policy, is_retryable=lambda exc: True, sleep=self._sleep
        )
        self.assertEqual(result, "ok")
        self.assertEqual(attempts["count"], 3)
        self.assertEqual(self.sleeps, [1.0, 2.0])  # doubling backoff

    def test_exhausts_attempts_and_raises_last_error(self) -> None:
        def always_fails() -> None:
            raise ValueError("still broken")

        with self.assertRaises(ValueError):
            retry_with_backoff(
                always_fails, policy=self.policy, is_retryable=lambda exc: True, sleep=self._sleep
            )
        self.assertEqual(len(self.sleeps), 2)  # slept between attempts 1->2 and 2->3, not after the last

    def test_non_retryable_exception_propagates_immediately(self) -> None:
        attempts = {"count": 0}

        def fails_hard() -> None:
            attempts["count"] += 1
            raise KeyError("not retryable")

        with self.assertRaises(KeyError):
            retry_with_backoff(
                fails_hard, policy=self.policy, is_retryable=lambda exc: False, sleep=self._sleep
            )
        self.assertEqual(attempts["count"], 1)
        self.assertEqual(self.sleeps, [])


class CircuitBreakerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = 0.0
        self.breaker = CircuitBreaker(failure_threshold=3, cooldown_seconds=10.0, clock=lambda: self.now)

    def test_successful_calls_pass_through(self) -> None:
        self.assertEqual(self.breaker.call(lambda: "ok"), "ok")

    def test_opens_after_threshold_consecutive_failures(self) -> None:
        for _ in range(3):
            with self.assertRaises(ValueError):
                self.breaker.call(self._raise_value_error)

        with self.assertRaises(CircuitOpenError):
            self.breaker.call(lambda: "should not run")

    def test_success_resets_failure_count(self) -> None:
        for _ in range(2):  # below threshold
            with self.assertRaises(ValueError):
                self.breaker.call(self._raise_value_error)

        self.breaker.call(lambda: "ok")  # resets count

        for _ in range(2):  # below threshold again, since count was reset
            with self.assertRaises(ValueError):
                self.breaker.call(self._raise_value_error)

        self.breaker.call(lambda: "still closed")  # would raise CircuitOpenError if not reset

    def test_closes_again_after_cooldown_elapses(self) -> None:
        for _ in range(3):
            with self.assertRaises(ValueError):
                self.breaker.call(self._raise_value_error)

        self.now += 10.0  # cooldown elapsed
        self.assertEqual(self.breaker.call(lambda: "recovered"), "recovered")

        # circuit is closed again - a fresh failure streak is needed to re-open it
        with self.assertRaises(ValueError):
            self.breaker.call(self._raise_value_error)
        self.assertEqual(self.breaker.call(lambda: "still closed"), "still closed")

    def _raise_value_error(self) -> None:
        raise ValueError("boom")


class CircuitBreakerStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        self.now = 0.0
        self.breaker = CircuitBreaker(failure_threshold=2, cooldown_seconds=10.0, clock=lambda: self.now)

    def test_closed_and_zero_failures_initially(self) -> None:
        self.assertFalse(self.breaker.is_open)
        self.assertEqual(self.breaker.consecutive_failures, 0)

    def test_is_open_true_once_threshold_hit(self) -> None:
        for _ in range(2):
            with self.assertRaises(ValueError):
                self.breaker.call(self._raise)
        self.assertTrue(self.breaker.is_open)
        self.assertEqual(self.breaker.consecutive_failures, 2)

    def test_is_open_false_again_after_cooldown_without_calling(self) -> None:
        for _ in range(2):
            with self.assertRaises(ValueError):
                self.breaker.call(self._raise)
        self.now += 10.0
        # is_open reflects cooldown elapsing even without a new call() -
        # a pure read, no side effect on the breaker's own state.
        self.assertFalse(self.breaker.is_open)

    def _raise(self) -> None:
        raise ValueError("boom")


if __name__ == "__main__":
    unittest.main()
