"""Email daemon — persistent IMAP connection pool.

Runs as a background process. Keeps IMAP connections warm so subsequent
CLI calls are near-instant. Communicates over a Unix domain socket
using JSON-RPC.

Usage:
    email daemon start     # Start in background
    email daemon status    # Check if running
    email daemon stop      # Graceful shutdown
"""

import fcntl
import imaplib
import json
import os
import re
import select
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from email_cli.config import get_account, get_password, load_accounts
from email_cli.models import EmailMessage, parse_address, parse_address_list


def _decode_body_preview(raw_body: bytes, max_len: int = 200) -> str:
    """Decode a raw MIME body part into a clean text preview.

    Handles base64, quoted-printable, and HTML bodies.
    Falls back to raw text if decoding fails.
    """
    import quopri
    import base64 as b64

    try:
        # Step 1: Try quoted-printable decode (most common for email text)
        text = raw_body.decode("utf-8", errors="replace")

        # Detect and decode quoted-printable (=\r\n soft lines, =XX hex chars)
        if re.search(r'=\r?\n', text) or re.search(r'=[0-9A-Fa-f]{2}', text):
            try:
                decoded = quopri.decodestring(raw_body)
                text = decoded.decode("utf-8", errors="replace")
            except Exception:
                pass

        # Step 2: If result looks like pure base64 (no spaces, all alphanumeric+/=)
        stripped = text.strip()
        if (stripped
            and not stripped.startswith("--")
            and not stripped.startswith("Content-")
            and re.match(r'^[A-Za-z0-9+/=\s]+$', stripped)
            and len(stripped.replace('\n','').replace('\r','').replace(' ','')) % 4 == 0
            and len(stripped) > 20
            and ' ' not in stripped[:60]):  # Real base64 has no spaces early on
            try:
                decoded = b64.b64decode(stripped)
                candidate = decoded.decode("utf-8", errors="replace")
                # Verify it decoded to readable text
                if len(candidate) > 10 and any(c.isalpha() for c in candidate[:50]):
                    text = candidate
            except Exception:
                pass

        # Step 3: Strip MIME boundaries and headers
        text = re.sub(r'^--[_\w\-]+$', '', text, flags=re.MULTILINE)
        text = re.sub(r'^Content-Transfer-Encoding:.*$', '', text, flags=re.MULTILINE | re.IGNORECASE)
        text = re.sub(r'^Content-Type:.*$', '', text, flags=re.MULTILINE | re.IGNORECASE)
        text = re.sub(r'^Content-Disposition:.*$', '', text, flags=re.MULTILINE | re.IGNORECASE)

        # Step 4: If HTML, strip tags
        if "<html" in text.lower() or "<body" in text.lower() or "<div" in text.lower() or "<p>" in text.lower():
            text = _html_to_text(text)

        # Step 5: Clean whitespace
        text = text.replace("\n", " ").replace("\r", "")
        text = re.sub(r'\s+', ' ', text).strip()

        return text[:max_len]
    except Exception:
        return ""


def _html_to_text(html_str: str) -> str:
    """Convert HTML to plain text."""
    from email_cli.client import _HTMLStripper
    stripper = _HTMLStripper()
    try:
        stripper.feed(html_str)
    except Exception:
        text = re.sub(r"<[^>]+>", "", html_str)
        return text.strip()
    return stripper.get_text()

SOCKET_PATH = Path.home() / ".config" / "email-cli" / "daemon.sock"
PID_FILE = Path.home() / ".config" / "email-cli" / "daemon.pid"
IDLE_TIMEOUT = 600  # 10 minutes — shut down if no requests


class ImapPool:
    """Pool of warm IMAP connections, one per account.

    Keeps connections logged in and folders selected to avoid
    expensive re-select on every search. Tracks which folder is
    currently selected per connection.
    """

    def __init__(self):
        self._connections: dict[str, imaplib.IMAP4_SSL] = {}
        self._selected_folder: dict[str, str] = {}
        self._last_used: dict[str, float] = {}
        self._passwords: dict[str, str] = {}

    def get(self, account_name: str, folder: str = "INBOX") -> imaplib.IMAP4_SSL:
        """Get or create a warm IMAP connection with the given folder selected."""
        now = time.time()
        # Check if existing connection is still alive
        if account_name in self._connections:
            conn = self._connections[account_name]
            try:
                conn.noop()
                self._last_used[account_name] = now
                # Ensure correct folder is selected
                current_folder = self._selected_folder.get(account_name)
                if current_folder != folder:
                    # Close current folder first (faster than SELECT on some servers)
                    try:
                        conn.close()
                    except Exception:
                        pass
                    status, _ = conn.select(folder)
                    if status == "OK":
                        self._selected_folder[account_name] = folder
                return conn
            except Exception:
                try:
                    conn.logout()
                except Exception:
                    pass
                del self._connections[account_name]
                self._selected_folder.pop(account_name, None)

        # Create new connection
        account = get_account(account_name)
        if not account:
            raise RuntimeError(f"Account '{account_name}' not found.")
        pw = self._passwords.get(account_name) or get_password(account_name)
        if not pw:
            raise RuntimeError(f"No password for account '{account_name}'.")
        self._passwords[account_name] = pw

        conn = imaplib.IMAP4_SSL(
            host=account.imap_host,
            port=account.imap_port,
            timeout=30,
        )
        conn.login(account.email, pw)
        conn.select(folder)
        self._connections[account_name] = conn
        self._selected_folder[account_name] = folder
        self._last_used[account_name] = now
        return conn

    def disconnect(self, account_name: str) -> None:
        """Disconnect a specific account."""
        conn = self._connections.pop(account_name, None)
        if conn:
            try:
                conn.logout()
            except Exception:
                pass
        self._last_used.pop(account_name, None)
        self._selected_folder.pop(account_name, None)

    def disconnect_all(self) -> None:
        """Disconnect all accounts."""
        for name in list(self._connections):
            self.disconnect(name)

    def prune_idle(self, max_idle: float = 300) -> list[str]:
        """Disconnect accounts idle longer than max_idle seconds. Returns pruned names."""
        now = time.time()
        pruned = []
        for name in list(self._last_used):
            idle = now - self._last_used[name]
            if idle > max_idle:
                self.disconnect(name)
                pruned.append(name)
        return pruned

    def status(self) -> dict:
        """Return pool status."""
        now = time.time()
        accounts = []
        for name, conn in self._connections.items():
            idle = now - self._last_used.get(name, now)
            try:
                conn.noop()
                alive = True
            except Exception:
                alive = False
            accounts.append({
                "name": name,
                "connected": alive,
                "selected_folder": self._selected_folder.get(name, ""),
                "idle_seconds": int(idle),
            })
        return {
            "active_connections": len(self._connections),
            "accounts": accounts,
        }


def _search_via_pool(pool: ImapPool, params: dict) -> dict:
    """Execute a search using the pool's warm connections.

    Uses IMAP server-side search when possible (FROM, SUBJECT, TO criteria)
    instead of fetching all emails and filtering client-side.
    """
    account_name = params.get("account")
    folder = params.get("folder", "INBOX")
    query = params.get("query", "")
    in_field = params.get("in_field")
    limit = params.get("limit", 20)
    since = params.get("since")
    before = params.get("before")

    # Get connection with folder already selected (pool handles SELECT)
    conn = pool.get(account_name, folder)

    # Build IMAP search criteria — use server-side when possible
    criteria_parts = []
    if in_field == "from" and query:
        criteria_parts.append(f'FROM "{query}"')
    elif in_field == "subject" and query:
        criteria_parts.append(f'SUBJECT "{query}"')
    elif in_field == "to" and query:
        criteria_parts.append(f'TO "{query}"')
    elif query:
        criteria_parts.append(f'TEXT "{query}"')
    else:
        criteria_parts.append("ALL")

    if since:
        try:
            d = datetime.strptime(since, "%Y-%m-%d")
            criteria_parts.append(f"SINCE {d.strftime('%d-%b-%Y')}")
        except (ValueError, TypeError):
            pass
    if before:
        try:
            d = datetime.strptime(before, "%Y-%m-%d")
            criteria_parts.append(f"BEFORE {d.strftime('%d-%b-%Y')}")
        except (ValueError, TypeError):
            pass

    criteria = " ".join(criteria_parts)

    status, data = conn.search(None, criteria)
    if status != "OK" or not data or not data[0]:
        return {"emails": [], "total": 0}

    uids = data[0].split()
    uids = uids[-limit:]

    import email as email_lib
    from email_cli.client import EmailClient

    results = []
    for uid in reversed(uids):
        uid_str = uid.decode()
        # Fetch headers + partial body + flags in one round trip
        # BODY.PEEK[1]<0.2048> gets first 2KB of first body part (text/plain)
        # This is ~100x lighter than fetching full RFC822
        status, msg_data = conn.fetch(
            uid,
            '(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM TO DATE CONTENT-TYPE)] BODY.PEEK[1]<0.2048> FLAGS)',
        )
        if status != "OK" or not msg_data:
            continue

        raw_headers = None
        raw_body = None
        flags = []
        for part in msg_data:
            if isinstance(part, tuple) and len(part) == 2:
                meta, payload = part
                meta_str = meta.decode("utf-8", errors="replace") if isinstance(meta, bytes) else meta
                if "HEADER" in meta_str.upper() or "FIELDS" in meta_str.upper():
                    raw_headers = payload
                elif "BODY" in meta_str.upper() and "HEADER" not in meta_str.upper():
                    raw_body = payload
            elif isinstance(part, bytes) and b"FLAGS" in part:
                flags = EmailClient._parse_flags(part.decode())

        if raw_headers is None:
            continue

        msg = email_lib.message_from_bytes(raw_headers)
        subject = EmailClient._decode_header_value(msg.get("Subject", ""))
        sender = EmailClient._decode_header_value(msg.get("From", ""))
        to = EmailClient._decode_header_value(msg.get("To", ""))
        raw_date = msg.get("Date", "")
        parsed_date = EmailClient._parse_date(raw_date)

        # Extract body preview from partial body fetch
        body_preview = ""
        if raw_body:
            body_preview = _decode_body_preview(raw_body)

        results.append({
            "uid": uid_str,
            "subject": subject,
            "sender": parse_address(sender),
            "to": parse_address_list(to),
            "date": parsed_date.isoformat() if parsed_date else None,
            "body_preview": body_preview,
            "flags": flags,
            "size": 0,
            "has_attachments": False,
            "account": account_name,
        })

    # Don't close the folder — pool keeps it selected for next query
    return {"emails": results[:limit], "total": len(results)}


def _handle_request(pool: ImapPool, request: dict) -> dict:
    """Route a JSON-RPC request to the appropriate handler."""
    method = request.get("method", "")
    params = request.get("params", {})

    if method == "search":
        return _search_via_pool(pool, params)
    elif method == "status":
        return pool.status()
    elif method == "ping":
        return {"pong": True, "time": datetime.now().isoformat()}
    elif method == "shutdown":
        return {"shutting_down": True}
    else:
        return {"error": f"Unknown method: {method}"}


def run_server():
    """Run the daemon server loop."""
    SOCKET_PATH.parent.mkdir(parents=True, exist_ok=True)

    # Clean up stale socket
    if SOCKET_PATH.exists():
        SOCKET_PATH.unlink()

    pool = ImapPool()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(SOCKET_PATH))
    server.listen(5)
    server.setblocking(False)

    # Write PID file
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))

    # Detach from parent (double-fork for background)
    print(f"[email-daemon] Listening on {SOCKET_PATH}", file=sys.stderr)
    print(f"[email-daemon] Idle timeout: {IDLE_TIMEOUT}s", file=sys.stderr)

    last_activity = time.time()
    running = True

    def handle_signal(signum, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    while running:
        # Prune idle connections every loop
        pool.prune_idle()

        # Check for idle shutdown
        idle_time = time.time() - last_activity
        if idle_time > IDLE_TIMEOUT and not pool._connections:
            print(f"[email-daemon] Idle for {IDLE_TIMEOUT}s, shutting down.", file=sys.stderr)
            break

        # Wait for connections with timeout
        try:
            readable, _, _ = select.select([server], [], [], 5.0)
        except (select.error, OSError):
            continue

        if not readable:
            continue

        try:
            conn, _ = server.accept()
        except Exception:
            continue

        last_activity = time.time()

        try:
            # Read request (length-prefixed JSON)
            data = b""
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                data += chunk
                if data.endswith(b"\n"):
                    break

            if not data:
                conn.close()
                continue

            request = json.loads(data.decode())
            t0 = time.time()
            response = _handle_request(pool, request)
            elapsed = (time.time() - t0) * 1000
            response["elapsed_ms"] = int(elapsed)

            if request.get("method") == "shutdown":
                running = False

            conn.sendall(json.dumps(response).encode() + b"\n")
        except Exception as exc:
            try:
                conn.sendall(json.dumps({"error": str(exc)}).encode() + b"\n")
            except Exception:
                pass
        finally:
            conn.close()

    # Cleanup
    pool.disconnect_all()
    server.close()
    if SOCKET_PATH.exists():
        SOCKET_PATH.unlink()
    if PID_FILE.exists():
        PID_FILE.unlink()
    print("[email-daemon] Shut down.", file=sys.stderr)


def send_to_daemon(request: dict, timeout: float = 30.0) -> Optional[dict]:
    """Send a request to the daemon and return the response."""
    if not SOCKET_PATH.exists():
        return None

    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(str(SOCKET_PATH))
        sock.sendall(json.dumps(request).encode() + b"\n")

        data = b""
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            data += chunk

        sock.close()

        if not data:
            return None

        return json.loads(data.decode())
    except Exception:
        return None


def is_daemon_running() -> bool:
    """Check if the daemon is running and responsive."""
    if not PID_FILE.exists():
        return False

    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)  # Check if process exists
    except (ProcessLookupError, ValueError, PermissionError):
        return False

    # Ping it
    resp = send_to_daemon({"method": "ping"})
    return resp is not None and resp.get("pong") is True


def start_daemon(background: bool = True) -> bool:
    """Start the daemon process."""
    if is_daemon_running():
        print("[email-daemon] Already running.", file=sys.stderr)
        return True

    if background:
        # Double-fork to detach
        pid = os.fork()
        if pid > 0:
            # Parent — wait briefly for daemon to start
            time.sleep(0.5)
            return is_daemon_running()
        # Child — become session leader, then fork again
        os.setsid()
        pid2 = os.fork()
        if pid2 > 0:
            os._exit(0)
        # Grandchild — redirect stdio, run server
        sys.stdin.close()
        devnull = os.open(os.devnull, os.O_WRONLY)
        os.dup2(devnull, 1)
        os.dup2(devnull, 2)
        os.close(devnull)
        run_server()
        os._exit(0)
    else:
        run_server()
        return True


def stop_daemon() -> bool:
    """Stop the daemon gracefully."""
    resp = send_to_daemon({"method": "shutdown"})
    if resp and resp.get("shutting_down"):
        # Wait for it to actually stop
        for _ in range(10):
            time.sleep(0.3)
            if not is_daemon_running():
                return True
        return False
    # Force kill via PID
    if PID_FILE.exists():
        try:
            pid = int(PID_FILE.read_text().strip())
            os.kill(pid, signal.SIGTERM)
            time.sleep(0.5)
            return True
        except (ProcessLookupError, ValueError, PermissionError):
            pass
    # Clean up stale files
    if SOCKET_PATH.exists():
        SOCKET_PATH.unlink()
    if PID_FILE.exists():
        PID_FILE.unlink()
    return False
