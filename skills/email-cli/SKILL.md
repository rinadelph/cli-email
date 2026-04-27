---
name: email-cli
version: 0.1.1
description: Guide for using the email CLI to interact with Gmail and other IMAP/SMTP email accounts. Use when the user asks about reading emails, searching emails, sending emails, managing accounts, or any email-related tasks from the command line.
triggers:
  - type: command
    pattern: "email.*"
  - type: intent
    pattern: "(check|read|search|find|send|list).*(email|mail|gmail|inbox)"
  - type: intent
    pattern: "(email|mail).*(from|sender|search|find|send)"
requires:
  bins: ["email"]
  auth: true
---

# Email CLI Usage Guide

Help users interact with email accounts from the command line using the `email` CLI.

## Agent Guidance

Best practices and operational guidance for AI coding agents using the Email CLI.

### Key Principles

- **Always run `email status` first** — This shows which accounts exist, which have credentials, which is the default, and cache freshness. One command gives full context without guessing.
- **Start the daemon for best performance** — The daemon keeps IMAP connections warm so queries are near-instant (~800ms vs ~3000ms+ cold). It auto-starts on first use, but starting it explicitly is cleaner.
- **Use `email from <sender>` shorthand** — This is the fastest way to search by sender. It's equivalent to `email search "sender" --in from` but shorter.
- **Use `--format json` for machine-readable output** — Pipe through `jq` for filtering. Human-readable output includes formatting that is hard to parse programmatically.
- **The daemon auto-starts invisibly** — If the daemon isn't running, CLI auto-starts it silently and waits for readiness. Agents don't need to remember `email daemon start`.
- **Use relative time filters** — `--since 7d`, `--since yesterday`, `--since this-week` are supported and easier than date math.

### Design Principles

The `email` CLI follows conventions from well-known tools:

- **`gh` (GitHub CLI) conventions**: Uses `<noun> <verb>` command pattern (e.g., `email list`, `email send`, `email search`). Flags follow common conventions: `--format` for output format, `--since` for time filtering, `--from` for sender filtering.
- **`jq`-friendly JSON output**: Use `--format json` to get structured output with `_meta` block showing accounts searched, skipped, and timing info.
- **KISS principle**: The daemon auto-starts on first use. No need to manually manage it unless you want to.

### Context Window Tips

- Use `--format json` to get structured data with full `_meta` block
- Use `--limit` to cap the number of results (default is usually 20)
- Use `--since` with relative dates (`7d`, `yesterday`, `this-week`, `this-month`) instead of absolute dates
- Use `email from <sender>` for quick sender searches instead of full search syntax
- The `_meta` block in JSON output shows:
  - `accounts_searched`: Which accounts were queried
  - `accounts_skipped`: Accounts without credentials or with errors
  - `account_timings`: Per-account breakdown of DNS, connect, search, filter times
  - `total_found`: Total matches before limit applied

### Safety Rules

- **Never expose email content in logs** — Email content may contain sensitive information
- **Confirm before destructive actions** — The CLI doesn't have destructive actions by default, but be careful with bulk operations
- **Respect read/unread state** — The CLI uses `BODY.PEEK` to avoid marking emails as read when fetching
- **Check `accounts_skipped` in _meta** — If results are missing, check if accounts were skipped due to missing credentials

### Workflow Patterns

#### First-Time Setup (Check Status)

```bash
# Always run this first to understand the environment
email status
```

Output shows:
- Available accounts and credential status
- Which account is default
- Daemon status (running/stopped)
- Cache age

#### Search Emails by Sender

```bash
# Quick shorthand for sender search
email from sender

# With time filter
email from sender --since 7d

# JSON output for processing
email from sender --since 7d --format json | jq '.results[] | {subject, date}'
```

#### Full-Text Search

```bash
# Search across all emails
email search "invoice"

# Search only in subject
email search "invoice" --in subject

# Search with sender filter
email search "invoice" --from accounting

# Time-bounded search
email search "invoice" --since this-month
```

#### Reading Emails

```bash
# Show specific email by UID
email show 4365

# Show with full headers
email show 4365 --headers

# Show with attachments
email show 4365 --with-attachments
```

#### Sending Emails

```bash
# Send simple email
email send --to user@example.com --subject "Hello" --body "Message text"

# Send from file
email send --to user@example.com --subject "Report" --body-file report.txt

# Send with attachment
email send --to user@example.com --subject "Report" --body "See attached" --attach report.pdf

# Preview before sending
email compose --to user@example.com --subject "Draft" --body "Text" --preview
```

#### Account Management

```bash
# List all accounts
email accounts list

# Test account connectivity
email accounts test primary

# Set default account
email accounts set-default work
```

#### Daemon Management (Optional)

```bash
# Start daemon explicitly (optional - auto-starts on first use)
email daemon start

# Check daemon status
email daemon status

# Stop daemon
email daemon stop
```

### Common Mistakes

- **Not running `email status` first**: Always check which accounts are available and have credentials before running queries.
- **Forgetting `--format json` for piping**: Human-readable output includes Rich formatting. Use `--format json` when processing programmatically.
- **Not checking `accounts_skipped`**: If you get fewer results than expected, check the `_meta.accounts_skipped` field in JSON output.
- **Using exact dates instead of relative**: Prefer `--since 7d` over calculating dates manually.
- **Not quoting multi-word search terms**: `email search "meeting notes"` not `email search meeting notes`.

## Prerequisites

The CLI must be installed and accounts configured before use.

### Installation

```bash
# System-wide installation
sudo pip3 install email-cli-0.1.1-py3-none-any.whl

# Or from PyPI (when published)
pip install email-cli
```

### Account Setup

```bash
# Add an account (interactive - will prompt for password)
email accounts add work work@gmail.com

# Add account non-interactively (with password file)
email accounts add work work@gmail.com --non-interactive < password.txt
```

## Command Reference

### Introspection Commands (Run These First)

| Command | What it does |
|---------|-------------|
| `email status` | Accounts, health, default, cache age. **Always run this first.** |
| `email accounts list` | All accounts with credential status (`+` = has password, `-` = no password) |
| `email accounts names` | Plain list of account names, one per line. For iteration. |
| `email accounts test <name>` | Test IMAP/SMTP connectivity for an account |

### Reading Emails

| Command | Example |
|---------|---------|
| `email list` | List recent emails across all accounts |
| `email list --unread` | Only unread emails |
| `email list --from sender` | Filter by sender |
| `email list --since 7d` | Last 7 days (also: `today`, `yesterday`, `this-week`, `this-month`, `30d`) |
| `email list --has-attachment` | Only emails with attachments |
| `email search "invoice"` | Full-text search across all accounts |
| `email search "invoice" --in subject` | Search only in subject field |
| `email search "invoice" --from sender` | Search + filter by sender |
| `email from sender` | **Shorthand** for searching by sender |
| `email from sender --since this-week` | Sender + time filter |
| `email show <uid>` | Show full email body |
| `email show <uid> --body-file /tmp/body.txt` | Save body to file |
| `email thread <uid>` | Show entire conversation thread |
| `email get <uid> --with-attachments` | Body + attachments in one shot |

### Sending Emails

| Command | Example |
|---------|---------|
| `email send --to user@example.com --subject "Hello" --body "text"` | Send inline |
| `email send --to a@x.com --to b@x.com --subject "Hi" --body-file msg.txt` | Multiple recipients, body from file |
| `email send --to x@x.com --subject "Report" --body-file r.txt --attach r.pdf` | With attachment |
| `email compose --to x@x.com --subject "Draft" --body "text" --preview` | Preview without sending |

### Account Management

| Command | Example |
|---------|---------|
| `email accounts add work work@gmail.com --non-interactive` | Add account (needs `EMAIL_PASSWORD` env or `--password-file`) |
| `email accounts set-default primary` | Change default account |
| `email accounts remove personal` | Remove account |
| `email accounts test <name>` | Test connectivity |

### Daemon Commands (Optional)

| Command | What it does |
|---------|-------------|
| `email daemon start` | Start background daemon with warm IMAP connections |
| `email daemon status` | Show running status, connected accounts, idle time |
| `email daemon stop` | Graceful shutdown |

The daemon keeps IMAP connections logged in and folders selected, so queries skip the expensive connect+login+select cycle. Auto-shuts down after 10 minutes idle. If the daemon is running, `email from`, `email search`, and `email list` automatically use it and fall back to direct IMAP if unavailable.

## Output Formats

All commands support `--format table|json|raw`:

- **json** (default for non-TTY): Structured JSON with `_meta` block
- **table** (default for TTY): Human-readable Rich tables
- **raw**: Tab-separated, for `cut`/`awk`/`grep` piping

Set `EMAIL_FORMAT=json` globally if preferred.

Search and list output in JSON mode includes a `_meta` block:

```json
{
  "results": [...],
  "_meta": {
    "accounts_searched": ["primary", "work"],
    "accounts_skipped": [{"name": "personal", "reason": "no_credentials"}],
    "accounts_failed": [],
    "account_timings": {
      "primary": {"elapsed_ms": 340, "timing": {"imap_connect": 181, "imap_search": 76}}
    },
    "total_found": 42,
    "limit_applied": 20,
    "daemon_mode": true,
    "timing": {"total_ms": 345}
  }
}
```

## Time Filters

The `--since` flag supports relative time expressions:

| Expression | Meaning |
|------------|---------|
| `today` | Since midnight today |
| `yesterday` | Since midnight yesterday |
| `this-week` | Since Monday of current week |
| `this-month` | Since 1st of current month |
| `7d`, `30d` | Last N days |
| `2024-01-15` | Specific date (ISO format) |

## Environment Variables

- `EMAIL_PASSWORD` — Password for non-interactive account setup
- `EMAIL_FORMAT` — Default output format (`json`, `table`, `raw`)
- `EMAIL_TRACE` — Enable timing/tracing output (`1` or `true`)

## Safety Checklist

Before running email automation:

- [ ] Run `email status` to verify accounts and credentials
- [ ] Check `accounts_skipped` in `_meta` if results are missing
- [ ] Use `--format json` for programmatic processing
- [ ] Quote multi-word search terms properly
- [ ] Use relative dates (`--since 7d`) instead of hardcoded dates
- [ ] Be aware that `email show` may expose sensitive content
