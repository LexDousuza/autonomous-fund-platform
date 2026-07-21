# Kite Connect (Zerodha) integration

Adds Kite as a second broker/market-data provider alongside Groww - same
architecture, same safety gates (`core/broker/factory.py`,
`core/market_data/factory.py`, `apps/live_trader/config.py`). See
`docs/runbooks/daily_paper_trading.md` for the equivalent Groww runbook;
this doc only covers what's different about Kite.

## The one real difference from Groww: daily login

Groww's credentials (`GROWW_API_KEY`/`GROWW_API_SECRET`) are a static
pair exchanged for a token automatically on every run - no human
involved. **Kite Connect has no equivalent headless flow.** Zerodha
requires a real browser login (username, password, 2FA) once a day; the
resulting `access_token` expires at 6 AM IST the next day (a regulatory
requirement, not a Kite quirk). This means:

- A Kite-backed session run entirely unattended (cron, no one around)
  will work fine **once the day's `KITE_ACCESS_TOKEN` is already set** -
  but that token has to come from a human logging in earlier that
  morning. There's no way around this without automating a real password
  + OTP entry into Zerodha's login page, which this platform deliberately
  does not do.
- Practically: run `scripts/kite_generate_access_token.py` yourself each
  trading morning (takes under a minute), *then* start today's session.

## One-time setup

1. Go to [developers.kite.trade](https://developers.kite.trade), log in
   with your Zerodha credentials, and create a new app under "Connect
   apps" (redirect URL can be anything reachable or even
   `http://127.0.0.1` - this platform reads the token from the URL you
   paste back, it doesn't need the redirect to actually load anything).
2. Subscribe to the Kite Connect API plan for that app (a paid
   subscription, billed monthly) - your app's `API key` and `API secret`
   appear on the app's page once that's active.
3. Copy the credentials template if you haven't already:
   ```
   cp .env.live_trader.example .env.live_trader
   ```
   Fill in `KITE_API_KEY` and `KITE_API_SECRET` in `.env.live_trader`
   (these don't change day to day). Leave `KITE_ACCESS_TOKEN` blank - the
   next step fills it in.
4. Install the new dependency if you haven't already:
   ```
   .venv/bin/pip install -r requirements.txt
   ```

## Every trading morning

```
.venv/bin/python scripts/kite_generate_access_token.py
```

This prints a real Zerodha login URL - open it, log in with your own
Zerodha credentials (this script never sees your password), and paste
back the redirected URL (or just the `request_token` in it) when
prompted. It writes `KITE_ACCESS_TOKEN` into `.env.live_trader`
automatically. That token is good until ~6 AM IST tomorrow.

## Running a paper-trading session against Kite's live quotes

Zero capital at risk - real market data, simulated fills:

```
export LIVE_BROKER_PROVIDER=paper
export LIVE_MARKET_DATA_PROVIDER=kite
export LIVE_MAX_BARS=4500   # a full trading day of 5s poll cycles; lower it for a quick smoke test
.venv/bin/python -m apps.live_trader.main
```

Or, via the existing cron-ready wrapper (same one Groww's daily runbook
uses):
```
LIVE_MARKET_DATA_PROVIDER=kite ./scripts/run_live_paper_session.sh
```

Check the startup banner for `Market data: kite/live` before trusting the
rest of the run.

## Moving to real capital later

Not covered by today's setup, and not something to do until the paper
run above has been validated over time (see
`docs/runbooks/daily_paper_trading.md`'s "When you're ready to size this
up"). When you are ready:

```
export LIVE_BROKER_PROVIDER=kite
export LIVE_MARKET_DATA_PROVIDER=kite
export LIVE_CONFIRM=I_UNDERSTAND_THIS_CAN_PLACE_REAL_ORDERS
```

This is a hard, explicit gate (`apps/live_trader/config.py`) - it will
refuse to start without that exact confirmation string, on top of the
existing reconciliation/unclean-session checks every live session goes
through regardless of broker.
