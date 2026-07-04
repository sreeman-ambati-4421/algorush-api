"""

This file will have all the general utilities

"""
import os
import pickle
import time
import pandas as pd
import requests

requests.packages.urllib3.disable_warnings()
import datetime as dt
from pathlib import Path
import psutil
from decimal import Decimal, ROUND_UP
from Algo.logger import logger, message_formatter
from Algo.utils.settings import *
from Algo.utils.creds import *


def sendeqtAlert(message: str) -> None:
    """
        Function to send alerts based on trades

        :param message: Message to be sent
        :return: None
    """
    if telegram.get('is_debug_mode', False):
        return

    sendurl = f"https://api.telegram.org/bot{telegram['eqt_token']['bot_id']}/sendmessage"
    params = {
        'chat_id': telegram['eqt_token']['chat_id'],
        'text': message
    }
    try:
        requests.get(sendurl, params=params, verify=False, timeout=5)
        logger.info(message_formatter(f"telegram trade alert sent: {message}"))

    except Exception as e:
        logger.error(f"telegram trade alert failed with error {e}")


def sendErrorAlert(message: str) -> None:
    """
        Function to send error alerts

        :param message: Message to be sent
        :return: None
    """

    sendurl = f"https://api.telegram.org/bot{telegram['eqt_token']['bot_id']}/sendmessage?chat_id={telegram['eqt_token']['chat_id']}&text={message}"
    try:
        requests.get(sendurl, verify=False, timeout=5)
        logger.info(message_formatter(f"telegram error alert sent: {message}"))

    except Exception as e:
        logger.error(f"telegram error alert failed with error {e}")

def sendDocument(file_path) -> None:
    """
        Function to send files based on request

        :param message: Message to be sent
        :return: None
    """
    if telegram.get('is_debug_mode', False):
        return

    if os.path.exists(file_path):
        files = {"document": open(file_path, "rb")}
    else:
        raise Exception("File cannot be opened or does not exist.")

    sendurl = f"https://api.telegram.org/bot{telegram['eqt_token']['bot_id']}/sendDocument?chat_id={telegram['eqt_token']['chat_id']}"
    try:
        requests.post(sendurl, files=files, verify=False, timeout=5)
        logger.info(message_formatter(f"telegram trade file sent"))

    except Exception as e:
        logger.error(f"telegram file alert failed with error {e}")

def sendAlertDocument(file_path) -> None:
    """
        Function to send files based on request

        :param message: Message to be sent
        :return: None
    """
    if telegram.get('is_debug_mode', False):
        return

    if os.path.exists(file_path):
        files = {"document": open(file_path, "rb")}
    else:
        raise Exception("File cannot be opened or does not exist.")

    sendurl = f"https://api.telegram.org/bot{telegram['tech_token']['bot_id']}/sendDocument?chat_id={telegram['tech_token']['chat_id']}"
    try:
        requests.post(sendurl, files=files, verify=False, timeout=5)
        logger.info(message_formatter(f"telegram trade file sent"))

    except Exception as e:
        logger.error(f"telegram file alert failed with error {e}")


def fetch_flag_accounts(flag: str, account="ALL") -> list:
    """
    Method is used to fetch account id in list based on the flag status

    :param flag: Keyword to check status
    :param account: Account ids
    :return: List of accounts
    """
    logger.info(message_formatter(f"Fetching accounts for {flag} with input {account}"))
    accounts_list = account.split(",")
    if "ALL" in accounts_list:
        accounts_list = []
        for key in list(ACCOUNTS.keys()):
            if ACCOUNTS[key].get(flag, False):
                accounts_list.append(key)
    else:
        for key in accounts_list:
            if key not in ACCOUNTS.keys() or not ACCOUNTS[key].get(flag, False):
                accounts_list.remove(key)
                sendErrorAlert(f"Invlaid, {key}")

    logger.info(message_formatter(f"Accounts list for {flag}: {accounts_list}"))
    return accounts_list


def roundtotick(price) -> float:
    x = Decimal(price)
    return (x * 2).quantize(Decimal('.1'), rounding=ROUND_UP) / 2


def kill_program(script: str) -> None:
    """
    Method used to terminate the python script

    :param script: Small unique string of python script
    :return: None
    """

    logger.info(message_formatter(f"Input to kill python script is {script}"))
    for p in psutil.process_iter(['pid', 'name']):
        if p.name().startswith('py'):
            if script in p.cmdline()[1]:
                logger.info(message_formatter(f"command line {p.cmdline()}"))
                logger.info(message_formatter(f"pid to terminate is {p.info['pid']}"))
                p.terminate()


def pickleWrite(filename: str) -> None:
    """
    This method will create pickle file for later use

    :param filename: Pickle file name
    :return: None
    """
    pickle_path = os.path.join(os.path.dirname(Path(__file__).parent), "tokens")
    filename = f"{pickle_path}//{filename}.pkl"
    fp = open(filename, "wb")
    pickle.dump(AlgoSettings, fp)
    logger.info(message_formatter(f"Pickle updated: {filename}"))

def getInstrumentsList(kite) -> dict:
    """
    This method is used fetch all the available instruments

    :param kite: zerodha session
    :return: instruments fetch from zerodha
    """
    for _ in range(10):
        try:
            logger.info(message_formatter(f"Fetching instruments"))
            instruments = kite.instruments()
            return (instruments)
        except Exception as e:
            logger.error(message_formatter(f"Fetch instruments failed with error: {e}"))
            sendErrorAlert("instrument pull -  Failed , Retrying")
            time.sleep(5)
            continue
        else:
            break

def getsymb(symb, instruments):
    pd.set_option('display.width', None)
    df = pd.DataFrame(instruments)
    eqdf = df.loc[
        df['tradingsymbol'].str.match(symb) & df['exchange'].str.match('NSE')]
    logger.debug(f"\n {eqdf}")
    symmbol = eqdf['tradingsymbol'].iloc[0]

    return symmbol

def gettoken(symb, instruments):
    pd.set_option('display.width', None)
    df = pd.DataFrame(instruments)
    eqdf = df.loc[
        (df['tradingsymbol'] == symb.split(':')[1]) & df['exchange'].str.match('NSE')]
    logger.debug(f"\n {eqdf}")
    token = eqdf['instrument_token'].iloc[0]

    return token


def pickleRead(filename: str) -> None:
    """
    This method will create pickle file for later use

    :param filename: Pickle file name
    :return: None
    """
    try:
        pickle_path = os.path.join(os.path.dirname(Path(__file__).parent), "tokens")
        filename = f"{pickle_path}//{filename}.pkl"
        fp = open(filename, "rb")
        AlgoSettings = pickle.load(fp)
        logger.info(message_formatter(f"Pickle downloaded: {filename}"))
        return (AlgoSettings)
    except Exception as e:
        logger.info(message_formatter(f"Pickle not found with error {e}"))


def create_log_path() -> str:
    """
    Function which creates log path in user current working directory

    :return: log path str
    """
    log_path = os.path.join(os.path.dirname(Path(__file__).parent), "logs")
    if not os.path.isdir(log_path):
        os.mkdir(log_path, mode=0o777)
    return log_path


def loadAccessCodes() -> None:
    """
    Function to read the generated accesstokens

    :return: None
    """
    token_path = os.path.join(os.path.dirname(Path(__file__).parent), "tokens")
    logger.info(f"token path: {token_path}")
    for key in list(ACCOUNTS.keys()):
        file_path = f"{token_path}/{key}.txt"
        if ACCOUNTS[key]['TradeOn'] and os.path.exists(file_path):
            logger.info(message_formatter(f"loading access token for {key}"))
            access_code_file = open(file_path, "r")
            ACCOUNTS[key]['access_token'] = access_code_file.read()
        else:
            logger.warning(message_formatter(f"No access token found for {key}"))


def getltp(kite, instru: str) -> int:
    """
    This method is used to fetch last trading price of the instrument

    :param kite: kite loggedin instance
    :param instru: Intrument for which ltp need to be pulled
    :return: last trading price
    """
    bse_strings = ['SENSEX', 'BANKEX']
    if instru in AlgoSettings['indices']:
        segment = AlgoSettings[instru]['exchange']
        instru = AlgoSettings[instru]['symbol']
    else:
        segment = 'BFO' if any(keyword in instru for keyword in bse_strings) else 'NFO'

    if instru.split(':')[0] not in ['NFO', 'BFO', 'NSE', 'BSE']:
        symbol = segment + ':' + instru
    else:
        symbol = instru

    for _ in range(100):
        try:
            ltp = kite.ltp(symbol)
        except Exception as e:
            logger.error(message_formatter(f"Fetching ltp failed with error - {e}"))
            time.sleep(2)
            logger.warning(message_formatter(f"retrying to fetch ltp"))
            continue
        else:
            break

    # did not break the for loop, therefore all attempts failed
    else:
        logger.error(message_formatter(f" getLTP failed all the retries"))

    logger.info(message_formatter(f"LTP for symbol {symbol} is {ltp[str(symbol)]['last_price']}"))
    return float(ltp[str(symbol)]['last_price'])


def roundfun(xyz):
    try:
        outputval = 0
        outputval = round(xyz, 1)
        return outputval

    except Exception as error:
        logger.error(message_formatter(f"rounding value function - {error}"))


def getInstrumentsList(kite) -> dict:
    """
    This method is used fetch all the available instruments

    :param kite: zerodha session
    :return: instruments fetch from zerodha
    """
    for _ in range(10):
        try:
            logger.info(message_formatter(f"Fetching instruments"))
            instruments = kite.instruments()
            return (instruments)
        except Exception as e:
            logger.error(message_formatter(f"Fetch instruments failed with error: {e}"))
            sendErrorAlert("instrument pull -  Failed , Retrying")
            time.sleep(5)
            continue
        else:
            break




def calculate_ema_manual(dataframe, period=100):
    prices = [x['close'] for x in dataframe]
    ema_values = []

    # Step 1: First EMA is just the SMA of the first 'period' prices
    sma = sum(prices[:period]) / period
    ema_values = [None] * (period - 1)  # First (period-1) EMA values are undefined
    ema_values.append(sma)

    # Step 2: Calculate smoothing factor
    alpha = 2 / (period + 1)

    # Step 3: Calculate EMA for the rest
    for price in prices[period:]:
        previous_ema = ema_values[-1]
        ema_today = (price * alpha) + (previous_ema * (1 - alpha))
        ema_values.append(ema_today)

    return ema_values[-1]

def calculate_ema_panda(dataframe, period=100):

    # If you want to match it with a pandas DataFrame:

    history_pd_frame_100 = pd.DataFrame(dataframe)
    ema_100 = ta.ema(history_pd_frame_100['close'], 100)
    ema_100_value = ema_100[ema_100.size - 1]
    return ema_100_value
