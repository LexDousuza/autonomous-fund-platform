#!/usr/bin/env bash
# Runs one full day of paper trading against Groww's real live market
# data, unattended - meant to be fired by cron each trading morning.
#
# Setup (once):
#   1. cp .env.live_trader.example .env.live_trader
#   2. Fill in real GROWW_API_KEY/GROWW_API_SECRET (and, recommended,
#      ALERT_WEBHOOK_URL) in .env.live_trader - it's gitignored, never
#      committed.
#   3. chmod +x scripts/run_live_paper_session.sh
#   4. Add a crontab entry (see docs/runbooks/daily_paper_trading.md).
set -euo pipefail

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

"$REPO_DIR/.venv/bin/python" scripts/set_mandate_trading_date.py

LOG_DIR="$REPO_DIR/data/cron_logs"
mkdir -p "$LOG_DIR"
LOG_FILE="$LOG_DIR/live_paper_$(date +%F).log"

# --- start the dashboard web server and Obsidian sync if not already
#     running (nohup+disown so they survive after this script exits, and
#     keep running even after today's session finishes - useful for
#     reviewing results later). Idempotent: cron firing this script again
#     tomorrow won't spawn duplicates, since these are checked, not
#     restarted, each time. Set OBSIDIAN_VAULT_PATH in .env.live_trader
#     to enable the Obsidian sync; leave it unset to skip it.
if ! pgrep -f "apps.reporting.dashboard --serve" > /dev/null 2>&1; then
  nohup "$REPO_DIR/.venv/bin/python" -m apps.reporting.dashboard --serve "${DASHBOARD_PORT:-8765}" \
    >> "$LOG_DIR/dashboard_server.log" 2>&1 &
  disown
  echo "$(date): started dashboard web server on port ${DASHBOARD_PORT:-8765} (pid $!)" >> "$LOG_FILE"
fi

if [ -n "${OBSIDIAN_VAULT_PATH:-}" ]; then
  if ! pgrep -f "apps.reporting.dashboard --obsidian" > /dev/null 2>&1; then
    nohup "$REPO_DIR/.venv/bin/python" -m apps.reporting.dashboard --obsidian "$OBSIDIAN_VAULT_PATH" 15 \
      >> "$LOG_DIR/obsidian_sync.log" 2>&1 &
    disown
    echo "$(date): started Obsidian sync into $OBSIDIAN_VAULT_PATH (pid $!)" >> "$LOG_FILE"
  fi
fi

export LIVE_BROKER_PROVIDER=paper
# Defaults to groww; override (e.g. `LIVE_MARKET_DATA_PROVIDER=kite
# ./scripts/run_live_paper_session.sh`) to run against Kite's live quotes
# instead - both are paper-fills-only, zero capital at risk either way.
export LIVE_MARKET_DATA_PROVIDER="${LIVE_MARKET_DATA_PROVIDER:-groww}"
export LIVE_MAX_BARS="${LIVE_MAX_BARS:-4500}"
export LIVE_CONFIRM=I_UNDERSTAND_THIS_CAN_PLACE_REAL_ORDERS

echo "=== starting live paper session at $(date) ===" >> "$LOG_FILE"
set +e  # a failed session must still fall through to the trailer log line below
"$REPO_DIR/.venv/bin/python" -m apps.live_trader.main >> "$LOG_FILE" 2>&1
exit_code=$?
set -e
echo "=== session ended at $(date), exit code $exit_code ===" >> "$LOG_FILE"
exit "$exit_code"
