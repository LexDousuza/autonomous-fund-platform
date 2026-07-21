#!/usr/bin/env bash
# Backs up data/{live_sessions,audit,cron_logs} to S3 - run after a
# session finishes, or as its own cron entry, so session records/audit
# logs survive even if the box running them is lost or rebuilt.
#
# Setup (once):
#   aws s3 mb s3://your-bucket-name --region ap-south-1
#   Attach an IAM policy scoped to just that bucket (s3:PutObject,
#   s3:ListBucket) to the instance's role/profile (or ~/.aws/credentials)
#   - see docs/runbooks/aws_integration.md.
#
# Usage: scripts/sync_audit_logs_to_s3.sh s3://your-bucket-name/aqtp-backups
set -euo pipefail

if [ $# -lt 1 ]; then
  echo "Usage: $0 s3://bucket-name/prefix" >&2
  exit 1
fi

DEST="$1"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

for dir in live_sessions audit cron_logs; do
  if [ -d "$REPO_DIR/data/$dir" ]; then
    aws s3 sync "$REPO_DIR/data/$dir" "$DEST/$dir" --only-show-errors
  fi
done

echo "Synced data/{live_sessions,audit,cron_logs} to $DEST"
