"""Tests for utility functions."""

import pytest

from email_cli.utils import resolve_account_name
from email_cli.config import add_account
from email_cli.models import Account


class TestResolveAccountName:
    def test_explicit_name(self, monkeypatch):
        assert resolve_account_name("work") == "work"

    def test_default_account(self, monkeypatch):
        acc = Account(name="personal", email="p@example.com")
        add_account(acc)
        assert resolve_account_name(None) == "personal"

    def test_no_accounts_raises(self, monkeypatch):
        with pytest.raises(RuntimeError, match="No accounts configured"):
            resolve_account_name(None)
