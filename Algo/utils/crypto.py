"""
Symmetric encryption for broker credentials stored in Postgres
(account_credentials table). Key management: CREDS_ENCRYPTION_KEY must be set
in the environment wherever this is imported (the bot host only -- never the
AlgoRush-UI dashboard, which never decrypts).

Generate a key with:
    python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""
import os
from cryptography.fernet import Fernet, InvalidToken

_fernet: Fernet | None = None


def _get_fernet() -> Fernet:
    global _fernet
    if _fernet is None:
        key = os.environ.get("CREDS_ENCRYPTION_KEY")
        if not key:
            raise RuntimeError(
                "CREDS_ENCRYPTION_KEY is not set. Generate one with: "
                'python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
            )
        try:
            _fernet = Fernet(key.encode())
        except (ValueError, TypeError) as e:
            raise RuntimeError(f"CREDS_ENCRYPTION_KEY is not a valid Fernet key: {e}") from e
    return _fernet


def encrypt(plaintext: str) -> str:
    return _get_fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    try:
        return _get_fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as e:
        raise RuntimeError(
            "Failed to decrypt a credential -- wrong CREDS_ENCRYPTION_KEY or corrupted ciphertext."
        ) from e
