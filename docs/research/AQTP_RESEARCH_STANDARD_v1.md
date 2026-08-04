# AQTP Research Standard v1.0

The acceptance bar for any strategy to enter Tier B (live/paper capital).
Written once, referenced by every future validation decision rather than
re-derived per-strategy. Superseded only by a v2 with an explicit reason for
the change.

## A strategy may enter Tier B only if it:

1. **Has a documented economic rationale** — not "the backtest says so."
   A mechanism (who's on the other side of this trade, and why they'd
   rationally keep doing it) recorded in the strategy's spec doc or its
   research-registry entry.
2. **Passes statistical significance testing** — a bootstrap confidence
   interval and a permutation-test p-value against a zero-edge null, not a
   single point-estimate profit factor.
3. **Survives out-of-sample validation** — scored on a holdout period (and,
   where enough history exists, multiple rolling walk-forward windows) it
   was never tuned against.
4. **Is robust to reasonable parameter changes** — a smooth performance
   curve across neighboring parameter values, not a spike surrounded by
   losses.
5. **Demonstrates cross-sectional or temporal consistency, as applicable** —
   present across more than one instrument where the universe allows it, or
   across more than one market regime (at minimum, high vs. low IV) where a
   single-instrument signal is regime-dependent.
6. **Satisfies predefined risk and liquidity constraints** — a kill
   criterion it must not have crossed, agreed *before* the strategy's
   results were computed, plus a basic check that intended position size
   fits inside real instrument liquidity.
7. **Is fully reproducible** — git commit hash, config version, data
   snapshot, and random seed recorded, such that rerunning the same
   experiment months later reproduces the same result unless the data
   changed intentionally.

Every strategy's status against these seven is recorded as an explicit
pass/fail per dimension in its research-registry entry
(`docs/research/experiments/`) — never averaged into a single score, since
that hides which specific dimension failed.

---

## Phase 1 kill criteria — PROPOSED, pending reviewer confirmation

These apply to the current re-audit of existing results (OU, credit-spread,
VRP) and are being sent for sign-off *before* Phase 1's bootstrap/permutation
numbers are computed. Confirming or adjusting them after seeing results
would defeat the purpose of pre-registering them at all.

- Reject if the bootstrap CI's lower bound on profit factor is ≤ 1.
- Reject if the Benjamini-Hochberg-corrected p-value is > 0.05.
- Reject if holdout-period profit factor drops more than 25% relative to
  the dev/in-sample profit factor.
- Reject if walk-forward windows disagree in sign (some profitable, some
  not, with no regime explanation).

Open question for the reviewer: do these thresholds look right, or would
you set them differently (e.g. White's Reality Check instead of
Benjamini-Hochberg, a tighter/looser holdout-drop tolerance)?
