#!/usr/bin/env bash
# Runs candidate selection (Angel One prescreen + historical fetch +
# bucket selection - see core/market_data/candidate_prescreen.py, can
# take an hour+ against the full NSE universe) for the NEXT trading day
# and persists the result, so scripts/run_live_paper_session.sh can skip
# straight to trading at market open instead of repeating all of it
# starting at 9 AM - see core/market_data/candidate_selection_store.py.
#
# Meant to be fired by cron once, well before the next market open (e.g.
# any time after today's close). Uses the same .env.live_trader as
# run_live_paper_session.sh - no separate setup needed, see that
# script's own header comment.
#
#   chmod +x scripts/run_overnight_selection.sh
#   (see docs/runbooks/daily_paper_trading.md for the crontab entry)
set -euo pipefail

export PYTHONUNBUFFERED=1  # see run_live_paper_session.sh's identical line for why

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

ENV_FILE="$REPO_DIR/.env.live_trader"
if [ ! -f "$ENV_FILE" ]; then
  echo "Missing $ENV_FILE - copy .env.live_trader.example and fill in real credentials." >&2
  exit 1
fi
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

# Selection is FOR the next trading day, not "today" (when this likely
# runs, e.g. tonight after close) - skips Sat/Sun, but not NSE holidays
# (no holiday calendar exists in this platform yet; a holiday run just
# saves a selection for a day nothing trades on, harmless but wasted).
NEXT_TRADING_DATE=$("$REPO_DIR/.venv/bin/python" -c "
from datetime import date, timedelta
d = date.today() + timedelta(days=1)
while d.weekday() >= 5:
    d += timedelta(days=1)
print(d.isoformat())
")
"$REPO_DIR/.venv/bin/python" scripts/set_mandate_trading_date.py "$NEXT_TRADING_DATE"

LOG_DIR="$REPO_DIR/data/cron_logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/overnight_selection_$(date +%F).log"

export LIVE_BROKER_PROVIDER=paper
export LIVE_MARKET_DATA_PROVIDER="${LIVE_MARKET_DATA_PROVIDER:-angelone}"

echo "=== starting overnight selection for $NEXT_TRADING_DATE at $(date) ===" >> "$LOG_FILE"
set +e  # a failed selection must still fall through to the trailer log line below
# caffeinate -i: see run_live_paper_session.sh's identical line - this can
# take an hour+ against the full NSE universe, and cron firing it doesn't
# by itself stop the Mac sleeping again mid-run.
caffeinate -i "$REPO_DIR/.venv/bin/python" -m apps.live_trader.select_candidates_main >> "$LOG_FILE" 2>&1
exit_code=$?
set -e
echo "=== selection ended at $(date), exit code $exit_code ===" >> "$LOG_FILE"
exit "$exit_code"
