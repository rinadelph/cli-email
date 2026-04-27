"""SQLite local cache for fast email lookups."""

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

CACHE_FILE = Path.home() / ".config" / "email-cli" / "cache.db"

STALE_MINUTES = 5


def _get_conn() -> sqlite3.Connection:
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(CACHE_FILE), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    _ensure_schema(conn)
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS accounts (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL UNIQUE,
            email TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS folders (
            id INTEGER PRIMARY KEY,
            account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            last_sync_at TIMESTAMP,
            total_messages INTEGER DEFAULT 0,
            uidvalidity TEXT DEFAULT '',
            UNIQUE(account_id, name)
        );
        CREATE TABLE IF NOT EXISTS emails (
            id INTEGER PRIMARY KEY,
            account_id INTEGER NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
            folder_id INTEGER NOT NULL REFERENCES folders(id) ON DELETE CASCADE,
            uid TEXT NOT NULL,
            subject TEXT DEFAULT '',
            sender TEXT DEFAULT '',
            recipients TEXT DEFAULT '',
            date TIMESTAMP,
            raw_date TEXT,
            body_preview TEXT DEFAULT '',
            flags TEXT DEFAULT '',
            size INTEGER DEFAULT 0,
            has_attachments INTEGER DEFAULT 0,
            cached_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(account_id, folder_id, uid)
        );
        CREATE INDEX IF NOT EXISTS idx_emails_account_folder ON emails(account_id, folder_id);
        CREATE INDEX IF NOT EXISTS idx_emails_date ON emails(date);
        CREATE INDEX IF NOT EXISTS idx_emails_subject ON emails(subject);
        CREATE INDEX IF NOT EXISTS idx_emails_sender ON emails(sender);
        CREATE INDEX IF NOT EXISTS idx_emails_body ON emails(body_preview);
        CREATE INDEX IF NOT EXISTS idx_emails_recipients ON emails(recipients);
        """
    )
    conn.commit()
    # Migrate existing databases: add uidvalidity column to folders
    try:
        conn.execute("ALTER TABLE folders ADD COLUMN uidvalidity TEXT DEFAULT ''")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # Column already exists


def upsert_account(name: str, email: str) -> int:
    conn = _get_conn()
    cur = conn.execute(
        "INSERT INTO accounts (name, email) VALUES (?, ?) ON CONFLICT(name) DO UPDATE SET email=excluded.email RETURNING id",
        (name, email),
    )
    row = cur.fetchone()
    conn.commit()
    conn.close()
    return row["id"]


def upsert_folder(account_id: int, name: str) -> int:
    conn = _get_conn()
    cur = conn.execute(
        "INSERT INTO folders (account_id, name) VALUES (?, ?) ON CONFLICT(account_id, name) DO UPDATE SET name=excluded.name RETURNING id",
        (account_id, name),
    )
    row = cur.fetchone()
    conn.commit()
    conn.close()
    return row["id"]


def sync_emails(account_id: int, folder_id: int, emails: list[dict]) -> None:
    """Replace cached emails for a folder with fresh data."""
    conn = _get_conn()
    now = datetime.now().isoformat()
    conn.execute("DELETE FROM emails WHERE account_id=? AND folder_id=?", (account_id, folder_id))
    for e in emails:
        conn.execute(
            """
            INSERT INTO emails (account_id, folder_id, uid, subject, sender, recipients, date, raw_date, body_preview, flags, size, has_attachments, cached_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                account_id,
                folder_id,
                e["uid"],
                e.get("subject", ""),
                e.get("sender", ""),
                e.get("to", ""),
                e.get("date"),
                e.get("raw_date", ""),
                e.get("body_preview", ""),
                json.dumps(e.get("flags", [])),
                e.get("size", 0),
                1 if e.get("has_attachments") else 0,
                now,
            ),
        )
    conn.execute(
        "UPDATE folders SET last_sync_at=?, total_messages=? WHERE id=?",
        (now, len(emails), folder_id),
    )
    conn.commit()
    conn.close()


def is_stale(account_id: int, folder_id: int) -> bool:
    conn = _get_conn()
    cur = conn.execute(
        "SELECT last_sync_at FROM folders WHERE account_id=? AND id=?",
        (account_id, folder_id),
    )
    row = cur.fetchone()
    conn.close()
    if not row or not row["last_sync_at"]:
        return True
    last_sync = datetime.fromisoformat(row["last_sync_at"])
    return datetime.now() - last_sync > timedelta(minutes=STALE_MINUTES)


def get_cached_emails(
    account_id: int,
    folder_id: int,
    limit: int = 20,
    query: Optional[str] = None,
    since: Optional[str] = None,
    before: Optional[str] = None,
    in_field: Optional[str] = None,
) -> list[dict]:
    """Query cached emails. Returns list of dicts matching EmailMessage fields."""
    conn = _get_conn()
    sql = "SELECT * FROM emails WHERE account_id=? AND folder_id=?"
    params: list = [account_id, folder_id]

    if query:
        q = f"%{query}%"
        if in_field == "subject":
            sql += " AND subject LIKE ?"
            params.append(q)
        elif in_field == "from":
            sql += " AND sender LIKE ?"
            params.append(q)
        elif in_field == "to":
            sql += " AND recipients LIKE ?"
            params.append(q)
        elif in_field == "body":
            sql += " AND body_preview LIKE ?"
            params.append(q)
        else:
            sql += " AND (subject LIKE ? OR sender LIKE ? OR recipients LIKE ? OR body_preview LIKE ?)"
            params.extend([q, q, q, q])

    if since:
        sql += " AND date >= ?"
        params.append(since)
    if before:
        sql += " AND date < ?"
        params.append(before)

    sql += " ORDER BY date DESC LIMIT ?"
    params.append(limit)

    cur = conn.execute(sql, params)
    rows = cur.fetchall()
    conn.close()

    results = []
    for r in rows:
        results.append({
            "uid": r["uid"],
            "subject": r["subject"],
            "sender": r["sender"],
            "to": r["recipients"],
            "date": r["date"] and datetime.fromisoformat(r["date"]),
            "raw_date": r["raw_date"],
            "body_preview": r["body_preview"],
            "flags": json.loads(r["flags"]) if r["flags"] else [],
            "size": r["size"],
            "has_attachments": bool(r["has_attachments"]),
        })
    return results


def clear_account_cache(account_id: int) -> None:
    conn = _get_conn()
    conn.execute("DELETE FROM emails WHERE account_id=?", (account_id,))
    conn.execute("DELETE FROM folders WHERE account_id=?", (account_id,))
    conn.execute("DELETE FROM accounts WHERE id=?", (account_id,))
    conn.commit()
    conn.close()


def get_folder_id(account_id: int, folder_name: str) -> Optional[int]:
    conn = _get_conn()
    cur = conn.execute(
        "SELECT id FROM folders WHERE account_id=? AND name=?",
        (account_id, folder_name),
    )
    row = cur.fetchone()
    conn.close()
    return row["id"] if row else None


def get_uidvalidity(folder_id: int) -> str:
    conn = _get_conn()
    cur = conn.execute("SELECT uidvalidity FROM folders WHERE id=?", (folder_id,))
    row = cur.fetchone()
    conn.close()
    return row["uidvalidity"] if row and row["uidvalidity"] else ""


def set_uidvalidity(folder_id: int, uidv: str) -> None:
    conn = _get_conn()
    conn.execute("UPDATE folders SET uidvalidity=? WHERE id=?", (uidv, folder_id))
    conn.commit()
    conn.close()


def clear_folder(folder_id: int) -> None:
    conn = _get_conn()
    conn.execute("DELETE FROM emails WHERE folder_id=?", (folder_id,))
    conn.commit()
    conn.close()
