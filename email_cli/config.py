"""Configuration management: accounts JSON and credential storage."""

import json
import os
from pathlib import Path
from typing import Optional

import keyring
from keyring.errors import KeyringError

from email_cli.models import Account


CONFIG_DIR = Path.home() / ".config" / "email-cli"
ACCOUNTS_FILE = CONFIG_DIR / "accounts.json"


def _ensure_config_dir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def load_accounts() -> dict:
    """Load accounts config from JSON. Returns dict with 'accounts' list and 'default'."""
    _ensure_config_dir()
    if not ACCOUNTS_FILE.exists():
        return {"accounts": [], "default": None}
    with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_accounts(data: dict) -> None:
    """Save accounts config to JSON."""
    _ensure_config_dir()
    with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def get_account(name: str) -> Optional[Account]:
    """Get an account by name."""
    data = load_accounts()
    for acc in data.get("accounts", []):
        if acc["name"] == name:
            return Account(**acc)
    return None


def list_account_names() -> list[str]:
    """Return list of configured account names."""
    data = load_accounts()
    return [a["name"] for a in data.get("accounts", [])]


def get_default_account_name() -> Optional[str]:
    """Return the default account name, or None."""
    data = load_accounts()
    return data.get("default")


def set_default_account(name: str) -> None:
    """Set the default account name."""
    data = load_accounts()
    names = [a["name"] for a in data.get("accounts", [])]
    if name not in names:
        raise ValueError(f"Account '{name}' not found.")
    data["default"] = name
    save_accounts(data)


def add_account(account: Account) -> None:
    """Add a new account to config."""
    data = load_accounts()
    names = [a["name"] for a in data.get("accounts", [])]
    if account.name in names:
        raise ValueError(f"Account name '{account.name}' already exists.")
    data["accounts"].append(account.model_dump())
    if data["default"] is None:
        data["default"] = account.name
    save_accounts(data)


def remove_account(name: str) -> None:
    """Remove an account from config and delete its stored password."""
    data = load_accounts()
    original_len = len(data.get("accounts", []))
    data["accounts"] = [a for a in data.get("accounts", []) if a["name"] != name]
    if len(data["accounts"]) == original_len:
        raise ValueError(f"Account '{name}' not found.")
    if data.get("default") == name:
        data["default"] = data["accounts"][0]["name"] if data["accounts"] else None
    save_accounts(data)
    delete_password(name)


# -- Credential storage via keyring --

SERVICE_NAME = "email-cli"

# In-process cache: avoid hitting keyring DBus more than once per account per process
_password_cache: dict[str, Optional[str]] = {}


def _keyring_service(account_name: str) -> str:
    return f"{SERVICE_NAME}/{account_name}"


def store_password(account_name: str, password: str) -> None:
    """Store password in system keyring and cache."""
    try:
        keyring.set_password(_keyring_service(account_name), "password", password)
        _password_cache[account_name] = password
    except KeyringError as exc:
        raise RuntimeError(f"Failed to store password in keyring: {exc}") from exc


def get_password(account_name: str) -> Optional[str]:
    """Retrieve password from cache or system keyring. Memoized per process."""
    if account_name in _password_cache:
        return _password_cache[account_name]
    try:
        pw = keyring.get_password(_keyring_service(account_name), "password")
    except KeyringError:
        pw = None
    _password_cache[account_name] = pw
    return pw


def delete_password(account_name: str) -> None:
    """Delete password from system keyring and cache."""
    _password_cache.pop(account_name, None)
    try:
        keyring.delete_password(_keyring_service(account_name), "password")
    except KeyringError:
        pass
