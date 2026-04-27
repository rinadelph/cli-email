# email-cli

A multi-account email CLI that uses **IMAP for reading and SMTP for sending** — no OAuth2, no API keys, no browser flows. Works with **any email provider** (Gmail, Outlook, Yahoo, corporate servers) using just a username and password.

## Why SMTP/IMAP Instead of Gmail API?

| | email-cli (SMTP/IMAP) | Gmail API |
|---|---|---|
| **Setup** | 1 password (app password) | OAuth2 app + credentials.json + token refresh |
| **Auth flow** | `username + password` | Browser consent → authorization code → access token → refresh token |
| **Rate limits** | Provider limits only | API quotas (250 quota units/day free) |
| **Portability** | Any IMAP/SMTP server | Gmail only |
| **Agent-friendly** | `export EMAIL_PASSWORD=...` → done | Requires headless browser or service account |
| **Attachments** | Standard MIME — no size limits | 25 MB limit, multipart upload complexity |
| **Sending** | Native SMTP — instant delivery | API batching + queue delays |

**For agents and automation scripts, SMTP/IMAP is dramatically simpler.** One environment variable, no token management, no OAuth scopes, no credential files to rotate.

## Requirements

- Python 3.10+
- An email account with **IMAP enabled**
- For Gmail: an [App Password](https://support.google.com/accounts/answer/185833) (regular passwords won't work with 2FA)

## Setup

```bash
# Clone and install
git clone https://github.com/rinadelph/cli-email.git
cd cli-email
pip install -e .

# Or if editable install fails, install dependencies directly:
pip install typer rich keyring cryptography pydantic

# Add the CLI to your PATH
export PATH="$HOME/.local/bin:$PATH"
```

## Quick Start

### 1. Add an account

```bash
# Gmail example (uses imap.gmail.com / smtp.gmail.com by default)
email accounts add personal me@gmail.com
# Enter your app-password when prompted

# Non-interactive (perfect for scripts/agents)
export EMAIL_PASSWORD="your-app-password"
email accounts add personal me@gmail.com --non-interactive

# Custom provider (Outlook, corporate, etc.)
export EMAIL_PASSWORD="your-password"
export EMAIL_IMAP_HOST="imap.outlook.com"
export EMAIL_SMTP_HOST="smtp.outlook.com"
email accounts add outlook me@outlook.com --non-interactive
```

### 2. List emails

```bash
email list                    # Default account, INBOX, last 20 emails
email list --limit 50         # Show 50 emails
email list --unread           # Only unread
email list --folder Sent      # Browse Sent folder

# JSON output for parsing
EMAIL_FORMAT=json email list --limit 5

# Compact JSON for piping
EMAIL_FORMAT=json EMAIL_COMPACT=1 email list --limit 5 | jq '.[].subject'

# Field filtering
EMAIL_FORMAT=raw EMAIL_FIELDS="uid,subject,sender" email list --limit 5
```

### 3. Search emails

```bash
email search "invoice"        # Search all fields
email search "john" --in from # Search only sender
email search "Clover" --in to # Search only recipients
email search "invoice" --since 2026-01-01 --before 2026-04-01
```

### 4. Show an email

```bash
email show 12345              # Full body with rich formatting
email show 12345 --format raw --body-file /tmp/body.txt
email show 12345 --format json --body-file /tmp/body.json
```

### 5. Attachments

```bash
email attachments list 12345              # List attachments
email attachments download 12345          # Download to current directory
email attachments download 12345 --output ~/Downloads --format json
```

### 6. Send an email (via SMTP)

```bash
# Simple text email
email send \
  --to recipient@example.com \
  --subject "Hello" \
  --body "This is a test email."

# With attachments
email send \
  --to recipient@example.com \
  --subject "Report" \
  --body "See attached." \
  --attach report.pdf \
  --attach data.csv

# Read body from file
email send \
  --to recipient@example.com \
  --subject "Update" \
  --body-file message.txt
```

### 7. Manage accounts

```bash
email accounts list                    # Show all accounts
email accounts set-default work        # Change default
email accounts remove personal       # Remove an account
```

## Agent / Automation Mode

This CLI is designed for AI agents and automation scripts:

```bash
# Set once, use everywhere
export EMAIL_FORMAT=json
export EMAIL_COMPACT=1

# List → parse with jq
email list --limit 10 | jq '.[] | {uid, subject, sender}'

# Search → extract UIDs → download attachments
for uid in $(email search "contract" --in subject --format raw | cut -f1); do
    email attachments download "$uid" --output ./contracts --format raw
done

# Non-interactive account setup
export EMAIL_PASSWORD="xxxx xxxx xxxx xxxx"
email accounts add bot bot@gmail.com --non-interactive
```

## CLI Reference

| Command | Description |
|---------|-------------|
| `email accounts add <name> <email>` | Add account (prompts for password) |
| `email accounts list` | Show configured accounts |
| `email accounts remove <name>` | Remove account and credentials |
| `email accounts set-default <name>` | Set default account |
| `email folders` | List IMAP folders |
| `email list` | List emails in a folder |
| `email search <query>` | Search emails |
| `email show <uid>` | Display full email |
| `email attachments list <uid>` | List attachments |
| `email attachments download <uid>` | Download attachments |
| `email send` | Send an email via SMTP |
| `email notes add <message>` | Add agent reminder |
| `email notes list` | List agent reminders |

## Environment Variables

| Variable | Purpose | Example |
|----------|---------|---------|
| `EMAIL_PASSWORD` | Password for non-interactive setup | `xxxx xxxx xxxx xxxx` |
| `EMAIL_FORMAT` | Default output format | `json`, `raw`, `table` |
| `EMAIL_COMPACT` | Compact JSON (one-line) | `1` |
| `EMAIL_FIELDS` | Comma-separated field filter | `uid,subject,sender` |
| `EMAIL_IMAP_HOST` | Custom IMAP server | `imap.outlook.com` |
| `EMAIL_SMTP_HOST` | Custom SMTP server | `smtp.outlook.com` |

## Security

- Passwords are stored in your **OS keyring** (macOS Keychain, Windows Credential Manager, Linux Secret Service)
- If no keyring is available, a fallback encrypted file is used
- Account config lives in `~/.config/email-cli/accounts.json` — **no passwords in this file**

## Multi-Account Support

Add as many accounts as you want. The default account is used when `--account` is omitted:

```bash
email accounts add gmail me@gmail.com
email accounts add outlook me@outlook.com
email accounts add corp work@company.com --imap-host imap.company.com

email list --account outlook
email send --account corp --to boss@company.com --subject "Update"
```

## How It Works

1. **Account setup** stores config (server, port, email) in `~/.config/email-cli/accounts.json`
2. **Passwords** go to the OS keyring under `email-cli/<account_name>`
3. **Reading** opens `IMAP4_SSL` to the provider, fetches headers + body, parses MIME
4. **Sending** opens `SMTP_SSL`, builds a MIME message, and dispatches it directly
5. **Attachments** are MIME parts decoded from base64/quoted-printable

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Gmail login fails | Use an **App Password**, not your regular password |
| IMAP disabled | Enable IMAP in your email provider settings |
| `email: command not found` | Ensure `~/.local/bin` is in your `$PATH` |
| Keyring errors on headless servers | Install `dbus-python` or use `--password-file` |
