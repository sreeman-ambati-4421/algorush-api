"""
Postgres (Neon) backed replacement for the Excel read/write helpers that used
to live in Algo/momentum/momentum_final.py and Algo/momentum_etf/momentum_etf.py.

Schema lives in momentum-dashboard/db/schema.sql (sibling project). Connects via
the DATABASE_URL env var, e.g.:

    postgresql://user:password@ep-xxxx.neon.tech/momentum?sslmode=require

Every function here mirrors the exact return/argument shape of the Excel
function it replaces, so the strategy logic in momentum_final.py / momentum_etf.py
does not need to change -- only the module-level I/O functions do.
"""

import os
from collections import OrderedDict
from datetime import datetime, date as date_cls

from sqlalchemy import (
    create_engine, Column, Text, Boolean, Numeric, Integer, Date, DateTime, Time,
    ForeignKey, UniqueConstraint, Enum as SAEnum, func, select, delete,
)
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import declarative_base, sessionmaker

Base = declarative_base()

DATE_FMT = "%d-%m-%Y"


def _engine():
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set -- point it at your Neon Postgres instance."
        )
    return create_engine(url, pool_pre_ping=True, pool_recycle=300)


_ENGINE = None
_SessionLocal = None


def get_session():
    global _ENGINE, _SessionLocal
    if _ENGINE is None:
        _ENGINE = _engine()
        _SessionLocal = sessionmaker(bind=_ENGINE, expire_on_commit=False)
    return _SessionLocal()


strategy_enum = SAEnum("momentum", "momentum_etf", name="strategy_name", create_type=False)
trade_side_enum = SAEnum("BUY", "SELL", name="trade_side", create_type=False)
trade_status_enum = SAEnum("PENDING", "COMPLETE", "REJECTED", name="trade_status", create_type=False)
job_run_status_enum = SAEnum("RUNNING", "SUCCESS", "FAILED", "SKIPPED", name="job_run_status", create_type=False)


class Account(Base):
    __tablename__ = "accounts"
    userid = Column(Text, primary_key=True)
    client_name = Column(Text, nullable=False)
    trade_on = Column(Boolean, nullable=False, default=True)
    is_base = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class StrategySettings(Base):
    __tablename__ = "strategy_settings"
    account_id = Column(Text, ForeignKey("accounts.userid"), primary_key=True)
    strategy = Column(strategy_enum, primary_key=True)
    reset_and_rebalance = Column(Boolean, nullable=False, default=False)
    rebalance_today = Column(Boolean, nullable=False, default=False)
    initial_capital = Column(Numeric, nullable=False, default=0)
    sip_amount = Column(Numeric, nullable=False, default=0)
    additional_capital = Column(Numeric, nullable=False, default=0)
    rebalance_counter = Column(Integer, nullable=False, default=5)
    updated_at = Column(DateTime(timezone=True), server_default=func.now())


class PortfolioMeta(Base):
    __tablename__ = "portfolio_meta"
    account_id = Column(Text, ForeignKey("accounts.userid"), primary_key=True)
    strategy = Column(strategy_enum, primary_key=True)
    executed_date = Column(Date, nullable=False)
    cash_remaining = Column(Numeric, nullable=False, default=0)
    rebalance_counter = Column(Integer, nullable=False, default=5)
    updated_at = Column(DateTime(timezone=True), server_default=func.now())


class PortfolioHolding(Base):
    __tablename__ = "portfolio_holdings"
    id = Column(Integer, primary_key=True)
    account_id = Column(Text, ForeignKey("accounts.userid"), nullable=False)
    strategy = Column(strategy_enum, nullable=False)
    ticker = Column(Text, nullable=False)
    entry_date = Column(Date, nullable=False)
    holding_days = Column(Integer, nullable=False, default=1)
    no_of_shares = Column(Numeric, nullable=False)
    buy_price = Column(Numeric, nullable=False)
    buy_amount = Column(Numeric, nullable=False)
    current_price = Column(Numeric, nullable=False, default=0)
    current_amount = Column(Numeric, nullable=False, default=0)
    ema_100 = Column(Numeric, nullable=False, default=0)
    profit_loss = Column(Numeric, nullable=False, default=0)
    percentage = Column(Numeric, nullable=False, default=0)
    updated_at = Column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (UniqueConstraint("account_id", "strategy", "ticker"),)


class SummaryHistory(Base):
    __tablename__ = "summary_history"
    id = Column(Integer, primary_key=True)
    account_id = Column(Text, ForeignKey("accounts.userid"), nullable=False)
    strategy = Column(strategy_enum, nullable=False)
    date = Column(Date, nullable=False)
    no_of_holdings = Column(Integer, nullable=False, default=0)
    invested_capital = Column(Numeric, nullable=False, default=0)
    holdings_value = Column(Numeric, nullable=False, default=0)
    cash_remaining = Column(Numeric, nullable=False, default=0)
    total_value_holdings = Column(Numeric, nullable=False, default=0)
    holding_values_diff = Column(Numeric, nullable=True)
    total_profit_loss = Column(Numeric, nullable=False, default=0)
    sip_amount_added = Column(Numeric, nullable=False, default=0)
    additional_capital_added = Column(Numeric, nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (UniqueConstraint("account_id", "strategy", "date"),)


class ExitedStock(Base):
    __tablename__ = "exited_stocks"
    id = Column(Integer, primary_key=True)
    account_id = Column(Text, ForeignKey("accounts.userid"), nullable=False)
    strategy = Column(strategy_enum, nullable=False)
    ticker = Column(Text, nullable=False)
    entry_date = Column(Date, nullable=False)
    exit_date = Column(Date, nullable=False)
    holding_days = Column(Integer, nullable=False)
    no_of_shares = Column(Numeric, nullable=False)
    buy_price = Column(Numeric, nullable=False)
    buy_amount = Column(Numeric, nullable=False)
    sell_price = Column(Numeric, nullable=False)
    sell_amount = Column(Numeric, nullable=False)
    ema_100 = Column(Numeric, nullable=False, default=0)
    profit_loss = Column(Numeric, nullable=False, default=0)
    percentage = Column(Numeric, nullable=False, default=0)
    exit_type = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class TradeOrder(Base):
    __tablename__ = "trade_orders"
    id = Column(Integer, primary_key=True)
    account_id = Column(Text, ForeignKey("accounts.userid"), nullable=False)
    strategy = Column(strategy_enum, nullable=False)
    ticker = Column(Text, nullable=False)
    side = Column(trade_side_enum, nullable=False)
    quantity = Column(Numeric, nullable=False)
    order_type = Column(Text, nullable=False, default="MKT")
    limit_price = Column(Numeric, nullable=True)
    status = Column(trade_status_enum, nullable=False, default="PENDING")
    kite_order_id = Column(Text, nullable=True)
    average_price = Column(Numeric, nullable=True)
    status_message = Column(Text, nullable=True)
    requested_by = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    completed_at = Column(DateTime(timezone=True), nullable=True)


class JobSchedule(Base):
    __tablename__ = "job_schedule"
    account_id = Column(Text, ForeignKey("accounts.userid"), primary_key=True)
    strategy = Column(strategy_enum, primary_key=True)
    run_time = Column(Time, nullable=False)
    enabled = Column(Boolean, nullable=False, default=True)
    monday = Column(Boolean, nullable=False, default=True)
    tuesday = Column(Boolean, nullable=False, default=True)
    wednesday = Column(Boolean, nullable=False, default=True)
    thursday = Column(Boolean, nullable=False, default=True)
    friday = Column(Boolean, nullable=False, default=True)
    saturday = Column(Boolean, nullable=False, default=False)
    sunday = Column(Boolean, nullable=False, default=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now())


class JobRun(Base):
    __tablename__ = "job_runs"
    id = Column(Integer, primary_key=True)
    account_id = Column(Text, ForeignKey("accounts.userid"), nullable=False)
    strategy = Column(strategy_enum, nullable=False)
    started_at = Column(DateTime(timezone=True), server_default=func.now())
    completed_at = Column(DateTime(timezone=True), nullable=True)
    status = Column(job_run_status_enum, nullable=False, default="RUNNING")
    message = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


def _to_date(date_str):
    return datetime.strptime(date_str, DATE_FMT).date()


def _to_str(d):
    if d is None:
        return None
    if isinstance(d, date_cls):
        return d.strftime(DATE_FMT)
    return d


# ── Portfolio (replaces read/write_current_portfolio_to_excel) ──────────────

def read_current_portfolio(account_id, strategy):
    """Mirrors read_current_portfolio_from_excel(userid)'s return shape."""
    session = get_session()
    try:
        meta = session.get(PortfolioMeta, (account_id, strategy))
        if meta is None:
            return {}

        holdings = session.scalars(
            select(PortfolioHolding).where(
                PortfolioHolding.account_id == account_id,
                PortfolioHolding.strategy == strategy,
            )
        ).all()

        portfolio = OrderedDict()
        for h in holdings:
            portfolio[h.ticker] = {
                "Entry_Date": _to_str(h.entry_date),
                "Holding_Days": h.holding_days,
                "No_Of_Shares": h.no_of_shares,
                "Buy_Price": h.buy_price,
                "Buy_Amount": h.buy_amount,
                "Current_Price": h.current_price,
                "Current_Amount": h.current_amount,
                "100_Days_EMA": h.ema_100,
                "Profit_Loss": h.profit_loss,
                "Percentage": h.percentage,
            }

        return {
            "Executed_Date": _to_str(meta.executed_date),
            "No_of_Holdings": len(portfolio),
            "Cash_Remaining": float(meta.cash_remaining),
            "Rebalance_Counter": meta.rebalance_counter,
            "portfolio": portfolio,
        }
    finally:
        session.close()


def write_current_portfolio(account_id, strategy, data):
    """Mirrors write_current_portfolio_to_excel(data)."""
    session = get_session()
    try:
        meta_stmt = pg_insert(PortfolioMeta).values(
            account_id=account_id,
            strategy=strategy,
            executed_date=_to_date(data["Executed_Date"]),
            cash_remaining=data["Cash_Remaining"],
            rebalance_counter=data["Rebalance_Counter"],
        )
        meta_stmt = meta_stmt.on_conflict_do_update(
            index_elements=["account_id", "strategy"],
            set_={
                "executed_date": meta_stmt.excluded.executed_date,
                "cash_remaining": meta_stmt.excluded.cash_remaining,
                "rebalance_counter": meta_stmt.excluded.rebalance_counter,
                "updated_at": func.now(),
            },
        )
        session.execute(meta_stmt)

        tickers_in_portfolio = list(data["portfolio"].keys())
        session.execute(
            delete(PortfolioHolding).where(
                PortfolioHolding.account_id == account_id,
                PortfolioHolding.strategy == strategy,
                PortfolioHolding.ticker.notin_(tickers_in_portfolio),
            )
        )

        for ticker, details in data["portfolio"].items():
            holding_stmt = pg_insert(PortfolioHolding).values(
                account_id=account_id,
                strategy=strategy,
                ticker=ticker,
                entry_date=_to_date(details["Entry_Date"]),
                holding_days=details["Holding_Days"],
                no_of_shares=details["No_Of_Shares"],
                buy_price=details["Buy_Price"],
                buy_amount=details["Buy_Amount"],
                current_price=details.get("Current_Price", 0),
                current_amount=details.get("Current_Amount", 0),
                ema_100=details.get("100_Days_EMA", 0),
                profit_loss=details.get("Profit_Loss", 0),
                percentage=details.get("Percentage", 0),
            )
            holding_stmt = holding_stmt.on_conflict_do_update(
                index_elements=["account_id", "strategy", "ticker"],
                set_={
                    "entry_date": holding_stmt.excluded.entry_date,
                    "holding_days": holding_stmt.excluded.holding_days,
                    "no_of_shares": holding_stmt.excluded.no_of_shares,
                    "buy_price": holding_stmt.excluded.buy_price,
                    "buy_amount": holding_stmt.excluded.buy_amount,
                    "current_price": holding_stmt.excluded.current_price,
                    "current_amount": holding_stmt.excluded.current_amount,
                    "ema_100": holding_stmt.excluded.ema_100,
                    "profit_loss": holding_stmt.excluded.profit_loss,
                    "percentage": holding_stmt.excluded.percentage,
                    "updated_at": func.now(),
                },
            )
            session.execute(holding_stmt)

        session.commit()
    finally:
        session.close()


# ── Summary history (replaces read/write_current_summary_to_excel) ─────────

def write_current_summary(account_id, strategy, current_stock_summary):
    """Mirrors write_current_summary_to_excel(current_stock_summary)."""
    session = get_session()
    try:
        stmt = pg_insert(SummaryHistory).values(
            account_id=account_id,
            strategy=strategy,
            date=_to_date(current_stock_summary["Date"]),
            no_of_holdings=current_stock_summary["No_of_Holdings"],
            invested_capital=current_stock_summary["Invested_Capital"],
            holdings_value=current_stock_summary["Holdings_Value"],
            cash_remaining=current_stock_summary["Cash_Remaining"],
            total_value_holdings=current_stock_summary["Total_Value_Holdings"],
            holding_values_diff=current_stock_summary.get("Holding_Values_Diff"),
            total_profit_loss=current_stock_summary["Total_Profit_Loss"],
            sip_amount_added=current_stock_summary.get("SIP_Amount_Added", 0),
            additional_capital_added=current_stock_summary.get("Additional_Capital_Added", 0),
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["account_id", "strategy", "date"],
            set_={
                "no_of_holdings": stmt.excluded.no_of_holdings,
                "invested_capital": stmt.excluded.invested_capital,
                "holdings_value": stmt.excluded.holdings_value,
                "cash_remaining": stmt.excluded.cash_remaining,
                "total_value_holdings": stmt.excluded.total_value_holdings,
                "holding_values_diff": stmt.excluded.holding_values_diff,
                "total_profit_loss": stmt.excluded.total_profit_loss,
                "sip_amount_added": stmt.excluded.sip_amount_added,
                "additional_capital_added": stmt.excluded.additional_capital_added,
            },
        )
        session.execute(stmt)
        session.commit()
    finally:
        session.close()


def read_current_summary(account_id, strategy):
    """Mirrors read_current_summary_from_excel()'s return shape: OrderedDict
    keyed by 'dd-mm-yyyy' date string, ascending, each value a dict of the
    remaining summary fields."""
    session = get_session()
    try:
        rows = session.scalars(
            select(SummaryHistory)
            .where(SummaryHistory.account_id == account_id, SummaryHistory.strategy == strategy)
            .order_by(SummaryHistory.date.asc())
        ).all()

        result = OrderedDict()
        for r in rows:
            result[_to_str(r.date)] = {
                "No_of_Holdings": r.no_of_holdings,
                "Invested_Capital": float(r.invested_capital),
                "Holdings_Value": float(r.holdings_value),
                "Cash_Remaining": float(r.cash_remaining),
                "Total_Value_Holdings": float(r.total_value_holdings),
                "Holding_Values_Diff": float(r.holding_values_diff) if r.holding_values_diff is not None else None,
                "Total_Profit_Loss": float(r.total_profit_loss),
            }
        return result
    finally:
        session.close()


# ── Exited stocks (replaces write_exit_stocks_to_excel) ─────────────────────

def write_exit_stocks(account_id, strategy, stocks_deleted, mode_str):
    """Mirrors write_exit_stocks_to_excel(stocks_deleted, mode_str)."""
    if not stocks_deleted:
        return
    session = get_session()
    try:
        for ticker, d in stocks_deleted.items():
            session.add(ExitedStock(
                account_id=account_id,
                strategy=strategy,
                ticker=ticker,
                entry_date=_to_date(d["Entry_Date"]),
                exit_date=_to_date(d["Exit_Date"]),
                holding_days=d["Holding_Days"],
                no_of_shares=d["No_Of_Shares"],
                buy_price=d["Buy_Price"],
                buy_amount=d["Buy_Amount"],
                sell_price=d["Sell_Price"],
                sell_amount=d["Sell_Amount"],
                ema_100=d.get("100_Days_EMA", 0),
                profit_loss=d["Profit_Loss"],
                percentage=d["Percentage"],
                exit_type=mode_str,
            ))
        session.commit()
    finally:
        session.close()


# ── Strategy settings (replaces load/save_momentum_settings_to_file) ───────

def load_strategy_settings(strategy):
    """Returns a dict keyed by userid, mirroring the settings.json shape."""
    session = get_session()
    try:
        rows = session.scalars(
            select(StrategySettings).where(StrategySettings.strategy == strategy)
        ).all()
        return {
            r.account_id: {
                "reset_and_rebalance": r.reset_and_rebalance,
                "rebalance_today": r.rebalance_today,
                "initial_capital": float(r.initial_capital),
                "sip_amount": float(r.sip_amount),
                "additional_capital": float(r.additional_capital),
                "rebalance_counter": r.rebalance_counter,
            }
            for r in rows
        }
    finally:
        session.close()


def save_strategy_settings(strategy, settings_by_userid):
    """settings_by_userid: dict keyed by userid -> settings dict (same shape
    as load_strategy_settings' return value)."""
    session = get_session()
    try:
        for account_id, s in settings_by_userid.items():
            stmt = pg_insert(StrategySettings).values(
                account_id=account_id,
                strategy=strategy,
                reset_and_rebalance=s.get("reset_and_rebalance", False),
                rebalance_today=s.get("rebalance_today", False),
                initial_capital=s.get("initial_capital", 0),
                sip_amount=s.get("sip_amount", 0),
                additional_capital=s.get("additional_capital", 0),
                rebalance_counter=s.get("rebalance_counter", 5),
            )
            stmt = stmt.on_conflict_do_update(
                index_elements=["account_id", "strategy"],
                set_={
                    "reset_and_rebalance": stmt.excluded.reset_and_rebalance,
                    "rebalance_today": stmt.excluded.rebalance_today,
                    "initial_capital": stmt.excluded.initial_capital,
                    "sip_amount": stmt.excluded.sip_amount,
                    "additional_capital": stmt.excluded.additional_capital,
                    "rebalance_counter": stmt.excluded.rebalance_counter,
                    "updated_at": func.now(),
                },
            )
            session.execute(stmt)
        session.commit()
    finally:
        session.close()


def ensure_account(account_id, client_name, trade_on=True, is_base=False):
    """Upserts the accounts row so foreign keys never fail on a fresh DB."""
    session = get_session()
    try:
        stmt = pg_insert(Account).values(
            userid=account_id, client_name=client_name, trade_on=trade_on, is_base=is_base
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=["userid"],
            set_={"client_name": stmt.excluded.client_name},
        )
        session.execute(stmt)
        session.commit()
    finally:
        session.close()


def add_manual_funds(account_id, strategy, amount):
    """Records a manual capital top-up (e.g. topping up the broker account
    after an insufficient-funds order failure, then placing the buy by hand).

    Bumps portfolio_meta.cash_remaining immediately so the next manual trade
    draws against the right balance. Separately, best-effort adds the amount
    to TODAY's summary_history.additional_capital_added -- the same field the
    bot's own SIP/lump-sum flow writes to -- so compute_scorecard's XIRR
    treats it as injected capital rather than return. Only updates an
    existing row for today; never creates one (that stays the bot's job, and
    a phantom row would corrupt the equity-curve chart until the bot next
    runs and overwrites it).

    Returns (new_cash_remaining, added_to_summary_today: bool).
    """
    session = get_session()
    try:
        meta = session.get(PortfolioMeta, (account_id, strategy))
        if meta is None:
            raise ValueError(
                "No portfolio_meta row for this account/strategy yet -- run the bot once first"
            )
        meta.cash_remaining = float(meta.cash_remaining) + amount

        today = date_cls.today()
        summary_row = session.scalars(
            select(SummaryHistory).where(
                SummaryHistory.account_id == account_id,
                SummaryHistory.strategy == strategy,
                SummaryHistory.date == today,
            )
        ).first()
        added_to_summary = False
        if summary_row is not None:
            summary_row.additional_capital_added = float(summary_row.additional_capital_added or 0) + amount
            added_to_summary = True

        session.commit()
        return float(meta.cash_remaining), added_to_summary
    finally:
        session.close()


_DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")


def get_job_schedule(account_id, strategy):
    """Returns the dashboard-editable schedule row, or sane defaults
    (09:15, enabled, weekdays only) if none has been saved yet."""
    session = get_session()
    try:
        row = session.get(JobSchedule, (account_id, strategy))
        if row is None:
            return {
                "run_time": "09:15",
                "enabled": True,
                **{d: d not in ("saturday", "sunday") for d in _DAYS},
            }
        return {
            "run_time": row.run_time.strftime("%H:%M"),
            "enabled": row.enabled,
            **{d: getattr(row, d) for d in _DAYS},
        }
    finally:
        session.close()


def save_job_schedule(account_id, strategy, run_time, enabled, days):
    """run_time: 'HH:MM' string. days: dict with the 7 lowercase weekday
    keys -> bool (missing keys default to True, matching the column
    defaults)."""
    session = get_session()
    try:
        values = dict(
            account_id=account_id,
            strategy=strategy,
            run_time=datetime.strptime(run_time, "%H:%M").time(),
            enabled=enabled,
            **{d: days.get(d, True) for d in _DAYS},
        )
        stmt = pg_insert(JobSchedule).values(**values)
        stmt = stmt.on_conflict_do_update(
            index_elements=["account_id", "strategy"],
            set_={**{k: getattr(stmt.excluded, k) for k in values if k not in ("account_id", "strategy")},
                  "updated_at": func.now()},
        )
        session.execute(stmt)
        session.commit()
    finally:
        session.close()


def get_recent_job_runs(account_id, strategy, limit=5):
    session = get_session()
    try:
        rows = session.scalars(
            select(JobRun)
            .where(JobRun.account_id == account_id, JobRun.strategy == strategy)
            .order_by(JobRun.started_at.desc())
            .limit(limit)
        ).all()
        return [
            {
                "id": r.id,
                "started_at": r.started_at.isoformat(),
                "completed_at": r.completed_at.isoformat() if r.completed_at else None,
                "status": r.status,
                "message": r.message,
            }
            for r in rows
        ]
    finally:
        session.close()


def record_job_run_start(account_id, strategy):
    """Called by schedule_runner.py right before launching the bot
    subprocess. Returns the new job_runs.id to pass to record_job_run_complete."""
    session = get_session()
    try:
        run = JobRun(account_id=account_id, strategy=strategy, status="RUNNING")
        session.add(run)
        session.commit()
        return run.id
    finally:
        session.close()


def record_job_run_complete(run_id, status, message=None):
    """status: 'SUCCESS' | 'FAILED'. message: e.g. captured subprocess
    output/error, truncated by the caller if very long."""
    session = get_session()
    try:
        run = session.get(JobRun, run_id)
        if run is not None:
            run.status = status
            run.message = message
            run.completed_at = datetime.utcnow()
            session.commit()
    finally:
        session.close()
