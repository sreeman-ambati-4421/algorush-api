from kiteconnect import KiteConnect
import sys, os, csv
from copy import deepcopy
import json
from pathlib import Path
from datetime import datetime, timedelta
import time
import pandas as pd  # type: ignore
from dateutil.relativedelta import relativedelta
from collections import OrderedDict

import argparse
from datetime import datetime

sys.path.insert(0, os.path.dirname(Path(__file__).parent.parent))

from Algo.utils.algoutils import getInstrumentsList, gettoken, loadAccessCodes, sendeqtAlert, sendDocument, sendAlertDocument, fetch_flag_accounts
from Algo.utils.creds import ACCOUNTS as cred_account_settings, telegram
from Algo.utils.src_bind import bind_to_source_ip, mount_source_ip, validate_outgoing_ip
from Algo.logger import logger, init_logging, set_file_handler
from Algo.utils.db import (
    read_current_portfolio, write_current_portfolio, write_current_summary,
    read_current_summary, write_exit_stocks, load_strategy_settings,
    save_strategy_settings, ensure_account,
)

STRATEGY = "momentum_etf"

MAX_CAPITAL = 100000
NIFTY_200_CHECK = False
EMA_EXEMPT_ETFS = {'NSE:GOLDBEES','NSE:SILVERBEES'}
LIQUID_ETF = 'NSE:LIQUIDCASE'
BACK_TEST_DAYS = 90
EMA_HISTORY_DAYS = 200
TOP_N_SELECT = 10
TOP_N_INVEST = 5
REBALANCE_COUNTER = 5
SIP_AMOUNT = 0
ADDITIONAL_AMOUNT = 0
IDLE_CASH_THRESHOLD = 25000
BUY_ORDER_RETRIES = 3
CONSOLIDATED_LOG_FILE = ""
TOP_N_SELECT_FILE = ""
EMERGENCY_BACKUP_FILE = ""
CURRENT_ACCOUNT_ID = ""

ORDER_TYPE_LIST = ["MKT", "LMT"]

base_acc = fetch_flag_accounts('Base')[0]

def get_previous_date(input_date, list_of_all_dates):
    """
    Find the most recent valid trading date from the input date.

    Args:
        input_date (datetime): Target date
        list_of_all_dates (list): List of valid trading dates

    Returns:
        datetime: Most recent valid trading date
    """
    logger.debug(f"Searching for valid trading date from: {input_date.strftime('%d-%m-%Y')}")

    max_lookback = 30
    for i in range(max_lookback):
        candidate = input_date - relativedelta(days=i)
        if candidate.strftime("%d-%m-%Y") in list_of_all_dates:
            return candidate
    raise ValueError(
        f"No valid trading date found within {max_lookback} days before {input_date.strftime('%d-%m-%Y')}. Earliest available: {list_of_all_dates[0] if list_of_all_dates else 'N/A'}")


def read_current_portfolio_from_excel(userid):
    """Reads current portfolio state from Postgres (name kept for call-site
    compatibility with the strategy logic below; no Excel involved anymore)."""
    logger.info(f"Reading current portfolio for {userid} ({STRATEGY}) from DB")
    data = read_current_portfolio(userid, STRATEGY)
    logger.info(f"Portfolio loaded successfully - {len(data.get('portfolio', {}))} holdings found")
    return data


def write_current_portfolio_to_excel(data):
    """Writes current portfolio state to Postgres (name kept for call-site
    compatibility)."""
    logger.info(f"Writing portfolio data to DB for {CURRENT_ACCOUNT_ID} ({STRATEGY})")
    write_current_portfolio(CURRENT_ACCOUNT_ID, STRATEGY, data)
    logger.info("Portfolio data successfully written to DB")


def write_current_summary_to_excel(current_stock_summary):
    """Appends current summary data to Postgres (name kept for call-site
    compatibility)."""
    logger.info("Writing current summary to DB")
    write_current_summary(CURRENT_ACCOUNT_ID, STRATEGY, current_stock_summary)
    logger.info("Summary data successfully appended to DB")


def read_current_summary_from_excel():
    """Reads full summary history from Postgres, oldest first (name kept for
    call-site compatibility)."""
    logger.debug("Reading current summary from DB")
    result = read_current_summary(CURRENT_ACCOUNT_ID, STRATEGY)
    logger.debug(f"Retrieved summary history: {len(result)} entries found")
    return result


def write_exit_stocks_to_excel(stocks_deleted, mode_str):
    """Writes exited-stock rows to Postgres (name kept for call-site
    compatibility)."""
    if not stocks_deleted:
        return
    write_exit_stocks(CURRENT_ACCOUNT_ID, STRATEGY, stocks_deleted, mode_str)
    logger.info(f"Exited Stocks data successfully written to DB: {len(stocks_deleted)} row(s)")


class MomentumFinal:
    """
        Main class for implementing momentum-based trading strategy.
    """

    def __init__(self, execution_date_obj, userid, debug_mode):
        """
        Initialize the momentum trading strategy.

        Args:
            execution_date_obj (datetime): Date to execute strategy
            userid (str): User identifier
            debug_mode (bool): Whether to run in debug mode
        """
        global CURRENT_ACCOUNT_ID
        self.debug_mode = debug_mode
        self.userid = userid
        CURRENT_ACCOUNT_ID = userid
        self.client_name = cred_account_settings[userid]['client_name']
        ensure_account(userid, self.client_name, trade_on=cred_account_settings[userid].get('TradeOn', True))

        self.execution_date_str = execution_date_obj.strftime("%d-%m-%Y")
        self.execution_date_obj = execution_date_obj
        self.top_select = None
        self.pivot_df_closing_price = None

        logger.info("Loading access codes to access Kite APIs")
        loadAccessCodes()

        # Initialize Kite connection
        api_key = cred_account_settings.get(userid).get('api_key')
        api_token = cred_account_settings.get(userid).get('access_token')

        logger.info(f"Connecting to Kite API: API Key: {api_key}, Access Token: {api_token}")
        self.kite = KiteConnect(api_key=api_key)
        self.kite.set_access_token(api_token)
        mount_source_ip(self.kite, cred_account_settings.get(userid, {}).get('source_ip'), userid)
        self.instruments = getInstrumentsList(self.kite)

        self.get_stock_history()

        logger.info("Kite API connection established successfully")

        logger.info(f"Initializing ETF Momentum Trading Strategy for User ID: {userid}")
        logger.info(f"Execution Date: {execution_date_obj.strftime('%d-%m-%Y')}")
        logger.info(f"Debug Mode: {'ON' if debug_mode else 'OFF'}")
        if userid not in cred_account_settings:
            logger.error(f"Account details not found for user: {userid}")
            raise Exception("Account details are not present..")

        self.momentum_settings = load_strategy_settings(STRATEGY)
        if userid not in self.momentum_settings:
            raise Exception(f"{userid} key not present in strategy_settings table for strategy '{STRATEGY}'")

        # Load configuration
        # Reset AND Rebalance
        self.reset_and_fresh_rebalance = self.momentum_settings[userid].get("reset_and_rebalance", False)
        self.rebalance_today = self.momentum_settings[userid].get("rebalance_today", False)

        self.maximum_capital = self.momentum_settings[userid].get("initial_capital", MAX_CAPITAL)
        if self.reset_and_fresh_rebalance is False:
            self.sip_amount = self.momentum_settings[userid].get("sip_amount", SIP_AMOUNT)
            self.additional_amount = self.momentum_settings[userid].get("additional_capital", ADDITIONAL_AMOUNT)
            logger.info(f"Additional Amount: {self.additional_amount:,.2f}")
        else:
            logger.info(f"RESET and REFRESH BALANCES IS SET TO TRUE")
            self.sip_amount = 0
            self.additional_amount = 0

        # Reset additional_capital to ZERO
        self.momentum_settings[userid]["additional_capital"] = 0
        self.momentum_settings[userid]["reset_and_rebalance"] = False
        self.momentum_settings[userid]["rebalance_today"] = False

        save_strategy_settings(STRATEGY, self.momentum_settings)

        logger.info(f"Maximum Capital: {self.maximum_capital:,.2f}")
        logger.info(f"SIP Amount: {self.sip_amount:,.2f}")
        logger.info(f"Additional Amount: {self.additional_amount:,.2f}")

        self.sip_amount_added = 0
        self.additional_amount_added = 0
        self._ema_cache = {}

        logger.info("Loading existing portfolio data...")
        current_portfolio_data_from_excel = read_current_portfolio_from_excel(userid)

        self.last_executed_date = current_portfolio_data_from_excel.get("Executed_Date", None)
        self.rebalance_counter = 1 if self.rebalance_today else current_portfolio_data_from_excel.get(
            "Rebalance_Counter", 1)
        self.portfolio = current_portfolio_data_from_excel.get("portfolio", OrderedDict())
        self.cash_remaining = current_portfolio_data_from_excel.get("Cash_Remaining", self.maximum_capital)

        logger.info(f"Previous Execution: {self.last_executed_date or 'None'}")
        logger.info(f"Rebalance Counter: {self.rebalance_counter}")
        logger.info(f"Current Holdings: {len(self.portfolio)}")
        logger.info(f"Cash Remaining: {self.cash_remaining:,.2f}")
        sendeqtAlert(f"{self.client_name} - ETF Holdings: {len(self.portfolio)} | Cash: {self.cash_remaining:,.0f} | Rebal: {self.rebalance_counter}")

        current_summary = read_current_summary_from_excel()
        self.all_executed_dates = list(current_summary.keys())
        current_summary_from_excel = list(current_summary.values())[-1] if current_summary else {}
        self.current_holdings_value = current_summary_from_excel.get('Total_Value_Holdings', 0)
        self.current_invested_capital = current_summary_from_excel.get('Invested_Capital', 0)

    def get_stock_history(self):
        """
                Fetch and prepare historical stock data for analysis.
                """
        logger.info("Preparing stock history data...")
        stock_history_path = f"Stock_History_Files"
        if not os.path.isdir(stock_history_path):
            os.mkdir(stock_history_path, mode=0o777)
            logger.debug(f"Created directory: {stock_history_path}")

        stock_history_file = os.path.join(stock_history_path, f'stock_history_{self.execution_date_str}.csv')
        if not os.path.exists(stock_history_file):
            logger.info(
                f"Zerodha stock history for {self.execution_date_str} does not exist.. Creating it now...")

            nifty_200_file = os.path.join(os.path.dirname(Path(__file__)), "etf_list.csv")
            df = pd.read_csv(nifty_200_file)

            stock_list = ["NSE:" + df["SYMBOL"][ind] for ind in df.index]
            stock_list.append(LIQUID_ETF)  # price data needed for parking logic

            # Use the ABB stock to get the initail date:
            stock = "NSE:NIFTY MIDCAP 150"

            token = gettoken(stock, self.instruments)
            logger.info(f"ZERODHA Fetch success for {stock}: {token}")

            stock_history = self.kite.historical_data(
                token, self.execution_date_obj - timedelta(days=EMA_HISTORY_DAYS), self.execution_date_obj, "day")

            if stock_history[-1]['date'].strftime('%d-%m-%Y') != self.execution_date_str:
                raise Exception(
                    "MARKETS ARE NOT OPEN. SCRIPT WILL NOT BE EXECUTED !")

            all_dates = [stock['date'].strftime('%d-%m-%Y') for stock in stock_history]
            last_year_date = self.execution_date_obj - relativedelta(days=EMA_HISTORY_DAYS)
            earliest_available = datetime.strptime(all_dates[0], "%d-%m-%Y")
            if last_year_date < earliest_available:
                last_year_date = earliest_available
            history_start_date = get_previous_date(last_year_date, all_dates)
            history_end_date = self.execution_date_obj

            failed_stocks = []
            stock_list.insert(0, "NSE:NIFTY MIDCAP 150")
            total_stock_history = {}
            for index, stock in enumerate(stock_list):
                try:
                    token = gettoken(stock, self.instruments)
                    stock_history = self.kite.historical_data(
                        token, history_start_date, history_end_date, "day"
                    )
                    if stock_history[0]["date"].strftime('%d-%m-%Y') == history_start_date.strftime('%d-%m-%Y'):
                        logger.info(
                            f"Index: {index + 1} Fetch success for {stock}: {len(stock_history)}"
                        )
                        total_stock_history[stock] = stock_history
                    else:
                        logger.info(
                            f"Index: {index + 1} Fetch failed for {stock}: Too latest: {str(stock_history[0]['date']).split()[0]}"
                        )
                        failed_stocks.append(stock)
                except Exception as e:
                    logger.info(
                        f"Index: {index + 1} Fetch failed for {stock}: Exception: {e}"
                    )
                    failed_stocks.append(stock)

            _tmp = stock_history_file + '.tmp'
            with open(_tmp, mode="w", newline="") as file:
                writer = csv.writer(file)
                writer.writerow(["Date", "Stock", "Open", "High", "Low", "Close"])
                for stock, stock_history in total_stock_history.items():
                    for entry in stock_history:
                        writer.writerow([
                            entry["date"].strftime("%Y-%m-%d"),
                            stock,
                            entry["open"],
                            entry["high"],
                            entry["low"],
                            entry["close"],
                        ])
            os.replace(_tmp, stock_history_file)

        stock_values_data_frame = pd.read_csv(stock_history_file, usecols=['Date', 'Stock', 'Close'])
        stock_values_data_frame["Date"] = pd.to_datetime(stock_values_data_frame["Date"], format="%Y-%m-%d")

        self.pivot_df_closing_price = stock_values_data_frame.pivot(
            index="Date", columns="Stock", values="Close"
        )
        del stock_values_data_frame

        self.pivot_df_closing_price = self.pivot_df_closing_price.sort_index()
        self.pivot_df_closing_price.index = self.pivot_df_closing_price.index.strftime("%d-%m-%Y")
        self.all_dates = list(self.pivot_df_closing_price.index)

    def get_pivot_df_closing_price(self, date=None, stock=None):
        """

        :param date:
        :param stock:
        :return:
        """
        try:
            if stock is not None and date is None:
                return self.pivot_df_closing_price[stock]
            elif stock is None and date is not None:
                return round(self.pivot_df_closing_price.loc[date], 2)
            elif stock is not None and date is not None:
                return round(self.pivot_df_closing_price.loc[date, stock], 2)
            else:
                raise Exception("Provide stock or date as input !")
        except KeyError:
            return 0

    def get_last_price(self, stock_list, debug_mode=True):
        """
        Get current prices for a list of stocks.

        Args:
            stock_list (list): List of stock symbols
            debug_mode (bool): Whether to use historical data or live prices

        Returns:
            dict: Stock prices dictionary
        """
        today_str = datetime.today().strftime("%d-%m-%Y")
        if not stock_list:
            return {}

        logger.debug(f"Fetching current prices for {len(stock_list)} stocks")
        if debug_mode or today_str != self.execution_date_str:
            prices = {}
            for stock in stock_list:
                prices.update(
                    {
                        stock: {
                            "last_price": round(self.get_pivot_df_closing_price(
                                date=self.execution_date_str, stock=stock
                            ), 2)
                        }
                    }
                )
            logger.debug("Using historical prices (debug mode)")
        else:
            prices = self.kite.ohlc(stock_list)
            for price in prices.values():
                price['last_price'] = round(price['last_price'], 2)
            logger.debug("Fetched live prices from API")
        return prices

    def get_order_status(self, iv_v_order_id, limitprice, debug_mode=True):
        """
        Check the status of a placed order.

        Args:
            iv_v_order_id: Order ID to check
            limitprice (float): Limit price of the order
            debug_mode (bool): Whether to simulate order completion

        Returns:
            tuple: (order_status, order_price)
        """
        logger.debug(f"Fetching status for {iv_v_order_id} {limitprice}")
        order_status = "PENDING"
        order_price = 0
        try:
            if debug_mode:
                order_status = "COMPLETE"
                order_price = limitprice
            else:
                order_book = self.kite.orders()
                order_df = pd.DataFrame(order_book)
                order_df = order_df[(order_df["order_id"] == str(iv_v_order_id))]
                order_status = str(order_df["status"][order_df.index[0]])
                if (
                        order_status == "CANCELLED"
                        or order_status == "REJECTED"
                        or order_status == "CANCELED"
                ):
                    order_price = 0
                    order_status = "REJECTED"
                elif order_status == "COMPLETE":
                    order_price = order_df["average_price"][order_df.index[0]]
                    order_status = "COMPLETE"
                elif order_status == "OPEN" or order_status == "TRIGGER PENDING":
                    order_status = "PENDING"
                    order_price = 1
                    time.sleep(0.5)
                logger.info(f"id {iv_v_order_id} {order_status}, order_price: {order_price}")
        except Exception as error:
            logger.error(f"Error fetching order status: {error}")
        return order_status, order_price

    def place_order(
            self,
            exchange,
            tran_type,
            symbol,
            ordertype,
            iv_quantity,
            limitprice,
            debug_mode=True,
    ):
        logger.debug(f"DEBUG MODE FLAG: {'ENABLED' if debug_mode else 'DISABLED'}")
        if exchange == "NSE":
            order_exchange = self.kite.EXCHANGE_NSE
        elif exchange == "BSE":
            order_exchange = self.kite.EXCHANGE_BSE
        else:
            raise Exception(f"Invalid order exchange specified: {exchange}")

        if tran_type == "BUY":
            tran_type = self.kite.TRANSACTION_TYPE_BUY
            limitprice = round(limitprice + limitprice * 0.04) if not self.debug_mode else round(limitprice)
        elif tran_type == "SELL":
            tran_type = self.kite.TRANSACTION_TYPE_SELL
            limitprice = round(limitprice - limitprice * 0.04) if not self.debug_mode else round(limitprice)
        else:
            raise Exception(f"Invalid transaction type specified: {tran_type}")

        if not self.debug_mode:
            if ordertype == "MKT":
                order_type = self.kite.ORDER_TYPE_MARKET
                limitprice = 0
            elif ordertype == "LMT":
                order_type = self.kite.ORDER_TYPE_LIMIT
                limitprice = limitprice
            else:
                raise Exception(f"Invalid order type specified: {ordertype}")

        product = self.kite.PRODUCT_CNC
        variety = self.kite.VARIETY_REGULAR
        tag = "momentum"
        status_message = ""
        order_id = 0
        lv_symbol = symbol.split(":")[1]
        for retry_count in range(3):
            try:
                logger.debug(
                    f"Initiate order: {lv_symbol}, {iv_quantity}, {ordertype}, {limitprice}, {tag}"
                )
                if not debug_mode:
                    if ordertype == "MKT":
                        order_id = self.kite.place_order(
                            tradingsymbol=lv_symbol,
                            exchange=order_exchange,
                            transaction_type=tran_type,
                            quantity=iv_quantity,
                            variety=variety,
                            order_type=order_type,
                            product=product,
                            tag=tag,
                            market_protection=-1,
                        )
                    if ordertype == "LMT":
                        order_id = self.kite.place_order(
                            tradingsymbol=lv_symbol,
                            exchange=order_exchange,
                            transaction_type=tran_type,
                            quantity=iv_quantity,
                            variety=variety,
                            order_type=order_type,
                            product=product,
                            price=limitprice,
                            trigger_price=None,
                            tag=tag,
                        )

                    time.sleep(0.3)

                # Timeout to prevent infinite loop on stuck orders
                max_wait_seconds = 60
                poll_start = time.time()
                while True:
                    status, average_price = self.get_order_status(
                        order_id, limitprice, debug_mode=debug_mode
                    )
                    if status == "REJECTED" or status == "COMPLETE":
                        break
                    if time.time() - poll_start > max_wait_seconds:
                        logger.warning(f"Order {order_id} still PENDING after {max_wait_seconds}s - treating as REJECTED")
                        sendeqtAlert(f"{self.client_name} - Order TIMEOUT: {symbol} pending > {max_wait_seconds}s")
                        status = "REJECTED"
                        average_price = 0
                        status_message = f"Order timeout after {max_wait_seconds}s"
                        break
                    time.sleep(0.5)

            except Exception as error:
                logger.error(f"Error placing order: {error}")
                order_id = 1
                average_price = 0
                status = "REJECTED"
                # str() for reliable string comparison
                status_message = str(error)

            if status == 'REJECTED' and "Insufficient funds" in status_message and iv_quantity > 2:
                logger.info("Decreasing the no of shares to buy by 1.")
                iv_quantity -= 1
            else:
                return order_id, iv_quantity, average_price, status, status_message

        # Return REJECTED instead of raising exception.
        # All callers already handle REJECTED gracefully - stock is simply skipped.
        logger.error(f"Order failed after 3 retries for {symbol}")
        sendeqtAlert(f"{self.client_name} - ORDER FAILED after 3 retries: {symbol}")
        return 0, iv_quantity, 0, "REJECTED", "Max retries exceeded"

    def place_sell_order(self,
                         exchange,
                         stock,
                         shares,
                         sellprice,
                         debug_mode):

        try:
            ordertype = ORDER_TYPE_LIST[0] # FIRST OPTION MKT ORDER.
            order_id, shares, average_price, status, status_message = self.place_order(
                exchange, "SELL", stock, ordertype, shares, sellprice, debug_mode
            )
            if status != 'COMPLETE':
                raise Exception(status_message)
            return order_id, shares, average_price, status, status_message
        except Exception:
            ordertype = ORDER_TYPE_LIST[1] # SECOND OPTION LMT ORDER BY DEFAULT
            order_id, shares, average_price, status, status_message = self.place_order(
                exchange, "SELL", stock, ordertype, shares, sellprice, debug_mode
            )
            if status != 'COMPLETE':
                raise Exception(status_message)
            return order_id, shares, average_price, status, status_message

    def place_buy_order(self,
                         exchange,
                         stock,
                         shares,
                         sellprice,
                         debug_mode):

        try:
            ordertype = ORDER_TYPE_LIST[0] # FIRST OPTION MKT ORDER.
            order_id, shares, average_price, status, status_message = self.place_order(
                exchange, "BUY", stock, ordertype, shares, sellprice, debug_mode
            )
            if status != 'COMPLETE':
                raise Exception(status_message)
            return order_id, shares, average_price, status, status_message
        except Exception:
            ordertype = ORDER_TYPE_LIST[1] # SECOND OPTION LMT ORDER BY DEFAULT
            order_id, shares, average_price, status, status_message = self.place_order(
                exchange, "BUY", stock, ordertype, shares, sellprice, debug_mode
            )
            if status != 'COMPLETE':
                logger.error(f"LMT buy order also failed for {stock}: {status_message}")
                return 0, shares, 0, "REJECTED", status_message
            return order_id, shares, average_price, status, status_message

    def buy_stocks_or_not(self):
        if NIFTY_200_CHECK:
            nifty_200_closing_price = self.get_pivot_df_closing_price(
                date=self.execution_date_str, stock="NSE:NIFTY MIDCAP 150"
            )
            nifty_200_ema = self.get_ema_for_date(
                "NSE:NIFTY MIDCAP 150", self.execution_date_str, 200
            )
            market_condition = "BULLISH" if nifty_200_closing_price > nifty_200_ema else "BEARISH"
            logger.info(f"Market Analysis (NIFTY 200):")
            logger.info(f"Current Price: {nifty_200_closing_price:,.2f}")
            logger.info(f"200-Day EMA: {nifty_200_ema:,.2f}")
            logger.info(f"Market Condition: {market_condition}")
            sendeqtAlert(f"{self.client_name} - ETF Market: {market_condition} (MC150: {nifty_200_closing_price:.0f} vs EMA: {nifty_200_ema:.0f})")
            return True if nifty_200_closing_price > nifty_200_ema else False

        else:
            logger.info("NIFTY 200 check disabled - proceeding with buy orders")
            return True

    def get_ema_for_date(self, stock_name, target_date, period=100):
        cache_key = (stock_name, str(target_date), period)
        if cache_key in self._ema_cache:
            return self._ema_cache[cache_key]

        target_date = pd.to_datetime(target_date, format="%d-%m-%Y")

        if period == 200:
            token = gettoken(stock_name, self.instruments)
            hist = self.kite.historical_data(
                token, target_date - timedelta(days=900), target_date, "day"
            )
            closes = pd.Series([d["close"] for d in hist])
            result = round(closes.ewm(span=period, adjust=False).mean().iloc[-1], 2)
            self._ema_cache[cache_key] = result
            return result

        series = self.get_pivot_df_closing_price(stock=stock_name)
        if isinstance(series, pd.Series):
            series.index = pd.to_datetime(series.index, format="%d-%m-%Y")
            filtered_series = series[series.index <= target_date]
            result = round(filtered_series.ewm(span=period, adjust=False).mean().iloc[-1], 2)
            self._ema_cache[cache_key] = result
            return result
        self._ema_cache[cache_key] = 0.0
        return 0.0

    def populate_stock_top_select_and_invest_data(self):
        """
         Identify top performing stocks based on price appreciation over BACK_TEST_DAYS.
         Creates lists of stocks to invest in and stocks to monitor.
         """
        logger.info("STARTING TOP STOCK SELECTION PROCESS")
        logger.info("-" * 60)
        last_year_date = self.execution_date_obj - relativedelta(days=BACK_TEST_DAYS)
        back_date = get_previous_date(last_year_date, self.all_dates)
        back_date_str = back_date.strftime("%d-%m-%Y")

        logger.info(f"Comparison Period: {back_date_str} to {self.execution_date_str}")
        logger.info(f"Looking back {BACK_TEST_DAYS} days for price appreciation analysis")
        today_prices = self.get_pivot_df_closing_price(date=self.execution_date_str)
        back_prices = self.get_pivot_df_closing_price(date=back_date_str)

        valid_stocks = today_prices[today_prices < 20000].index
        today_prices = today_prices.loc[valid_stocks]
        back_prices = back_prices.loc[valid_stocks]

        logger.info(f"Filtering stocks under 20,000: {len(valid_stocks)} stocks qualify")

        # adjusted_today_prices = round(today_prices + today_prices * 0.02, 2)
        appreciation = ((today_prices - back_prices) / back_prices) * 100
        appreciation = appreciation.dropna()

        appreciation = appreciation[(appreciation > 0) | appreciation.index.isin(EMA_EXEMPT_ETFS)]
        appreciation = appreciation[appreciation.index != LIQUID_ETF]

        logger.info(f"Calculated appreciation for {len(appreciation)} stocks")

        self.top_select = appreciation.sort_values(ascending=False).head(TOP_N_SELECT)

        # TO ROLL BACK CHANGE, TOP_N_SELECT TO TOP_N_INVEST
        self.top_invest_list = self.top_select.head(TOP_N_SELECT).index.tolist()

        self.top_check_list = self.top_select.head(TOP_N_SELECT).index.tolist()

        logger.info(f"Selected Top {TOP_N_SELECT} stocks for monitoring")
        logger.info(f"Selected Top {TOP_N_INVEST} stocks for investment")

        logger.info("TOP STOCK SELECTION RESULTS:")
        select_str = f"{'Stock':<15} {'Buy Price':<12} {'Back Date':<12} " \
                     f"{'Back Price':<12} {'Appreciation %':<18} {'Selected':<10}"
        logger.info(select_str)
        logger.info("-" * 90)
        select_content = select_str + "\n" + "-" * 90 + "\n"
        for stock in self.top_select.index:
            buy_price = today_prices.loc[stock]
            back_price = back_prices.loc[stock]
            appreciation_percentage = str(round(
                ((buy_price - back_price) / back_price) * 100, 2
            ))
            selected_marker = "(*)" if stock in self.top_invest_list else ""
            select_str = f"{stock:<15} {buy_price:<12} {back_date_str:<12} {back_price:<12} " \
                         f"{appreciation_percentage + ' %':<18} {selected_marker:<10}"
            logger.info(select_str)
            select_content = select_content + select_str + "\n"

        _tmp = TOP_N_SELECT_FILE + '.tmp'
        with open(_tmp, "w") as fp:
            fp.write(select_content)
        os.replace(_tmp, TOP_N_SELECT_FILE)

        sendDocument(TOP_N_SELECT_FILE)
        logger.info("-" * 90)
        logger.info("TOP STOCK SELECTION COMPLETED")
        if base_acc == self.userid:
            sendAlertDocument(TOP_N_SELECT_FILE)

    def sell_stocks_and_reclaim_capital(self, stocks_to_sell):
        """
        Sell specified stocks and add proceeds to available cash.

        Args:
            stocks_to_sell (dict): Dictionary of stocks to sell with their prices

        Returns:
            OrderedDict: Details of stocks that were sold
        """

        logger.info("STARTING STOCK SELLING PROCESS")
        logger.info(f"Number of ETFs to sell: {len(stocks_to_sell)}")
        logger.info("-" * 60)

        stocks_deleted = OrderedDict()
        current_stock_prices = self.get_last_price(list(stocks_to_sell.keys()), debug_mode=self.debug_mode)

        total_proceeds = 0.0
        logger.info(f"{'Stock':<15} {'Shares':<10} {'Buy Price':<12} {'Sell Price':<12} {'Proceeds':<12} {'P&L':<12}")
        logger.info("-" * 80)

        for stock in stocks_to_sell:
            sell_price = current_stock_prices[stock]["last_price"]
            # if not pd.isna(current_price):
            shares = int(self.portfolio[stock]["No_Of_Shares"])
            order_id, shares, average_price, status, status_message = self.place_sell_order(
                "NSE", stock, shares, sell_price, self.debug_mode
            )
            if status == 'COMPLETE':
                proceeds = round(shares * average_price, 2)
                self.cash_remaining += proceeds
                buy_amount = round(
                    self.portfolio[stock]["Buy_Price"]
                    * self.portfolio[stock]["No_Of_Shares"],
                    2,
                )
                profit_loss = round(proceeds - buy_amount, 2)
                entry_date_obj = datetime.strptime(
                    self.portfolio[stock]["Entry_Date"], "%d-%m-%Y"
                )
                stocks_deleted[stock] = {
                    "Entry_Date": self.portfolio[stock]["Entry_Date"],
                    "Exit_Date": self.execution_date_str,
                    'Holding_Days': (self.execution_date_obj - entry_date_obj).days + 1,
                    "No_Of_Shares": shares,
                    "Buy_Price": self.portfolio[stock]["Buy_Price"],
                    "Buy_Amount": buy_amount,
                    "Sell_Price": sell_price,
                    "Sell_Amount": proceeds,
                    "100_Days_EMA": self.get_ema_for_date(stock, self.execution_date_str),
                    "Profit_Loss": profit_loss,
                    "Percentage": round((profit_loss / buy_amount) * 100, 2),
                }
                logger.info(
                    f"{stock:<15} {shares:<10} {self.portfolio[stock]['Buy_Price']:<11} {average_price:<11} {proceeds:<11} {profit_loss:<11}"
                )
                del self.portfolio[stock]
            else:
                logger.error(f"{'':<20} {stock:<15} Cannot be sold at this moment.. Order Rejected")
                sendeqtAlert(f"{self.client_name} - ETF SELL REJECTED: {stock} - {status_message}")

        logger.info("-" * 80)
        logger.info(f"Total proceeds from sales: {total_proceeds:,.2f}")
        logger.info(f"Updated cash remaining: {self.cash_remaining:,.2f}")

        # Alert with sold ETF names and P&L
        if stocks_deleted:
            sold_details = [f"{s.replace('NSE:', '')}({d['Profit_Loss']:,.0f})" for s, d in stocks_deleted.items()]
            total_sell_pnl = sum(d['Profit_Loss'] for d in stocks_deleted.values())
            sendeqtAlert(
                f"{self.client_name} - ETF Sold {len(stocks_deleted)}: "
                f"{', '.join(sold_details)} | Total PnL: {total_sell_pnl:,.0f} | Cash: {self.cash_remaining:,.0f}"
            )

        logger.info("STOCK SELLING COMPLETED")
        return stocks_deleted

    def get_stocks_to_sell_at_rebalance(self):
        """
        Identify stocks in portfolio that are not in the current top selection
        and should be sold during rebalancing.

        Returns:
            OrderedDict: Stocks to sell with their current prices
        """
        logger.info("IDENTIFYING STOCKS TO SELL DURING REBALANCE")
        stocks_to_sell = OrderedDict()
        portfolio_stocks = list(self.portfolio.keys())

        logger.info(f"Current portfolio stocks: {len(portfolio_stocks)}")
        logger.info(f"Current top check list: {len(self.top_check_list)} stocks")

        top_invest_indices = self.top_check_list[:TOP_N_INVEST]

        for stock in portfolio_stocks:
            if stock == LIQUID_ETF:
                logger.info(f"{stock} is parking vehicle - skipping rebalance sell")
                continue
            if stock in EMA_EXEMPT_ETFS:
                if stock not in top_invest_indices:
                    current_price = self.get_pivot_df_closing_price(date=self.execution_date_str, stock=stock)
                    stocks_to_sell[stock] = current_price
                    logger.info(f"{stock} EMA-exempt but outside top {TOP_N_INVEST} - marked for sale at {current_price}")
                else:
                    logger.info(f"{stock} EMA-exempt and in top {TOP_N_INVEST} - keeping")
                continue
            if stock not in self.top_check_list:
                current_price = self.get_pivot_df_closing_price(date=self.execution_date_str, stock=stock)
                stocks_to_sell[stock] = current_price
                logger.info(f"{stock} not in top list - marked for sale at {current_price}")
            else:
                logger.info(f"{stock} still in top list - keeping")
        logger.info(f"Total stocks to sell: {len(stocks_to_sell)}")
        return stocks_to_sell

    def get_stocks_to_buy(self, remaining_slots):
        """
        Identify new stocks to buy based on available slots and EMA criteria.

        Args:
            remaining_slots (int): Number of new stock positions to fill

        Returns:
            OrderedDict: Stocks to buy with investment amounts
        """
        logger.info("IDENTIFYING STOCKS TO BUY")
        logger.info(f"Available slots to fill: {remaining_slots}")
        logger.info("-" * 60)
        stocks_to_buy = OrderedDict()

        if remaining_slots <= 0:
            logger.info("No remaining slots for new investments")
            return stocks_to_buy

        investment_per_stock = self.cash_remaining / remaining_slots
        logger.info(f"Investment per stock: {investment_per_stock:,.2f}")
        logger.info(f"Available cash: {self.cash_remaining:,.2f}")
        sendeqtAlert(f"{self.client_name} - ETF Cash: {self.cash_remaining:,.0f} | Slots: {remaining_slots}")

        logger.info(f"{'Stock':<15} {'Current Price':<14} {'100-Day EMA':<14} {'Status':<10} {'Decision':<10}")
        logger.info("-" * 75)

        for stock in self.top_invest_list:
            if stock in self.portfolio.keys():
                logger.info(f"{stock:<15} {'Already owned':<14} {'N/A':<14} {'OWNED':<10} {'SKIP':<10}")
                continue

            raw_price = self.get_pivot_df_closing_price(date=self.execution_date_str, stock=stock)

            if stock in EMA_EXEMPT_ETFS:
                stocks_to_buy[stock] = investment_per_stock
                logger.info(f"{stock:<15} {raw_price:<13} {'N/A':<13} {'EMA EXEMPT':<10} {'BUY':<10}")
                if len(stocks_to_buy) == remaining_slots:
                    logger.info("All slots filled")
                    break
                continue

            ema_value = self.get_ema_for_date(stock, self.execution_date_str)

            if round(raw_price, 2) <= round(ema_value, 2):
                status = "BELOW EMA"
                decision = "SKIP"
                logger.info(f"{stock:<15} {raw_price:<13} {ema_value:<13} {status:<10} {decision:<10}")
            else:
                stocks_to_buy[stock] = investment_per_stock
                status = "ABOVE EMA"
                decision = "BUY"
                logger.info(f"{stock:<15} {raw_price:<13} {ema_value:<13} {status:<10} {decision:<10}")

            if len(stocks_to_buy) == remaining_slots:
                logger.info("All slots filled")
                break

        logger.info("-" * 75)
        logger.info(f"Selected {len(stocks_to_buy)} ETFs for purchase")
        logger.info("STOCK SELECTION FOR BUYING COMPLETED")

        return stocks_to_buy

    def buy_stocks_and_spend_capital(self, stocks_to_buy):
        """
        Execute buy orders for selected stocks.

        Args:
            stocks_to_buy (dict): Dictionary of stocks to buy with investment amounts

        Returns:
            OrderedDict: Details of stocks that were purchased
        """

        if not stocks_to_buy:
            logger.info("No stocks to buy")
            return OrderedDict()

        logger.info("STARTING STOCK BUYING PROCESS")
        logger.info("-" * 60)

        stocks_added = OrderedDict()
        current_stock_prices = self.get_last_price(list(stocks_to_buy.keys()), debug_mode=self.debug_mode)

        etf_count = sum(1 for s in self.portfolio if s != LIQUID_ETF)
        remaining_slots = TOP_N_INVEST - etf_count
        investment_per_stock = self.cash_remaining / remaining_slots
        logger.info(f"Investment per stock: {investment_per_stock:,.2f}")

        logger.info(f"{'Stock':<15} {'Price':<12} {'Shares':<10} {'Amount':<12} {'Status':<15}")
        logger.info("-" * 70)
        total_invested = 0.0
        for stock in stocks_to_buy.keys():
            buy_price = current_stock_prices[stock]["last_price"]
            shares = round(investment_per_stock / buy_price)

            if shares > 0:
                order_id, shares, average_price, status, status_message = self.place_buy_order(
                    "NSE", stock, shares, buy_price, self.debug_mode
                )
                if status == 'COMPLETE':
                    spent = round(shares * average_price, 2)
                    self.cash_remaining -= spent
                    total_invested += spent
                    # Update portfolio
                    self.portfolio[stock] = {
                        "No_Of_Shares": shares,
                        "Current_Price": round(average_price, 2),
                        "Current_Amount": spent,
                        "Buy_Price": round(average_price, 2),
                        "Buy_Amount": spent,
                        "Entry_Date": self.execution_date_str,
                        "Holding_Days": 1,
                    }
                    stocks_added[stock] = {
                        "No_Of_Shares": shares,
                        "Buy_Price": self.portfolio[stock]["Buy_Price"],
                        "Buy_Amount": spent,
                    }
                    logger.info(f"{stock:<15} {average_price:<11} {shares:<10} {spent:<11} {'SUCCESS':<15}")
                else:
                    logger.error(f"{'':<20} {stock:<15} Cannot be bought at this moment.. Order Rejected")
                    sendeqtAlert(f"{self.client_name} - ETF BUY REJECTED: {stock} - {status_message}")
            else:
                logger.info(f"{stock:<15} {buy_price:<11} {shares:<10} 0.00<11 {'NO SHARES':<15}")

        logger.info("-" * 70)
        logger.info(f"Total amount invested: {total_invested:,.2f}")
        logger.info(f"Remaining cash: {self.cash_remaining:,.2f}")

        # Alert with bought ETF names
        if stocks_added:
            bought_names = [s.replace('NSE:', '') for s in stocks_added.keys()]
            sendeqtAlert(
                f"{self.client_name} - ETF Bought {len(stocks_added)}: "
                f"{', '.join(bought_names)} | Invested: {total_invested:,.0f} | Cash: {self.cash_remaining:,.0f}"
            )

        logger.info("STOCK BUYING COMPLETED")
        return stocks_added

    def get_portfolio_stocks_fell_below_ema(self):
        """
        Identify portfolio stocks that have fallen below their 100-day EMA
        and should be sold.

        Returns:
            OrderedDict: Stocks that fell below EMA with their current prices
        """
        logger.info("CHECKING PORTFOLIO STOCKS AGAINST EMA")
        logger.info("-" * 60)
        stocks_to_sell = OrderedDict()
        for stock in self.portfolio:
            if stock == LIQUID_ETF or stock in EMA_EXEMPT_ETFS:
                logger.info(f"Stock: {stock:<15}  EMA check skipped (parking/exempt)")
                continue
            ema_average = self.get_ema_for_date(
                stock,
                self.execution_date_str,
            )
            raw_price = self.get_pivot_df_closing_price(date=self.execution_date_str, stock=stock)
            if round(raw_price, 2) <= round(ema_average, 2):
                logger.info(
                    f"Stock: {stock:<15}  Date: {self.execution_date_str:<12}: 100 Days EMA: {ema_average:<12}, "
                    f"Current Price: {raw_price:<12}   BELOW"
                )
                stocks_to_sell[stock] = self.get_pivot_df_closing_price(date=self.execution_date_str, stock=stock)
            else:
                logger.info(
                    f"Stock: {stock:<15}  Date: {self.execution_date_str:<12}: 100 Days EMA: {ema_average:<12}, "
                    f"Current Price: {raw_price:<12}   ABOVE"
                )

        logger.info("-" * 75)
        logger.info(f"ETFs to sell due to EMA breach: {len(stocks_to_sell)}")

        if stocks_to_sell:
            # Alert only when there are actual EMA breaches with ETF names
            ema_stocks_str = ", ".join([s.replace("NSE:", "") for s in stocks_to_sell.keys()])
            sendeqtAlert(f"{self.client_name} - ETF EMA BREACH ({len(stocks_to_sell)}): {ema_stocks_str}")
            logger.info("EMA BREACH DETECTED - PREPARING TO SELL")
        else:
            sendeqtAlert(f"{self.client_name} - ETF EMA check done. No breach. {len(self.portfolio)} holdings safe")
            logger.info("All portfolio ETFs above EMA - no immediate sells needed")

        return stocks_to_sell

    def sell_stocks_which_fell_below_ema(self):
        logger.info("=" * 100)
        stocks_to_sell = self.get_portfolio_stocks_fell_below_ema()
        old_stocks_deleted = self.sell_stocks_and_reclaim_capital(stocks_to_sell)
        logger.info("=" * 100)
        logger.info(f"Stocks to let go: {stocks_to_sell}")

        if old_stocks_deleted:
            logger.info(
                f"{'Stock':<15} {'Holding Shares':<15} {'Buy Price':<14} {'Buy Amount':<14} {'Sell Price':<14} {'Sell Amount':<14} {'Profit Loss %':<17}"
            )
            for stock, stock_value in old_stocks_deleted.items():
                shares = old_stocks_deleted[stock]["No_Of_Shares"]
                buy_price = old_stocks_deleted[stock]["Buy_Price"]
                buy_amount = old_stocks_deleted[stock]["Buy_Amount"]
                sell_price = old_stocks_deleted[stock]["Sell_Price"]
                sell_amount = old_stocks_deleted[stock]["Sell_Amount"]
                profit_loss = old_stocks_deleted[stock]["Profit_Loss"]
                logger.info(
                    f"{stock:<15} {shares:<15} {buy_price:<14} {buy_amount:<14} {sell_price:<14} {sell_amount:<14} {profit_loss:<17}"
                )
        logger.info("=" * 100)
        return old_stocks_deleted

    def analyze_current_portfolio(self):
        logger.info("=" * 100)

        logger.info(
            f"{'Stock':<15} {'Holdings':<10} {'Buy Day Price':<16} {'Buy Day Value':<16} {'Current Price':<16} {'Current Value':<16} {'Total Profit/Loss':<17}"
        )
        current_stock_prices = self.get_last_price(list(self.portfolio.keys()), debug_mode=self.debug_mode)
        for stock in self.portfolio:
            shares = self.portfolio[stock]["No_Of_Shares"]

            raw_price = current_stock_prices[stock]["last_price"]
            today_price = round(raw_price, 2)
            stock_value_today = round(today_price * shares, 2)

            buy_price = self.portfolio[stock]["Buy_Price"]
            stock_value_at_buy = round(self.portfolio[stock]["Buy_Price"] * shares, 2)

            total_stock_pnl = round(stock_value_today - stock_value_at_buy, 2)

            logger.info(
                f"{stock:<15} {shares:<10} {buy_price:<16} {stock_value_at_buy:<16} {today_price:<16} {stock_value_today:<16} {total_stock_pnl:<17}"
            )

            self.portfolio[stock]["Current_Price"] = today_price
            self.portfolio[stock]["Current_Amount"] = stock_value_today

            entry_date_obj = datetime.strptime(
                self.portfolio[stock]["Entry_Date"], "%d-%m-%Y"
            )
            self.portfolio[stock]["Holding_Days"] = (self.execution_date_obj - entry_date_obj).days + 1
            self.portfolio[stock]["100_Days_EMA"] = self.get_ema_for_date(stock, self.execution_date_str)
            self.portfolio[stock]["Profit_Loss"] = total_stock_pnl
            self.portfolio[stock]['Percentage'] = round((total_stock_pnl / stock_value_at_buy) * 100, 2)

        if self.current_invested_capital:
            invested_capital = self.current_invested_capital
            invested_capital += self.additional_amount_added
            invested_capital += self.sip_amount_added
        else:
            invested_capital = self.maximum_capital

        if self.cash_remaining < 0:
            logger.info(f"Invested capital before adding cash remaining: {invested_capital}")
            invested_capital = invested_capital + abs(self.cash_remaining)
            logger.info(f"Invested capital after adding negative cash remaining: {invested_capital}")
            logger.info(
                f"Cash remaining is negative. Setting it to 0 and increasing invested capital by {self.cash_remaining}")
            self.cash_remaining = 0

        invested_capital = round(invested_capital, 2)

        holdings_value = round(sum([stock["Current_Amount"] for stock in self.portfolio.values()]), 2)
        total_value_holdings = round(holdings_value + self.cash_remaining, 2)
        if self.current_holdings_value:
            holding_values_diff = round(total_value_holdings - self.current_holdings_value, 2)
        else:
            holding_values_diff = None

        total_profit_loss = round(total_value_holdings - invested_capital, 2)

        logger.info("=" * 100)
        logger.info(
            f"Invested Capital: {invested_capital}, Current Holdings Value: {total_value_holdings}, Total Profit/Loss: {total_profit_loss}")
        logger.info("=" * 100)
        # Summary metrics
        logger.info("PORTFOLIO SUMMARY:")
        logger.info(f"Analysis Date: {self.execution_date_str}")
        logger.info(f"Invested Capital: {invested_capital:,.2f}")
        logger.info(f"Holdings Value: {holdings_value:,.2f}")
        logger.info(f"Cash Remaining: {self.cash_remaining:,.2f}")
        logger.info(f"Total Portfolio Value: {total_value_holdings:,.2f}")
        logger.info(f"Total P&L: {total_profit_loss:,.2f}")

        if self.sip_amount_added > 0:
            logger.info(f"SIP Amount Added: {self.sip_amount_added:,.2f}")
        if self.additional_amount_added > 0:
            logger.info(f"Additional Capital Added: {self.additional_amount_added:,.2f}")

        return {
            'Date': self.execution_date_str,
            'No_of_Holdings': len(self.portfolio),
            'Invested_Capital': invested_capital,
            'Holdings_Value': holdings_value,
            'Cash_Remaining': round(self.cash_remaining, 2),
            'Total_Value_Holdings': total_value_holdings,
            'Holding_Values_Diff': holding_values_diff,
            'Total_Profit_Loss': total_profit_loss,
            'SIP_Amount_Added': self.sip_amount_added,
            'Additional_Capital_Added': self.additional_amount_added
        }

    def add_additional_amount_to_current_portfolio(self, additional_amount):
        """
        Add additional capital to existing portfolio positions proportionally.

        Args:
            additional_amount (float): Amount to add to portfolio
        """
        logger.info("ADDING ADDITIONAL CAPITAL TO PORTFOLIO")
        logger.info(f"Additional Amount: {additional_amount:,.2f}")
        logger.info("-" * 70)

        if not self.portfolio:
            logger.info("Portfolio is empty - cannot add to existing positions")
            return

        no_of_holdings = len(self.portfolio)
        # self.cash_remaining = self.cash_remaining + additional_amount
        investment_per_stock = additional_amount / TOP_N_INVEST

        logger.info(f"Current Holdings: {no_of_holdings}")
        logger.info(f"Total additional amount (not including cash): {additional_amount:,.2f}")
        logger.info(f"Investment per stock: {investment_per_stock:,.2f}")
        sendeqtAlert(f"{self.client_name} - ETF Additional: {additional_amount:,.0f} | Per stock: {investment_per_stock:,.0f} | Holdings: {no_of_holdings}")

        etf_stocks = [s for s in self.portfolio if s != LIQUID_ETF]
        current_stock_prices = self.get_last_price(etf_stocks, debug_mode=self.debug_mode)
        logger.info(
            f"{'Additional Amount':<20} {'Stock':<15} {'Price':<15} {'Spent':<15} {'Current':<15} {'Remaining Capital':<20}")
        rejected_stocks = []
        for stock in etf_stocks:
            buy_price = current_stock_prices[stock]["last_price"]
            shares = int(investment_per_stock / buy_price)
            if shares > 0:
                order_id, shares, average_price, status, status_message = self.place_buy_order(
                    "NSE", stock, shares, buy_price, self.debug_mode
                )
                if status == 'COMPLETE':
                    spent = round(shares * average_price, 2)
                    additional_amount -= spent
                    self.portfolio[stock]["No_Of_Shares"] += shares
                    self.portfolio[stock]["Buy_Amount"] += spent
                    self.portfolio[stock]["Buy_Price"] = round(
                        (self.portfolio[stock]["Buy_Amount"] / self.portfolio[stock]["No_Of_Shares"]), 2)
                    self.portfolio[stock]["Current_Price"] = round(average_price, 2)
                    self.portfolio[stock]["Current_Amount"] = round(
                        average_price * self.portfolio[stock]["No_Of_Shares"], 2)
                    logger.info(
                        f"{'':<20} {stock:<15} {buy_price:<15} {spent:<15} {self.portfolio[stock]['Current_Amount']:<15} {additional_amount:<20}")
                else:
                    rejected_stocks.append(stock)
                    logger.error(f"{'':<20} {stock:<15} Cannot be bought at this moment.. Order Rejected")
                    sendeqtAlert(f"{self.client_name} - ETF BUY REJECTED (Additional): {stock} - {status_message}")
            else:
                logger.info(f"{'':<20} {stock:<15} Cannot be bought at this price.. Hence skipping")

        logger.info("-" * 85)
        logger.info(f"Additional capital allocation completed, Remaining amount: {additional_amount:,.2f}")
        self.cash_remaining += additional_amount
        logger.info(f"Final cash remaining: {self.cash_remaining:,.2f}")
        sendeqtAlert(f"{self.client_name} - ETF Additional capital done. Cash: {self.cash_remaining:,.0f}")
        if self.cash_remaining > 0 and not rejected_stocks:
            self.park_in_liquidcase()
        elif rejected_stocks:
            logger.info(f"Keeping cash as-is for retry - rejected stocks: {rejected_stocks}")
            sendeqtAlert(f"{self.client_name} - ETF Additional: cash kept for retry ({', '.join(r.replace('NSE:','') for r in rejected_stocks)} rejected)")


    def get_liquidcase_value(self):
        """Return current market value of LIQUIDCASE holding, or 0 if not held."""
        if LIQUID_ETF not in self.portfolio:
            return 0
        prices = self.get_last_price([LIQUID_ETF], debug_mode=self.debug_mode)
        return round(prices[LIQUID_ETF]['last_price'] * self.portfolio[LIQUID_ETF]['No_Of_Shares'], 2)

    def exit_liquidcase(self, amount_to_exit):
        """Partially sell LIQUIDCASE worth amount_to_exit. Proceeds added to cash_remaining."""
        stocks_deleted = OrderedDict()
        if LIQUID_ETF not in self.portfolio:
            return stocks_deleted
        prices = self.get_last_price([LIQUID_ETF], debug_mode=self.debug_mode)
        price = prices[LIQUID_ETF]['last_price']
        units_to_sell = min(int(amount_to_exit / price), self.portfolio[LIQUID_ETF]['No_Of_Shares'])
        if units_to_sell <= 0:
            return stocks_deleted
        order_id, units_sold, avg_price, status, msg = self.place_sell_order(
            "NSE", LIQUID_ETF, units_to_sell, price, self.debug_mode
        )
        if status == 'COMPLETE':
            proceeds = round(units_sold * avg_price, 2)
            self.cash_remaining += proceeds
            lc = self.portfolio[LIQUID_ETF]
            cost_basis = round((units_sold / lc['No_Of_Shares']) * lc['Buy_Amount'], 2)
            profit_loss = round(proceeds - cost_basis, 2)
            entry_date_obj = datetime.strptime(lc['Entry_Date'], "%d-%m-%Y")
            stocks_deleted[LIQUID_ETF] = {
                "Entry_Date": lc['Entry_Date'],
                "Exit_Date": self.execution_date_str,
                "Holding_Days": (self.execution_date_obj - entry_date_obj).days + 1,
                "No_Of_Shares": units_sold,
                "Buy_Price": lc['Buy_Price'],
                "Buy_Amount": cost_basis,
                "Sell_Price": round(avg_price, 2),
                "Sell_Amount": proceeds,
                "Profit_Loss": profit_loss,
                "Percentage": round((profit_loss / cost_basis) * 100, 2) if cost_basis else 0,
            }
            remaining_units = lc['No_Of_Shares'] - units_sold
            if remaining_units > 0:
                lc['No_Of_Shares'] = remaining_units
                lc['Buy_Amount'] = round(lc['Buy_Amount'] - cost_basis, 2)
                lc['Current_Price'] = avg_price
                lc['Current_Amount'] = round(remaining_units * avg_price, 2)
            else:
                del self.portfolio[LIQUID_ETF]
            logger.info(f"LIQUIDCASE exit: sold {units_sold} units @ {avg_price}, proceeds: {proceeds:,.2f}, cash: {self.cash_remaining:,.2f}")
            sendeqtAlert(f"{self.client_name} - LIQUIDCASE exit: {units_sold} units, proceeds: {proceeds:,.0f}, cash: {self.cash_remaining:,.0f}")
        return stocks_deleted

    def park_in_liquidcase(self):
        """Buy LIQUIDCASE with all remaining cash. Accumulates if already held."""
        if self.cash_remaining <= 0:
            return
        prices = self.get_last_price([LIQUID_ETF], debug_mode=self.debug_mode)
        price = prices[LIQUID_ETF]['last_price']
        units = int(self.cash_remaining / price)
        if units <= 0:
            return
        order_id, units_bought, avg_price, status, msg = self.place_buy_order(
            "NSE", LIQUID_ETF, units, price, self.debug_mode
        )
        if status == 'COMPLETE':
            spent = round(units_bought * avg_price, 2)
            self.cash_remaining -= spent
            if LIQUID_ETF in self.portfolio:
                existing = self.portfolio[LIQUID_ETF]
                total_units = existing['No_Of_Shares'] + units_bought
                total_cost = existing['Buy_Amount'] + spent
                existing['No_Of_Shares'] = total_units
                existing['Buy_Amount'] = total_cost
                existing['Buy_Price'] = round(total_cost / total_units, 2)
                existing['Current_Price'] = round(avg_price, 2)
                existing['Current_Amount'] = round(total_units * avg_price, 2)
            else:
                self.portfolio[LIQUID_ETF] = {
                    "No_Of_Shares": units_bought,
                    "Current_Price": round(avg_price, 2),
                    "Current_Amount": spent,
                    "Buy_Price": round(avg_price, 2),
                    "Buy_Amount": spent,
                    "Entry_Date": self.execution_date_str,
                    "Holding_Days": 1,
                }
            logger.info(f"LIQUIDCASE park: bought {units_bought} units @ {avg_price}, spent: {spent:,.2f}, cash: {self.cash_remaining:,.2f}")
            sendeqtAlert(f"{self.client_name} - LIQUIDCASE park: {units_bought} units, spent: {spent:,.0f}, cash: {self.cash_remaining:,.0f}")


def perform_momentum_stock_options_on_date(execution_date_obj, userid, debug_mode):
    """
    Execute momentum stock trading strategy for a given date

    :param execution_date_obj: Date object for execution
    :param userid: User identifier
    :param debug_mode: Boolean flag for debug mode
    :return: None
    """
    client_name = cred_account_settings[userid]['client_name']
    logger.info("=" * 80)
    logger.info(f"STARTING MOMENTUM ETF OPTIONS EXECUTION: {client_name}")
    sendeqtAlert(f"{client_name} - ETF Momentum script started for {execution_date_obj.strftime('%d-%m-%Y')}")
    logger.info(f"Execution Date: {execution_date_obj}")
    logger.info(f"User ID: {userid}")
    logger.info(f"Debug Mode: {debug_mode}")
    logger.info("=" * 80)
    momentum = MomentumFinal(execution_date_obj, userid, debug_mode)

    # try/finally to always persist state even on crash
    try:
        already_executed_flag = (momentum.execution_date_str in momentum.all_executed_dates)
        if momentum.reset_and_fresh_rebalance:
            stocks_to_sell = deepcopy(momentum.portfolio)
            stocks_deleted = momentum.sell_stocks_and_reclaim_capital(stocks_to_sell)
            write_exit_stocks_to_excel(stocks_deleted, "MOMENTUM_RESET_REFRESH")
            already_executed_flag = False
            momentum.rebalance_counter = 1
        else:
            stocks_deleted = momentum.sell_stocks_which_fell_below_ema()
            write_exit_stocks_to_excel(stocks_deleted, "EMA BREACH")
            if momentum.additional_amount > 0 and momentum.portfolio:
                logger.info(f"Adding additional amount of Rs.{momentum.additional_amount} and extending portfolio..")
                sendeqtAlert(f"{client_name} - ETF Adding Rs.{momentum.additional_amount:,.0f} additional capital")
                momentum.add_additional_amount_to_current_portfolio(momentum.additional_amount)
                momentum.additional_amount_added = momentum.additional_amount

        if already_executed_flag:
            logger.info(
                f"Script is already ran on {momentum.execution_date_str}. Skipping rebalancing and counter wont change.."
            )
            sendeqtAlert(f"{client_name} - ETF Already executed on {momentum.execution_date_str}, skipping rebalance")
        if momentum.rebalance_counter - 1 == 0 and not already_executed_flag:
            momentum.buy_stocks_or_not = momentum.buy_stocks_or_not()

            momentum.populate_stock_top_select_and_invest_data()
            stocks_to_sell = momentum.get_stocks_to_sell_at_rebalance()
            if stocks_to_sell:
                old_stocks_deleted = momentum.sell_stocks_and_reclaim_capital(
                    stocks_to_sell
                )
                write_exit_stocks_to_excel(old_stocks_deleted, "MOMENTUM_REBALANCE")
            else:
                logger.info("NO ETF TO SELL")
                sendeqtAlert(f"{client_name} - No ETFs to sell at rebalance")
                old_stocks_deleted = {}

            if momentum.sip_amount > 0:
                momentum.cash_remaining += momentum.sip_amount
                momentum.sip_amount_added = momentum.sip_amount
                logger.info(f"Added SIP amount: {momentum.sip_amount:,.2f}")
                logger.info(f"Updated available cash: {momentum.cash_remaining:,.2f}")
                sendeqtAlert(f"{client_name} - ETF SIP {momentum.sip_amount:,.0f} added. Cash: {momentum.cash_remaining:,.0f}")

            new_stocks_added = {}
            stocks_to_buy = {}
            if momentum.buy_stocks_or_not:
                etf_count = sum(1 for s in momentum.portfolio if s != LIQUID_ETF)
                remaining_slots = TOP_N_INVEST - etf_count
                stocks_to_buy = momentum.get_stocks_to_buy(remaining_slots)

                # Exit LIQUIDCASE proportionally for each new ETF slot to fill
                if stocks_to_buy and LIQUID_ETF in momentum.portfolio:
                    lc_value = momentum.get_liquidcase_value()
                    exit_amount = (lc_value / remaining_slots) * len(stocks_to_buy)
                    logger.info(f"Exiting LIQUIDCASE {exit_amount:,.2f} for {len(stocks_to_buy)} new ETF(s) ({remaining_slots} slots)")
                    liquid_exit = momentum.exit_liquidcase(exit_amount)
                    write_exit_stocks_to_excel(liquid_exit, "LIQUIDCASE_PARTIAL")

                new_stocks_added = momentum.buy_stocks_and_spend_capital(stocks_to_buy)

            # Park idle cash in LIQUIDCASE only when no ETFs were attempted or all succeeded
            # If any order was rejected, leave cash as-is for next rebalancing day
            etf_count_after = sum(1 for s in momentum.portfolio if s != LIQUID_ETF)
            unfilled_slots = TOP_N_INVEST - etf_count_after
            no_rejections = len(new_stocks_added) == len(stocks_to_buy)
            if unfilled_slots > 0 and momentum.cash_remaining > 0 and no_rejections:
                logger.info(f"Parking {momentum.cash_remaining:,.2f} in LIQUIDCASE ({unfilled_slots} unfilled slots)")
                momentum.park_in_liquidcase()
            elif unfilled_slots == 0 and momentum.cash_remaining > IDLE_CASH_THRESHOLD:
                logger.info(f"All slots filled; idle cash {momentum.cash_remaining:,.2f} > {IDLE_CASH_THRESHOLD:,} - treating as additional capital")
                sendeqtAlert(f"{client_name} - ETF Idle cash {momentum.cash_remaining:,.0f} > {IDLE_CASH_THRESHOLD:,}, investing in existing holdings")
                extra = momentum.cash_remaining
                momentum.cash_remaining = 0
                momentum.add_additional_amount_to_current_portfolio(extra)

            old_stocks_continued = list(
                set(momentum.portfolio.keys()) - set(new_stocks_added.keys())
            )
            logger.info(" ")
            logger.info("=" * 100)
            logger.info(
                f"No. of stocks old stocks continued during rebalancing: {len(old_stocks_continued)}"
            )

            if old_stocks_continued:
                logger.info(
                    f"{'Stock':<15} {'Holding Shares':<15} {'Buy Price':<14} {'Buy Amount':<14} {'Current Price':<14} {'Current Amount':<14} {'Profit Loss %':<17}"
                )
                for stock in momentum.portfolio:
                    if stock in old_stocks_continued:
                        shares = momentum.portfolio[stock]["No_Of_Shares"]
                        buy_price = momentum.portfolio[stock]["Buy_Price"]
                        buy_amount = round(buy_price * shares, 2)
                        current_price = momentum.portfolio[stock]["Current_Price"]
                        current_amount = round(current_price * shares, 2)
                        profit_loss = round(buy_amount - current_amount, 2)
                        logger.info(
                            f"{stock:<15} {shares:<15} {buy_price:<14} {buy_amount:<14} {current_price:<14} {current_amount:<14} {profit_loss:<17}"
                        )
            logger.info("=" * 100)

            logger.info(" ")
            logger.info("=" * 100)
            logger.info(
                f"No. of stocks newly added during rebalancing: {len(new_stocks_added)}"
            )
            if new_stocks_added:
                logger.info(
                    f"{'Stock':<15} {'Holding Shares':<15} {'Buy Price':<14} {'Buy Amount':<14} "
                )
                for stock, stock_value in new_stocks_added.items():
                    shares = new_stocks_added[stock]["No_Of_Shares"]
                    buy_price = round(new_stocks_added[stock]["Buy_Price"], 2)
                    buy_amount = round(new_stocks_added[stock]["Buy_Amount"], 2)
                    logger.info(
                        f"{stock:<15} {shares:<15} {buy_price:<14} {buy_amount:<14} "
                    )
            logger.info(" ")
            logger.info("=" * 100)
            logger.info(
                f"No. of stocks which are sold during rebalancing: {len(old_stocks_deleted)}"
            )
            if old_stocks_deleted:
                logger.info(
                    f"{'Stock':<15} {'Holding Shares':<15} {'Buy Price':<14} {'Buy Amount':<14} {'Sell Price':<14} {'Sell Amount':<14} {'Profit Loss %':<17}"
                )
                for stock, stock_value in old_stocks_deleted.items():
                    shares = old_stocks_deleted[stock]["No_Of_Shares"]
                    buy_price = old_stocks_deleted[stock]["Buy_Price"]
                    buy_amount = old_stocks_deleted[stock]["Buy_Amount"]
                    sell_price = old_stocks_deleted[stock]["Sell_Price"]
                    sell_amount = old_stocks_deleted[stock]["Sell_Amount"]
                    profit_loss = old_stocks_deleted[stock]["Profit_Loss"]
                    logger.info(
                        f"{stock:<15} {shares:<15} {buy_price:<14} {buy_amount:<14} {sell_price:<14} {sell_amount:<14} {profit_loss:<17}"
                    )
            logger.info("=" * 100)
            # Resetting rebalance counter to 7
            momentum.rebalance_counter = momentum.momentum_settings.get(userid, {}).get("rebalance_counter",
                                                                                        REBALANCE_COUNTER)
        else:
            if not already_executed_flag:
                momentum.rebalance_counter = momentum.rebalance_counter - 1
                sendeqtAlert(f"{client_name} - ETF No rebalance today. Next rebalance in {momentum.rebalance_counter} day(s)")

                # On any non-rebalance day, try to fill empty slots with fresh calculation
                # Covers: EMA breach sells, prior rejected buys, any other reason slot is open
                etf_count = sum(1 for s in momentum.portfolio if s != LIQUID_ETF)
                remaining_slots = TOP_N_INVEST - etf_count
                if remaining_slots > 0 and momentum.cash_remaining > 0:
                    logger.info("=" * 80)
                    logger.info("EMPTY SLOT(S) DETECTED - ATTEMPTING TO FILL WITH FRESH CALCULATION")
                    sendeqtAlert(f"{client_name} - ETF {remaining_slots} empty slot(s), attempting buy. Cash: {momentum.cash_remaining:,.0f}")
                    ema_stocks_to_buy = {}
                    ema_stocks_added = {}
                    if momentum.buy_stocks_or_not():
                        momentum.populate_stock_top_select_and_invest_data()
                        ema_stocks_to_buy = momentum.get_stocks_to_buy(remaining_slots)
                        # Exit LIQUIDCASE proportionally to fund new buys (if parked earlier)
                        if ema_stocks_to_buy and LIQUID_ETF in momentum.portfolio:
                            lc_value = momentum.get_liquidcase_value()
                            exit_amount = (lc_value / remaining_slots) * len(ema_stocks_to_buy)
                            logger.info(f"Exiting LIQUIDCASE {exit_amount:,.2f} for {len(ema_stocks_to_buy)} new ETF(s)")
                            liquid_exit = momentum.exit_liquidcase(exit_amount)
                            write_exit_stocks_to_excel(liquid_exit, "LIQUIDCASE_PARTIAL")
                        ema_stocks_added = momentum.buy_stocks_and_spend_capital(ema_stocks_to_buy)
                    # Park idle cash in LIQUIDCASE if nothing to buy or all buys succeeded
                    # If buy was rejected, leave cash as-is to retry next day
                    etf_count_after = sum(1 for s in momentum.portfolio if s != LIQUID_ETF)
                    no_rejections = len(ema_stocks_added) == len(ema_stocks_to_buy)
                    if TOP_N_INVEST - etf_count_after > 0 and momentum.cash_remaining > 0 and no_rejections:
                        logger.info(f"Parking {momentum.cash_remaining:,.2f} in LIQUIDCASE - no eligible stock found")
                        momentum.park_in_liquidcase()

                # Deploy idle cash when all ETF slots are filled (covers: slots were already full,
                # or slot-filling above just completed all positions)
                _etf_final = sum(1 for s in momentum.portfolio if s != LIQUID_ETF)
                if _etf_final == TOP_N_INVEST and momentum.cash_remaining > IDLE_CASH_THRESHOLD:
                    logger.info(f"All slots filled; idle cash {momentum.cash_remaining:,.2f} > {IDLE_CASH_THRESHOLD:,} - treating as additional capital")
                    sendeqtAlert(f"{client_name} - ETF Idle cash {momentum.cash_remaining:,.0f} > {IDLE_CASH_THRESHOLD:,}, investing in existing holdings")
                    extra = momentum.cash_remaining
                    momentum.cash_remaining = 0
                    momentum.add_additional_amount_to_current_portfolio(extra)

    except Exception as e:
        logger.error(f"CRITICAL ERROR during execution: {e}", exc_info=True)
        sendeqtAlert(f"{client_name} - CRITICAL ERROR: {e}")

    finally:
        # ALWAYS persist portfolio state - even if execution crashed midway.
        # This ensures Excel reflects whatever broker operations actually completed.
        try:
            current_summary = momentum.analyze_current_portfolio()

            output_data = {
                "Executed_Date": momentum.execution_date_str,
                "No_of_Holdings": len(momentum.portfolio),
                "Cash_Remaining": momentum.cash_remaining,
                "Rebalance_Counter": momentum.rebalance_counter,
                "portfolio": momentum.portfolio,
            }

            write_current_summary_to_excel(current_summary)
            write_current_portfolio_to_excel(output_data)

            # Day-end summary alert
            pnl = current_summary['Total_Profit_Loss']
            invested = current_summary['Invested_Capital']
            pnl_pct = round((pnl / invested) * 100, 2) if invested else 0
            day_diff = current_summary.get('Holding_Values_Diff')
            if day_diff is not None:
                prev_value = current_summary['Total_Value_Holdings'] - day_diff
                day_diff_pct = round((day_diff / prev_value) * 100, 2) if prev_value else 0
                day_diff_str = f" | Day Chg: {day_diff:,.0f} ({day_diff_pct:.2f}%)"
            else:
                day_diff_str = ""

            # Find top gainer and top loser from current portfolio
            top_gainer_str = ""
            top_loser_str = ""
            if momentum.portfolio:
                sorted_by_pct = sorted(momentum.portfolio.items(), key=lambda x: x[1].get('Percentage', 0))
                worst = sorted_by_pct[0]
                best = sorted_by_pct[-1]
                top_gainer_str = f"Best: {best[0].replace('NSE:', '')}({best[1]['Percentage']:.1f}%)"
                top_loser_str = f"Worst: {worst[0].replace('NSE:', '')}({worst[1]['Percentage']:.1f}%)"

            sendeqtAlert(
                f"{client_name} - ETF DAY END {momentum.execution_date_str}\n"
                f"Holdings: {len(momentum.portfolio)} | Invested: {invested:,.0f} | "
                f"Value: {current_summary['Total_Value_Holdings']:,.0f} | "
                f"Cash: {momentum.cash_remaining:,.0f}\n"
                f"PnL: {pnl:,.0f} ({pnl_pct}%){day_diff_str}\n"
                f"{top_gainer_str} | {top_loser_str}"
            )

            logger.info("=" * 80)
            logger.info("MOMENTUM ETF OPTIONS EXECUTION COMPLETED SUCCESSFULLY")
            logger.info(f"Holdings: {len(momentum.portfolio)} | Cash Remaining: Rs.{momentum.cash_remaining}")
            logger.info("=" * 80)
            # Scorecard metrics (XIRR/Sharpe/Calmar/drawdown) are now computed live by the
            # dashboard's internal API straight from summary_history/exited_stocks -- no
            # Excel artifact to rebuild or send here anymore.

        except Exception as save_err:
            logger.error(f"CRITICAL: Failed to persist portfolio state: {save_err}", exc_info=True)
            sendeqtAlert(f"{client_name} - CRITICAL: Portfolio save failed: {save_err}")
            # Emergency JSON backup as last resort
            try:
                emergency_file = EMERGENCY_BACKUP_FILE
                backup_data = {
                    "Executed_Date": momentum.execution_date_str,
                    "No_of_Holdings": len(momentum.portfolio),
                    "Cash_Remaining": momentum.cash_remaining,
                    "Rebalance_Counter": momentum.rebalance_counter,
                    "portfolio": {k: v for k, v in momentum.portfolio.items()},
                }
                _tmp = emergency_file + '.tmp'
                with open(_tmp, 'w') as f:
                    json.dump(backup_data, f, indent=2, default=str)
                os.replace(_tmp, emergency_file)
                logger.info(f"Emergency backup saved: {emergency_file}")
                sendeqtAlert(f"{client_name} - Emergency backup saved: {emergency_file}")
            except Exception as backup_err:
                logger.error(f"DOUBLE CRITICAL: Even JSON backup failed: {backup_err}")


def setup_logging(userid, date_str):
    """
    :param userid:
    :return:
    """
    global CONSOLIDATED_LOG_FILE
    global PER_DAY_LOG_FILE
    global TOP_N_SELECT_FILE
    global EMERGENCY_BACKUP_FILE

    log_path = os.path.join(userid, f"Logs_ETF")
    os.makedirs(log_path, mode=0o700, exist_ok=True)

    CONSOLIDATED_LOG_FILE = os.path.join(
        log_path, rf"{cred_account_settings[userid]['client_name']}_Consolidated_Momentum_Logs.txt")
    PER_DAY_LOG_FILE = os.path.join(
        log_path, rf"{cred_account_settings[userid]['client_name']}_{date_str}_Momentum_Logs.txt")
    TOP_N_SELECT_FILE = os.path.join(
        log_path, rf"{cred_account_settings[userid]['client_name']}_{date_str}_ETF.txt")
    EMERGENCY_BACKUP_FILE = os.path.join(
        log_path, rf"{cred_account_settings[userid]['client_name']}_EMERGENCY_BACKUP.json")

    if not logger.handlers:
        init_logging(CONSOLIDATED_LOG_FILE, log_level='INFO')
    # set_file_handler(PER_DAY_LOG_FILE)


def valid_date(date_str):
    try:
        return datetime.strptime(date_str, "%d-%m-%Y")
    except ValueError:
        raise argparse.ArgumentTypeError(f"Invalid date format: '{date_str}'. Use dd-mm-yyyy.")


def main_function(userid, date_obj, debug):
    telegram['is_debug_mode'] = debug
    bind_to_source_ip(cred_account_settings[userid].get('source_ip'))
    logger.info(f"Outgoing IP for {userid}: {validate_outgoing_ip(cred_account_settings[userid].get('source_ip'))}")
    setup_logging(userid, date_obj.strftime("%d-%m-%Y"))
    perform_momentum_stock_options_on_date(date_obj, userid, debug)


if __name__ == "__main__":
    # Get today's date string in dd-mm-yyyy format
    today_str = datetime.today().strftime("%d-%m-%Y")
    parser = argparse.ArgumentParser(description="Script with optional date, userid, and debug flag.")
    parser.add_argument(
        "--date", "-date",
        type=valid_date,
        default=valid_date(today_str),
        help="Date in dd-mm-yyyy format (default: today)"
    )
    parser.add_argument(
        "--userid", "-userid",
        type=str,
        required=True,
        help="User ID (any alphanumeric string)"
    )
    parser.add_argument(
        "--debug", "-debug",
        action="store_true",
        help="Enable debug mode"
    )

    args = parser.parse_args()
    main_function(args.userid, args.date, args.debug)