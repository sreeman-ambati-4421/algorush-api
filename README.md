# AlgoRush-API

The Python side of AlgoRush: the daily momentum / momentum_etf trading bots,
the Postgres (Neon) data layer, and the internal trade + scorecard service
that the [AlgoRush-UI](../AlgoRush-UI) dashboard talks to.

This is a **trimmed, standalone copy** pulled out of the original `EquityAlgo`
monorepo -- only the files these two strategies actually need. `EquityAlgo`
itself is untouched and keeps running independently; nothing here was deleted
from it.

## What's here

```
Algo/
  logger.py               stdlib logging wrapper
  utils/
    algoutils.py           Telegram alerts, instrument/token lookups
    db.py                  Postgres (Neon) models + read/write helpers
    settings.py            static AlgoSettings (indices, etc.)
    src_bind.py             per-account source-IP binding (broker allow-listing)
    creds.py.example        template -- copy to creds.py, fill in real values (gitignored)
  momentum/
    momentum_final.py       the daily momentum strategy bot
    build_scorecard.py      XIRR/Sharpe/Calmar math (reused by Algo/api/scorecard.py)
    stocks_list.csv          Nifty Midcap 150 universe
    sync_stocks_list.py      weekly refresh of stocks_list.csv
  momentum_etf/
    momentum_etf.py          the daily momentum ETF strategy bot
    etf_list.csv             ETF universe
  api/
    trade_service.py         FastAPI service: manual buy/sell + live scorecard
    scorecard.py              DB-backed scorecard computation
  modules/
    token_generator.py       generates each account's daily Kite access_token
db/
  schema.sql                Postgres schema -- apply once to Neon
  migrate_excel_to_db.py     one-off backfill from EquityAlgo's old .xlsx workbooks
requirements.txt
```

Deliberately left out (not needed by these two strategies): `equity_shop`
(a different DCA strategy), `backtest/`, `old_momentum.py`, `orderutils.py`,
`izg276check.py`. Pull any of these over from `EquityAlgo` if you need them
later.

## Setup

1. **Credentials**: `cp Algo/utils/creds.py.example Algo/utils/creds.py` and
   fill in real values (`ACCOUNTS`, `telegram`). This file is gitignored.
2. **Install deps**: `pip install -r requirements.txt`
3. **Database**: create a Neon Postgres project, apply the schema:
   ```
   psql "$DATABASE_URL" -f db/schema.sql
   ```
4. **Backfill history** (optional, only if migrating from an existing
   EquityAlgo install with real `.xlsx` workbooks):
   ```
   export DATABASE_URL=postgresql://...
   python db/migrate_excel_to_db.py --xlsx-source-root /path/to/old/EquityAlgo
   ```
5. **Daily tokens**: `python Algo/modules/token_generator.py --i <ACCOUNT>`
   (needs interactive TOTP login the first time per Kite's flow) -- run before
   market open, however you already schedule this.
6. **Run the bots** (same invocation as before, just needs `DATABASE_URL` set
   in the environment now instead of an `.xlsx` file path):
   ```
   python -m Algo.momentum.momentum_final --userid ZU9940
   python -m Algo.momentum_etf.momentum_etf --userid ZU9940
   ```
7. **Run the trade service** (what AlgoRush-UI's manual buy/sell and scorecard
   pages call):
   ```
   export TRADE_API_SECRET=$(openssl rand -base64 32)
   uvicorn Algo.api.trade_service:app --host 0.0.0.0 --port 8787
   ```
   Put this behind a TLS-terminating reverse proxy (nginx/Caddy) if exposed
   beyond localhost -- see AlgoRush-UI's README for the full architecture.
