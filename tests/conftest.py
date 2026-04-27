"""Shared pytest fixtures."""

import tempfile
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def temp_config(monkeypatch):
    """Use a temporary directory for all config and keyring operations."""
    with tempfile.TemporaryDirectory() as tmpdir:
        config_path = Path(tmpdir) / "accounts.json"
        monkeypatch.setattr("email_cli.config.ACCOUNTS_FILE", config_path)
        monkeypatch.setattr("email_cli.config.CONFIG_DIR", Path(tmpdir))
        yield
