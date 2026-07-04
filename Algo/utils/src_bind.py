"""
Source IP binding utilities for multi-account Kite API calls.

- SourceIPAdapter : thread-safe requests adapter — use for multi-account processes
                    (token_generator --i all, equity_shop)
- mount_source_ip : attach SourceIPAdapter to any requests.Session or kite.reqsess
- bind_to_source_ip : global socket patch — use at startup of single-account scripts
                      (momentum_final, momentum_etf)

Auto-detects environment:
- On EC2: source IP exists on the machine → binding applied → correct EIP used
- On local laptop: source IP not found → binding silently skipped → laptop IP used
  (safe for local debug/testing since IP check only applies to order endpoints)
"""

import socket
import functools
import logging
import requests as _requests
from requests.adapters import HTTPAdapter

logger = logging.getLogger('Algo.logger')


def _ip_available_locally(ip: str) -> bool:
    """Return True if the IP is assigned to a local network interface."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind((ip, 0))
        s.close()
        return True
    except OSError:
        return False


def validate_outgoing_ip(source_ip: str | None = None) -> str | None:
    """
    Make a quick call to api.ipify.org and return the actual public IP being used.
    Pass source_ip to bind the validation request to a specific interface.
    Returns the IP string, or None if the call fails.

    Usage:
        actual_ip = validate_outgoing_ip(ACCOUNTS[account].get('source_ip'))
        logger.info(message_formatter(f"Outgoing IP for {account}: {actual_ip}"))
    """
    try:
        session = _requests.Session()
        mount_source_ip(session, source_ip)
        resp = session.get('https://api.ipify.org', timeout=5)
        return resp.text.strip()
    except Exception as e:
        logger.warning(f"Could not fetch outgoing IP: {e}")
        return None


class SourceIPAdapter(HTTPAdapter):
    """
    Requests transport adapter that binds all outgoing connections to a
    specific source IP address. Thread-safe: each adapter owns its own
    urllib3 connection pool with source_address set at pool creation time.
    """

    def __init__(self, source_ip: str, **kwargs):
        self.source_ip = source_ip
        super().__init__(**kwargs)

    def init_poolmanager(self, *args, **kwargs):
        kwargs['source_address'] = (self.source_ip, 0)
        super().init_poolmanager(*args, **kwargs)

    def proxy_manager_for(self, proxy, **proxy_kwargs):
        proxy_kwargs['source_address'] = (self.source_ip, 0)
        return super().proxy_manager_for(proxy, **proxy_kwargs)


def mount_source_ip(session_or_kite, source_ip: str, account: str = '') -> None:
    """
    Mount a SourceIPAdapter on a requests.Session or KiteConnect instance.

    Accepts either a requests.Session directly or a KiteConnect object —
    handles different kiteconnect versions by probing for the session attribute
    (reqsess, _session, session) so call sites never need to access it directly.

    No-op if source_ip is None/empty or not available on this machine (local dev).

    Usage:
        kite = KiteConnect(api_key=...)
        mount_source_ip(kite, ACCOUNTS[account].get('source_ip'), account)

        sesh = requests.Session()
        mount_source_ip(sesh, ACCOUNTS[account].get('source_ip'), account)
    """
    tag = f"[{account}] " if account else ""

    if not source_ip:
        logger.debug(f"{tag}mount_source_ip: no source_ip configured — skipping")
        return

    if not _ip_available_locally(source_ip):
        logger.info(f"{tag}mount_source_ip: {source_ip} not local — dev mode, binding skipped")
        return

    # Resolve to actual requests.Session
    session = session_or_kite
    if not hasattr(session, 'mount'):
        # KiteConnect object — probe known attribute names across versions
        for attr in ('reqsession', 'reqsess', '_session', 'session'):
            candidate = getattr(session_or_kite, attr, None)
            if candidate is not None and hasattr(candidate, 'mount'):
                session = candidate
                break
        else:
            logger.warning(f"{tag}mount_source_ip: could not find session on KiteConnect object — skipping")
            return

    adapter = SourceIPAdapter(source_ip)
    session.mount('https://', adapter)
    session.mount('http://', adapter)
    logger.info(f"{tag}mount_source_ip: bound to {source_ip}")


def bind_to_source_ip(source_ip: str, account: str = '') -> None:
    """
    Globally patch socket.create_connection to always use source_ip.

    Use this at startup of single-account scripts (momentum_final, momentum_etf,
    etc.) before any API calls are made.
    Since the whole process handles one account, global patching is safe.

    No-op if source_ip is None/empty or not available on this machine (local dev).

    Usage (in main(), after parsing --userid arg, before API calls):
        from Algo.utils.src_bind import bind_to_source_ip
        bind_to_source_ip(cred_account_settings[userid].get('source_ip'))
    """
    tag = f"[{account}] " if account else ""

    if not source_ip:
        logger.debug(f"{tag}bind_to_source_ip: no source_ip configured — skipping")
        return

    if not _ip_available_locally(source_ip):
        logger.info(f"{tag}bind_to_source_ip: {source_ip} not local — dev mode, binding skipped")
        return

    _orig = socket.create_connection

    @functools.wraps(_orig)
    def _patched(address, timeout=socket.getdefaulttimeout(), source_address=None):
        return _orig(address, timeout, source_address=(source_ip, 0))

    socket.create_connection = _patched
    logger.info(f"{tag}bind_to_source_ip: all connections bound to {source_ip}")
