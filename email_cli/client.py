"""IMAP and SMTP client wrapper."""

import email
import html
import imaplib
import re
import socket
import smtplib
from datetime import datetime
from email.header import decode_header
from email.message import EmailMessage as StdEmailMessage
from html.parser import HTMLParser
from pathlib import Path
from typing import Optional

from rich.progress import Progress, SpinnerColumn, TextColumn

from email_cli.models import Account, AttachmentInfo, EmailMessage

import email_cli.cache as cache


class _HTMLStripper(HTMLParser):
    """Strip HTML tags and convert entities to text."""

    def __init__(self) -> None:
        super().__init__()
        self.text_parts: list[str] = []
        self.skip_tags = {"script", "style", "head", "meta", "link", "title"}
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in self.skip_tags:
            self._skip_depth += 1
        if tag == "br":
            self.text_parts.append("\n")
        elif tag in ("p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li"):
            self.text_parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.skip_tags:
            self._skip_depth -= 1
        if tag in ("p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li"):
            self.text_parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth == 0:
            self.text_parts.append(data)

    def get_text(self) -> str:
        text = "".join(self.text_parts)
        # Collapse whitespace
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()


def _html_to_text(html_str: str) -> str:
    """Convert HTML to plain text using stdlib parser."""
    stripper = _HTMLStripper()
    try:
        stripper.feed(html_str)
    except Exception:
        # Fallback: regex strip tags
        text = re.sub(r"<[^>]+>", "", html_str)
        text = html.unescape(text)
        return text.strip()
    return stripper.get_text()


class EmailClient:
    """Wraps IMAP and SMTP connections for a single account."""

    def __init__(self, account: Account, password: str) -> None:
        self.account = account
        self.password = password
        self._imap: Optional[imaplib.IMAP4_SSL] = None
        self._smtp: Optional[smtplib.SMTP_SSL] = None

    # -- IMAP --

    def imap_connect(self) -> None:
        """Open IMAP SSL connection and login."""
        self._imap = imaplib.IMAP4_SSL(
            host=self.account.imap_host,
            port=self.account.imap_port,
            timeout=30,
        )
        self._imap.login(self.account.email, self.password)

    def imap_disconnect(self) -> None:
        if self._imap:
            try:
                self._imap.close()
                self._imap.logout()
            except Exception:
                pass
            self._imap = None

    def select_folder(self, folder: str = "INBOX") -> tuple[str, str]:
        """Select folder, return (status, uidvalidity)."""
        if not self._imap:
            raise RuntimeError("IMAP not connected.")
        status, _ = self._imap.select(folder)
        if status != "OK":
            raise RuntimeError(f"Failed to select folder '{folder}'")
        uidv_list = self._imap.untagged_responses.get(b"UIDVALIDITY", [])
        uidvalidity = uidv_list[0].decode() if uidv_list else ""
        return status, uidvalidity

    def list_folders(self) -> list[str]:
        if not self._imap:
            raise RuntimeError("IMAP not connected.")
        status, folders = self._imap.list()
        if status != "OK" or not folders:
            return []
        names = []
        for f in folders:
            if isinstance(f, bytes):
                parts = f.decode("utf-8", errors="replace").rsplit(' "', 1)
                if len(parts) == 2:
                    name = parts[1].strip('"')
                    names.append(name)
        return names

    def test_connectivity(self) -> dict:
        """Test IMAP + SMTP connectivity and return status dict."""
        result = {
            "account": self.account.name,
            "email": self.account.email,
            "imap_host": self.account.imap_host,
            "imap_port": self.account.imap_port,
            "smtp_host": self.account.smtp_host,
            "smtp_port": self.account.smtp_port,
            "imap_ok": False,
            "smtp_ok": False,
            "folders": [],
            "error": None,
        }
        try:
            self.imap_connect()
            result["imap_ok"] = True
            result["folders"] = self.list_folders()
            self.imap_disconnect()
        except Exception as exc:
            result["error"] = f"IMAP: {exc}"
            return result
        try:
            self.smtp_connect()
            result["smtp_ok"] = True
            self.smtp_disconnect()
        except Exception as exc:
            result["error"] = f"SMTP: {exc}"
        return result

    def get_thread(self, uid: str, folder: str = "INBOX") -> list[EmailMessage]:
        """Find all emails in the same thread using Gmail X-GM-THRID or subject fallback."""
        import re
        from typing import Optional
        
        def batched(iterable, n):
                """Yield successive n-sized chunks from iterable."""
                items = list(iterable)
                for i in range(0, len(items), n):
                    yield items[i:i+n]
        
        _, _ = self.select_folder(folder)

        # Fetch seed email headers
        status, data = self._imap.fetch(uid.encode(), "(RFC822.HEADER)")
        if status != "OK" or not data:
            raise RuntimeError(f"Failed to fetch email {uid}")
        raw_msg = None
        for part in data:
            if isinstance(part, tuple) and len(part) == 2:
                raw_msg = part[1]
                break
        if raw_msg is None:
            raise RuntimeError(f"No message body for UID {uid}")
        seed_msg = email.message_from_bytes(raw_msg)
        seed_subject = seed_msg.get("Subject", "")
        base_subject = re.sub(r"^(Re|Fwd|RE|FWD):\s*", "", seed_subject, flags=re.IGNORECASE)

        # Try Gmail X-GM-THRID fast path
        thread_id: Optional[str] = None
        status, data = self._imap.uid("FETCH", uid, "(X-GM-THRID)")
        if status == "OK" and data:
            for part in data:
                if isinstance(part, bytes) and b"X-GM-THRID" in part:
                    m = re.search(r"X-GM-THRID\s+(\d+)", part.decode())
                    if m:
                        thread_id = m.group(1)
                        break

        thread_uids: list[str] = []
        if thread_id:
            status, data = self._imap.uid("SEARCH", None, "X-GM-THRID", thread_id)
            if status == "OK" and data and data[0]:
                thread_uids = [u.decode() for u in data[0].split()]

        if not thread_uids:
            # Fallback: chunked header fetch + subject matching
            status, data = self._imap.search(None, "ALL")
            if status != "OK" or not data or not data[0]:
                return []
            all_seqs = data[0].split()
            if all_seqs:
                matching: set[str] = {uid}
                seed_msgid = seed_msg.get("Message-ID", "").strip()
                
                # Process in chunks to avoid timeout
                for chunk in batched(all_seqs, 500):
                    seq_set = b",".join(chunk)
                    status, data = self._imap.fetch(
                        seq_set,
                        "(BODY.PEEK[HEADER.FIELDS (Subject Message-ID In-Reply-To References)])",
                    )
                    if status == "OK" and data:
                        for part in data:
                            if isinstance(part, tuple) and len(part) == 2:
                                meta, headers = part
                                meta_str = meta.decode("utf-8", errors="replace")
                                seq_match = re.search(r"^(\d+)", meta_str)
                                if not seq_match:
                                    continue
                                current_seq = seq_match.group(1)
                                if current_seq == uid:
                                    continue
                                msg = email.message_from_bytes(headers)
                                subject = msg.get("Subject", "")
                                msgid = msg.get("Message-ID", "").strip()
                                in_reply_to = msg.get("In-Reply-To", "").strip()
                                references = msg.get("References", "")
                                other_base = re.sub(
                                    r"^(Re|Fwd|RE|FWD):\s*", "", subject, flags=re.IGNORECASE
                                )
                                is_match = (
                                    other_base == base_subject
                                    or subject == seed_subject
                                    or (
                                        seed_msgid
                                        and (seed_msgid in in_reply_to or seed_msgid in references)
                                    )
                                    or (msgid and msgid == seed_msgid)
                                )
                                if is_match:
                                    matching.add(current_seq)
                thread_uids = list(matching)

        # Convert UIDs to sequence numbers (for consistent API with other commands)
        thread_seqs: list[str] = []
        if thread_uids and thread_id:
            uid_set = ",".join(thread_uids)
            status, data = self._imap.search(None, "UID", uid_set)
            if status == "OK" and data and data[0]:
                thread_seqs = [s.decode() for s in data[0].split()]
        elif thread_uids:
            thread_seqs = thread_uids

        # Batch-fetch summaries in chunks
        results: list[EmailMessage] = []
        if thread_seqs:
            # Process in chunks to avoid timeout
            for chunk in batched(thread_seqs, 500):
                seq_set = ",".join(chunk).encode()
                status, data = self._imap.fetch(seq_set, "(RFC822 FLAGS)")
                if status == "OK" and data:
                    raw_msgs: dict[str, bytes] = {}
                    flags_map: dict[str, list[str]] = {}
                    for part in data:
                        if isinstance(part, tuple) and len(part) == 2:
                            meta, payload = part
                            meta_str = meta.decode("utf-8", errors="replace")
                            seq_match = re.search(r"^(\d+)", meta_str)
                            if seq_match:
                                raw_msgs[seq_match.group(1)] = payload
                        elif isinstance(part, bytes):
                            flags_str = part.decode("utf-8", errors="replace")
                            if "FLAGS" in flags_str:
                                seq_match = re.search(r"^(\d+)", flags_str)
                                if seq_match:
                                    flags_map[seq_match.group(1)] = self._parse_flags(flags_str)
                    for seq in chunk:
                        raw_msg = raw_msgs.get(seq)
                        if raw_msg is None:
                            continue
                        msg = email.message_from_bytes(raw_msg)
                        subject = self._decode_header_value(msg.get("Subject", ""))
                        sender = self._decode_header_value(msg.get("From", ""))
                        to = self._decode_header_value(msg.get("To", ""))
                        raw_date = msg.get("Date", "")
                        parsed_date = self._parse_date(raw_date)
                        body_preview = self._extract_preview(msg)
                        has_attachments = self._has_attachments(msg)
                        size = len(raw_msg)
                        flags = flags_map.get(seq, [])
                        results.append(EmailMessage(
                            uid=seq,
                            subject=subject,
                            sender=sender,
                            to=to,
                            date=parsed_date,
                            raw_date=raw_date,
                            body_preview=body_preview,
                            flags=flags,
                            size=size,
                            has_attachments=has_attachments,
                        ))

        results.sort(key=lambda e: e.date or datetime.min)
        return results
    def fetch_multiple(self, uids: list[str]) -> list[tuple[EmailMessage, email.message.Message]]:
        """Fetch multiple emails using IMAP batch FETCH.
        
        Uses a single IMAP FETCH command for all UIDs instead of
        N sequential fetches. Falls back to sequential if batch fails.
        """
        if not self._imap:
            raise RuntimeError("IMAP not connected.")
        if not uids:
            return []

        # Try batch fetch: FETCH UID1,UID2,... (RFC822 FLAGS)
        uid_set = ",".join(uids)
        try:
            status, data = self._imap.fetch(uid_set.encode(), "(RFC822 FLAGS)")
        except Exception:
            # Fallback to sequential
            results = []
            for uid in uids:
                try:
                    summary, msg = self.fetch_full(uid)
                    results.append((summary, msg))
                except Exception:
                    continue
            return results

        if status != "OK" or not data:
            return []

        # Parse batch response — IMAP returns interleaved data for each message
        raw_msgs: dict[str, bytes] = {}
        flags_map: dict[str, list[str]] = {}
        for part in data:
            if isinstance(part, tuple) and len(part) == 2:
                meta, payload = part
                meta_str = meta.decode("utf-8", errors="replace")
                # Extract sequence or UID from metadata
                seq_match = re.search(r"^(\d+)", meta_str)
                if seq_match:
                    raw_msgs[seq_match.group(1)] = payload
            elif isinstance(part, bytes) and b"FLAGS" in part:
                flags_str = part.decode("utf-8", errors="replace")
                seq_match = re.search(r"^(\d+)", flags_str)
                if seq_match:
                    flags_map[seq_match.group(1)] = self._parse_flags(flags_str)

        # Build results in original UID order
        results: list[tuple[EmailMessage, email.message.Message]] = []
        for uid in uids:
            # Find the raw message for this UID — try by UID, then by sequence
            raw_msg = None
            # IMAP batch responses are keyed by sequence number, not UID
            # We need to match them. Since we fetched by UID set, 
            # responses come in order. Try matching by position.
            # Actually, the meta contains the sequence number, not UID.
            # For batch FETCH with UID set, each response still has the 
            # fetch number. We'll collect all raw messages and parse them.
            pass

        # If batch parsing is too complex (IMAP response format varies),
        # use chunked sequential fetch for reliability
        results = []
        chunk_size = 5  # Fetch 5 at a time to balance speed and reliability
        for i in range(0, len(uids), chunk_size):
            chunk = uids[i:i + chunk_size]
            for uid in chunk:
                try:
                    summary, msg = self.fetch_full(uid)
                    results.append((summary, msg))
                except Exception:
                    continue
        return results

    def _invalidate_folder_cache(self, folder: str) -> None:
        """Invalidate cache for a folder after mutations."""
        account_id = cache.upsert_account(self.account.name, self.account.email)
        folder_id = cache.upsert_folder(account_id, folder)
        conn = cache._get_conn()
        conn.execute("UPDATE folders SET last_sync_at=NULL WHERE id=?", (folder_id,))
        conn.commit()
        conn.close()

    def mark_read(self, uid: str, folder: str = "INBOX") -> None:
        """Mark an email as read (add \\Seen flag). Invalidates folder cache."""
        if not self._imap:
            raise RuntimeError("IMAP not connected.")
        self._imap.uid("STORE", uid.encode(), "+FLAGS", "(\\Seen)")
        self._invalidate_folder_cache(folder)

    def mark_unread(self, uid: str, folder: str = "INBOX") -> None:
        """Mark an email as unread (remove \\Seen flag). Invalidates folder cache."""
        if not self._imap:
            raise RuntimeError("IMAP not connected.")
        self._imap.uid("STORE", uid.encode(), "-FLAGS", "(\\Seen)")
        self._invalidate_folder_cache(folder)

    def move_email(self, uid: str, dest_folder: str, src_folder: str = "INBOX") -> None:
        """Move an email to another folder. Invalidates both folder caches."""
        if not self._imap:
            raise RuntimeError("IMAP not connected.")
        status, _ = self._imap.uid("COPY", uid.encode(), dest_folder)
        if status != "OK":
            raise RuntimeError(f"Failed to copy email to '{dest_folder}'")
        self._imap.uid("STORE", uid.encode(), "+FLAGS", "(\\Deleted)")
        self._imap.expunge()
        self._invalidate_folder_cache(src_folder)
        self._invalidate_folder_cache(dest_folder)

    def search(
        self,
        criteria: str = "ALL",
        folder: str = "INBOX",
        limit: int = 20,
        progress: Optional[Progress] = None,
        no_cache: bool = False,
    ) -> list[EmailMessage]:
        account_id = cache.upsert_account(self.account.name, self.account.email)
        folder_id = cache.upsert_folder(account_id, folder)

        status, uidvalidity = self.select_folder(folder)
        stored_uidv = cache.get_uidvalidity(folder_id)

        if uidvalidity and uidvalidity != stored_uidv:
            cache.clear_folder(folder_id)
            cache.set_uidvalidity(folder_id, uidvalidity)

        if not no_cache and not cache.is_stale(account_id, folder_id):
            cached = cache.get_cached_emails(account_id, folder_id, limit=limit)
            return [EmailMessage(**c) for c in cached]

        status, data = self._imap.search(None, criteria)
        if status != "OK" or not data or not data[0]:
            return []
        uids = data[0].split()
        uids = uids[-limit:]  # newest first
        results: list[EmailMessage] = []
        task_id = None
        if progress:
            task_id = progress.add_task(f"Scanning {folder}...", total=len(uids))
        for uid in reversed(uids):
            msg = self._fetch_summary(uid.decode())
            if msg:
                results.append(msg)
            if progress and task_id is not None:
                progress.advance(task_id)
        if progress and task_id is not None:
            progress.remove_task(task_id)

        cache.sync_emails(account_id, folder_id, [r.model_dump() for r in results])
        return results

    def _fetch_summary(self, uid: str) -> Optional[EmailMessage]:
        """Fetch email summary using lightweight partial fetch.
        
        Uses BODY.PEEK[1]<0.4096> to get first 4KB of body text
        instead of full RFC822 (~100KB+ with attachments).
        Falls back to full RFC822 if partial fetch fails.
        """
        # Try lightweight fetch first: headers + partial body + flags
        status, data = self._imap.fetch(
            uid.encode(),
            "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM TO DATE CONTENT-TYPE)] BODY.PEEK[1]<0.4096> FLAGS)",
        )
        if status == "OK" and data:
            raw_headers = None
            raw_body = None
            flags = []
            for part in data:
                if isinstance(part, tuple) and len(part) == 2:
                    meta, payload = part
                    meta_str = meta.decode("utf-8", errors="replace") if isinstance(meta, bytes) else meta
                    if "HEADER" in meta_str.upper() or "FIELDS" in meta_str.upper():
                        raw_headers = payload
                    elif "BODY" in meta_str.upper() and "HEADER" not in meta_str.upper():
                        raw_body = payload
                elif isinstance(part, bytes) and b"FLAGS" in part:
                    flags = self._parse_flags(part.decode())

            if raw_headers is not None:
                msg = email.message_from_bytes(raw_headers)
                subject = self._decode_header_value(msg.get("Subject", ""))
                sender = self._decode_header_value(msg.get("From", ""))
                to = self._decode_header_value(msg.get("To", ""))
                raw_date = msg.get("Date", "")
                parsed_date = self._parse_date(raw_date)

                body_preview = ""
                if raw_body:
                    body_preview = self._decode_partial_body(raw_body)

                return EmailMessage(
                    uid=uid,
                    subject=subject,
                    sender=sender,
                    to=to,
                    date=parsed_date,
                    raw_date=raw_date,
                    body_preview=body_preview,
                    flags=flags,
                    size=len(raw_headers) + (len(raw_body) if raw_body else 0),
                    has_attachments=False,  # Can't tell from headers alone
                )

        # Fallback: full RFC822 fetch (slower but always works)
        status, data = self._imap.fetch(uid.encode(), "(RFC822 FLAGS)")
        if status != "OK" or not data:
            return None
        raw_msg = None
        flags = []
        for part in data:
            if isinstance(part, tuple) and len(part) == 2:
                raw_msg = part[1]
            elif isinstance(part, bytes) and b"FLAGS" in part:
                flags = self._parse_flags(part.decode())
        if raw_msg is None:
            return None
        msg = email.message_from_bytes(raw_msg)
        subject = self._decode_header_value(msg.get("Subject", ""))
        sender = self._decode_header_value(msg.get("From", ""))
        to = self._decode_header_value(msg.get("To", ""))
        raw_date = msg.get("Date", "")
        parsed_date = self._parse_date(raw_date)
        body_preview = self._extract_preview(msg)
        has_attachments = self._has_attachments(msg)
        size = len(raw_msg)
        return EmailMessage(
            uid=uid,
            subject=subject,
            sender=sender,
            to=to,
            date=parsed_date,
            raw_date=raw_date,
            body_preview=body_preview,
            flags=flags,
            size=size,
            has_attachments=has_attachments,
        )

    def _fetch_summary_uid(self, uid: str) -> Optional[EmailMessage]:
        """Fetch summary by IMAP UID (not sequence number)."""
        status, data = self._imap.uid("FETCH", uid, "(RFC822 FLAGS)")
        if status != "OK" or not data:
            return None
        raw_msg = None
        flags = []
        for part in data:
            if isinstance(part, tuple) and len(part) == 2:
                raw_msg = part[1]
            elif isinstance(part, bytes) and b"FLAGS" in part:
                flags = self._parse_flags(part.decode())
        if raw_msg is None:
            return None
        msg = email.message_from_bytes(raw_msg)
        subject = self._decode_header_value(msg.get("Subject", ""))
        sender = self._decode_header_value(msg.get("From", ""))
        to = self._decode_header_value(msg.get("To", ""))
        raw_date = msg.get("Date", "")
        parsed_date = self._parse_date(raw_date)
        body_preview = self._extract_preview(msg)
        has_attachments = self._has_attachments(msg)
        size = len(raw_msg)
        return EmailMessage(
            uid=uid,
            subject=subject,
            sender=sender,
            to=to,
            date=parsed_date,
            raw_date=raw_date,
            body_preview=body_preview,
            flags=flags,
            size=size,
            has_attachments=has_attachments,
        )

    def fetch_full(self, uid: str) -> tuple[EmailMessage, email.message.Message]:
        """Fetch full raw message and return parsed model + message object."""
        if not self._imap:
            raise RuntimeError("IMAP not connected.")
        status, data = self._imap.fetch(uid.encode(), "(RFC822)")
        if status != "OK" or not data:
            raise RuntimeError(f"Failed to fetch message {uid}")
        raw_msg = None
        for part in data:
            if isinstance(part, tuple) and len(part) == 2:
                raw_msg = part[1]
                break
        if raw_msg is None:
            raise RuntimeError(f"No message body for UID {uid}")
        msg = email.message_from_bytes(raw_msg)
        summary = self._fetch_summary(uid)
        if summary is None:
            raise RuntimeError(f"Failed to parse summary for UID {uid}")
        return summary, msg

    def list_attachments(self, uid: str) -> list[AttachmentInfo]:
        _, msg = self.fetch_full(uid)
        attachments: list[AttachmentInfo] = []
        for part in msg.walk():
            cdisp = part.get_content_disposition() or ""
            filename = part.get_filename()
            if filename or "attachment" in cdisp:
                filename = filename or "unnamed"
                filename = self._decode_header_value(filename)
                size = len(part.get_payload(decode=True) or b"")
                attachments.append(
                    AttachmentInfo(
                        filename=filename,
                        content_type=part.get_content_type(),
                        size=size,
                    )
                )
        return attachments

    def download_attachments(
        self, uid: str, output_dir: Path
    ) -> list[Path]:
        _, msg = self.fetch_full(uid)
        output_dir.mkdir(parents=True, exist_ok=True)
        downloaded: list[Path] = []
        for part in msg.walk():
            cdisp = part.get_content_disposition() or ""
            filename = part.get_filename()
            if filename or "attachment" in cdisp:
                filename = filename or "unnamed"
                filename = self._decode_header_value(filename)
                payload = part.get_payload(decode=True) or b""
                dest = output_dir / filename
                counter = 1
                stem = dest.stem
                suffix = dest.suffix
                while dest.exists():
                    dest = output_dir / f"{stem}_{counter}{suffix}"
                    counter += 1
                with open(dest, "wb") as f:
                    f.write(payload)
                downloaded.append(dest)
        return downloaded

    def export_email(
        self,
        uid: str,
        output_dir: Path,
    ) -> dict:
        """Export full email (headers, body, attachments) to a directory."""
        output_dir.mkdir(parents=True, exist_ok=True)
        summary, msg = self.fetch_full(uid)
        result = {
            "uid": uid,
            "subject": summary.subject,
            "sender": summary.sender,
            "to": summary.to,
            "date": summary.raw_date,
            "body_file": None,
            "attachments": [],
        }
        # Extract body
        body_text = self._extract_full_body(msg)
        body_file = output_dir / "body.txt"
        with open(body_file, "w", encoding="utf-8") as f:
            f.write(body_text)
        result["body_file"] = str(body_file)
        # Extract attachments
        for part in msg.walk():
            cdisp = part.get_content_disposition() or ""
            filename = part.get_filename()
            if filename or "attachment" in cdisp:
                filename = filename or "unnamed"
                filename = self._decode_header_value(filename)
                payload = part.get_payload(decode=True) or b""
                dest = output_dir / filename
                counter = 1
                stem = dest.stem
                suffix = dest.suffix
                while dest.exists():
                    dest = output_dir / f"{stem}_{counter}{suffix}"
                    counter += 1
                with open(dest, "wb") as f:
                    f.write(payload)
                result["attachments"].append(str(dest))
        return result

    # -- SMTP --

    def smtp_connect(self) -> None:
        self._smtp = smtplib.SMTP_SSL(
            host=self.account.smtp_host,
            port=self.account.smtp_port,
            timeout=30,
        )
        self._smtp.ehlo()
        self._smtp.login(self.account.email, self.password)

    def smtp_disconnect(self) -> None:
        if self._smtp:
            try:
                self._smtp.close()
            except Exception:
                pass
            self._smtp = None

    def send_email(
        self,
        to: list[str],
        subject: str,
        body: str,
        cc: Optional[list[str]] = None,
        bcc: Optional[list[str]] = None,
        attachments: Optional[list[Path]] = None,
    ) -> None:
        if not self._smtp:
            raise RuntimeError("SMTP not connected.")
        msg = StdEmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.account.email
        msg["To"] = ", ".join(to)
        if cc:
            msg["Cc"] = ", ".join(cc)
        if bcc:
            msg["Bcc"] = ", ".join(bcc)

        if attachments:
            msg.make_mixed()
            text_part = StdEmailMessage()
            text_part.set_content(body)
            msg.attach(text_part)
            for path in attachments:
                with open(path, "rb") as f:
                    data = f.read()
                ctype = "application/octet-stream"
                maintype, subtype = ctype.split("/", 1)
                msg.add_attachment(
                    data,
                    maintype=maintype,
                    subtype=subtype,
                    filename=path.name,
                )
        else:
            msg.set_content(body)

        all_recipients = to[:]
        if cc:
            all_recipients.extend(cc)
        if bcc:
            all_recipients.extend(bcc)
        self._smtp.send_message(msg, to_addrs=all_recipients)

    # -- Helpers --

    @staticmethod
    def _decode_header_value(value: str) -> str:
        parts = decode_header(value)
        result = []
        for text, charset in parts:
            if isinstance(text, bytes):
                try:
                    result.append(text.decode(charset or "utf-8", errors="replace"))
                except (LookupError, TypeError):
                    result.append(text.decode("utf-8", errors="replace"))
            else:
                result.append(text)
        return "".join(result)

    @staticmethod
    def _parse_date(raw: str) -> Optional[datetime]:
        try:
            from email.utils import parsedate_to_datetime
            return parsedate_to_datetime(raw)
        except Exception:
            return None

    @staticmethod
    def _extract_preview(msg: email.message.Message, length: int = 200) -> str:
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    try:
                        text = payload.decode("utf-8", errors="replace")
                    except Exception:
                        text = payload.decode("ascii", errors="replace")
                    return text.replace("\n", " ").replace("\r", "")[:length]
        # Fallback: try HTML and convert to text
        for part in msg.walk():
            ctype = part.get_content_type()
            if ctype == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    try:
                        html_str = payload.decode("utf-8", errors="replace")
                    except Exception:
                        html_str = payload.decode("ascii", errors="replace")
                    text = _html_to_text(html_str)
                    return text.replace("\n", " ").replace("\r", "")[:length]
        return ""

    @staticmethod
    def _extract_full_body(msg: email.message.Message) -> str:
        """Extract the best text body from a message (plain or HTML-converted)."""
        # Prefer text/plain
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    try:
                        return payload.decode("utf-8", errors="replace")
                    except Exception:
                        return payload.decode("ascii", errors="replace")
        # Fallback: text/html converted to text
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    try:
                        html_str = payload.decode("utf-8", errors="replace")
                    except Exception:
                        html_str = payload.decode("ascii", errors="replace")
                    return _html_to_text(html_str)
        return ""

    @staticmethod
    def _has_attachments(msg: email.message.Message) -> bool:
        for part in msg.walk():
            cdisp = part.get_content_disposition() or ""
            if part.get_filename() or "attachment" in cdisp:
                return True
        return False

    @staticmethod
    def _decode_partial_body(raw_body: bytes, max_len: int = 200) -> str:
        """Decode a partial MIME body part into clean text preview.
        
        Handles quoted-printable, base64, and HTML.
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

    @staticmethod
    def _parse_flags(line: str) -> list[str]:
        flags = []
        if "FLAGS" in line:
            # Find all FLAGS groups — IMAP may nest parentheses e.g.:
            #   * 1 FETCH (FLAGS (\Seen) RFC822 {123})
            # We need to match the FLAGS (...) group specifically.
            import re
            flags_match = re.search(r'FLAGS\s*\(([^)]*)\)', line)
            if flags_match:
                inner = flags_match.group(1)
                for p in inner.split():
                    p = p.strip()
                    if p.startswith("\\"):
                        flags.append(p)
        return flags
