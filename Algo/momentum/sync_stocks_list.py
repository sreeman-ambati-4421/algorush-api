"""
Nifty Midcap 150 — stocks_list.csv auto-sync

Sources tried in order (first success wins):
  1. NSE India JSON API  — most reliable, session-cookie based
  2. niftyindices.com CSV — official NSE Indices Ltd constituent file
     Columns: Company Name, Industry, Symbol, Series, ISIN Code

Reconstitution schedule (NSE semi-annual):
  Cut-off dates : Jan 31 and Jul 31
  Announcement  : ~4 weeks after cut-off  (end of Feb / end of Aug)
  Effective date: ~4 weeks after announcement (end of Mar / end of Sep)
  → Script runs every Monday at 08:00 so no rebalancing is ever missed.

To schedule on AWS Linux via crontab (runs every Monday 08:00 IST = 02:30 UTC):
  crontab -e
  Add this line:
    30 2 * * 1 cd /path/to/EquityAlgo && /path/to/venv/bin/python Algo/momentum/sync_stocks_list.py >> /path/to/logs/midcap150_sync.log 2>&1

Usage:
  python sync_stocks_list.py              # sync; alert + update CSV only if changed
  python sync_stocks_list.py --dry-run    # print diff, no write, no alert
"""

import ssl
import sys
import time
import argparse
import datetime as dt
from io import StringIO
from pathlib import Path

import pandas as pd
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context

requests.packages.urllib3.disable_warnings()

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from Algo.utils.algoutils import sendeqtAlert, sendErrorAlert
from Algo.logger import logger, message_formatter

# ── Config ────────────────────────────────────────────────────────────────────

STOCKS_LIST_PATH = Path(__file__).parent / 'stocks_list.csv'

_BROWSER_HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/124.0.0.0 Safari/537.36'
    ),
    'Accept-Language': 'en-US,en;q=0.9',
    'Accept-Encoding': 'gzip, deflate, br',
    'Connection':      'keep-alive',
}

# ── SSL adapter for sites with TLS compatibility issues (e.g. older NSE hosts) ──

class _LegacyTLSAdapter(HTTPAdapter):
    """Drops OpenSSL security level to 1 so it can handshake with servers
    using older cipher suites (fixes TLSV1_ALERT_INTERNAL_ERROR on NSE hosts)."""
    def init_poolmanager(self, *args, **kwargs):
        ctx = create_urllib3_context()
        ctx.set_ciphers('DEFAULT@SECLEVEL=1')
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        kwargs['ssl_context'] = ctx
        super().init_poolmanager(*args, **kwargs)


# ── Source 1: NSE India JSON API ──────────────────────────────────────────────

def _fetch_from_nse_api() -> list:
    """
    Fetches via the NSE India equity indices API (JSON).
    Requires a primed session (homepage visit first to receive cookies).
    Returns a sorted list of NSE trading symbols.
    """
    headers = {
        **_BROWSER_HEADERS,
        'Accept':  'application/json, text/plain, */*',
        'Referer': 'https://www.nseindia.com/',
    }
    session = requests.Session()
    session.headers.update(headers)
    session.mount('https://', _LegacyTLSAdapter())

    # Prime the session — NSE rejects API calls without a valid session cookie
    session.get('https://www.nseindia.com', timeout=20, verify=False)
    time.sleep(1)

    resp = session.get(
        'https://www.nseindia.com/api/equity-stockIndices?index=NIFTY%20MIDCAP%20150',
        timeout=20, verify=False,
    )
    resp.raise_for_status()
    data = resp.json().get('data', [])

    # First row is the index summary itself (symbol contains spaces like "NIFTY MIDCAP 150")
    symbols = sorted([
        d['symbol'] for d in data
        if d.get('symbol') and ' ' not in d['symbol'].strip()
    ])
    if len(symbols) < 100:
        raise ValueError(f"Only {len(symbols)} symbols returned — response looks incomplete")
    return symbols


# ── Source 2: niftyindices.com CSV ────────────────────────────────────────────

def _fetch_from_niftyindices() -> list:
    """
    Downloads the official constituent CSV from niftyindices.com.
    CSV columns: Company Name, Industry, Symbol, Series, ISIN Code
    Returns a sorted list of NSE trading symbols.
    """
    headers = {
        **_BROWSER_HEADERS,
        'Accept':  'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
        'Referer': 'https://niftyindices.com/',
    }
    session = requests.Session()
    session.headers.update(headers)
    session.mount('https://', _LegacyTLSAdapter())

    # Prime session with the index landing page to get cookies
    session.get(
        'https://niftyindices.com/indices/equity/broad-based-indices/nifty-midcap-150',
        timeout=20, verify=False,
    )
    time.sleep(1)

    resp = session.get(
        'https://niftyindices.com/IndexConstituent/ind_niftymidcap150list.csv',
        timeout=30, verify=False,
    )
    resp.raise_for_status()

    df = pd.read_csv(StringIO(resp.text))
    symbol_col = next(
        (c for c in df.columns if c.strip().lower() in ('symbol', 'ticker')), None
    )
    if symbol_col is None:
        raise ValueError(f"No 'Symbol' column found. Got: {list(df.columns)}")

    symbols = sorted(df[symbol_col].str.strip().tolist())
    if len(symbols) < 100:
        raise ValueError(f"Only {len(symbols)} symbols returned — response looks incomplete")
    return symbols


# ── Orchestrator: try sources in order ───────────────────────────────────────

def fetch_live_symbols() -> list:
    sources = [
        ('NSE India API',    _fetch_from_nse_api),
        ('niftyindices.com', _fetch_from_niftyindices),
    ]
    errors = []
    for name, fn in sources:
        try:
            symbols = fn()
            logger.info(message_formatter(f"Fetched {len(symbols)} symbols via {name}"))
            return symbols
        except Exception as e:
            errors.append(f"{name}: {e}")
            logger.warning(message_formatter(f"Source [{name}] failed: {e}"))

    raise RuntimeError("All sources failed:\n" + "\n".join(errors))


# ── Local CSV helpers ─────────────────────────────────────────────────────────

def load_current_symbols() -> list:
    df = pd.read_csv(STOCKS_LIST_PATH)
    return sorted(df['SYMBOL'].str.strip().tolist())


def save_symbols(symbols: list) -> None:
    pd.DataFrame({'SYMBOL': sorted(symbols)}).to_csv(STOCKS_LIST_PATH, index=False)
    logger.info(message_formatter(f"stocks_list.csv updated → {len(symbols)} symbols"))


# ── Main ──────────────────────────────────────────────────────────────────────

def sync(dry_run: bool = False) -> None:
    today = dt.date.today().isoformat()
    logger.info(message_formatter(f"Nifty Midcap 150 sync started (dry_run={dry_run})"))

    try:
        live = fetch_live_symbols()
    except Exception as e:
        msg = f"[{today}] stocks_list sync FAILED — {e}"
        logger.error(message_formatter(msg))
        sendErrorAlert(msg)
        print(msg)
        return

    current = load_current_symbols()
    added   = sorted(set(live) - set(current))
    removed = sorted(set(current) - set(live))

    if not added and not removed:
        msg = f"[{today}] Nifty Midcap 150 unchanged ({len(live)} stocks)"
        logger.info(message_formatter(msg))
        print(msg)
        return

    lines = [f"NIFTY MIDCAP 150 INDEX UPDATED ({today})"]
    if added:
        lines.append(f"ADDED ({len(added)}):   {', '.join(added)}")
    if removed:
        lines.append(f"REMOVED ({len(removed)}): {', '.join(removed)}")
    lines.append(f"Total stocks now: {len(live)}")
    msg = '\n'.join(lines)

    print(msg)
    logger.info(message_formatter(msg))

    if dry_run:
        print("[dry-run] stocks_list.csv NOT updated. No alert sent.")
        return

    save_symbols(live)
    sendeqtAlert(msg)
    print("stocks_list.csv updated and Telegram alert sent.")


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        description='Sync Nifty Midcap 150 stocks_list.csv from NSE Indices'
    )
    parser.add_argument(
        '--dry-run', action='store_true',
        help='Show diff only — do not write CSV or send Telegram alert'
    )
    args = parser.parse_args()
    sync(dry_run=args.dry_run)
