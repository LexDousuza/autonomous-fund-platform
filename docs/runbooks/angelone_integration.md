# Angel One (SmartAPI) integration - historical data only

Angel One is deliberately scoped to **historical market data only** in
this platform - it is never a live/paper trading broker. Kite Connect
remains the only broker real orders go through. See
`core/market_data/angelone_transport.py`'s module docstring for why; if
you're looking to add real order placement here, that's a different,
much larger decision this platform has explicitly not made.

Concretely: `core/market_data/factory.py`'s `provider="angelone"` branch
only accepts `mode="historical"` - `mode="live"`/`mode="paper"` raise
outright, and there is no `core/broker/angelone_adapter.py` at all.

## Why Angel One at all, given Kite/Groww already exist

Angel One's SmartAPI historical-candle endpoint is free with an API key
(no paid subscription tier, unlike Kite Connect's historical data) and,
unlike Kite, its login flow is fully headless - see below.

## One-time setup

1. Create an app at [smartapi.angelbroking.com](https://smartapi.angelbroking.com)
   ("Add App"). The **Redirect URL** field does not accept `localhost` -
   use any address you control instead (e.g. your server's static IP);
   nothing needs to actually be listening there since this platform
   never uses the redirect/order-placement side of SmartAPI at all, only
   the historical-data API. **Primary Static IP** is also required by
   this form - your server's IP (see `docs/runbooks/aws_integration.md`
   for allocating a stable Elastic IP if running on EC2).
2. Enable TOTP-based 2FA on your Angel One account if you haven't
   already, and note the base32 seed shown under the TOTP QR code (not
   the 6-digit code itself - the seed it's generated from).
3. Copy the credentials template if you haven't already:
   ```
   cp .env.live_trader.example .env.live_trader
   ```
   Fill in `ANGELONE_API_KEY` (from the app you just created),
   `ANGELONE_CLIENT_CODE` (your trading account ID), `ANGELONE_PASSWORD`
   (your login PIN), and `ANGELONE_TOTP_SECRET` (the seed from step 2).
4. Install the new dependencies if you haven't already:
   ```
   .venv/bin/pip install -r requirements.txt
   ```

## No daily manual step

Unlike Kite Connect (which needs a real browser login once a day - see
`docs/runbooks/kite_integration.md`), Angel One's login
(`generateSession`) only needs the client code, PIN, and a TOTP code
computed from the seed above - entirely headless, no browser, no human
in the loop. `core/market_data/angelone_transport.py` computes that code
itself (`core/utils/totp.py`, the same RFC 6238 implementation Groww's
login already uses) and re-authenticates fresh every time a transport is
constructed - there is no `scripts/angelone_generate_access_token.py` to
run each morning because there is nothing to generate ahead of time.

## Fetching historical candles

```python
from datetime import date
from core.market_data.factory import build_market_data_feed
from core.market_data.factory_models import MarketDataConfig

feed = build_market_data_feed(MarketDataConfig(
    provider="angelone",
    mode="historical",
    metadata={
        "api_key": "...",
        "client_code": "...",
        "password": "...",
        "totp_secret": "...",
        "instruments": [{"symbol": "RELIANCE", "exchange": "NSE"}],
        "from_date": "2026-01-01",
        "to_date": "2026-01-31",
        "interval": "ONE_DAY",  # or ONE_MINUTE/THREE_MINUTE/.../ONE_HOUR
    },
))
bars = feed.get_history("RELIANCE")
```

`interval` must be one of Angel One's real interval strings (see
`core/market_data/angelone_transport.MAX_DAYS_PER_INTERVAL` for the full
list and each one's max days-per-request ceiling, verified from
[the official docs](https://smartapi.angelbroking.com/docs/Historical)) -
a window wider than that ceiling is rejected immediately with a clear
error rather than silently truncated.

## Symbol resolution

Angel One needs a numeric `symboltoken` per instrument, not a plain
symbol string - resolved automatically from Angel One's public scrip
master (a ~35MB JSON dump of every listed instrument across all
exchanges/segments, fetched and cached once per transport instance; see
`AngelOneMarketDataTransport._load_scrip_master`). Only cash-equity
entries are matched (`instrumenttype == ""`) - an NFO/BFO derivative
sharing the same underlying name is never resolved by this lookup.
