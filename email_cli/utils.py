"""Utility helpers."""

import os
import re
import sys
import time
from datetime import datetime, timedelta
from typing import Optional


# -- Timing / Tracing --

class Timer:
    """Context manager + explicit lap tracker for timing CLI operations.
    
    Usage:
        t = Timer("search")
        t.lap("imap_connect")
        client.imap_connect()
        t.lap("search")
        emails = client.search(...)
        t.lap("disconnect")
        client.imap_disconnect()
        t.finish()
        # t.elapsed_ms, t.laps, t.trace_lines all available
    """
    def __init__(self, label: str = "cmd"):
        self.label = label
        self.start = time.time()
        self.laps: list[tuple[str, float]] = [("start", self.start)]
        self.trace_lines: list[str] = []
    
    def lap(self, name: str) -> float:
        """Record a named checkpoint. Returns ms since last lap."""
        now = time.time()
        last_time = self.laps[-1][1]
        ms = (now - last_time) * 1000
        self.laps.append((name, now))
        self.trace_lines.append(f"[{self.label}] {name}: {ms:.0f}ms")
        return ms
    
    def finish(self) -> float:
        """Finish timing. Returns total ms."""
        now = time.time()
        ms = (now - self.start) * 1000
        self.laps.append(("done", now))
        self.trace_lines.append(f"[{self.label}] total: {ms:.0f}ms")
        return ms
    
    @property
    def elapsed_ms(self) -> int:
        return int((time.time() - self.start) * 1000)
    
    def dump_trace(self, file=None) -> None:
        """Print all trace lines to stderr (or given file)."""
        target = file or sys.stderr
        for line in self.trace_lines:
            print(line, file=target)
    
    def to_meta(self) -> dict:
        """Return a _meta-compatible timing dict."""
        lap_dict = {}
        prev_time = self.start
        for name, t in self.laps[1:]:  # skip "start"
            lap_dict[name] = int((t - prev_time) * 1000)
            prev_time = t
        return {
            "elapsed_ms": self.elapsed_ms,
            "timing": lap_dict,
        }


def _is_trace_enabled() -> bool:
    """Check if EMAIL_TRACE=1 is set for verbose timing output."""
    return os.environ.get("EMAIL_TRACE", "0").lower() in ("1", "true", "yes")


def trace(msg: str) -> None:
    """Print a trace line to stderr if EMAIL_TRACE is enabled."""
    if _is_trace_enabled():
        print(f"[trace] {msg}", file=sys.stderr)


# -- Account resolution --


def resolve_account_name(name: Optional[str]) -> str:
    """Resolve account name from explicit arg or default.
    
    If the default account has no credentials, auto-falls back to the
    first account that does, and emits a warning to stderr.
    """
    from email_cli.config import get_default_account_name, list_account_names, get_account, get_password

    if name:
        return name
    default = get_default_account_name()
    if default:
        pw = get_password(default)  # memoized — only hits keyring once
        acc = get_account(default)
        if pw and acc:
            return default
        # Default has no creds — find first healthy account
        names = list_account_names()
        for n in names:
            if n == default:
                continue  # already checked above
            if get_password(n):  # memoized
                print(
                    f"[email-cli] WARNING: Default account '{default}' has no credentials. "
                    f"Using '{n}' instead. Run `email accounts set-default {n}` to make this permanent.",
                    file=sys.stderr,
                )
                return n
        return default
    names = list_account_names()
    if not names:
        raise RuntimeError(
            "No accounts configured. Run 'email accounts add <name> <email>' first."
        )
    return names[0]


def get_healthy_account_name() -> Optional[str]:
    """Return the name of the first account with stored credentials, or None."""
    from email_cli.config import list_account_names, get_password
    for name in list_account_names():
        if get_password(name):
            return name
    return None


def parse_relative_date(value: str) -> Optional[str]:
    """Parse relative date expressions like '7d', 'today', 'this-week' into YYYY-MM-DD.
    
    Returns None if the value is already in YYYY-MM-DD format or unparseable.
    """
    if not value:
        return None
    
    # Already a full date
    if re.match(r'^\d{4}-\d{2}-\d{2}$', value):
        return value
    
    now = datetime.now()
    value_lower = value.lower().strip()
    
    if value_lower == "today":
        return now.strftime("%Y-%m-%d")
    if value_lower == "yesterday":
        return (now - timedelta(days=1)).strftime("%Y-%m-%d")
    if value_lower == "this-week":
        # Start of current week (Monday)
        days_since_monday = now.weekday()
        return (now - timedelta(days=days_since_monday)).strftime("%Y-%m-%d")
    if value_lower == "this-month":
        return now.replace(day=1).strftime("%Y-%m-%d")
    
    # Relative: Nd, Nw, Nm (days, weeks, months ago)
    m = re.match(r'^(\d+)([dwm])$', value_lower)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        if unit == "d":
            return (now - timedelta(days=n)).strftime("%Y-%m-%d")
        elif unit == "w":
            return (now - timedelta(weeks=n)).strftime("%Y-%m-%d")
        elif unit == "m":
            # Approximate months as 30 days
            return (now - timedelta(days=n * 30)).strftime("%Y-%m-%d")
    
    return value  # Pass through — let date parser handle it
