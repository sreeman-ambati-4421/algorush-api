"""
Interactive CLI to register a new broker account, or rotate an existing
account's credentials. Run manually by the operator -- never from the web app.

Encrypts the Kite api_key/api_secret/password/TOTP secret with
Algo/utils/crypto.py (Fernet, CREDS_ENCRYPTION_KEY) before writing to
Postgres, and links the account to a Google email for AlgoRush-UI sign-in.

Idempotent: re-running for an existing account_id UPDATEs both accounts and
account_credentials (upsert via ON CONFLICT) -- safe to use for credential
rotation, not just first-time registration.

Requires DATABASE_URL and CREDS_ENCRYPTION_KEY set in the environment.

Usage:
    python db/register_account.py
"""
import os
import sys
from getpass import getpass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select, func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError

from Algo.utils.db import get_session, Account, AccountCredentials
from Algo.utils.crypto import encrypt


def _required(prompt):
    while True:
        v = input(f"{prompt}: ").strip()
        if v:
            return v
        print("  required")


def _secret(prompt):
    while True:
        v = getpass(f"{prompt} (hidden): ").strip()
        if v:
            return v
        print("  required")


def _yn(prompt, default):
    suffix = "Y/n" if default else "y/N"
    v = input(f"{prompt} [{suffix}]: ").strip().lower()
    if not v:
        return default
    return v.startswith("y")


def main():
    print("=== AlgoRush account registration / credential rotation ===")
    account_id = _required("Account id (Kite userid)").upper()

    session = get_session()
    try:
        existing = session.get(Account, account_id)
        if existing:
            print(f"  {account_id} already exists ({existing.client_name}) -- this will UPDATE it.")
            if not _yn("Continue", default=False):
                print("Aborted.")
                return

        client_name = _required("Client name")
        google_email = _required("Google email (for dashboard login)").lower()
        api_key = _secret("Kite api_key")
        api_secret = _secret("Kite api_secret")
        password = _secret("Zerodha password")
        totp_key = _secret("TOTP secret")
        source_ip = input("Source IP (optional, blank to skip): ").strip() or None
        trade_on = _yn("TradeOn", default=True)
        is_base = _yn("Base account", default=False)

        if is_base:
            other_base = session.scalars(
                select(Account.userid).where(Account.is_base.is_(True), Account.userid != account_id)
            ).first()
            if other_base:
                if _yn(f"  {other_base} is currently the Base account -- unset it now", default=True):
                    session.execute(
                        Account.__table__.update().where(Account.userid == other_base).values(is_base=False)
                    )
                else:
                    print("  leaving multiple Base accounts -- fix this manually if unintended")

        acct_stmt = pg_insert(Account).values(
            userid=account_id,
            client_name=client_name,
            trade_on=trade_on,
            is_base=is_base,
            google_email=google_email,
        )
        acct_stmt = acct_stmt.on_conflict_do_update(
            index_elements=["userid"],
            set_={
                "client_name": acct_stmt.excluded.client_name,
                "trade_on": acct_stmt.excluded.trade_on,
                "is_base": acct_stmt.excluded.is_base,
                "google_email": acct_stmt.excluded.google_email,
            },
        )
        session.execute(acct_stmt)

        cred_stmt = pg_insert(AccountCredentials).values(
            account_id=account_id,
            api_key_enc=encrypt(api_key),
            api_secret_enc=encrypt(api_secret),
            password_enc=encrypt(password),
            totp_secret_enc=encrypt(totp_key),
            source_ip=source_ip,
        )
        cred_stmt = cred_stmt.on_conflict_do_update(
            index_elements=["account_id"],
            set_={
                "api_key_enc": cred_stmt.excluded.api_key_enc,
                "api_secret_enc": cred_stmt.excluded.api_secret_enc,
                "password_enc": cred_stmt.excluded.password_enc,
                "totp_secret_enc": cred_stmt.excluded.totp_secret_enc,
                "source_ip": cred_stmt.excluded.source_ip,
                "updated_at": func.now(),
            },
        )
        session.execute(cred_stmt)

        session.commit()
        print(f"OK: {account_id} ({client_name}) registered, linked to {google_email}.")
    except IntegrityError as e:
        session.rollback()
        print(f"Failed -- likely google_email already linked to a different account: {e}")
    finally:
        session.close()


if __name__ == "__main__":
    main()
