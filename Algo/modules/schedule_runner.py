"""
Checks job_schedule and launches the daily momentum bot when due, recording
the outcome in job_runs -- the DB-driven counterpart to the dashboard's
Schedule page (Algo/api/trade_service.py's /schedule and /runs endpoints).

Does NOT touch momentum_final.py / momentum_etf.py's trading logic at all --
just wraps whichever module is due as a subprocess and records the result.

Meant to be invoked frequently and cheaply by the host's own scheduler
(cron / Task Scheduler), e.g. every 5 minutes during the morning window:

    */5 8-10 * * 1-6  cd /path/to/AlgoRush-API && .venv/bin/python -m Algo.modules.schedule_runner

Safe to run as often as you like -- it's a no-op once a strategy has already
run today, isn't enabled, isn't scheduled for today's weekday, or its
run_time hasn't passed yet.
"""

import subprocess
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from Algo.logger import logger, init_logging
from Algo.utils.creds import ACCOUNTS
from Algo.utils.db import get_job_schedule, get_recent_job_runs, record_job_run_start, record_job_run_complete

STRATEGY_MODULE = {
    "momentum": "Algo.momentum.momentum_final",
    "momentum_etf": "Algo.momentum_etf.momentum_etf",
}

_WEEKDAY_FIELDS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
# run_time in job_schedule is always meant as IST (NSE market hours) --
# compare against IST explicitly rather than the host's system timezone, so
# this keeps working correctly no matter what timezone the bot host is set to.
IST = ZoneInfo("Asia/Kolkata")


def _already_ran_today(account_id, strategy):
    runs = get_recent_job_runs(account_id, strategy, limit=1)
    if not runs:
        return False
    started = datetime.fromisoformat(runs[0]["started_at"]).astimezone(IST).date()
    return started == datetime.now(IST).date()


def _is_due(schedule):
    now_ist = datetime.now(IST)
    if not schedule["enabled"]:
        return False
    if not schedule[_WEEKDAY_FIELDS[now_ist.weekday()]]:
        return False
    run_time = datetime.strptime(schedule["run_time"], "%H:%M").time()
    return now_ist.time() >= run_time


def run_if_due(account_id, strategy):
    schedule = get_job_schedule(account_id, strategy)
    if not _is_due(schedule) or _already_ran_today(account_id, strategy):
        return

    module = STRATEGY_MODULE[strategy]
    logger.info(f"Schedule due: launching {module} --userid {account_id}")
    run_id = record_job_run_start(account_id, strategy)
    try:
        result = subprocess.run(
            [sys.executable, "-m", module, "--userid", account_id],
            capture_output=True, text=True, timeout=3600,
        )
        output = (result.stdout + "\n" + result.stderr).strip()[-4000:]
        status = "SUCCESS" if result.returncode == 0 else "FAILED"
        record_job_run_complete(run_id, status, output or None)
        logger.info(f"{account_id}/{strategy} finished: {status} (exit {result.returncode})")
    except Exception as e:
        record_job_run_complete(run_id, "FAILED", str(e))
        logger.error(f"{account_id}/{strategy} run failed to launch: {e}")


def main():
    if not logger.handlers:
        init_logging("schedule_runner.log", log_level="INFO")
    for account_id, cfg in ACCOUNTS.items():
        if not cfg.get("TradeOn", True):
            continue
        for strategy in STRATEGY_MODULE:
            run_if_due(account_id, strategy)


if __name__ == "__main__":
    main()
