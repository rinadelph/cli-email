# Email CLI — Agent Reference

**First command every agent should run:**

```bash
email status
```

This shows which accounts exist, which have credentials, which is the default, and cache freshness. One command, zero guessing.

**For best performance, start the daemon:**

```bash
email daemon start
```

This keeps IMAP connections warm so subsequent calls are near-instant (~800ms vs ~3000ms+ cold). The daemon auto-shuts down after 10 minutes idle.

---

## Quick Reference

### Introspection (run these first)

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

### Daemon (Persistent Connections)

| Command | What it does |
|---------|-------------|
| `email daemon start` | Start background daemon with warm IMAP connections |
| `email daemon status` | Show running status, connected accounts, idle time |
| `email daemon stop` | Graceful shutdown |

The daemon keeps IMAP connections logged in and folders selected, so queries skip the expensive connect+login+select cycle. Auto-shuts down after 10 minutes idle. If the daemon is running, `email from`, `email search`, and `email list` automatically use it and fall back to direct IMAP if unavailable.

---

## Output Format

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
    "accounts_searched": ["primary", "secondary"],
    "accounts_skipped": [{"name": "personal", "reason": "no_credentials"}],
    "accounts_failed": [],
    "account_timings": {
      "primary": {"elapsed_ms": 340, "timing": {"imap_connect": 181, "imap_search": 76}},
      "secondary": {"elapsed_ms": 1441, "timing": {"imap_connect": 551, "imap_search": 558}}
    },
    "elapsed_ms": 2258
  }
}
```

### Pre-parsed Addresses

Sender and recipient fields are returned as structured objects:

```json
"sender": {"name": "Sender Name", "email": "sender@example.com"},
"to": [{"name": "User Name", "email": "user@example.com"}]
```

No need to regex-parse RFC2822 addresses.

---

## Timing & Tracing

Set `EMAIL_TRACE=1` for verbose timing output on stderr:

```bash
EMAIL_TRACE=1 email from sender --limit 5
```

This prints per-account timing to stderr (connect, search, filter, disconnect) while keeping JSON on stdout clean. Use this to diagnose slow operations.

Every command includes `elapsed_ms` in `_meta` by default — no env var needed for basic timing.

---

## Date Filters

All `--since` and `--before` flags accept:

| Format | Example |
|--------|---------|
| Absolute | `2026-05-01` |
| Relative days | `7d`, `30d` |
| Relative weeks | `2w` |
| Relative months | `3m` |
| Named | `today`, `yesterday`, `this-week`, `this-month` |

---

## Search Filters

`email search` and `email list` support:

| Flag | Description |
|------|-------------|
| `--from <name-or-email>` | Filter by sender |
| `--to <name-or-email>` | Filter by recipient |
| `--subject <text>` | Filter by subject |
| `--has-attachment` | Only emails with attachments |
| `--unread` | Only unread emails |
| `--since <date>` | Emails since date |
| `--before <date>` | Emails before date |
| `--in subject|from|to|body` | Restrict text search to one field |

---

## Multi-Account Behavior

When `--account` is omitted, commands search **all accounts** in parallel. Results include an `account` field on every email so you know which account it came from.

If the default account has no credentials, the CLI auto-falls back to the first healthy account and emits a warning to stderr:

```
[email-cli] WARNING: Default account 'personal' has no credentials. Using 'primary' instead.
```

Fix this permanently: `email accounts set-default primary`

---

## Setup (Non-Interactive)

```bash
# Via environment variable
export EMAIL_PASSWORD="your-app-password"
email accounts add work work@gmail.com --non-interactive

# Via password file
echo "your-app-password" > /tmp/pw.txt
email accounts add work work@gmail.com --password-file /tmp/pw.txt --non-interactive

# Custom provider
export EMAIL_PASSWORD="your-password"
export EMAIL_IMAP_HOST="imap.outlook.com"
export EMAIL_SMTP_HOST="smtp.outlook.com"
email accounts add outlook me@outlook.com --non-interactive
```

---

## Exit Codes

| Code | Meaning |
|------|---------|
| 0 | Success / no results (not an error) |
| 1 | Error (auth failure, not found, etc.) |

---

## Environment Variables

| Variable | Description |
|----------|-------------|
| `EMAIL_FORMAT` | Default output format: `json`, `table`, `raw` |
| `EMAIL_COMPACT` | Compact JSON output: `1` or `true` |
| `EMAIL_FIELDS` | Comma-separated fields to include |
| `EMAIL_TRACE` | Enable verbose timing on stderr: `1` or `true` |
| `EMAIL_PASSWORD` | Password for `accounts add --non-interactive` |
| `EMAIL_IMAP_HOST` | Override IMAP host for `accounts add` |
| `EMAIL_SMTP_HOST` | Override SMTP host for `accounts add` |
