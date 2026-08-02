# Daily unattended paper trading (Groww live data)

Runs `simple_breakout_momentum` automatically each trading day against
Groww's real live market data, with simulated (paper) fills - no real
capital at risk, no manual steps once set up.

## One-time setup

1. Get a Groww API key + secret from your Groww account's API settings.
2. Copy the credentials template and fill it in:
   ```
   cp .env.live_trader.example .env.live_trader
   ```
   Edit `.env.live_trader`: set `GROWW_API_KEY`, `GROWW_API_SECRET`, and
   (strongly recommended) `ALERT_WEBHOOK_URL` - this is unattended, so a
   webhook is the only way you'll find out something went wrong without
   checking logs by hand. `.env.live_trader` is gitignored; never commit it.
3. Confirm both wrapper scripts are executable:
   ```
   chmod +x scripts/run_live_paper_session.sh scripts/run_overnight_selection.sh
   ```
4. Test each manually once, outside cron, before scheduling either:
   ```
   ./scripts/run_overnight_selection.sh
   ```
   Check `data/cron_logs/overnight_selection_<today>.log` for a
   `=== OVERNIGHT CANDIDATE SELECTION ===` summary and a
   `Saved to data/candidate_selection/<next-trading-date>.json` line -
   this can take a while (see the cron step below for why).
   ```
   ./scripts/run_live_paper_session.sh
   ```
   Let it run a few minutes (during market hours), then Ctrl-C, and check
   `data/cron_logs/live_paper_<today>.log` for a clean startup banner
   (`Broker: paper/paper`, `Market data: groww/live`, `Reconciliation: clean`).
5. Add **two** cron entries - `crontab -e`, then add these lines (this
   machine's local timezone is already IST, so no timezone conversion
   needed):
   ```
   0 20 * * 0-4 /Users/neel/Claude/Project/autonomous-fund-platform/scripts/run_overnight_selection.sh
   0 9 * * 1-5 /Users/neel/Claude/Project/autonomous-fund-platform/scripts/run_live_paper_session.sh
   ```
   The first runs candidate selection (Angel One prescreen + historical
   fetch + bucket selection - can take an hour+ against the full NSE
   universe, see `core/market_data/candidate_prescreen.py`) at 8 PM
   Sun-Thu, for the *next* trading day - so it's done well before market
   open, not eating into the trading day. The second is trading itself,
   at 9 AM Mon-Fri: it reads that overnight result automatically if
   present for today and skips straight to it (see
   `core/market_data/candidate_selection_store.py`); if the overnight
   job didn't run (first setup, or it failed), it just falls back to
   selecting live - slower, but never blocks trading from starting.

   Both scripts share `.env.live_trader` and `LIVE_MARKET_DATA_PROVIDER`
   - keep them consistent, or the overnight job's selection won't be for
   the provider the morning job actually trades against.

   **macOS gotcha:** cron often needs Full Disk Access to reliably read
   files outside its own sandbox. If scheduled runs silently don't
   produce a log file, go to System Settings -> Privacy & Security ->
   Full Disk Access and add `/usr/sbin/cron` (or grant it to your
   terminal app if cron itself isn't listed).

   **The bigger macOS gotcha (confirmed the cause of missed runs
   2026-07-27 through 2026-07-31): cron cannot wake a sleeping Mac.** If
   the lid is closed or the machine is just idle at the scheduled minute,
   the firing is silently skipped - no error, no log file, nothing
   in `mail` you'd actually notice; on those dates `pmset -g log` showed
   the Mac asleep straight through 09:00 or 20:00, waking minutes later
   for an unrelated background maintenance blip. Both cron scripts now
   wrap their main work in `caffeinate -i` so a run already in progress
   can't be interrupted by sleep, but that doesn't help if the Mac was
   already asleep *at* 08:58 when cron tried to start it in the first
   place. Fix that with a scheduled wake a couple minutes early:
   ```
   sudo pmset repeat wake MTWRFSU 08:58:00
   ```
   `pmset repeat` only holds one daily wake time, so this covers the
   09:00 trading job, not the 20:00 overnight-selection one - that's the
   right tradeoff, not a compromise: overnight selection missing is
   already designed to be harmless (the daily-operation section above -
   "if the overnight job didn't run... it just falls back to selecting
   live"), while missing the trading job entirely skips a day. If you
   want the 20:00 job reliable too, just leave the lid open some evenings
   or add a second, separately-scripted `pmset schedule wake` (a one-shot
   command that needs re-issuing daily, unlike `repeat`) - not set up
   here. `sudo pmset -g sched` shows what's currently scheduled;
   `sudo pmset repeat cancel` clears it.

## Daily operation (once set up, this is automatic)

Each evening (Sun-Thu), cron fires `run_overnight_selection.sh`, which:
1. Loads credentials from `.env.live_trader`.
2. Sets `config/mandates/sample_mandate.yaml`'s `trading_date` to the
   *next* trading day (skips weekends).
3. Runs `apps.live_trader.select_candidates_main`, logging to
   `data/cron_logs/overnight_selection_<date>.log`, and saves the result
   to `data/candidate_selection/<next-trading-date>.json`.
4. Fires an alert on failure - a missed/failed overnight run just means
   tomorrow's trading job selects live instead (slower, not broken).

Each trading morning, cron fires `run_live_paper_session.sh`, which:
1. Loads credentials from `.env.live_trader`.
2. Sets `config/mandates/sample_mandate.yaml`'s `trading_date` to today.
3. Runs `apps.live_trader.main` for ~4500 poll cycles (a full trading day),
   logging to `data/cron_logs/live_paper_<date>.log` - using last night's
   precomputed candidate selection if one was saved for today, or
   selecting live if not.
4. Fires an alert (console + `data/alerts.log` + webhook if configured) on
   any failure, and on a session that completed but skipped bars due to
   errors.

## Checking on it

- **Dashboard** (optional, for a visual view): `python -m
  apps.reporting.dashboard --serve 8765`, open `http://127.0.0.1:8765`.
- **Log file**: `tail -f data/cron_logs/live_paper_$(date +%F).log`
- **Alerts**: `cat data/alerts.log`
- **Full session record**: `data/live_sessions/<session_id>/session.json`
  and `events.jsonl`.

## Reaching the dashboard from your phone (or anywhere else)

By default the dashboard binds to `127.0.0.1` with no login - fine for
the same machine, or the SSH-tunnel setup in step 7 below from your own
laptop. Neither of those works from a phone away from home, and the
dashboard has POST endpoints (`/add-symbol`, `/update-mandate`) that
change live capital/risk settings, so a public URL needs a real login in
front of it first - this isn't optional.

1. **Set a token.** Add a long random secret to `.env.live_trader`:
   ```
   DASHBOARD_AUTH_TOKEN=$(python3 -c "import secrets; print(secrets.token_urlsafe(24))")
   ```
   Every request now needs this as an HTTP Basic auth password (any
   username - it's one shared secret, not per-user accounts). Your
   phone's browser will just show the normal native login prompt.

2. **Put a public HTTPS tunnel in front of it** - simplest is
   [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/do-more-with-tunnels/trycloudflare/),
   which needs no account, no domain, and no inbound firewall port (it's
   an outbound connection from wherever the dashboard runs, so this works
   identically whether that's your laptop or the EC2 box from step 7):
   ```
   brew install cloudflared   # or the equivalent for your OS
   cloudflared tunnel --url http://127.0.0.1:8765
   ```
   This prints a `https://<random-name>.trycloudflare.com` URL - open
   that from your phone, anywhere, and you'll get the login prompt from
   step 1. The dashboard itself still only needs to bind to
   `127.0.0.1:8765` as usual (`DASHBOARD_HOST` doesn't need changing) -
   `cloudflared` is what's actually reachable from outside, and it's
   talking to the dashboard over loopback on the same machine.

   A free-tier quick tunnel like this gets a new random URL each time you
   start it. If you want a stable, memorable address instead, that needs
   a (still free) Cloudflare account and a named tunnel - more setup, not
   covered here.

3. **Skip the tunnel and expose the port directly instead?** You can
   (`DASHBOARD_HOST=0.0.0.0`, then open the port in your firewall/router)
   but you're then relying on the token alone against the entire
   internet scanning that port, with no rate-limiting or IP allowlisting
   - the tunnel path above is the one actually recommended.

## If something goes wrong mid-day

Find the process (`ps aux | grep apps.live_trader.main`) and `kill` it -
paper positions live only in that process's memory, so there's no
external account state to flatten (the kill switch is for the real Groww
broker case, not paper mode). The next scheduled run will refuse to start
until you either investigate or set `LIVE_ALLOW_UNCLEAN_PRIOR_SESSION=true`
in `.env.live_trader` - that's the crash-detection gate working as intended.

## Running on a cloud server instead of your laptop

Cron on your laptop only fires while the laptop is on and awake - if it's
closed, asleep, or off, that trading day just doesn't run. Moving this to
a small always-on VPS makes it independent of your laptop entirely.

1. **Provision a server.** Sign up with a provider (DigitalOcean, Hetzner,
   AWS Lightsail, Google Cloud - any of these work; this repo doesn't
   care) and create the cheapest Ubuntu 24.04 LTS instance (~$5-8/month,
   1GB RAM is plenty for this), in an India region if the provider offers
   one (lower latency to NSE/BSE, and some brokers expect an Indian
   source IP for whitelisting - GCP's is `asia-south1` (Mumbai)). Upload
   your SSH public key during creation rather than using a password.

   **If a broker requires IP whitelisting** (e.g. Groww's/Kite's API
   dashboard), don't just use the instance's default IP - it changes if
   you ever resize or recreate the instance, silently breaking
   whitelisting. Attach a *reserved/static* IP instead, which stays fixed
   independent of the underlying instance:
   - DigitalOcean: Networking -> Reserved IPs -> Assign to Droplet.
   - AWS: allocate an Elastic IP, associate it with the instance.
   - Hetzner: Networking -> Floating IPs -> assign to server.
   - Google Cloud: VPC network -> IP addresses -> Reserve External
     Static Address (Type: Regional, same region as the VM) -> attach it
     to the running instance. Console path:
     `console.cloud.google.com/networking/addresses`.

   This reserved IP is free while attached to a running instance on all
   of these - it starts accruing a small hourly charge only if you detach
   it and leave it unattached, so don't allocate one before the server
   exists. Register *this* IP (not any other address the instance shows)
   with the broker's whitelist, and use it for every step below.

2. **Prep the server** (SSH in first: `ssh root@<server-ip>`):
   ```
   apt update && apt upgrade -y
   apt install -y python3 python3-venv rsync cron ufw
   timedatectl set-timezone Asia/Kolkata   # critical - see note below
   ufw allow OpenSSH && ufw enable         # basic firewall, SSH only
   ```
   **Timezone matters a lot here**: a fresh cloud server defaults to UTC,
   not IST. The cron line below (`14 9 * * 1-5`) assumes local time is
   already IST - on a UTC server, uncorrected, it would fire at 9:14 AM
   UTC = 2:44 PM IST, nowhere near market open. Run `date` after
   `timedatectl set-timezone` and confirm it shows IST before continuing.

3. **Deploy the code** - from your laptop, not the server:
   ```
   scripts/deploy_to_server.sh root@<server-ip> ~/autonomous-fund-platform
   ```
   This rsyncs everything except `.venv`, local session data, and
   `.env.live_trader` (credentials are never synced automatically - you
   create them directly on the server so they don't sit in a laptop-side
   sync history).

4. **Set up the server-side environment** (SSH in):
   ```
   ssh root@<server-ip>
   cd ~/autonomous-fund-platform
   python3 -m venv .venv
   .venv/bin/pip install --upgrade pip
   .venv/bin/pip install -r requirements.txt
   cp .env.live_trader.example .env.live_trader
   nano .env.live_trader   # fill in real credentials, save
   chmod 600 .env.live_trader   # only the owning user can read it
   chmod +x scripts/run_live_paper_session.sh scripts/set_mandate_trading_date.py
   ```

5. **Test manually once**, same as the laptop setup: `./scripts/run_live_paper_session.sh`,
   let it run a few minutes during market hours, Ctrl-C, check the log.

6. **Add the same cron entry**, just on the server this time (`crontab -e`):
   ```
   14 9 * * 1-5 /root/autonomous-fund-platform/scripts/run_live_paper_session.sh
   ```
   No Full Disk Access gotcha here - that's a macOS-only cron quirk;
   plain Linux cron just works.

7. **Monitor remotely.** The dashboard binds to `127.0.0.1` on the server
   on purpose (it has an unauthenticated "add stock" endpoint - exposing
   it to the public internet would be a real hole). To view it from your
   laptop, tunnel instead of opening the port:
   ```
   ssh -L 8765:localhost:8765 root@<server-ip>
   # then, on the server side in that same SSH session:
   cd ~/autonomous-fund-platform && .venv/bin/python -m apps.reporting.dashboard --serve 8765
   ```
   Now `http://localhost:8765` on your laptop reaches the server's
   dashboard through the tunnel. Otherwise, `ALERT_WEBHOOK_URL` is the
   real answer for "how do I know it's still working" day to day - you
   shouldn't need to SSH in unless something's actually wrong.

Re-run `scripts/deploy_to_server.sh` any time you change code locally and
want the server to pick it up - it won't touch `.env.live_trader` or
server-side data.

## When you're ready to size this up

This is still 100% paper trading - Groww's real quotes, simulated fills,
zero capital risk. Moving to real capital is a separate, deliberate step
(`LIVE_BROKER_PROVIDER=groww`, `LIVE_CONFIRM=...`) that should only happen
after weeks of this running cleanly and the P&L actually looking like
something worth risking money on - see the readiness assessment from
earlier in this project's history for the full list of what else that
needs.
