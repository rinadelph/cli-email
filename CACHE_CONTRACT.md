## Cache Integration Contract

### 1. IMAP inputs → SQL parameters (client.search at lines 190-220)
| IMAP Input | SQL Parameter | How |
|---|---|---|
| `folder: str` | `folder_id: int` | `cache.get_or_create_folder(account_id, folder)` |
| `account: Account` | `account_id: int` | `cache.get_account_id(account.name, account.email)` |
| `criteria: str` | `query, since, before` | Caller parses "SINCE 27-Apr-2026 ALL" into SQL filters |
| `limit: int` | `LIMIT ?` | Direct pass-through |

### 2. Return type mapping: EmailMessage ↔ emails table
| EmailMessage field | emails column | Conversion |
|---|---|---|
| `uid` | `uid TEXT` | direct |
| `subject` | `subject TEXT` | direct |
| `sender` | `sender TEXT` | direct |
| `to` | `recipients TEXT` | merged To field |
| `date` | `date TIMESTAMP` | `isoformat()` write, `fromisoformat()` read |
| `raw_date` | `raw_date TEXT` | direct |
| `body_preview` | `body_preview TEXT` | direct |
| `flags` | `flags TEXT` | `json.dumps()` / `json.loads()` |
| `size` | `size INTEGER` | direct |
| `has_attachments` | `has_attachments INTEGER` | `1` / `0` |

### 3. Short-circuit point
```
search(criteria, folder, limit)
  → account_id = cache.get_account_id(...)
  → folder_id = cache.get_or_create_folder(account_id, folder)
  → if cache.is_stale(account_id, folder_id):
      → select_folder(folder)          # IMAP SELECT
      → uidv = extract_uidvalidity()    # from SELECT response
      → if uidv_changed(folder_id, uidv): clear folder cache
      → IMAP SEARCH → UIDs
      → for UID: _fetch_summary()       # parse MIME
      → cache.sync_emails(...)          # write to SQLite
  → return cache.get_cached_emails(...)   # instant SQL
```

### 4. UIDVALIDITY extraction
After `self._imap.select(folder)`, read `self._imap.untagged_responses.get('UIDVALIDITY', [])` to get the UIDVALIDITY value. Store in `folders.uidvalidity`. On sync, if stored != current, delete all cached emails for that folder before syncing fresh data.
