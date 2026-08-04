"""
Corporate-action classification and price adjustment - closes the gap
docs/research/data_quality_audit_phase1.md flagged and
docs/research/data/universe_expansion_2026-07-30.md then hit for real
(SKFINDIA's unadjusted demerger silently producing a fake ~55% "return").

NSE's own corporate-actions feed (fetched by
scripts/fetch_corporate_actions_nse.py) isn't a clean structured ratio
table - its free-text ``subject`` field is prose written for a human
reading a filing, e.g. "Bonus 1:2", "Face Value Split (Sub-Division) -
From Rs 2 Per Share To Rs 1 Per Share", "Rights 3:25 @ Premium Rs 1799/-",
"Demerger", "Buy Back", "Dividend - Rs 40 Per Share". This module
classifies that text and computes a numeric backward-adjustment factor
ONLY for the two action types where the ratio is unambiguous from the
text alone (BONUS, SPLIT) - see ActionType's own docstring for why the
others are deliberately left unadjusted rather than guessed. A wrong
adjustment factor is worse than no adjustment: it looks correct and
silently corrupts every feature/return computed across that date, where
no adjustment at least leaves an honest, visible discontinuity a >30%
jump heuristic (or a sharper eye) can still catch.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import date
from enum import Enum

from core.strategy.base import MarketBar


class ActionType(Enum):
    """
    BONUS/SPLIT: ratio is unambiguous from NSE's own text - adjustment_factor
    gets computed automatically.

    RIGHTS: has a ratio (e.g. "3:25") but the correct price adjustment is
    the theoretical ex-rights price formula, which also needs the
    subscription premium AND the cum-rights market price on the record
    date (not available from this feed alone) - recording the event, not
    computing a factor, would be worse than admitting it can't be done
    from this data source.

    DEMERGER/MERGER: no ratio in the text at all (NSE files these as free
    text describing a scheme, not a numeric split) - the real adjustment
    requires the scheme document's specific share-entitlement ratio.

    BUYBACK/DIVIDEND/AGM: don't need backward price adjustment for
    return-continuity purposes the way BONUS/SPLIT/RIGHTS do - a dividend
    causes an expected, real ex-date price drop (part of realistic total
    return, not a data artifact); a buyback changes share count, not the
    per-share price series' continuity.

    OTHER: text didn't match any known pattern - recorded for visibility,
    never silently dropped.
    """

    BONUS = "BONUS"
    SPLIT = "SPLIT"
    RIGHTS = "RIGHTS"
    DEMERGER = "DEMERGER"
    MERGER = "MERGER"
    BUYBACK = "BUYBACK"
    DIVIDEND = "DIVIDEND"
    AGM = "AGM"
    INTEREST_PAYMENT = "INTEREST_PAYMENT"  # debenture/bond interest - not an equity action at all
    OTHER = "OTHER"


# Action types whose price effect is a real, honest part of the return
# series (not a data artifact) - never a reason to exclude a symbol or
# adjust its prices.
NO_ADJUSTMENT_NEEDED = {ActionType.BUYBACK, ActionType.DIVIDEND, ActionType.AGM, ActionType.INTEREST_PAYMENT}

# Action types with a real price discontinuity but no adjustment factor
# computed here - a symbol with one of these in the analysis window
# should be excluded, not silently left unadjusted.
NEEDS_ADJUSTMENT_NOT_COMPUTED = {ActionType.RIGHTS, ActionType.DEMERGER, ActionType.MERGER, ActionType.OTHER}


@dataclass(frozen=True, slots=True)
class CorporateAction:
    symbol: str
    ex_date: date
    subject_raw: str
    action_type: ActionType
    # shares_after / shares_before - multiply a pre-event price by this to
    # make it comparable to post-event prices. None unless action_type is
    # BONUS or SPLIT.
    adjustment_factor: float | None


_BONUS_RE = re.compile(r"bonus\s+(\d+)\s*:\s*(\d+)", re.IGNORECASE)
# NSE writes "Rs" for whole rupees but "Re" (singular) when the value is
# exactly 1 (e.g. "From Rs 10/- Per Share To Re 1/- Per Share") - found the
# hard way: an "rs"-only pattern silently missed 19 of 20 real split
# records in the first live fetch (2026-07-30), all landing in the "split
# language matched but no factor" fallback below rather than adjusting
# anything wrong - safe, but worth fixing rather than leaving that many
# real splits unadjusted. "Per Share" and "/-" are both optional (NSE
# drops them in some subjects, e.g. "Face Value Split From Rs 10 To Re 2").
_FACE_VALUE_SPLIT_RE = re.compile(
    r"from\s+r[se]\.?\s*([\d.]+)\s*/?-?\s*(?:per\s+share)?\s+to\s+r[se]\.?\s*([\d.]+)", re.IGNORECASE
)
_RIGHTS_RE = re.compile(r"rights\s+(\d+)\s*:\s*(\d+)", re.IGNORECASE)


def classify(subject: str) -> tuple[ActionType, float | None]:
    """
    Classify one NSE corporate-action ``subject`` string and compute its
    ``adjustment_factor`` if - and only if - the action type is BONUS or
    SPLIT (see ActionType's docstring). Checked in this specific order:
    bonus/split patterns before the generic keyword checks below them, so
    e.g. a hypothetical "Bonus cum Rights" subject classifies as BONUS
    (the part this function CAN compute a factor for) rather than falling
    through to RIGHTS.
    """
    text = subject.strip()
    lower = text.lower()

    bonus_match = _BONUS_RE.search(text)
    if bonus_match:
        new_shares, held_shares = int(bonus_match.group(1)), int(bonus_match.group(2))
        if new_shares > 0 and held_shares > 0:
            factor = held_shares / (held_shares + new_shares)
            return ActionType.BONUS, factor

    split_match = _FACE_VALUE_SPLIT_RE.search(text)
    if split_match:
        old_face, new_face = float(split_match.group(1)), float(split_match.group(2))
        if old_face > 0 and new_face > 0:
            factor = new_face / old_face
            return ActionType.SPLIT, factor

    if "split" in lower or "sub-division" in lower or "sub division" in lower:
        return ActionType.SPLIT, None  # split-like language but ratio didn't match the known pattern

    if _RIGHTS_RE.search(text) or "rights" in lower:
        return ActionType.RIGHTS, None

    if "demerger" in lower:
        return ActionType.DEMERGER, None

    if "merger" in lower or "amalgamation" in lower or "scheme of arrangement" in lower:
        return ActionType.MERGER, None

    if "buyback" in lower or "buy back" in lower or "buy-back" in lower:
        return ActionType.BUYBACK, None

    if "interest payment" in lower:
        return ActionType.INTEREST_PAYMENT, None  # debenture/bond interest - not an equity action

    if "dividend" in lower or "divid" in lower or re.search(r"\bint\s*div\b", lower):
        # "divid" (not "dividend") also catches NSE's own typos in this
        # feed (e.g. "Interim Dividned") - still unambiguous, dividend is
        # the only real corporate-action word with that stem.
        return ActionType.DIVIDEND, None

    if "annual general meeting" in lower or lower.startswith("agm"):
        return ActionType.AGM, None

    return ActionType.OTHER, None


def parse_action(symbol: str, ex_date: date, subject: str) -> CorporateAction:
    action_type, factor = classify(subject)
    return CorporateAction(symbol=symbol, ex_date=ex_date, subject_raw=subject, action_type=action_type, adjustment_factor=factor)


def load_corporate_actions(csv_path: str) -> dict[str, list[CorporateAction]]:
    """Load scripts/fetch_corporate_actions_nse.py's output CSV into one parsed CorporateAction list per symbol."""
    import csv as csv_module

    by_symbol: dict[str, list[CorporateAction]] = {}
    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        for row in csv_module.DictReader(f):
            action = parse_action(row["symbol"], date.fromisoformat(row["ex_date"]), row["subject"])
            by_symbol.setdefault(action.symbol, []).append(action)
    return by_symbol


def adjust_bars(bars: list[MarketBar], actions: list[CorporateAction]) -> tuple[list[MarketBar], bool]:
    """
    Backward-adjusts ``bars`` (must be one symbol, chronologically
    sorted) for every BONUS/SPLIT action in ``actions`` whose ex_date
    falls within the bars' date range: every bar strictly before
    ex_date gets open/high/low/close multiplied by that action's
    adjustment_factor (cumulative if multiple qualifying actions apply -
    applied earliest-first so each is composed onto the ones before it).
    Volume is deliberately left unadjusted (existing convention in this
    codebase - see e.g. HistoricalOptionChainProvider's docstring on
    scope, and no other adjustment path in this platform touches volume
    either).

    Returns ``(adjusted_bars, has_unadjustable_action)`` - the second
    value is True if ANY action in ``actions`` within the bars' date
    range has action_type in NEEDS_ADJUSTMENT_NOT_COMPUTED (Rights/
    Demerger/Merger/Other) - the caller should exclude this symbol
    entirely in that case, since a real discontinuity exists that this
    function did not (and structurally cannot, from this data alone)
    correct for.
    """
    if not bars:
        return bars, False

    start, end = bars[0].timestamp.date(), bars[-1].timestamp.date()
    relevant = [a for a in actions if start <= a.ex_date <= end]

    has_unadjustable = any(a.action_type in NEEDS_ADJUSTMENT_NOT_COMPUTED for a in relevant)

    adjustable = sorted(
        (a for a in relevant if a.adjustment_factor is not None),
        key=lambda a: a.ex_date,
    )
    if not adjustable:
        return bars, has_unadjustable

    adjusted: list[MarketBar] = []
    for bar in bars:
        bar_date = bar.timestamp.date()
        cumulative_factor = 1.0
        for action in adjustable:
            if bar_date < action.ex_date:
                cumulative_factor *= action.adjustment_factor
        if cumulative_factor == 1.0:
            adjusted.append(bar)
        else:
            adjusted.append(
                replace(
                    bar,
                    open=bar.open * cumulative_factor,
                    high=bar.high * cumulative_factor,
                    low=bar.low * cumulative_factor,
                    close=bar.close * cumulative_factor,
                )
            )
    return adjusted, has_unadjustable
