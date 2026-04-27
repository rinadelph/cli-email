"""Pydantic models for accounts and emails."""

import re
from datetime import datetime
from typing import Optional
from pydantic import BaseModel, Field


class Account(BaseModel):
    """An email account configuration."""
    name: str = Field(..., description="User-defined alias for this account")
    email: str = Field(..., description="Email address")
    imap_host: str = Field(default="imap.gmail.com", description="IMAP server hostname")
    imap_port: int = Field(default=993, description="IMAP server port")
    smtp_host: str = Field(default="smtp.gmail.com", description="SMTP server hostname")
    smtp_port: int = Field(default=465, description="SMTP server port")
    use_ssl: bool = Field(default=True, description="Use SSL/TLS connections")


class EmailMessage(BaseModel):
    """A simplified email message representation."""
    uid: str = Field(..., description="IMAP UID")
    subject: str = Field(default="", description="Email subject")
    sender: str = Field(default="", description="From address")
    to: str = Field(default="", description="To address")
    date: Optional[datetime] = Field(default=None, description="Parsed date")
    raw_date: str = Field(default="", description="Raw date string from headers")
    body_preview: str = Field(default="", description="First ~200 chars of text body")
    flags: list[str] = Field(default_factory=list, description="IMAP flags")
    size: int = Field(default=0, description="Message size in bytes")
    has_attachments: bool = Field(default=False, description="Whether MIME parts indicate attachments")
    account: Optional[str] = Field(default=None, description="Account name this email belongs to")


class AttachmentInfo(BaseModel):
    """Info about an email attachment."""
    filename: str = Field(..., description="Attachment filename")
    content_type: str = Field(default="application/octet-stream", description="MIME type")
    size: int = Field(default=0, description="Approximate size in bytes")


# -- Address parsing helpers --

def parse_address(raw: str) -> dict:
    """Parse an RFC2822 address like 'Name <email>' into {name, email}."""
    raw = raw.strip()
    m = re.match(r'"?([^"<]+)"?\s*<([^>]+)>', raw)
    if m:
        return {"name": m.group(1).strip().strip('"'), "email": m.group(2).strip()}
    # Bare email or angle-bracket only
    m2 = re.match(r'<([^>]+)>', raw)
    if m2:
        return {"name": "", "email": m2.group(1).strip()}
    # Bare email address
    if "@" in raw:
        return {"name": "", "email": raw}
    return {"name": raw, "email": ""}


def parse_address_list(raw: str) -> list[dict]:
    """Parse a multi-recipient RFC2822 string into a list of {name, email}."""
    # Split on comma, but respect angle brackets
    parts = re.split(r',\s*(?=[^,]*[<@]|[^,]*$)', raw)
    results = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        results.append(parse_address(part))
    return results
