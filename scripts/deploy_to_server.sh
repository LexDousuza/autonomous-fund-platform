#!/usr/bin/env bash
# Syncs this repo to a remote always-on server over SSH, excluding
# local-only artifacts and credentials. Re-run any time you change code
# and want the server to pick it up.
#
# Usage: scripts/deploy_to_server.sh user@server-ip [remote-path]
set -euo pipefail

if [ $# -lt 1 ]; then
  echo "Usage: $0 user@server-ip [remote-path]" >&2
  exit 1
fi

REMOTE="$1"
REMOTE_PATH="${2:-~/autonomous-fund-platform}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

rsync -avz --delete \
  --exclude '.venv' \
  --exclude '__pycache__' \
  --exclude '.pytest_cache' \
  --exclude '.mypy_cache' \
  --exclude 'data/live_sessions' \
  --exclude 'data/cron_logs' \
  --exclude 'data/alerts.log' \
  --exclude '.env.live_trader' \
  --exclude '.git' \
  "$REPO_DIR/" "$REMOTE:$REMOTE_PATH/"

echo
echo "Synced to $REMOTE:$REMOTE_PATH"
echo "Credentials were NOT synced (.env.live_trader is excluded on purpose)."
echo "On first deploy, SSH in and create it directly on the server:"
echo "  ssh $REMOTE"
echo "  cd $REMOTE_PATH && cp .env.live_trader.example .env.live_trader && nano .env.live_trader"
