"""Fallback encryption for credentials when keyring is unavailable."""

import os
from pathlib import Path

from cryptography.fernet import Fernet

CONFIG_DIR = Path.home() / ".config" / "email-cli"
KEY_FILE = CONFIG_DIR / ".key"


def _get_or_create_key() -> bytes:
    if KEY_FILE.exists():
        with open(KEY_FILE, "rb") as f:
            return f.read()
    key = Fernet.generate_key()
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(KEY_FILE, "wb") as f:
        f.write(key)
    os.chmod(KEY_FILE, 0o600)
    return key


def encrypt(data: str) -> str:
    """Encrypt a string and return URL-safe base64 ciphertext."""
    f = Fernet(_get_or_create_key())
    return f.encrypt(data.encode("utf-8")).decode("utf-8")


def decrypt(token: str) -> str:
    """Decrypt a URL-safe base64 ciphertext string."""
    f = Fernet(_get_or_create_key())
    return f.decrypt(token.encode("utf-8")).decode("utf-8")
