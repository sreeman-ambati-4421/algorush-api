"""

This module is used for logging

"""

import datetime
import logging
from logging.handlers import RotatingFileHandler

logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)


def format_time_rfc3339(self, record, datefmt=None):
    return (
        datetime.datetime.fromtimestamp(record.created, datetime.timezone.utc).astimezone().isoformat(
            timespec='milliseconds')
    )


formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')

def get_log_level(level):
    """
    Function which returns the log level

    :param level: level which the user want to see example info
    :return: corresponding logging
    """
    data = {"info": logging.INFO, "warning": logging.WARNING, "debug": logging.DEBUG, "error": logging.ERROR,
            "critical": logging.CRITICAL}

    result = data.get(level.lower())
    return result


def set_stream_handler(level):
    """
    Function which set logs in stream

    :param level: log level
    :return: None
    """

    ch_handler = logging.StreamHandler()
    ch_handler.setLevel(level)
    logging.Formatter.formatTime = format_time_rfc3339
    ch_handler.setFormatter(formatter)
    logger.addHandler(ch_handler)


def set_file_handler(filename):
    """
    Function which set log in file

    :param filename: filename where logs would be written
    :return: None
    """
    ch_handler = RotatingFileHandler(filename, maxBytes=5 * 1024 * 1024, backupCount=10)
    ch_handler.setLevel(logging.DEBUG)
    logging.Formatter.formatTime = format_time_rfc3339
    ch_handler.setFormatter(formatter)
    logger.addHandler(ch_handler)


def init_logging(filename, log_level='DEBUG'):
    """
    Function for initialization logs

    :param filename: FIlename where logs would be written
    :param log_level: log level
    :return: None

    """
    level = get_log_level(log_level)
    set_stream_handler(level)
    set_file_handler(filename)


def message_formatter(msg: str) -> str:
    """
    Functions which would format the message to make it to standard

    :param msg: logging message
    :return: message string
    """
    # msg = msg.capitalize()
    full_stop = "."
    if not msg[::-1].startswith("."):
        msg = f"{msg}{full_stop}"
    else:
        msg = f"{msg}"

    return msg