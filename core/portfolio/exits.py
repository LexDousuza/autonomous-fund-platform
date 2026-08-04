from __future__ import annotations

from typing import Any

from core.portfolio.exit_models import ExitDecision, ExitPolicyConfig, PositionSnapshot


class ExitPolicyEvaluator:
    """
    Deterministic V1 exit-rule engine.

    Handles both long (``quantity > 0``) and short (``quantity < 0``,
    e.g. a naked-written option - see ``core.broker.base.PositionEffect``)
    positions: a short's stop/target are the mirror image of a long's
    (loss on a rising price, profit on a falling one), so both branches
    are checked explicitly rather than assuming a sign. Rules are
    checked in a fixed priority order - stop loss, target, square-off
    time, max holding bars - and the first rule that triggers wins, so a
    single call always produces the same decision for the same inputs.
    """

    def evaluate(self, snapshot: PositionSnapshot, policy: ExitPolicyConfig) -> ExitDecision:
        """Return the exit decision for ``snapshot`` under ``policy``."""
        is_short = snapshot.quantity < 0

        stop_distance = self._distance(
            policy.stop_loss_pct, policy.stop_loss_atr_multiplier, snapshot
        )
        if stop_distance is not None:
            if is_short:
                stop_price = snapshot.entry_price + stop_distance
                if snapshot.current_price >= stop_price:
                    return self._exit(snapshot, "stop_loss", {"stop_price": stop_price})
            else:
                stop_price = snapshot.entry_price - stop_distance
                if snapshot.current_price <= stop_price:
                    return self._exit(snapshot, "stop_loss", {"stop_price": stop_price})

        target_distance = self._distance(policy.target_pct, policy.target_atr_multiplier, snapshot)
        if target_distance is not None:
            if is_short:
                target_price = snapshot.entry_price - target_distance
                if snapshot.current_price <= target_price:
                    return self._exit(snapshot, "target", {"target_price": target_price})
            else:
                target_price = snapshot.entry_price + target_distance
                if snapshot.current_price >= target_price:
                    return self._exit(snapshot, "target", {"target_price": target_price})

        if policy.trailing_stop_pct is not None and snapshot.best_price_since_entry is not None:
            trail_distance = snapshot.best_price_since_entry * (policy.trailing_stop_pct / 100)
            if is_short:
                trailing_stop_price = snapshot.best_price_since_entry + trail_distance
                if snapshot.current_price >= trailing_stop_price:
                    return self._exit(
                        snapshot, "trailing_stop", {"trailing_stop_price": trailing_stop_price}
                    )
            else:
                trailing_stop_price = snapshot.best_price_since_entry - trail_distance
                if snapshot.current_price <= trailing_stop_price:
                    return self._exit(
                        snapshot, "trailing_stop", {"trailing_stop_price": trailing_stop_price}
                    )

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
    def _distance(
        pct: float | None, atr_multiplier: float | None, snapshot: PositionSnapshot
    ) -> float | None:
        """
        Absolute price distance for a stop/target rule: ATR-based when
        both ``atr_multiplier`` and ``snapshot.atr`` are available
        (preferred - see ``ExitPolicyConfig``'s docstring), else the flat
        ``pct`` of entry price, else ``None`` (rule not configured/skipped).
        """
        if atr_multiplier is not None and snapshot.atr is not None:
            return atr_multiplier * snapshot.atr
        if pct is not None:
            return snapshot.entry_price * (pct / 100)
        return None

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
