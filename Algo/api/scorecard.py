"""
DB-backed port of Algo/momentum/build_scorecard.py's metrics (XIRR, Sharpe,
Calmar, drawdown, win-rate, monthly P&L, stock-repeat analysis). Instead of
writing a styled 'Scorecard' Excel sheet, this returns a plain JSON-able dict
computed live from summary_history / exited_stocks / portfolio_holdings, for
the dashboard's GET /scorecard/{account}/{strategy} endpoint.

Reuses the XIRR solver from build_scorecard.py rather than reimplementing it.
"""

import numpy as np
import pandas as pd

from Algo.momentum.build_scorecard import xirr
from Algo.utils.db import get_session, SummaryHistory, ExitedStock, PortfolioHolding, PortfolioMeta


def compute_scorecard(account_id: str, strategy: str, initial_cap: float) -> dict:
    session = get_session()
    try:
        summary_rows = session.query(SummaryHistory).filter(
            SummaryHistory.account_id == account_id, SummaryHistory.strategy == strategy
        ).order_by(SummaryHistory.date.asc()).all()

        if not summary_rows:
            return {"error": "no summary_history rows for this account/strategy yet"}

        daily = pd.DataFrame([{
            "Date": r.date, "SIPAdded": float(r.sip_amount_added or 0),
            "AddCapAdded": float(r.additional_capital_added or 0),
            "LastValue": float(r.total_value_holdings or 0),
            "LastPnL": float(r.total_profit_loss or 0),
            "LastInvested": float(r.invested_capital or 0),
        } for r in summary_rows])

        latest = daily.iloc[-1]
        strategy_start = daily.iloc[0]["Date"]
        total_invested = float(latest["LastInvested"])
        current_value = float(latest["LastValue"])
        current_pnl = float(latest["LastPnL"])
        days_active = (latest["Date"] - strategy_start).days
        total_sip = daily["SIPAdded"].sum()
        total_add = daily["AddCapAdded"].sum()
        simple_roic = round(current_pnl / total_invested * 100, 2) if total_invested else 0
        annualised = round(simple_roic * 365 / days_active, 2) if days_active > 0 else 0

        cfs = [(strategy_start, -initial_cap)]
        for _, row in daily.iterrows():
            if row["AddCapAdded"] > 1:
                cfs.append((row["Date"], -row["AddCapAdded"]))
            if row["SIPAdded"] > 1:
                cfs.append((row["Date"], -row["SIPAdded"]))
        cfs.append((latest["Date"], current_value))
        xirr_val = xirr(sorted(cfs, key=lambda x: x[0]))

        daily_rets = daily["LastValue"].pct_change().dropna()
        rf_daily = 0.06 / 252
        sharpe = None
        if len(daily_rets) > 1 and daily_rets.std() > 0:
            sharpe = round((daily_rets.mean() - rf_daily) / daily_rets.std() * np.sqrt(252), 2)

        daily["Peak"] = daily["LastValue"].cummax()
        daily["DD_pct"] = ((daily["LastValue"] - daily["Peak"]) / daily["Peak"] * 100).round(2)
        peak_val = daily["LastValue"].max()
        peak_date = daily.loc[daily["LastValue"].idxmax(), "Date"]
        max_dd = float(daily["DD_pct"].min())
        max_dd_date = daily.loc[daily["DD_pct"].idxmin(), "Date"]
        curr_dd = round(((current_value - peak_val) / peak_val) * 100, 2) if peak_val else 0

        in_dd_list = (daily["DD_pct"] < 0).tolist()
        max_dd_dur = cur_streak = 0
        for v in in_dd_list:
            cur_streak = cur_streak + 1 if v else 0
            max_dd_dur = max(max_dd_dur, cur_streak)
        curr_dd_dur = 0
        for v in reversed(in_dd_list):
            if v:
                curr_dd_dur += 1
            else:
                break

        calmar = round(xirr_val / abs(max_dd), 2) if (xirr_val is not None and xirr_val > 0 and max_dd != 0) else None

        daily["Month"] = pd.to_datetime(daily["Date"]).dt.to_period("M")
        monthly = (daily.groupby("Month")
                   .agg(LastValue=("LastValue", "last"), LastPnL=("LastPnL", "last"),
                        SIPAdded=("SIPAdded", "sum"), AddCap=("AddCapAdded", "sum"))
                   .reset_index())
        monthly["Monthly_PnL"] = monthly["LastPnL"].diff()
        monthly.loc[monthly.index[0], "Monthly_PnL"] = monthly.iloc[0]["LastPnL"]
        months_green = int((monthly["Monthly_PnL"] > 0).sum())

        exited_rows = session.query(ExitedStock).filter(
            ExitedStock.account_id == account_id, ExitedStock.strategy == strategy
        ).all()
        trade_quality = None
        if exited_rows:
            exited = pd.DataFrame([{
                "Stock": r.ticker, "Profit_Loss": float(r.profit_loss), "Percentage": float(r.percentage),
                "Holding_Days": r.holding_days, "Exit_Type": r.exit_type,
            } for r in exited_rows])
            n_total = len(exited)
            n_wins = int((exited["Profit_Loss"] > 0).sum())
            winners = exited[exited["Profit_Loss"] > 0]
            losers = exited[exited["Profit_Loss"] <= 0]
            trade_quality = {
                "total_exits": n_total,
                "win_rate_pct": round(n_wins / n_total * 100, 1),
                "wins": n_wins,
                "losses": n_total - n_wins,
                "avg_win_pct": round(winners["Percentage"].mean(), 2) if len(winners) else 0,
                "avg_loss_pct": round(losers["Percentage"].mean(), 2) if len(losers) else 0,
                "net_pnl_closed": round(float(exited["Profit_Loss"].sum()), 2),
                "by_exit_type": (
                    exited.groupby("Exit_Type")
                    .agg(count=("Profit_Loss", "count"), net_pnl=("Profit_Loss", "sum"),
                         avg_pct=("Percentage", "mean"))
                    .round(2).reset_index().to_dict(orient="records")
                ),
            }

        holdings = session.query(PortfolioHolding).filter(
            PortfolioHolding.account_id == account_id, PortfolioHolding.strategy == strategy
        ).all()
        meta = session.get(PortfolioMeta, (account_id, strategy))
        open_pnl = sum(float(h.profit_loss) for h in holdings)
        open_invested = sum(float(h.buy_amount) for h in holdings)

        return {
            "account_id": account_id,
            "strategy": strategy,
            "period": {
                "start": strategy_start.isoformat(),
                "end": latest["Date"].isoformat(),
                "days_active": days_active,
            },
            "returns": {
                "initial_capital": initial_cap,
                "total_invested": total_invested,
                "total_sip": float(total_sip),
                "total_lump_sum": float(total_add),
                "current_value": current_value,
                "net_pnl": current_pnl,
                "roic_pct": simple_roic,
                "annualised_pct": annualised,
                "xirr_pct": xirr_val,
            },
            "risk": {
                "sharpe_ratio": sharpe,
                "calmar_ratio": calmar,
                "peak_value": float(peak_val),
                "peak_date": peak_date.isoformat(),
                "max_drawdown_pct": max_dd,
                "max_drawdown_date": max_dd_date.isoformat(),
                "max_drawdown_duration_days": max_dd_dur,
                "current_drawdown_pct": curr_dd,
                "current_drawdown_duration_days": curr_dd_dur,
            },
            "consistency": {
                "months_green": months_green,
                "total_months": len(monthly),
            },
            "trade_quality": trade_quality,
            "open_positions": {
                "count": len(holdings),
                "cash_remaining": float(meta.cash_remaining) if meta else 0,
                "invested": round(open_invested, 2),
                "unrealized_pnl": round(open_pnl, 2),
            },
        }
    finally:
        session.close()
