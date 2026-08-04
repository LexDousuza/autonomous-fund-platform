# Corporate-actions table (2026-07-30)

Closes the gap flagged in `data_quality_audit_phase1.md` and hit for real
in `universe_expansion_2026-07-30.md` (SKFINDIA's unadjusted demerger
silently producing a fake ~55% "return").

## Source

NSE's own public `corporates-corporateActions` endpoint - free, no
credentials, gated by the same basic bot filtering (session cookie +
browser-like User-Agent/Referer) `fetch_historical_options_nse.py` already
handles for bhavcopy, not a paywall. Verified working via a direct
`urllib` session-cookie handshake before any code was written.

`scripts/fetch_corporate_actions_nse.py` fetched all 129
`liquid_universe.LIQUID_UNIVERSE_SYMBOLS`, 2016-01-01 to present:
**129/129 succeeded, 2,327 actions**, written to
`data/processed/nse_corporate_actions.csv`. Same overwrite-safety pattern
as the other fetch scripts (refuses to write on a total failure).

## Classification

`core/market_data/corporate_actions.py` classifies each action's free-text
`subject` field and computes a numeric price-adjustment factor ONLY for
the two types where NSE's text gives an unambiguous ratio:

- **BONUS** ("Bonus X:Y") - factor = Y / (X+Y)
- **SPLIT** ("Face Value Split... From Rs A... To Rs/Re B...") - factor = B/A

Everything else is classified but deliberately left unadjusted:
RIGHTS/DEMERGER/MERGER (real price discontinuity, but the correct
adjustment needs information NSE's text doesn't give - a wrong guess
would be worse than no adjustment) trigger excluding the symbol from any
analysis window containing them; BUYBACK/DIVIDEND/AGM/INTEREST_PAYMENT
need no price adjustment at all (real, expected return components, not
data artifacts).

Final classification of all 2,327 real actions: 1,918 DIVIDEND, 215 AGM,
65 BONUS, 63 BUYBACK, 20 SPLIT, 14 INTEREST_PAYMENT, 12 RIGHTS, 11
DEMERGER, 4 MERGER, 5 OTHER (unrecognized text - NSE's own typos in rare
cases, e.g. "Annnual General Meeting", "Fv Splt Frm Rs 10 To Re 1").

**Found and fixed during first real-data validation, not assumed correct
from the 5-symbol sample the classifier was designed against:** the first
version of the split regex required literal "Rs" before both numbers, but
NSE writes "Re" (singular) when the target face value is exactly 1 (e.g.
"...To Re 1/- Per Share") - this alone left 19 of 20 real SPLIT records
without a computed factor. Also added: "Interest Payment" (debenture/bond
interest, not an equity action - was falling into the
needs-adjustment-not-computed bucket and would have caused unnecessary
exclusions) and abbreviated "Int Div" / typo'd "Dividned" dividend text.

## Effect on the universe

Applying the real table to `nse_daily_12mo.csv`'s 127 fetched symbols: 6
price-adjusted (real BONUS/SPLIT events silently uncorrected before this
existed), 5 excluded (ADANIENT, HINDUNILVR, SKFINDIA, TVSMOTOR, VEDL - real
Rights/Demerger/Merger events, only 1 of which the earlier >30%-jump
heuristic had caught). 122 usable symbols. See
`validation_audit_phase_a.md` for how this changed Phase A's actual
results (`atr_pct`'s apparent near-significance in the intermediate state
turned out to be a contamination artifact, not a real effect).
