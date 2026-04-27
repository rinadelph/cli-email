"""Tests for config module."""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from email_cli.config import (
    ACCOUNTS_FILE,
    add_account,
    get_account,
    list_account_names,
    load_accounts,
    remove_account,
    save_accounts,
    set_default_account,
)
from email_cli.models import Account


def test_load_accounts_empty():
    data = load_accounts()
    assert data == {"accounts": [], "default": None}


def test_add_and_get_account():
    acc = Account(name="work", email="work@example.com")
    add_account(acc)
    assert list_account_names() == ["work"]
    fetched = get_account("work")
    assert fetched is not None
    assert fetched.email == "work@example.com"


def test_add_duplicate_raises():
    acc = Account(name="work", email="work@example.com")
    add_account(acc)
    with pytest.raises(ValueError, match="already exists"):
        add_account(acc)


def test_remove_account():
    acc = Account(name="work", email="work@example.com")
    add_account(acc)
    remove_account("work")
    assert list_account_names() == []


def test_remove_nonexistent_raises():
    with pytest.raises(ValueError, match="not found"):
        remove_account("nobody")


def test_set_default():
    acc1 = Account(name="work", email="w@example.com")
    acc2 = Account(name="personal", email="p@example.com")
    add_account(acc1)
    add_account(acc2)
    set_default_account("personal")
    data = load_accounts()
    assert data["default"] == "personal"
