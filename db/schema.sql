-- AlgoRush schema (Neon Postgres)
-- Replaces the per-account Excel workbooks used by Algo/momentum/momentum_final.py
-- and Algo/momentum_etf/momentum_etf.py. One row per unit of data instead of one
-- sheet per account.

CREATE TYPE strategy_name AS ENUM ('momentum', 'momentum_etf');
CREATE TYPE trade_side AS ENUM ('BUY', 'SELL');
CREATE TYPE trade_status AS ENUM ('PENDING', 'COMPLETE', 'REJECTED');

-- One row per broker account. google_email links the account to exactly one
-- Google sign-in for the dashboard (1:1, enforced by UNIQUE) -- the UI gates
-- login and per-account access off this column alone, it never touches
-- account_credentials below. Broker secrets live in account_credentials,
-- encrypted at rest; only the bot host (holding CREDS_ENCRYPTION_KEY) can
-- decrypt them, via Algo/utils/db.py's load_account_credentials().
CREATE TABLE accounts (
    userid       TEXT PRIMARY KEY,
    client_name  TEXT NOT NULL,
    trade_on     BOOLEAN NOT NULL DEFAULT TRUE,
    is_base      BOOLEAN NOT NULL DEFAULT FALSE,
    google_email TEXT UNIQUE,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- At most one Base account at a time (register_account.py also enforces this
-- interactively, but a DB-level guarantee catches any other write path too).
CREATE UNIQUE INDEX accounts_one_base ON accounts (is_base) WHERE is_base;

-- Encrypted broker credentials, one row per account. *_enc columns hold
-- Fernet ciphertext (see Algo/utils/crypto.py) -- registered/rotated via
-- db/register_account.py, never written from the web app. Deliberately a
-- separate table from accounts so nothing in the UI (which only ever queries
-- accounts) can accidentally select ciphertext.
CREATE TABLE account_credentials (
    account_id       TEXT PRIMARY KEY REFERENCES accounts(userid),
    api_key_enc      TEXT NOT NULL,
    api_secret_enc   TEXT NOT NULL,
    password_enc     TEXT NOT NULL,
    totp_secret_enc  TEXT NOT NULL,
    source_ip        TEXT,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Replaces settings.json (which never actually existed on disk -- this is the
-- first real, durable home for these fields).
CREATE TABLE strategy_settings (
    account_id           TEXT NOT NULL REFERENCES accounts(userid),
    strategy             strategy_name NOT NULL,
    reset_and_rebalance  BOOLEAN NOT NULL DEFAULT FALSE,
    rebalance_today       BOOLEAN NOT NULL DEFAULT FALSE,
    initial_capital       NUMERIC NOT NULL DEFAULT 0,
    sip_amount            NUMERIC NOT NULL DEFAULT 0,
    additional_capital    NUMERIC NOT NULL DEFAULT 0,
    rebalance_counter     INTEGER NOT NULL DEFAULT 5,
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, strategy)
);

-- Replaces the metadata row (row 2, columns A-D) of the Current_Portfolio sheet.
CREATE TABLE portfolio_meta (
    account_id         TEXT NOT NULL REFERENCES accounts(userid),
    strategy           strategy_name NOT NULL,
    executed_date      DATE NOT NULL,
    cash_remaining     NUMERIC NOT NULL DEFAULT 0,
    rebalance_counter  INTEGER NOT NULL DEFAULT 5,
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, strategy)
);

-- Replaces the per-ticker rows of the Current_Portfolio sheet (open positions).
CREATE TABLE portfolio_holdings (
    id               BIGSERIAL PRIMARY KEY,
    account_id       TEXT NOT NULL REFERENCES accounts(userid),
    strategy         strategy_name NOT NULL,
    ticker           TEXT NOT NULL,
    entry_date       DATE NOT NULL,
    holding_days     INTEGER NOT NULL DEFAULT 1,
    no_of_shares     NUMERIC NOT NULL,
    buy_price        NUMERIC NOT NULL,
    buy_amount       NUMERIC NOT NULL,
    current_price    NUMERIC NOT NULL DEFAULT 0,
    current_amount   NUMERIC NOT NULL DEFAULT 0,
    ema_100          NUMERIC NOT NULL DEFAULT 0,
    profit_loss      NUMERIC NOT NULL DEFAULT 0,
    percentage       NUMERIC NOT NULL DEFAULT 0,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (account_id, strategy, ticker)
);

-- Replaces the Current_Summary sheet -- one append-only row per trading day,
-- drives the equity-curve / drawdown charts.
CREATE TABLE summary_history (
    id                        BIGSERIAL PRIMARY KEY,
    account_id                TEXT NOT NULL REFERENCES accounts(userid),
    strategy                  strategy_name NOT NULL,
    date                      DATE NOT NULL,
    no_of_holdings            INTEGER NOT NULL DEFAULT 0,
    invested_capital          NUMERIC NOT NULL DEFAULT 0,
    holdings_value            NUMERIC NOT NULL DEFAULT 0,
    cash_remaining            NUMERIC NOT NULL DEFAULT 0,
    total_value_holdings      NUMERIC NOT NULL DEFAULT 0,
    holding_values_diff       NUMERIC,
    total_profit_loss         NUMERIC NOT NULL DEFAULT 0,
    sip_amount_added          NUMERIC NOT NULL DEFAULT 0,
    additional_capital_added  NUMERIC NOT NULL DEFAULT 0,
    created_at                TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (account_id, strategy, date)
);

-- Replaces the Exited_Stocks sheet.
CREATE TABLE exited_stocks (
    id             BIGSERIAL PRIMARY KEY,
    account_id     TEXT NOT NULL REFERENCES accounts(userid),
    strategy       strategy_name NOT NULL,
    ticker         TEXT NOT NULL,
    entry_date     DATE NOT NULL,
    exit_date      DATE NOT NULL,
    holding_days   INTEGER NOT NULL,
    no_of_shares   NUMERIC NOT NULL,
    buy_price      NUMERIC NOT NULL,
    buy_amount     NUMERIC NOT NULL,
    sell_price     NUMERIC NOT NULL,
    sell_amount    NUMERIC NOT NULL,
    ema_100        NUMERIC NOT NULL DEFAULT 0,
    profit_loss    NUMERIC NOT NULL DEFAULT 0,
    percentage     NUMERIC NOT NULL DEFAULT 0,
    exit_type      TEXT NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Audit log of every manual buy/sell placed from the dashboard.
CREATE TABLE trade_orders (
    id             BIGSERIAL PRIMARY KEY,
    account_id     TEXT NOT NULL REFERENCES accounts(userid),
    strategy       strategy_name NOT NULL,
    ticker         TEXT NOT NULL,
    side           trade_side NOT NULL,
    quantity       NUMERIC NOT NULL,
    order_type     TEXT NOT NULL DEFAULT 'MKT',
    limit_price    NUMERIC,
    status         trade_status NOT NULL DEFAULT 'PENDING',
    kite_order_id  TEXT,
    average_price  NUMERIC,
    status_message TEXT,
    requested_by   TEXT NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at   TIMESTAMPTZ
);

CREATE INDEX idx_summary_history_lookup ON summary_history (account_id, strategy, date);
CREATE INDEX idx_exited_stocks_lookup ON exited_stocks (account_id, strategy, exit_date);
CREATE INDEX idx_trade_orders_lookup ON trade_orders (account_id, strategy, created_at);

-- Dashboard-editable run time/day for the daily bot. The actual OS-level
-- trigger (cron/Task Scheduler on the bot host) fires frequently and cheaply
-- (e.g. every 5 min in the morning window) and only actually launches the
-- bot once current time crosses run_time on an enabled day -- see
-- Algo/modules/schedule_runner.py. Editing this table is all the dashboard
-- needs to do; no server/crontab access required from the web app.
CREATE TABLE job_schedule (
    account_id   TEXT NOT NULL REFERENCES accounts(userid),
    strategy     strategy_name NOT NULL,
    run_time     TIME NOT NULL DEFAULT '09:15:00',
    enabled      BOOLEAN NOT NULL DEFAULT TRUE,
    monday       BOOLEAN NOT NULL DEFAULT TRUE,
    tuesday      BOOLEAN NOT NULL DEFAULT TRUE,
    wednesday    BOOLEAN NOT NULL DEFAULT TRUE,
    thursday     BOOLEAN NOT NULL DEFAULT TRUE,
    friday       BOOLEAN NOT NULL DEFAULT TRUE,
    saturday     BOOLEAN NOT NULL DEFAULT FALSE,
    sunday       BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (account_id, strategy)
);

-- One row per schedule_runner-triggered bot run, for the dashboard's
-- "latest runs" panel. Written by schedule_runner.py, which wraps the bot
-- subprocess without touching momentum_final.py's own trading logic.
CREATE TYPE job_run_status AS ENUM ('RUNNING', 'SUCCESS', 'FAILED', 'SKIPPED');

CREATE TABLE job_runs (
    id           BIGSERIAL PRIMARY KEY,
    account_id   TEXT NOT NULL REFERENCES accounts(userid),
    strategy     strategy_name NOT NULL,
    started_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at TIMESTAMPTZ,
    status       job_run_status NOT NULL DEFAULT 'RUNNING',
    message      TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_job_runs_lookup ON job_runs (account_id, strategy, started_at DESC);

-- The dashboard (AlgoRush-UI) uses NextAuth v4 with JWT sessions and no
-- database adapter -- it needs no NextAuth-owned tables at all. Sign-in is
-- gated purely off accounts.google_email (see lib/auth.ts).
