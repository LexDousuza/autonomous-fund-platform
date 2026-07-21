# AWS integration

`docs/runbooks/daily_paper_trading.md`'s "Running on a cloud server"
section already covers provisioning any VPS (AWS included) and deploying
this repo to it - that generic setup doesn't need repeating here. This
doc only covers what's AWS-specific: picking EC2 for that generic
runbook, and two optional upgrades once you're there - Secrets Manager
instead of a plaintext `.env.live_trader`, and S3 for off-box audit-log
backups.

## 1. EC2 as the "always-on server"

Follow `docs/runbooks/daily_paper_trading.md`'s cloud-server steps
verbatim, with these AWS-specific notes:

- **Instance**: `t3.micro` (or `t3.small` if you also want the dashboard
  running comfortably) on Ubuntu 24.04 LTS, in `ap-south-1` (Mumbai) for
  lowest latency to NSE/BSE.
- **Elastic IP**: allocate one and associate it with the instance (EC2
  console -> Elastic IPs -> Allocate -> Associate). Needed if
  Groww/Kite's API dashboard ever requires IP whitelisting, and it keeps
  your address stable across instance stop/start.
- **Security group**: this is the part `ufw` alone doesn't cover on
  AWS - the security group attached to the instance is a separate,
  outer firewall. Restrict inbound SSH (port 22) to your own IP, not
  `0.0.0.0/0`. No other inbound ports are needed (the dashboard binds to
  `127.0.0.1` and is reached via SSH tunnel, per the existing runbook).
- **IAM role**: if you plan to use Secrets Manager and/or S3 below,
  create an instance IAM role now (EC2 console -> instance -> Actions ->
  Security -> Modify IAM role) rather than storing long-lived AWS access
  keys on the box - the policies in steps 2-3 attach to this role.

Everything else - `deploy_to_server.sh`, venv setup, cron entry, IST
timezone - is identical to the generic runbook.

## 2. Secrets Manager instead of `.env.live_trader` (optional)

`apps/live_trader/aws_secrets.py` is opt-in: if
`AWS_SECRETS_MANAGER_SECRET_ID` isn't set, nothing changes and
`.env.live_trader` works exactly as it always has. When set, one secret
is fetched at startup and merged into the process environment before any
credential loader runs.

1. Create the secret (from your laptop, with `aws` CLI configured, or
   the AWS Console):
   ```
   aws secretsmanager create-secret \
     --name aqtp/live-trader \
     --secret-string '{
       "GROWW_API_KEY": "...", "GROWW_API_SECRET": "...",
       "KITE_API_KEY": "...", "KITE_API_SECRET": "...", "KITE_ACCESS_TOKEN": "...",
       "ALERT_WEBHOOK_URL": "..."
     }'
   ```
2. Attach a policy to the EC2 instance role from step 1, scoped to just
   this secret (least privilege - not `secretsmanager:*` on `*`):
   ```json
   {
     "Version": "2012-10-17",
     "Statement": [{
       "Effect": "Allow",
       "Action": "secretsmanager:GetSecretValue",
       "Resource": "arn:aws:secretsmanager:ap-south-1:<account-id>:secret:aqtp/live-trader-*"
     }]
   }
   ```
3. On the server, set one env var (e.g. in `.env.live_trader`, or
   exported directly - it's not a secret itself, just a pointer):
   ```
   AWS_SECRETS_MANAGER_SECRET_ID=aqtp/live-trader
   ```
   `.venv/bin/pip install -r requirements.txt` (installs `boto3`).

**Kite's `KITE_ACCESS_TOKEN` still expires daily** - Secrets Manager
doesn't change that. After running
`scripts/kite_generate_access_token.py` each morning (see
`docs/runbooks/kite_integration.md`), push the refreshed token back up:
```
aws secretsmanager put-secret-value --secret-id aqtp/live-trader \
  --secret-string "$(aws secretsmanager get-secret-value --secret-id aqtp/live-trader --query SecretString --output text | jq --arg t "$NEW_TOKEN" '.KITE_ACCESS_TOKEN = $t')"
```
(or just edit `.env.live_trader` directly on the server, as before -
Secrets Manager is an alternative credential source, not a requirement).

## 3. S3 for audit-log backup (optional)

```
aws s3 mb s3://your-bucket-name --region ap-south-1
```

Attach a second policy to the same instance role:
```json
{
  "Version": "2012-10-17",
  "Statement": [{
    "Effect": "Allow",
    "Action": ["s3:PutObject", "s3:ListBucket"],
    "Resource": [
      "arn:aws:s3:::your-bucket-name",
      "arn:aws:s3:::your-bucket-name/*"
    ]
  }]
}
```

Then, after each session (or as its own cron line):
```
scripts/sync_audit_logs_to_s3.sh s3://your-bucket-name/aqtp-backups
```

This backs up `data/live_sessions`, `data/audit`, and `data/cron_logs` -
the durable session/audit records - so they survive even if the instance
is lost or rebuilt.
