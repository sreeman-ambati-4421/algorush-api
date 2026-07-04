"""
Internal trade + scorecard API for AlgoRush.

Runs on the SAME host as the daily momentum_final.py / momentum_etf.py cron
(not on Vercel) because Kite order placement is bound to a broker-allow-listed
source IP (see Algo/utils/src_bind.py) that only exists on that host. The
Next.js dashboard on Vercel reads Neon Postgres directly for everything except
placing manual trades and computing scorecard metrics, both of which come
through here.

Auth: a single shared-secret bearer token (TRADE_API_SECRET env var), meant to
be called only from the dashboard's server-side API route -- never from the
browser directly.

Run with:
    uvicorn Algo.api.trade_service:app --host 0.0.0.0 --port 8787
(behind a reverse proxy that terminates TLS and only exposes this port
internally / to Vercel's egress ranges).
"""

import os
import time
from datetime import datetime

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
from kiteconnect import KiteConnect

from Algo.logger import logger, init_logging
from Algo.utils.algoutils import getInstrumentsList, gettoken, loadAccessCodes
from Algo.utils.creds import ACCOUNTS as cred_account_settings
from Algo.utils.src_bind import mount_source_ip
from Algo.utils.db import (
    read_current_portfolio, write_current_portfolio, get_session, TradeOrder,
)
from Algo.api.scorecard import compute_scorecard

if not logger.handlers:
    init_logging("trade_service.log", log_level="INFO")

app = FastAPI(title="AlgoRush Trade API")

_kite_sessions: dict[str, KiteConnect] = {}
_instruments_cache: dict[str, list] = {}

STRATEGY_DEFAULT_CAPITAL = {"momentum": 500000, "momentum_etf": 100000}


def _check_auth(authorization: str | None):
    secret = os.environ.get("TRADE_API_SECRET")
    if not secret:
        raise HTTPException(500, "TRADE_API_SECRET is not configured on the server")
    if authorization != f"Bearer {secret}":
        raise HTTPException(401, "Invalid or missing bearer token")


def _get_kite(account_id: str) -> tuple[KiteConnect, list]:
    if account_id not in cred_account_settings:
        raise HTTPException(404, f"Unknown account: {account_id}")
    if account_id not in _kite_sessions:
        loadAccessCodes()
        cfg = cred_account_settings[account_id]
        kite = KiteConnect(api_key=cfg.get("api_key"))
        kite.set_access_token(cfg.get("access_token"))
        mount_source_ip(kite, cfg.get("source_ip"), account_id)
        _kite_sessions[account_id] = kite
        _instruments_cache[account_id] = getInstrumentsList(kite)
    return _kite_sessions[account_id], _instruments_cache[account_id]


def _place_kite_order(kite, exchange, tran_type, symbol, ordertype, iv_quantity, limitprice, dry_run):
    """Mirrors MomentumFinal.place_order in momentum_final.py -- same retry
    (3x, backing off share count on insufficient funds) and 60s stuck-order
    timeout, minus the strategy-specific bookkeeping."""
    order_exchange = kite.EXCHANGE_NSE if exchange == "NSE" else kite.EXCHANGE_BSE

    if tran_type == "BUY":
        kite_tran_type = kite.TRANSACTION_TYPE_BUY
        limitprice = round(limitprice + limitprice * 0.02) if not dry_run else round(limitprice)
    else:
        kite_tran_type = kite.TRANSACTION_TYPE_SELL
        limitprice = round(limitprice - limitprice * 0.02) if not dry_run else round(limitprice)

    order_type = kite.ORDER_TYPE_MARKET if ordertype == "MKT" else kite.ORDER_TYPE_LIMIT
    if ordertype == "MKT":
        limitprice = 0

    lv_symbol = symbol.split(":")[1]
    for _ in range(3):
        try:
            if dry_run:
                order_id, status, average_price, status_message = 0, "COMPLETE", limitprice or 1, ""
            else:
                if ordertype == "MKT":
                    order_id = kite.place_order(
                        tradingsymbol=lv_symbol, exchange=order_exchange, transaction_type=kite_tran_type,
                        quantity=iv_quantity, variety=kite.VARIETY_REGULAR, order_type=order_type,
                        product=kite.PRODUCT_CNC, tag="dashboard_manual", market_protection=-1,
                    )
                else:
                    order_id = kite.place_order(
                        tradingsymbol=lv_symbol, exchange=order_exchange, transaction_type=kite_tran_type,
                        quantity=iv_quantity, variety=kite.VARIETY_REGULAR, order_type=order_type,
                        product=kite.PRODUCT_CNC, price=limitprice, trigger_price=None, tag="dashboard_manual",
                    )
                time.sleep(0.3)

                status, average_price, status_message = "PENDING", 0, ""
                poll_start = time.time()
                while True:
                    order_book = kite.orders()
                    match = next((o for o in order_book if str(o["order_id"]) == str(order_id)), None)
                    if match:
                        if match["status"] in ("CANCELLED", "REJECTED", "CANCELED"):
                            status, average_price = "REJECTED", 0
                            status_message = match.get("status_message", "")
                            break
                        if match["status"] == "COMPLETE":
                            status, average_price = "COMPLETE", match["average_price"]
                            break
                    if time.time() - poll_start > 60:
                        status, average_price, status_message = "REJECTED", 0, "Order timeout after 60s"
                        break
                    time.sleep(0.5)

            return order_id, iv_quantity, average_price, status, status_message
        except Exception as error:
            status_message = str(error)
            if "Insufficient funds" in status_message and iv_quantity > 2:
                iv_quantity -= 1
                continue
            return 0, iv_quantity, 0, "REJECTED", status_message

    return 0, iv_quantity, 0, "REJECTED", "Max retries exceeded"


class TradeRequest(BaseModel):
    account_id: str
    strategy: str  # 'momentum' | 'momentum_etf'
    ticker: str  # e.g. 'NSE:TCS'
    side: str  # 'BUY' | 'SELL'
    quantity: int
    order_type: str = "MKT"
    limit_price: float | None = None
    requested_by: str
    dry_run: bool = False


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/trade")
def place_trade(req: TradeRequest, authorization: str | None = Header(default=None)):
    _check_auth(authorization)
    if req.strategy not in STRATEGY_DEFAULT_CAPITAL:
        raise HTTPException(400, f"Unknown strategy: {req.strategy}")
    if req.side not in ("BUY", "SELL"):
        raise HTTPException(400, "side must be BUY or SELL")

    kite, instruments = _get_kite(req.account_id)
    token = gettoken(req.ticker, instruments)
    ltp = kite.ltp([req.ticker])[req.ticker]["last_price"] if not req.dry_run else (req.limit_price or 0)

    order_id, filled_qty, avg_price, status, status_message = _place_kite_order(
        kite, "NSE", req.side, req.ticker, req.order_type, req.quantity,
        req.limit_price or ltp, req.dry_run,
    )

    session = get_session()
    try:
        trade_order = TradeOrder(
            account_id=req.account_id, strategy=req.strategy, ticker=req.ticker, side=req.side,
            quantity=filled_qty, order_type=req.order_type, limit_price=req.limit_price,
            status=status, kite_order_id=str(order_id), average_price=avg_price,
            status_message=status_message, requested_by=req.requested_by,
            completed_at=datetime.utcnow() if status == "COMPLETE" else None,
        )
        session.add(trade_order)
        session.commit()
        trade_id = trade_order.id
    finally:
        session.close()

    if status == "COMPLETE":
        _apply_fill_to_portfolio(req.account_id, req.strategy, req.ticker, req.side, filled_qty, avg_price)

    logger.info(f"Manual trade [{trade_id}] {req.account_id}/{req.strategy} {req.side} {req.ticker} "
                f"qty={filled_qty} status={status} by={req.requested_by}")

    return {
        "trade_id": trade_id, "status": status, "average_price": avg_price,
        "filled_quantity": filled_qty, "status_message": status_message,
    }


def _apply_fill_to_portfolio(account_id, strategy, ticker, side, qty, avg_price):
    """Updates portfolio_holdings + cash_remaining to reflect a completed
    manual trade, same bookkeeping as buy_stocks_and_spend_capital /
    sell_stocks_and_reclaim_capital in momentum_final.py."""
    data = read_current_portfolio(account_id, strategy)
    if not data:
        raise HTTPException(409, "No portfolio_meta row for this account/strategy yet -- run the bot once first")

    amount = round(qty * avg_price, 2)
    today_str = datetime.now().strftime("%d-%m-%Y")

    if side == "BUY":
        data["Cash_Remaining"] = float(data["Cash_Remaining"]) - amount
        if ticker in data["portfolio"]:
            existing = data["portfolio"][ticker]
            total_shares = existing["No_Of_Shares"] + qty
            total_buy_amount = round(existing["Buy_Amount"] + amount, 2)
            existing["No_Of_Shares"] = total_shares
            existing["Buy_Amount"] = total_buy_amount
            existing["Buy_Price"] = round(total_buy_amount / total_shares, 2)
        else:
            data["portfolio"][ticker] = {
                "Entry_Date": today_str, "Holding_Days": 1, "No_Of_Shares": qty,
                "Buy_Price": avg_price, "Buy_Amount": amount, "Current_Price": avg_price,
                "Current_Amount": amount, "100_Days_EMA": 0, "Profit_Loss": 0, "Percentage": 0,
            }
    else:  # SELL
        if ticker not in data["portfolio"]:
            raise HTTPException(409, f"{ticker} is not an open position for {account_id}/{strategy}")
        existing = data["portfolio"][ticker]
        remaining_shares = existing["No_Of_Shares"] - qty
        data["Cash_Remaining"] = float(data["Cash_Remaining"]) + amount
        if remaining_shares <= 0:
            del data["portfolio"][ticker]
        else:
            existing["No_Of_Shares"] = remaining_shares
            existing["Buy_Amount"] = round(existing["Buy_Price"] * remaining_shares, 2)

    data["No_of_Holdings"] = len(data["portfolio"])
    write_current_portfolio(account_id, strategy, data)


@app.get("/scorecard/{account_id}/{strategy}")
def scorecard(account_id: str, strategy: str, initial_capital: float | None = None,
              authorization: str | None = Header(default=None)):
    _check_auth(authorization)
    if strategy not in STRATEGY_DEFAULT_CAPITAL:
        raise HTTPException(400, f"Unknown strategy: {strategy}")
    cap = initial_capital if initial_capital is not None else STRATEGY_DEFAULT_CAPITAL[strategy]
    return compute_scorecard(account_id, strategy, cap)
