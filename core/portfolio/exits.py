from __future__ import annotations

from typing import Any

from core.portfolio.exit_models import ExitDecision, ExitPolicyConfig, PositionSnapshot


class ExitPolicyEvaluator:
    """
    Deterministic V1 exit-rule engine.

    Assumes long-only positions (``quantity > 0``), matching the platform's
    current long-only strategies and backtester. Rules are checked in a
    fixed priority order - stop loss, target, square-off time, max holding
    bars - and the first rule that triggers wins, so a single call always
    produces the same decision for the same inputs.
    """

    def evaluate(self, snapshot: PositionSnapshot, policy: ExitPolicyConfig) -> ExitDecision:
        """Return the exit decision for ``snapshot`` under ``policy``."""
        if policy.stop_loss_pct is not None:
            stop_price = snapshot.entry_price * (1 - policy.stop_loss_pct / 100)
            if snapshot.current_price <= stop_price:
                return self._exit(snapshot, "stop_loss", {"stop_price": stop_price})

        if policy.target_pct is not None:
            target_price = snapshot.entry_price * (1 + policy.target_pct / 100)
            if snapshot.current_price >= target_price:
                return self._exit(snapshot, "target", {"target_price": target_price})

        if policy.square_off_time is not None:
            if snapshot.current_time.time() >= policy.square_off_time:
                return self._exit(
                    snapshot, "square_off", {"square_off_time": str(policy.square_off_time)}
                )

        if policy.max_holding_bars is not None:
            if snapshot.bars_held >= policy.max_holding_bars:
                return self._exit(
                    snapshot, "max_holding_bars", {"bars_held": snapshot.bars_held}
                )

        return ExitDecision(
            should_exit=False,
            symbol=snapshot.symbol,
            exchange=snapshot.exchange,
            quantity=snapshot.quantity,
        )

    @staticmethod
    def _exit(snapshot: PositionSnapshot, reason: str, metadata: dict[str, Any]) -> ExitDecision:
        return ExitDecision(
            should_exit=True,
            symbol=snapshot.symbol,
            exchange=snapshot.exchange,
            quantity=snapshot.quantity,
            reason=reason,
            exit_price=snapshot.current_price,
            metadata=metadata,
        )
