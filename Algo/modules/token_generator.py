"""

This module is used for generating access token

"""
import datetime as dt
import sys, os
from pathlib import Path
import time
import pyotp
from kiteconnect import KiteConnect
import urllib.parse as urlparse
from urllib.parse import parse_qs
sys.path.insert(0, os.path.dirname(Path(__file__).parent.parent))
from Algo.logger import logger, message_formatter, init_logging
from Algo.utils.algoutils import *
from Algo.utils.creds import *
from Algo.utils.settings import *
from Algo.utils.src_bind import mount_source_ip, validate_outgoing_ip


def get_access_token(account: str) -> str:
    """
    This method generates access token for individual zerodha accounts

    :param account: Zerodha Account id
    :return: Access token or None if failed
    """
    api_key = ACCOUNTS[account]['api_key']
    api_secret = ACCOUNTS[account]['api_secret']
    zerodha_password = ACCOUNTS[account]['password']

    logger.debug(message_formatter(f"account: {account}"))
    logger.debug(message_formatter(f"api_key: {api_key}"))
    logger.debug(message_formatter(f"api_secret: {api_secret}"))

    logger.info(message_formatter(f"logging in {account}"))

    reqToken = None  # Initialize to None

    for attempt in range(2):
        try:
            sesh2 = requests.Session()
            mount_source_ip(sesh2, ACCOUNTS[account].get('source_ip'), account)
            logger.info(message_formatter(f"Outgoing IP for {account}: {validate_outgoing_ip(ACCOUNTS[account].get('source_ip'))}"))
            url = "https://kite.zerodha.com/api/login"
            twofaUrl = "https://kite.zerodha.com/api/twofa"
            reqId = sesh2.post(url, {"user_id": account, "password": zerodha_password}).json()['data']['request_id']

            try:
                totp_key = ACCOUNTS[account]['totp_key']
                totp_pin = pyotp.TOTP(totp_key)
                login = sesh2.post(twofaUrl,
                                   {"user_id": account, "request_id": reqId, "twofa_value": totp_pin.now()})
            except Exception as e:
                logger.error(message_formatter(f'totp error for {account}: {e}'))

            reqToken = sesh2.get("https://kite.trade/connect/login?api_key=" + api_key)
            logger.debug(message_formatter(f"URL - {reqToken.url}"))
            reqToken = parse_qs(urlparse.urlparse(reqToken.url).query)['request_token'][0]
            logger.debug(message_formatter(f"Logged in successfully. - {reqToken}"))
            break  # Success, exit retry loop

        except Exception as e:
            logger.error(message_formatter(f"token generation failed (attempt {attempt + 1}/2) for {account} - {e}"))
            if attempt == 0:  # Only send retry alert on first failure
                sendErrorAlert(f"Token generation Failed:{account} - Retrying")
                time.sleep(5)

    # Check if we successfully got the request token
    if reqToken is None:
        sendErrorAlert(f"Token generation - Failed Fully:{account}, ALERT")
        logger.error(message_formatter(f"Token generation - Failed Fully:{account}, ALERT"))
        return None  # Return None to indicate failure

    try:
        # Generate session with the request token
        kite = KiteConnect(api_key=api_key)
        mount_source_ip(kite, ACCOUNTS[account].get('source_ip'), account)
        data = kite.generate_session(reqToken, api_secret=api_secret)
        access_token = data["access_token"]

        # Save token to file
        token_path = os.path.join(os.path.dirname(Path(__file__).parent), "tokens")
        if not os.path.isdir(token_path):
            os.mkdir(token_path, mode=0o777)

        filename = token_path + "/%s.txt" % account
        with open(filename, 'w') as file1:
            file1.write(str(access_token))

        logger.debug(message_formatter(f"File updated at {filename}"))
        logger.info(message_formatter(f"Updated {account} file with access_token {access_token}"))
        sendeqtAlert(f"access token - {account} - {access_token}")

        return access_token

    except Exception as e:
        logger.error(message_formatter(f"Failed to generate session for {account}: {e}"))
        sendErrorAlert(f"Session generation failed for {account}: {e}")
        return None


def main() -> None:
    """
    Main entry point of the program

    :return: None
    """

    log_path = create_log_path()
    datetime_string = dt.datetime.now(dt.UTC).strftime('%d_%m_%Y')
    log_file_name = f"token_generator_{datetime_string}.log"
    log_complete_path = str(os.path.join(log_path, log_file_name))
    init_logging(log_complete_path)

    try:
        if (sys.argv[1] != '--i'
                or len(sys.argv) < 3):
            raise RuntimeError('Provide valid arguments. e.g. --i RK0709')
            sys.exit(2)
        else:
            account = sys.argv[2].upper()
            logger.info(message_formatter(f"Input for token generation {account}"))
    except IndexError as err:
        logger.error(message_formatter('Provide valid arguments. e.g. --i RK0709'))
        sys.exit(2)

    LIST = fetch_flag_accounts('TradeOn', account)
    logger.debug(message_formatter(f"access token for the list {LIST}"))

    successful_tokens = 0
    failed_accounts = []

    for ZERODHA_ID in LIST:
        logger.debug(message_formatter(f"Initiating access token for {ZERODHA_ID}"))
        try:
            result = get_access_token(ZERODHA_ID)
            if result is not None:
                successful_tokens += 1
                logger.info(message_formatter(f"Successfully generated token for {ZERODHA_ID}"))
            else:
                failed_accounts.append(ZERODHA_ID)
                logger.warning(message_formatter(f"Failed to generate token for {ZERODHA_ID}"))
        except Exception as e:
            logger.error(message_formatter(f"Unexpected error processing {ZERODHA_ID}: {e}"))
            failed_accounts.append(ZERODHA_ID)

    # Summary logging
    logger.info(message_formatter(f"Token generation summary: {successful_tokens}/{len(LIST)} successful"))
    if failed_accounts:
        logger.warning(message_formatter(f"Failed accounts: {', '.join(failed_accounts)}"))
        sendErrorAlert(f"Token generation completed with failures: {', '.join(failed_accounts)}")
    else:
        logger.info(message_formatter("All tokens generated successfully"))


if __name__ == '__main__':
    main()