"""Formatting helpers for CLI output: table, JSON, raw, compact."""

import json
import re
import sys
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from rich.console import Console
from rich.table import Table

from email_cli.models import AttachmentInfo, EmailMessage, parse_address, parse_address_list


class OutputFormat(str, Enum):
    TABLE = "table"
    JSON = "json"
    RAW = "raw"


console = Console()


def _email_to_dict(msg: EmailMessage) -> dict:
    """Serialize EmailMessage to a JSON-safe dict with pre-parsed addresses."""
    return {
        "uid": msg.uid,
        "subject": msg.subject,
        "sender": parse_address(msg.sender),
        "to": parse_address_list(msg.to),
        "date": msg.date.isoformat() if msg.date else None,
        "body_preview": _sanitize_preview(msg.body_preview),
        "flags": msg.flags,
        "size": msg.size,
        "has_attachments": msg.has_attachments,
        "account": msg.account,
    }


def _sanitize_preview(text: str) -> str:
    """Strip zero-width/invisible chars and collapse whitespace from body preview."""
    # Remove zero-width chars, joiners, non-breaking spaces, soft hyphens
    invisible = re.sub(r'[\u200b\u200c\u200d\u200e\u200f\u202a-\u202e\u2060\ufeff\u00ad\u0080-\u009f\u2028\u2029\u2000-\u200a\u205f\u180e]+', '', text)
    # Collapse whitespace
    return re.sub(r'\s+', ' ', invisible).strip()


def _attachment_to_dict(att: AttachmentInfo) -> dict:
    """Serialize AttachmentInfo to a JSON-safe dict."""
    return {
        "filename": att.filename,
        "content_type": att.content_type,
        "size": att.size,
    }


def _filter_fields(data: dict, fields: Optional[list[str]]) -> dict:
    if not fields:
        return data
    return {k: v for k, v in data.items() if k in fields}


def print_emails(
    emails: list[EmailMessage],
    fmt: OutputFormat,
    compact: bool = False,
    fields: Optional[list[str]] = None,
    meta: Optional[dict] = None,
) -> None:
    if fmt == OutputFormat.JSON:
        data = [_filter_fields(_email_to_dict(e), fields) for e in emails]
        output = data
        if meta:
            output = {"results": data, "_meta": meta}
        json.dump(output, sys.stdout, indent=None if compact else 2, ensure_ascii=False)
        print()
        return
    if fmt == OutputFormat.RAW:
        for msg in emails:
            read_flag = "R" if "\\Seen" in msg.flags else "U"
            att_flag = "A" if msg.has_attachments else "-"
            date_str = msg.date.strftime("%Y-%m-%d %H:%M") if msg.date else "N/A"
            account_str = msg.account or ""
            # Support field filtering in raw mode
            if fields:
                d = _email_to_dict(msg)
                values = [str(d.get(f, "")) for f in fields]
                print("\t".join(values))
            else:
                print(f"{msg.uid}\t{account_str}\t{date_str}\t{msg.sender}\t{msg.subject}\t{att_flag}\t{read_flag}")
        return
    # TABLE (default)
    table = Table(title="Emails")
    table.add_column("UID", style="cyan", no_wrap=True)
    table.add_column("Account", style="blue", no_wrap=True)
    table.add_column("Date", style="magenta", no_wrap=True)
    table.add_column("From", style="green")
    table.add_column("Subject", style="white")
    table.add_column("Size", justify="right", style="yellow")
    table.add_column("Att", justify="center", style="red")
    for msg in emails:
        date_str = msg.date.strftime("%Y-%m-%d %H:%M") if msg.date else msg.raw_date[:20]
        att_marker = "A" if msg.has_attachments else ""
        flags_prefix = ""
        if "\\Seen" not in msg.flags:
            flags_prefix = "[bold]*[/bold] "
        account_str = msg.account or ""
        table.add_row(
            msg.uid,
            account_str[:15],
            date_str,
            msg.sender[:40],
            f"{flags_prefix}{msg.subject[:60]}",
            human_readable_size(msg.size),
            att_marker,
        )
    console.print(table)


def print_email_detail(
    msg: EmailMessage,
    body: str,
    fmt: OutputFormat,
    body_file: Optional[str] = None,
    compact: bool = False,
    fields: Optional[list[str]] = None,
) -> None:
    if body_file:
        with open(body_file, "w", encoding="utf-8") as f:
            f.write(body)
    if fmt == OutputFormat.JSON:
        data = _email_to_dict(msg)
        data["body"] = body
        data = _filter_fields(data, fields)
        json.dump(data, sys.stdout, indent=None if compact else 2, ensure_ascii=False)
        print()
        return
    if fmt == OutputFormat.RAW:
        if fields:
            d = _email_to_dict(msg)
            d["body"] = body
            values = [str(d.get(f, "")) for f in fields]
            print("\t".join(values))
        else:
            print(f"UID: {msg.uid}")
            print(f"From: {msg.sender}")
            print(f"To: {msg.to}")
            print(f"Subject: {msg.subject}")
            print(f"Date: {msg.date.isoformat() if msg.date else msg.raw_date}")
            print(f"Flags: {', '.join(msg.flags) or 'none'}")
            print(f"Size: {msg.size}")
            print(f"HasAttachments: {msg.has_attachments}")
            print("---BODY---")
            print(body)
        return
    # TABLE
    console.print(f"[bold cyan]UID:[/bold cyan] {msg.uid}")
    console.print(f"[bold cyan]From:[/bold cyan] {msg.sender}")
    console.print(f"[bold cyan]To:[/bold cyan] {msg.to}")
    console.print(f"[bold cyan]Subject:[/bold cyan] {msg.subject}")
    console.print(f"[bold cyan]Date:[/bold cyan] {msg.date.isoformat() if msg.date else msg.raw_date}")
    console.print(f"[bold cyan]Flags:[/bold cyan] {', '.join(msg.flags) or 'none'}")
    console.print(f"[bold cyan]Size:[/bold cyan] {human_readable_size(msg.size)}")
    console.print("[bold cyan]Body:[/bold cyan]")
    console.print(body)


def print_attachments(
    attachments: list[AttachmentInfo],
    fmt: OutputFormat,
    compact: bool = False,
) -> None:
    if fmt == OutputFormat.JSON:
        data = [_attachment_to_dict(a) for a in attachments]
        json.dump(data, sys.stdout, indent=None if compact else 2, ensure_ascii=False)
        print()
        return
    if fmt == OutputFormat.RAW:
        for att in attachments:
            print(f"{att.filename}\t{att.content_type}\t{att.size}")
        return
    table = Table(title="Attachments")
    table.add_column("Filename", style="cyan")
    table.add_column("Type", style="green")
    table.add_column("Size", justify="right", style="yellow")
    for att in attachments:
        table.add_row(att.filename, att.content_type, human_readable_size(att.size))
    console.print(table)


def print_folders(folders: list[str], fmt: OutputFormat, compact: bool = False) -> None:
    if fmt == OutputFormat.JSON:
        json.dump(folders, sys.stdout, indent=None if compact else 2, ensure_ascii=False)
        print()
        return
    if fmt == OutputFormat.RAW:
        for f in folders:
            print(f)
        return
    for f in folders:
        console.print(f"[cyan]{f}[/cyan]")


def print_downloaded(paths: list, fmt: OutputFormat, compact: bool = False) -> None:
    if fmt == OutputFormat.JSON:
        json.dump([str(p) for p in paths], sys.stdout, indent=None if compact else 2, ensure_ascii=False)
        print()
        return
    if fmt == OutputFormat.RAW:
        for p in paths:
            print(p)
        return
    from rich import print as rprint
    for p in paths:
        rprint(f"[green]Saved:[/green] {p}")


def print_error(message: str, fmt: OutputFormat, compact: bool = False) -> None:
    """Print errors in the same format as successful output for agent parsing."""
    if fmt == OutputFormat.JSON:
        json.dump({"error": message}, sys.stdout, indent=None if compact else 2, ensure_ascii=False)
        print()
        return
    if fmt == OutputFormat.RAW:
        print(f"ERROR\t{message}")
        return
    console.print(f"[red]{message}[/red]")


def print_success(message: str, fmt: OutputFormat, compact: bool = False) -> None:
    if fmt == OutputFormat.JSON:
        json.dump({"success": message}, sys.stdout, indent=None if compact else 2, ensure_ascii=False)
        print()
        return
    if fmt == OutputFormat.RAW:
        print(f"OK\t{message}")
        return
    console.print(f"[green]{message}[/green]")


def human_readable_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.1f} GB"
