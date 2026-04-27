"""CLI entry point using Typer — agent-first email CLI via IMAP/SMTP."""

import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

import typer
from rich import print as rprint
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.prompt import Confirm, Prompt

from email_cli.client import EmailClient
from email_cli.config import (
    add_account,
    get_account,
    get_password,
    list_account_names,
    load_accounts,
    remove_account,
    set_default_account,
    store_password,
)
from email_cli.formatter import (
    OutputFormat,
    print_attachments,
    print_downloaded,
    print_email_detail,
    print_emails,
    print_error,
    print_folders,
    print_success,
)
from email_cli.models import Account
from email_cli.notes import (
    add_note,
    get_notes,
    remove_note,
)
from email_cli.utils import resolve_account_name, get_healthy_account_name, parse_relative_date, Timer, trace, _is_trace_enabled

app = typer.Typer(help="email-cli: Multi-account email CLI via IMAP/SMTP")

accounts_app = typer.Typer(help="Manage email accounts")
app.add_typer(accounts_app, name="accounts")

attachments_app = typer.Typer(help="List or download attachments")
app.add_typer(attachments_app, name="attachments")

notes_app = typer.Typer(help="Agent notes and reminders")
app.add_typer(notes_app, name="notes")

daemon_app = typer.Typer(help="Manage the email daemon (persistent IMAP connections)")
app.add_typer(daemon_app, name="daemon")


def _is_tty() -> bool:
    return sys.stdout.isatty()


def _fmt_opt() -> OutputFormat:
    env = os.environ.get("EMAIL_FORMAT", "").lower()
    if env in ("json", "raw"):
        return OutputFormat(env)
    if not _is_tty():
        return OutputFormat.JSON
    return OutputFormat.TABLE


def _compact_opt() -> bool:
    return os.environ.get("EMAIL_COMPACT", "0").lower() in ("1", "true", "yes")


def _fields_opt() -> Optional[list[str]]:
    env = os.environ.get("EMAIL_FIELDS", "").strip()
    if env:
        return [f.strip() for f in env.split(",")]
    return None


def _get_client(name: Optional[str]) -> EmailClient:
    account_name = resolve_account_name(name)
    account = get_account(account_name)
    if not account:
        raise RuntimeError(f"Account '{account_name}' not found.")
    password = get_password(account_name)
    if not password:
        # Find accounts with credentials
        working = []
        for acc_name in list_account_names():
            if get_password(acc_name):
                acc = get_account(acc_name)
                if acc:
                    working.append(f"{acc_name} ({acc.email})")
        if working:
            raise RuntimeError(
                f"No stored password for account '{account_name}'.\n"
                f"Available accounts with credentials: {', '.join(working)}"
            )
        raise RuntimeError(f"No stored password for account '{account_name}'. Add credentials first.")
    client = EmailClient(account, password)
    return client


# -- Accounts commands --

@accounts_app.command("add")
def accounts_add(
    name: str = typer.Argument(..., help="Alias for this account"),
    email: str = typer.Argument(..., help="Email address"),
    imap_host: str = typer.Option("imap.gmail.com", help="IMAP server host"),
    imap_port: int = typer.Option(993, help="IMAP server port"),
    smtp_host: str = typer.Option("smtp.gmail.com", help="SMTP server host"),
    smtp_port: int = typer.Option(465, help="SMTP server port"),
    password: Optional[str] = typer.Option(None, help="Password/app-password. Prompted if omitted."),
    password_file: Optional[Path] = typer.Option(None, help="Read password from file (for automation)"),
    non_interactive: bool = typer.Option(False, "--non-interactive", help="Fail instead of prompting"),
    fmt: OutputFormat = typer.Option(_fmt_opt(), "--format", help="Output format"),
    compact: bool = typer.Option(_compact_opt(), "--compact", help="Compact JSON output"),
) -> None:
    """Add a new email account."""
    password = password or os.environ.get("EMAIL_PASSWORD")
    imap_host = os.environ.get("EMAIL_IMAP_HOST", imap_host)
    smtp_host = os.environ.get("EMAIL_SMTP_HOST", smtp_host)
    if not password and password_file:
        try:
            password = password_file.read_text().strip()
        except Exception as exc:
            print_error(f"Failed to read password file: {exc}", fmt, compact)
            raise typer.Exit(1)
    if not password:
        if non_interactive:
            print_error("Password required.", fmt, compact)
            raise typer.Exit(1)
        password = Prompt.ask(f"Password for {email}", password=True)
    account = Account(name=name, email=email, imap_host=imap_host, imap_port=imap_port, smtp_host=smtp_host, smtp_port=smtp_port)
    try:
        client = EmailClient(account, password)
        client.imap_connect()
        client.imap_disconnect()
    except Exception as exc:
        print_error(f"Failed to connect: {exc}", fmt, compact)
        raise typer.Exit(1)
    add_account(account)
    store_password(name, password)
    print_success(f"Account '{name}' added successfully.", fmt, compact)


@accounts_app.command("list")
def accounts_list(
    fmt: OutputFormat = typer.Option(_fmt_opt(), "--format", help="Output format"),
    compact: bool = typer.Option(_compact_opt(), "--compact", help="Compact JSON output"),
) -> None:
    """List configured accounts with health status."""
    data = load_accounts()
    default = data.get("default")
    accounts = data.get("accounts", [])
    if not accounts:
        if fmt != OutputFormat.RAW:
            print_error("No accounts configured.", fmt, compact)
        raise typer.Exit(1)
    if fmt == OutputFormat.JSON:
        # Enrich with credential status
        enriched = []
        for acc in accounts:
            entry = dict(acc)
            entry["has_credentials"] = bool(get_password(acc["name"]))
            entry["is_default"] = (acc["name"] == default)
            enriched.append(entry)
        json.dump({"default": default, "accounts": enriched}, sys.stdout, indent=None if compact else 2, ensure_ascii=False)
        print()
        return
    if fmt == OutputFormat.RAW:
        for acc in accounts:
            is_default = "*" if acc["name"] == default else ""
            has_pw = "+" if get_password(acc["name"]) else "-"
            print(f"{is_default}\t{has_pw}\t{acc['name']}\t{acc['email']}")
        return
    for acc in accounts:
        marker = " (default)" if acc["name"] == default else ""
        has_pw = get_password(acc["name"])
        status = "[green]✓[/green]" if has_pw else "[yellow]⚠ no creds[/yellow]"
        rprint(f"  {status}  [cyan]{acc['name']}[/cyan]: {acc['email']}{marker}")


@accounts_app.command("names")
def accounts_names(
    fmt: OutputFormat = typer.Option(_fmt_opt(), "--format", help="Output format"),
) -> None:
    """List account names only, one per line. Ideal for agent iteration."""
    data = load_accounts()
    accounts = data.get("accounts", [])
    if not accounts:
        raise typer.Exit(1)
    for acc in accounts:
        print(acc["name"])


@accounts_app.command("remove")
def accounts_remove(
    name: str = typer.Argument(..., help="Account alias to remove"),
    yes: bool = typer.Option(False, "--yes", help="Skip confirmation"),
    fmt: OutputFormat = typer.Option(_fmt_opt(), "--format", help="Output format"),
    compact: bool = typer.Option(_compact_opt(), "--compact", help="Compact JSON output"),
) -> None:
    """Remove an account."""
    if not yes and _fmt_opt() == OutputFormat.TABLE:
        if not Confirm.ask(f"Remove account '{name}'?"):
            raise typer.Exit(0)
    try:
        remove_account(name)
        print_success(f"Account '{name}' removed.", fmt, compact)
    except ValueError as exc:
        print_error(str(exc), fmt, compact)
        raise typer.Exit(1)


@accounts_app.command("set-default")
def accounts_set_default(
    name: str = typer.Argument(..., help="Account alias to set as default"),
    fmt: OutputFormat = typer.Option(_fmt_opt(), "--format", help="Output format"),
    compact: bool = typer.Option(_compact_opt(), "--compact", help="Compact JSON output"),
) -> None:
    """Set the default account."""
    try:
        set_default_account(name)
        print_success(f"Default account set to '{name}'.", fmt, compact)
    except ValueError as exc:
        print_error(str(exc), fmt, compact)
        raise typer.Exit(1)


@accounts_app.command("test")
def accounts_test(
    name: str = typer.Argument(..., help="Account alias to test"),
    fmt: OutputFormat = typer.Option(_fmt_opt(), "--format", help="Output format"),
    compact: bool = typer.Option(_compact_opt(), "--compact", help="Compact JSON output"),
) -> None:
    """Test IMAP/SMTP connectivity for an account."""
    account = get_account(name)
    if not account:
        print_error(f"Account '{name}' not found.", fmt, compact)
        raise typer.Exit(1)
    password = get_password(name)
    if not password:
        print_error(f"No stored password for account '{name}'.", fmt, compact)
        raise typer.Exit(1)
    client = EmailClient(account, password)
    result = client.test_connectivity()
    if fmt == OutputFormat.JSON:
        import json, sys
        json.dump(result, sys.stdout, indent=None if compact else 2, ensure_ascii=False)
        print()
        return
    if fmt == OutputFormat.RAW:
        status = "OK" if result["imap_ok"] and result["smtp_ok"] else "FAIL"
        print(f"{status}\t{result['account']}\t{result['email']}")
        if result["error"]:
            print(f"ERROR\t{result['error']}")
        return
    rprint(f"[cyan]Account:[/cyan] {result['account']} ({result['email']})")
    imap_status = "[green]OK[/green]" if result["imap_ok"] else "[red]FAIL[/red]"
    smtp_status = "[green]OK[/green]" if result["smtp_ok"] else "[red]FAIL[/red]"
    rprint(f"  IMAP ({result['imap_host']}:{result['imap_port']}): {imap_status}")
    rprint(f"  SMTP ({result['smtp_host']}:{result['smtp_port']}): {smtp_status}")
    if result["folders"]:
        rprint(f"  Folders: {len(result['folders'])} found")
    if result["error"]:
        rprint(f"  [red]Error: {result['error']}[/red]")


# -- Status command (agent introspection) --

@app.command("status")
def status_cmd(
    fmt: OutputFormat = typer.Option(_fmt_opt(), "--format", help="Output format"),
    compact: bool = typer.Option(_compact_opt(), "--compact", help="Compact JSON output"),
) -> None:
    """Show current CLI state: accounts, health, default, cache age. First command an agent should run."""
    data = load_accounts()
    default_name = data.get("default")
    accounts = data.get("accounts", [])

    if not accounts:
        print_error("No accounts configured. Run 'email accounts add <name> <email>' first.", fmt, compact)
        raise typer.Exit(1)

    account_statuses = []
    for acc in accounts:
        name = acc["name"]
        email = acc["email"]
        has_pw = bool(get_password(name))
        is_default = (name == default_name)

        # Check cache freshness
        cache_age = None
        try:
            import email_cli.cache as cache
            account_id = cache.upsert_account(name, email)
            conn = cache._get_conn()
            cur = conn.execute(
                "SELECT MAX(last_sync_at) as last_sync FROM folders WHERE account_id=?",
                (account_id,),
            )
            row = cur.fetchone()
            conn.close()
            if row and row["last_sync"]:
                last = datetime.fromisoformat(row["last_sync"])
                age_seconds = (datetime.now() - last).total_seconds()
                if age_seconds < 60:
                    cache_age = f"{int(age_seconds)}s"
                elif age_seconds < 3600:
                    cache_age = f"{int(age_seconds / 60)}m"
                else:
                    cache_age = f"{int(age_seconds / 3600)}h"
        except Exception:
            pass

        if has_pw:
            status = "ready"
            status_icon = "✓"
        else:
            status = "no_credentials"
            status_icon = "⚠"

        account_statuses.append({
            "name": name,
            "email": email,
            "status": status,
            "is_default": is_default,
            "cache_age": cache_age,
            "status_icon": status_icon,
        })

    # Determine effective default (falls back if default has no creds)
    effective_default = None
    if default_name:
        for s in account_statuses:
            if s["name"] == default_name:
                if s["status"] == "ready":
                    effective_default = default_name
                break
    if not effective_default:
        for s in account_statuses:
            if s["status"] == "ready":
                effective_default = s["name"]
                break

    if fmt == OutputFormat.JSON:
        result = {
            "default_account": default_name,
            "effective_default": effective_default,
            "default_has_credentials": any(
                s["name"] == default_name and s["status"] == "ready" for s in account_statuses
            ) if default_name else False,
            "accounts": account_statuses,
            "healthy_count": sum(1 for s in account_statuses if s["status"] == "ready"),
            "total_count": len(account_statuses),
        }
        json.dump(result, sys.stdout, indent=None if compact else 2, ensure_ascii=False)
        print()
        return

    if fmt == OutputFormat.RAW:
        print(f"default\t{default_name}")
        print(f"effective_default\t{effective_default}")
        for s in account_statuses:
            marker = "*" if s["is_default"] else " "
            print(f"{marker}\t{s['status']}\t{s['name']}\t{s['email']}\t{s.get('cache_age', '-')}")
        return

    # TABLE
    if default_name and effective_default != default_name:
        rprint(f"[yellow]⚠ Default '{default_name}' has no credentials — using '{effective_default}'[/yellow]")
        rprint(f"[dim]  Run `email accounts set-default {effective_default}` to fix.[/dim]")
    elif effective_default:
        rprint(f"[green]Default: {effective_default}[/green]")

    for s in account_statuses:
        icon = s["status_icon"]
        name_col = f"[cyan]{s['name']}[/cyan]"
        default_tag = " (default)" if s["is_default"] else ""
        cache_str = f"  cache: {s['cache_age']}" if s.get("cache_age") else ""
        if s["status"] == "ready":
            rprint(f"  [green]{icon}[/green]  {name_col}: {s['email']}{default_tag}{cache_str}")
        else:
            rprint(f"  [yellow]{icon}[/yellow]  {name_col}: {s['email']}{default_tag}  [yellow]no credentials[/yellow]")

    healthy = sum(1 for s in account_statuses if s["status"] == "ready")
    rprint(f"\n[dim]{healthy}/{len(account_statuses)} accounts ready[/dim]")


# -- Folders --

@app.command("folders")
def folders(
    account: Optional[str] = typer.Option(None, "--account", "-a", help="Account alias"),
    fmt: OutputFormat = typer.Option(_fmt_opt(), "--format", help="Output format"),
    compact: bool = typer.Option(_compact_opt(), "--compact", help="Compact JSON output"),
) -> None:
    """List IMAP folders."""
    try:
        client = _get_client(account)
    except RuntimeError as exc:
        print_error(str(exc), fmt, compact)
        raise typer.Exit(1)
    try:
        client.imap_connect()
        folders = client.list_folders()
        client.imap_disconnect()
    except Exception as exc:
        print_error(f"Error: {exc}", fmt, compact)
        raise typer.Exit(1)
    print_folders(folders, fmt, compact)


# -- Cross-account helpers --

def _try_daemon_search(
    accounts: list[str],
    query: str,
    folder: str,
    limit: int,
    in_field: Optional[str],
    since: Optional[str],
    before: Optional[str],
) -> Optional[tuple]:
    """Try searching via the daemon. Auto-starts daemon if not running.
    
    Returns (emails, meta) or None if daemon completely fails (fall back to direct IMAP).
    """
    from email_cli.daemon import is_daemon_running, send_to_daemon, start_daemon
    from email_cli.models import EmailMessage

    if not is_daemon_running():
        trace("Daemon not running — auto-starting...")
        ok = start_daemon(background=True)
        if ok:
            trace("Daemon auto-started, waiting for ready...")
            for _ in range(15):
                time.sleep(0.2)
                if is_daemon_running():
                    break
            else:
                trace("Daemon didn't become ready in time, falling back to direct IMAP")
                return None
        else:
            trace("Daemon auto-start failed, falling back to direct IMAP")
            return None

    all_emails = []
    meta = {
        "accounts_searched": [],
        "accounts_skipped": [],
        "accounts_failed": [],
        "account_timings": {},
        "source": "daemon",
    }

    for acc_name in accounts:
        t0 = time.time()
        try:
            resp = send_to_daemon({
                "method": "search",
                "params": {
                    "account": acc_name,
                    "folder": folder,
                    "criteria": "ALL",
                    "query": query,
                    "in_field": in_field,
                    "limit": limit,
                },
            }, timeout=30)
        except Exception as exc:
            meta["accounts_failed"].append({"name": acc_name, "error": str(exc)})
            continue

        if resp is None:
            meta["accounts_skipped"].append({"name": acc_name, "reason": "daemon_no_response"})
            continue

        if "error" in resp:
            meta["accounts_failed"].append({"name": acc_name, "error": resp["error"]})
            continue

        elapsed = (time.time() - t0) * 1000
        meta["accounts_searched"].append(acc_name)
        meta["account_timings"][acc_name] = {
            "elapsed_ms": int(elapsed),
            "daemon_ms": resp.get("elapsed_ms", 0),
        }

        for e_dict in resp.get("emails", []):
            # Reconstruct sender/to from pre-parsed or raw format
            sender_raw = e_dict.get("sender", "")
            if isinstance(sender_raw, dict):
                sender_str = f"{sender_raw.get('name', '')} <{sender_raw.get('email', '')}>" if sender_raw.get('name') else sender_raw.get('email', '')
            else:
                sender_str = sender_raw

            to_raw = e_dict.get("to", "")
            if isinstance(to_raw, list) and to_raw and isinstance(to_raw[0], dict):
                to_str = ", ".join(
                    f"{t.get('name', '')} <{t.get('email', '')}>" if t.get('name') else t.get('email', '')
                    for t in to_raw
                )
            else:
                to_str = to_raw if isinstance(to_raw, str) else ""

            e = EmailMessage(
                uid=e_dict["uid"],
                subject=e_dict.get("subject", ""),
                sender=sender_str,
                to=to_str,
                date=datetime.fromisoformat(e_dict["date"]) if e_dict.get("date") else None,
                raw_date="",
                body_preview=e_dict.get("body_preview", ""),
                flags=e_dict.get("flags", []),
                size=e_dict.get("size", 0),
                has_attachments=e_dict.get("has_attachments", False),
                account=acc_name,
            )
            all_emails.append(e)

    return all_emails, meta


def _search_accounts(
    query: str,
    accounts: list[str],
    folder: str,
    limit: int,
    in_field: Optional[str],
    since: Optional[str],
    before: Optional[str],
    progress: Optional[Progress],
    no_cache: bool,
) -> tuple:
    """Search multiple accounts and return (emails, meta) tuple.
    
    meta dict contains: accounts_searched, accounts_skipped, accounts_failed, elapsed_ms, from_cache, timing
    """
    t_total = Timer("search")
    all_emails = []
    meta = {
        "accounts_searched": [],
        "accounts_skipped": [],
        "accounts_failed": [],
        "from_cache": False,
        "account_timings": {},
    }
    total = len(accounts)

    for i, acc_name in enumerate(accounts, 1):
        t_acc = Timer(f"search.{acc_name}")
        try:
            client = _get_client(acc_name)
        except RuntimeError as exc:
            reason = str(exc)
            meta["accounts_skipped"].append({"name": acc_name, "reason": reason})
            print(f"[{i}/{total}] {acc_name}... SKIPPED (no credentials)", file=sys.stderr)
            continue

        print(f"[{i}/{total}] {acc_name}...", end="", file=sys.stderr)

        imap_criteria = ["ALL"]
        if since:
            parsed_since = parse_relative_date(since)
            if parsed_since:
                try:
                    d = datetime.strptime(parsed_since, "%Y-%m-%d")
                    imap_criteria.append(f"SINCE {d.strftime('%d-%b-%Y')}")
                except ValueError:
                    pass
        if before:
            parsed_before = parse_relative_date(before)
            if parsed_before:
                try:
                    d = datetime.strptime(parsed_before, "%Y-%m-%d")
                    imap_criteria.append(f"BEFORE {d.strftime('%d-%b-%Y')}")
                except ValueError:
                    pass
        criteria_str = " ".join(imap_criteria)

        try:
            t_acc.lap("init")
            trace(f"{acc_name}: connecting IMAP...")
            client.imap_connect()
            t_acc.lap("imap_connect")
            trace(f"{acc_name}: IMAP connected in {t_acc.laps[-2][0]}→{t_acc.laps[-1][0]}")

            trace(f"{acc_name}: searching (criteria={criteria_str}, folder={folder}, limit=200)...")
            emails = client.search(criteria=criteria_str, folder=folder, limit=200, progress=progress, no_cache=no_cache)
            t_acc.lap("imap_search")
            trace(f"{acc_name}: search returned {len(emails)} emails")

            query_lower = query.lower()
            filtered = []
            for e in emails:
                if in_field == "subject":
                    match = query_lower in e.subject.lower()
                elif in_field == "from":
                    match = query_lower in e.sender.lower()
                elif in_field == "to":
                    match = query_lower in e.to.lower()
                elif in_field == "body":
                    match = query_lower in e.body_preview.lower()
                else:
                    match = (
                        query_lower in e.subject.lower()
                        or query_lower in e.sender.lower()
                        or query_lower in e.to.lower()
                        or query_lower in e.body_preview.lower()
                    )
                if match:
                    filtered.append(e)
            filtered = filtered[:limit]
            t_acc.lap("filter")

            trace(f"{acc_name}: disconnecting...")
            client.imap_disconnect()
            t_acc.lap("imap_disconnect")

            meta["accounts_searched"].append(acc_name)
            acc_meta = t_acc.to_meta()
            meta["account_timings"][acc_name] = acc_meta
            print(f" {len(filtered)} hits ({acc_meta['elapsed_ms']}ms)", file=sys.stderr)
            t_acc.dump_trace()

        except Exception as exc:
            t_acc.lap("error")
            meta["accounts_failed"].append({"name": acc_name, "error": str(exc)})
            print(f" FAILED ({exc})", file=sys.stderr)
            t_acc.dump_trace()
            continue

        for e in filtered:
            e.account = acc_name
        all_emails.extend(filtered)

    # Deduplicate by (account, uid), sort by date desc
    t_total.lap("dedup")
    all_emails.sort(key=lambda e: e.date or datetime.min, reverse=True)
    seen = set()
    deduped = []
    for e in all_emails:
        key = (getattr(e, "account", ""), e.uid)
        if key not in seen:
            seen.add(key)
            deduped.append(e)

    t_total.finish()
    meta.update(t_total.to_meta())
    trace(f"search total: {t_total.elapsed_ms}ms")
    if _is_trace_enabled():
        t_total.dump_trace()

    return deduped[:limit], meta


# -- List emails (cross-account) --

@app.command("list")
def list_emails(
    account: Optional[str] = typer.Option(None, "--account", "-a", help="Account alias (omit for all)"),
    folder: str = typer.Option("INBOX", help="IMAP folder"),
    limit: int = typer.Option(20, help="Max emails to show"),
    unread: bool = typer.Option(False, "--unread", help="Only unread emails"),
    from_addr: Optional[str] = typer.Option(None, "--from", help="Filter by sender (name or email)"),
    to_addr: Optional[str] = typer.Option(None, "--to", help="Filter by recipient"),
    subject: Optional[str] = typer.Option(None, "--subject", help="Filter by subject"),
    has_attachment: bool = typer.Option(False, "--has-attachment", help="Only emails with attachments"),
    since: Optional[str] = typer.Option(None, help="Only emails since date (YYYY-MM-DD or relative: 7d, today, this-week)"),
    fmt: OutputFormat = typer.Option(_fmt_opt(), "--format", help="Output format"),
    compact: bool = typer.Option(_compact_opt(), "--compact", help="Compact JSON output"),
    fields: Optional[list[str]] = typer.Option(_fields_opt(), help="Fields to include (comma-separated)"),
    no_cache: bool = typer.Option(False, "--no-cache", help="Bypass cache and fetch from IMAP"),
) -> None:
    """List emails. Searches all accounts if --account omitted."""
    t = Timer("list")
    accounts = [account] if account else list_account_names()
    if not accounts:
        print_error("No accounts configured.", fmt, compact)
        raise typer.Exit(1)

    all_emails = []
    meta = {
        "accounts_searched": [],
        "accounts_skipped": [],
        "accounts_failed": [],
        "from_cache": False,
        "account_timings": {},
    }
    total = len(accounts)

    for i, acc_name in enumerate(accounts, 1):
        t_acc = Timer(f"list.{acc_name}")
        try:
            client = _get_client(acc_name)
        except RuntimeError as exc:
            meta["accounts_skipped"].append({"name": acc_name, "reason": str(exc)})
            print(f"[{i}/{total}] {acc_name}... SKIPPED (no credentials)", file=sys.stderr)
            continue
        criteria = "UNSEEN" if unread else "ALL"

        parsed_since = parse_relative_date(since) if since else None
        if parsed_since:
            try:
                d = datetime.strptime(parsed_since, "%Y-%m-%d")
                criteria = f'{criteria} SINCE {d.strftime("%d-%b-%Y")}' if criteria != "UNSEEN" else f'SINCE {d.strftime("%d-%b-%Y")}'
            except ValueError:
                pass

        print(f"[{i}/{total}] {acc_name}...", end="", file=sys.stderr)
        try:
            t_acc.lap("init")
            trace(f"{acc_name}: connecting IMAP...")
            client.imap_connect()
            t_acc.lap("imap_connect")
            with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), transient=True) as progress:
                emails = client.search(criteria=criteria, folder=folder, limit=limit, progress=progress, no_cache=no_cache)
            t_acc.lap("imap_search")
            client.imap_disconnect()
            t_acc.lap("imap_disconnect")
            meta["accounts_searched"].append(acc_name)
        except Exception as exc:
            t_acc.lap("error")
            meta["accounts_failed"].append({"name": acc_name, "error": str(exc)})
            print(f" FAILED ({exc})", file=sys.stderr)
            t_acc.dump_trace()
            continue

        # Apply client-side filters
        filtered = emails
        if from_addr:
            fa = from_addr.lower()
            filtered = [e for e in filtered if fa in e.sender.lower()]
        if to_addr:
            ta = to_addr.lower()
            filtered = [e for e in filtered if ta in e.to.lower()]
        if subject:
            s = subject.lower()
            filtered = [e for e in filtered if s in e.subject.lower()]
        if has_attachment:
            filtered = [e for e in filtered if e.has_attachments]
        t_acc.lap("filter")

        acc_meta = t_acc.to_meta()
        meta["account_timings"][acc_name] = acc_meta
        print(f" {len(filtered)} emails ({acc_meta['elapsed_ms']}ms)", file=sys.stderr)
        if _is_trace_enabled():
            t_acc.dump_trace()

        for e in filtered:
            e.account = acc_name
        all_emails.extend(filtered)

    t.lap("fetch")
    all_emails.sort(key=lambda e: e.date or datetime.min, reverse=True)
    seen = set()
    deduped = []
    for e in all_emails:
        key = (getattr(e, "account", ""), e.uid)
        if key not in seen:
            seen.add(key)
            deduped.append(e)
    all_emails = deduped[:limit]
    t.finish()

    meta.update(t.to_meta())

    if not all_emails:
        if fmt != OutputFormat.RAW:
            print_error("No emails found.", fmt, compact)
        raise typer.Exit(0)
    print_emails(all_emails, fmt, compact, fields, meta=meta)


# -- Search emails (cross-account) --

@app.command("search")
def search_emails(
    query: str = typer.Argument(..., help="Search string"),
    account: Optional[str] = typer.Option(None, "--account", "-a", help="Account alias (omit for all)"),
    folder: str = typer.Option("INBOX", help="IMAP folder"),
    limit: int = typer.Option(20, help="Max results"),
    in_field: Optional[str] = typer.Option(None, "--in", help="Search only in field: subject|from|to|body"),
    from_addr: Optional[str] = typer.Option(None, "--from", help="Filter by sender (name or email)"),
    to_addr: Optional[str] = typer.Option(None, "--to", help="Filter by recipient"),
    subject: Optional[str] = typer.Option(None, "--subject", help="Filter by subject"),
    has_attachment: bool = typer.Option(False, "--has-attachment", help="Only emails with attachments"),
    unread: bool = typer.Option(False, "--unread", help="Only unread emails"),
    since: Optional[str] = typer.Option(None, help="Only emails since date (YYYY-MM-DD or relative: 7d, today, this-week)"),
    before: Optional[str] = typer.Option(None, help="Only emails before date (YYYY-MM-DD or relative)"),
    fmt: OutputFormat = typer.Option(_fmt_opt(), "--format", help="Output format"),
    compact: bool = typer.Option(_compact_opt(), "--compact", help="Compact JSON output"),
    fields: Optional[list[str]] = typer.Option(_fields_opt(), help="Fields to include (comma-separated)"),
    no_cache: bool = typer.Option(False, "--no-cache", help="Bypass cache and fetch from IMAP"),
) -> None:
    """Search emails across all accounts by default."""
    # Resolve --from/--to/--subject into in_field for backward compat
    if from_addr and not in_field:
        pass
    if subject and not in_field:
        pass

    t = Timer("search_cmd")
    accounts = [account] if account else list_account_names()
    if not accounts:
        print_error("No accounts configured.", fmt, compact)
        raise typer.Exit(1)

    all_emails = []
    meta = {"accounts_searched": [], "account_timings": {}}
    for acc_name in accounts:
        with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), transient=True) as progress:
            filtered, search_meta = _search_accounts(query, [acc_name], folder, limit, in_field, since, before, progress, no_cache)
        meta["accounts_searched"].extend(search_meta.get("accounts_searched", []))
        if "account_timings" in search_meta:
            meta["account_timings"].update(search_meta["account_timings"])
        for e in filtered:
            e.account = acc_name
        # Apply additional client-side filters
        if from_addr:
            fa = from_addr.lower()
            filtered = [e for e in filtered if fa in e.sender.lower()]
        if to_addr:
            ta = to_addr.lower()
            filtered = [e for e in filtered if ta in e.to.lower()]
        if subject:
            s = subject.lower()
            filtered = [e for e in filtered if s in e.subject.lower()]
        if has_attachment:
            filtered = [e for e in filtered if e.has_attachments]
        if unread:
            filtered = [e for e in filtered if "\\Seen" not in e.flags]
        all_emails.extend(filtered)

    # Final dedup and sort across all accounts
    t.lap("dedup")
    all_emails.sort(key=lambda e: e.date or datetime.min, reverse=True)
    seen = set()
    deduped = []
    for e in all_emails:
        key = (getattr(e, "account", ""), e.uid)
        if key not in seen:
            seen.add(key)
            deduped.append(e)
    all_emails = deduped[:limit]

    t.finish()
    meta.update(t.to_meta())
    meta["total_results"] = len(all_emails)

    if not all_emails:
        if fmt != OutputFormat.RAW:
            print_error("No emails matched.", fmt, compact)
        raise typer.Exit(0)
    print_emails(all_emails, fmt, compact, fields, meta=meta)


# -- From shorthand --

@app.command("from")
def from_search(
    sender: str = typer.Argument(..., help="Sender name or email to search for"),
    account: Optional[str] = typer.Option(None, "--account", "-a", help="Account alias (omit for all)"),
    folder: str = typer.Option("INBOX", help="IMAP folder"),
    limit: int = typer.Option(20, help="Max results"),
    since: Optional[str] = typer.Option(None, help="Only emails since date (YYYY-MM-DD or relative: 7d, today)"),
    has_attachment: bool = typer.Option(False, "--has-attachment", help="Only emails with attachments"),
    fmt: OutputFormat = typer.Option(_fmt_opt(), "--format", help="Output format"),
    compact: bool = typer.Option(_compact_opt(), "--compact", help="Compact JSON output"),
    fields: Optional[list[str]] = typer.Option(_fields_opt(), help="Fields to include (comma-separated)"),
    no_cache: bool = typer.Option(False, "--no-cache", help="Bypass cache and fetch from IMAP"),
) -> None:
    """Search emails from a specific sender. Shorthand for 'search --from'."""
    t = Timer("from_cmd")
    accounts = [account] if account else list_account_names()
    if not accounts:
        print_error("No accounts configured.", fmt, compact)
        raise typer.Exit(1)

    # Try daemon first (near-instant if running)
    daemon_result = _try_daemon_search(accounts, sender, folder, limit, "from", since, None)
    if daemon_result is not None:
        all_emails, meta = daemon_result
        # Client-side filter for accuracy
        sender_lower = sender.lower()
        all_emails = [e for e in all_emails if sender_lower in e.sender.lower()]
        if has_attachment:
            all_emails = [e for e in all_emails if e.has_attachments]
        trace(f"from_cmd: daemon returned {len(all_emails)} emails")
        t.lap("daemon")
    else:
        all_emails = []
        meta = {
            "accounts_searched": [],
            "accounts_skipped": [],
            "accounts_failed": [],
            "account_timings": {},
        }

        for acc_name in accounts:
            with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"), transient=True) as progress:
                filtered, search_meta = _search_accounts(sender, [acc_name], folder, limit, "from", since, None, progress, no_cache)
            meta["accounts_searched"].extend(search_meta.get("accounts_searched", []))
            meta["accounts_skipped"].extend(search_meta.get("accounts_skipped", []))
            meta["accounts_failed"].extend(search_meta.get("accounts_failed", []))
            if "account_timings" in search_meta:
                meta["account_timings"].update(search_meta["account_timings"])
            sender_lower = sender.lower()
            filtered = [e for e in filtered if sender_lower in e.sender.lower()]
            if has_attachment:
                filtered = [e for e in filtered if e.has_attachments]
            for e in filtered:
                e.account = acc_name
            all_emails.extend(filtered)
        t.lap("direct_imap")

    t.lap("dedup")
    all_emails.sort(key=lambda e: e.date or datetime.min, reverse=True)
    seen = set()
    deduped = []
    for e in all_emails:
        key = (getattr(e, "account", ""), e.uid)
        if key not in seen:
            seen.add(key)
            deduped.append(e)
    all_emails = deduped[:limit]

    t.finish()
    meta.update(t.to_meta())
    meta["total_results"] = len(all_emails)

    if not all_emails:
        if fmt != OutputFormat.RAW:
            print_error(f"No emails from '{sender}' found.", fmt, compact)
        raise typer.Exit(0)
    print_emails(all_emails, fmt, compact, fields, meta=meta)


# -- Show email (batch) --

@app.command("show")
def show_email(
    uids: list[str] = typer.Argument(..., help="Email UID(s)"),
    account: Optional[str] = typer.Option(None, "--account", "-a", help="Account alias"),
    folder: str = typer.Option("INBOX", help="IMAP folder"),
    fmt: OutputFormat = typer.Option(_fmt_opt(), "--format", help="Output format"),
    compact: bool = typer.Option(_compact_opt(), "--compact", help="Compact JSON output"),
    body_file: Optional[str] = typer.Option(None, help="Write body to a file (only for single UID)"),
    fields: Optional[list[str]] = typer.Option(_fields_opt(), help="Fields to include (comma-separated)"),
) -> None:
    """Show full email content. Supports multiple UIDs."""
    t = Timer("show")
    try:
        client = _get_client(account)
    except RuntimeError as exc:
        print_error(str(exc), fmt, compact)
        raise typer.Exit(1)
    try:
        t.lap("init")
        client.imap_connect()
        client.select_folder(folder)
        t.lap("connect")
        results = []
        total = len(uids)
        for i, uid in enumerate(uids, 1):
            if total > 1:
                print(f"[{i}/{total}] fetching {uid}...", file=sys.stderr)
            summary, msg = client.fetch_full(uid)
            body = client._extract_full_body(msg)
            if fmt == OutputFormat.JSON:
                from email_cli.formatter import _email_to_dict
                d = _email_to_dict(summary)
                d["body"] = body
                results.append(d)
            elif fmt == OutputFormat.RAW:
                print(f"UID: {summary.uid}")
                print(f"From: {summary.sender}")
                print(f"To: {summary.to}")
                print(f"Subject: {summary.subject}")
                print(f"Date: {summary.raw_date}")
                print(f"Size: {summary.size}")
                print(f"HasAttachments: {summary.has_attachments}")
                print("---BODY---")
                print(body)
                print("---END---")
            else:
                print_email_detail(summary, body, fmt, body_file=body_file if len(uids) == 1 else None, compact=compact, fields=fields)
        t.lap("fetch")
        if fmt == OutputFormat.JSON:
            json.dump(results, sys.stdout, indent=None if compact else 2, ensure_ascii=False)
            print()
        client.imap_disconnect()
        t.finish()
        if _is_trace_enabled():
            t.dump_trace()
    except Exception as exc:
        print_error(f"Error: {exc}", fmt, compact)
        raise typer.Exit(1)


# -- Thread view --

@app.command("thread")
def thread_view(
    uid: str = typer.Argument(..., help="Email UID in the thread"),
    account: Optional[str] = typer.Option(None, "--account", "-a", help="Account alias"),
    folder: str = typer.Option("INBOX", help="IMAP folder"),
    fmt: OutputFormat = typer.Option(_fmt_opt(), "--format", help="Output format"),
    compact: bool = typer.Option(_compact_opt(), "--compact", help="Compact JSON output"),
    fields: Optional[list[str]] = typer.Option(_fields_opt(), help="Fields to include (comma-separated)"),
) -> None:
    """Show all emails in the same conversation thread."""
    try:
        client = _get_client(account)
    except RuntimeError as exc:
        print_error(str(exc), fmt, compact)
        raise typer.Exit(1)
    try:
        client.imap_connect()
        emails = client.get_thread(uid, folder)
        client.imap_disconnect()
    except Exception as exc:
        print_error(f"Error: {exc}", fmt, compact)
        raise typer.Exit(1)
    if not emails:
        if fmt != OutputFormat.RAW:
            print_error("No thread emails found.", fmt, compact)
        raise typer.Exit(0)
    print_emails(emails, fmt, compact, fields)


# -- Unified get command --

@app.command("get")
def get_email(
    uid: str = typer.Argument(..., help="Email UID"),
    account: Optional[str] = typer.Option(None, "--account", "-a", help="Account alias"),
    folder: str = typer.Option("INBOX", help="IMAP folder"),
    output: Path = typer.Option(Path("."), help="Output directory for body + attachments"),
    with_attachments: bool = typer.Option(False, "--with-attachments", help="Also download attachments"),
    fmt: OutputFormat = typer.Option(_fmt_opt(), "--format", help="Output format"),
    compact: bool = typer.Option(_compact_opt(), "--compact", help="Compact JSON output"),
) -> None:
    """Get everything from an email: body + attachments in one shot."""
    try:
        client = _get_client(account)
    except RuntimeError as exc:
        print_error(str(exc), fmt, compact)
        raise typer.Exit(1)
    try:
        client.imap_connect()
        client.select_folder(folder)
        summary, msg = client.fetch_full(uid)
        body = client._extract_full_body(msg)
        client.imap_disconnect()
    except Exception as exc:
        print_error(f"Error: {exc}", fmt, compact)
        raise typer.Exit(1)

    result = {
        "uid": uid,
        "subject": summary.subject,
        "sender": summary.sender,
        "to": summary.to,
        "date": summary.raw_date,
        "body": body,
        "body_file": None,
        "attachments": [],
    }

    output.mkdir(parents=True, exist_ok=True)
    body_file = output / f"{uid}_body.txt"
    with open(body_file, "w", encoding="utf-8") as f:
        f.write(body)
    result["body_file"] = str(body_file)

    if with_attachments:
        try:
            client.imap_connect()
            client.select_folder(folder)
            paths = client.download_attachments(uid, output)
            client.imap_disconnect()
            result["attachments"] = [str(p) for p in paths]
        except Exception as exc:
            print_error(f"Attachment download failed: {exc}", fmt, compact)

    if fmt == OutputFormat.JSON:
        import json, sys
        json.dump(result, sys.stdout, indent=None if compact else 2, ensure_ascii=False)
        print()
        return
    if fmt == OutputFormat.RAW:
        print(f"UID\t{result['uid']}")
        print(f"Subject\t{result['subject']}")
        print(f"Body\t{result['body_file']}")
        for a in result["attachments"]:
            print(f"Attachment\t{a}")
        return
    rprint(f"[cyan]UID:[/cyan] {result['uid']}")
    rprint(f"[cyan]Subject:[/cyan] {result['subject']}")
    rprint(f"[cyan]Body:[/cyan] {result['body_file']}")
    if result["attachments"]:
        rprint(f"[cyan]Attachments:[/cyan]")
        for a in result["attachments"]:
            rprint(f"  [green]{a}[/green]")


# -- Export email --

@app.command("export")
def export_email(
    uid: str = typer.Argument(..., help="Email UID"),
    account: Optional[str] = typer.Option(None, "--account", "-a", help="Account alias"),
    folder: str = typer.Option("INBOX", help="IMAP folder"),
    output: Path = typer.Option(Path("."), help="Output directory"),
    fmt: OutputFormat = typer.Option(_fmt_opt(), "--format", help="Output format"),
    compact: bool = typer.Option(_compact_opt(), "--compact", help="Compact JSON output"),
) -> None:
    """Export full email (body + attachments) to a directory."""
    try:
        client = _get_client(account)
    except RuntimeError as exc:
        print_error(str(exc), fmt, compact)
        raise typer.Exit(1)
    try:
        client.imap_connect()
        client.select_folder(folder)
        result = client.export_email(uid, output)
        client.imap_disconnect()
    except Exception as exc:
        print_error(f"Error: {exc}", fmt, compact)
        raise typer.Exit(1)
    if fmt == OutputFormat.JSON:
        import json, sys
        json.dump(result, sys.stdout, indent=None if compact else 2, ensure_ascii=False)
        print()
        return
    if fmt == OutputFormat.RAW:
        print(f"UID\t{result['uid']}")
        print(f"Subject\t{result['subject']}")
        print(f"Body\t{result['body_file']}")
        for a in result["attachments"]:
            print(f"Attachment\t{a}")
        return
    rprint(f"[cyan]UID:[/cyan] {result['uid']}")
    rprint(f"[cyan]Subject:[/cyan] {result['subject']}")
    rprint(f"[cyan]Body:[/cyan] {result['body_file']}")
    if result["attachments"]:
        rprint(f"[cyan]Attachments:[/cyan]")
        for a in result["attachments"]:
            rprint(f"  [green]{a}[/green]")


# -- Attachments commands --

@attachments_app.command("list")
def attachments_list(
    uid: str = typer.Argument(..., help="Email UID"),
    account: Optional[str] = typer.Option(None, "--account", "-a", help="Account alias"),
    folder: str = typer.Option("INBOX", help="IMAP folder"),
    fmt: OutputFormat = typer.Option(_fmt_opt(), "--format", help="Output format"),
    compact: bool = typer.Option(_compact_opt(), "--compact", help="Compact JSON output"),
) -> None:
    """List attachments for an email."""
    try:
        client = _get_client(account)
    except RuntimeError as exc:
        print_error(str(exc), fmt, compact)
        raise typer.Exit(1)
    try:
        client.imap_connect()
        client.select_folder(folder)
        attachments = client.list_attachments(uid)
        client.imap_disconnect()
    except Exception as exc:
        print_error(f"Error: {exc}", fmt, compact)
        raise typer.Exit(1)
    if not attachments:
        if fmt != OutputFormat.RAW:
            print_error("No attachments found.", fmt, compact)
        raise typer.Exit(0)
    print_attachments(attachments, fmt, compact)


@attachments_app.command("download")
def attachments_download(
    uid: str = typer.Argument(..., help="Email UID"),
    account: Optional[str] = typer.Option(None, "--account", "-a", help="Account alias"),
    folder: str = typer.Option("INBOX", help="IMAP folder"),
    output: Path = typer.Option(Path("."), help="Output directory"),
    fmt: OutputFormat = typer.Option(_fmt_opt(), "--format", help="Output format"),
    compact: bool = typer.Option(_compact_opt(), "--compact", help="Compact JSON output"),
) -> None:
    """Download all attachments from an email."""
    try:
        client = _get_client(account)
    except RuntimeError as exc:
        print_error(str(exc), fmt, compact)
        raise typer.Exit(1)
    try:
        client.imap_connect()
        client.select_folder(folder)
        paths = client.download_attachments(uid, output)
        client.imap_disconnect()
    except Exception as exc:
        print_error(f"Error: {exc}", fmt, compact)
        raise typer.Exit(1)
    if not paths:
        if fmt != OutputFormat.RAW:
            print_error("No attachments found.", fmt, compact)
        raise typer.Exit(0)
    print_downloaded(paths, fmt, compact)


# -- Compose with preview --

@app.command("compose")
def compose_email(
    to: list[str] = typer.Option([], help="Recipient email address(es)"),
    subject: str = typer.Option(..., help="Email subject"),
    body: Optional[str] = typer.Option(None, help="Email body text"),
    body_file: Optional[Path] = typer.Option(None, help="Read body from file"),
    account: Optional[str] = typer.Option(None, "--account", "-a", help="Account alias"),
    cc: Optional[list[str]] = typer.Option(None, help="CC addresses"),
    bcc: Optional[list[str]] = typer.Option(None, help="BCC addresses"),
    attach: Optional[list[Path]] = typer.Option(None, help="File(s) to attach"),
    preview: bool = typer.Option(False, "--preview", help="Show formatted email without sending"),
    draft: bool = typer.Option(False, "--draft", help="Save to drafts instead of sending (not yet implemented)"),
    fmt: OutputFormat = typer.Option(_fmt_opt(), "--format", help="Output format"),
    compact: bool = typer.Option(_compact_opt(), "--compact", help="Compact JSON output"),
) -> None:
    """Compose an email. Use --preview to review before sending."""
    if not to:
        print_error("At least one --to recipient is required.", fmt, compact)
        raise typer.Exit(1)
    if body_file:
        try:
            body = body_file.read_text()
        except Exception as exc:
            print_error(f"Failed to read body file: {exc}", fmt, compact)
            raise typer.Exit(1)
    if body is None:
        body = typer.prompt("Email body", default="")

    account_name = resolve_account_name(account)
    account_obj = get_account(account_name)
    if not account_obj:
        print_error(f"Account '{account_name}' not found.", fmt, compact)
        raise typer.Exit(1)

    if draft:
        print_error("Draft save not yet implemented. Use --preview to review.", fmt, compact)
        raise typer.Exit(1)

    if preview:
        preview_data = {
            "from": account_obj.email,
            "to": to,
            "cc": cc or [],
            "bcc": bcc or [],
            "subject": subject,
            "body": body,
            "attachments": [str(p) for p in attach] if attach else [],
        }
        if fmt == OutputFormat.JSON:
            import json, sys
            json.dump(preview_data, sys.stdout, indent=None if compact else 2, ensure_ascii=False)
            print()
            return
        rprint(f"[yellow][PREVIEW][/yellow]")
        rprint(f"[cyan]From:[/cyan] {preview_data['from']}")
        rprint(f"[cyan]To:[/cyan] {', '.join(preview_data['to'])}")
        if preview_data['cc']:
            rprint(f"[cyan]Cc:[/cyan] {', '.join(preview_data['cc'])}")
        rprint(f"[cyan]Subject:[/cyan] {preview_data['subject']}")
        rprint(f"[cyan]Body:[/cyan]\n{preview_data['body']}")
        if preview_data['attachments']:
            rprint(f"[cyan]Attachments:[/cyan] {', '.join(preview_data['attachments'])}")
        return

    # Only get password and create client when actually sending
    password = get_password(account_name)
    if not password:
        print_error(f"No stored password for account '{account_name}'.", fmt, compact)
        raise typer.Exit(1)
    client = EmailClient(account_obj, password)

    try:
        client.smtp_connect()
        client.send_email(
            to=to,
            subject=subject,
            body=body,
            cc=cc or [],
            bcc=bcc or [],
            attachments=attach,
        )
        client.smtp_disconnect()
    except Exception as exc:
        print_error(f"Failed to send: {exc}", fmt, compact)
        raise typer.Exit(1)
    print_success("Email sent successfully.", fmt, compact)


# -- Send with dry-run --

@app.command("send")
def send_email(
    to: list[str] = typer.Option([], help="Recipient email address(es)"),
    subject: str = typer.Option(..., help="Email subject"),
    body: Optional[str] = typer.Option(None, help="Email body text"),
    account: Optional[str] = typer.Option(None, "--account", "-a", help="Account alias"),
    cc: Optional[list[str]] = typer.Option(None, help="CC addresses"),
    bcc: Optional[list[str]] = typer.Option(None, help="BCC addresses"),
    attach: Optional[list[Path]] = typer.Option(None, help="File(s) to attach"),
    body_file: Optional[Path] = typer.Option(None, help="Read body from file"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Print email without sending"),
    fmt: OutputFormat = typer.Option(_fmt_opt(), "--format", help="Output format"),
    compact: bool = typer.Option(_compact_opt(), "--compact", help="Compact JSON output"),
) -> None:
    """Send an email. Use --dry-run to preview."""
    if not to:
        print_error("At least one --to recipient is required.", fmt, compact)
        raise typer.Exit(1)
    if body_file:
        try:
            body = body_file.read_text()
        except Exception as exc:
            print_error(f"Failed to read body file: {exc}", fmt, compact)
            raise typer.Exit(1)
    if body is None:
        body = typer.prompt("Email body", default="")

    account_name = resolve_account_name(account)
    account_obj = get_account(account_name)
    if not account_obj:
        print_error(f"Account '{account_name}' not found.", fmt, compact)
        raise typer.Exit(1)

    if dry_run:
        preview = {
            "from": account_obj.email,
            "to": to,
            "cc": cc or [],
            "bcc": bcc or [],
            "subject": subject,
            "body": body,
            "attachments": [str(p) for p in attach] if attach else [],
        }
        if fmt == OutputFormat.JSON:
            import json, sys
            json.dump(preview, sys.stdout, indent=None if compact else 2, ensure_ascii=False)
            print()
            return
        rprint(f"[yellow][DRY RUN] Would send:[/yellow]")
        rprint(f"[cyan]From:[/cyan] {preview['from']}")
        rprint(f"[cyan]To:[/cyan] {', '.join(preview['to'])}")
        rprint(f"[cyan]Subject:[/cyan] {preview['subject']}")
        rprint(f"[cyan]Body:[/cyan]\n{preview['body']}")
        return

    # Only get password and create client when actually sending
    password = get_password(account_name)
    if not password:
        print_error(f"No stored password for account '{account_name}'.", fmt, compact)
        raise typer.Exit(1)
    client = EmailClient(account_obj, password)

    try:
        client.smtp_connect()
        client.send_email(
            to=to,
            subject=subject,
            body=body,
            cc=cc or [],
            bcc=bcc or [],
            attachments=attach,
        )
        client.smtp_disconnect()
    except Exception as exc:
        print_error(f"Failed to send: {exc}", fmt, compact)
        raise typer.Exit(1)
    print_success("Email sent successfully.", fmt, compact)


# -- Flag manipulation --

@app.command("mark-read")
def mark_read(
    uid: str = typer.Argument(..., help="Email UID"),
    account: Optional[str] = typer.Option(None, "--account", "-a", help="Account alias"),
    folder: str = typer.Option("INBOX", help="IMAP folder"),
    fmt: OutputFormat = typer.Option(_fmt_opt(), "--format", help="Output format"),
    compact: bool = typer.Option(_compact_opt(), "--compact", help="Compact JSON output"),
) -> None:
    """Mark an email as read."""
    try:
        client = _get_client(account)
    except RuntimeError as exc:
        print_error(str(exc), fmt, compact)
        raise typer.Exit(1)
    try:
        client.imap_connect()
        client.select_folder(folder)
        client.mark_read(uid, folder)
        client.imap_disconnect()
    except Exception as exc:
        print_error(f"Error: {exc}", fmt, compact)
        raise typer.Exit(1)
    print_success(f"Marked {uid} as read.", fmt, compact)


@app.command("mark-unread")
def mark_unread(
    uid: str = typer.Argument(..., help="Email UID"),
    account: Optional[str] = typer.Option(None, "--account", "-a", help="Account alias"),
    folder: str = typer.Option("INBOX", help="IMAP folder"),
    fmt: OutputFormat = typer.Option(_fmt_opt(), "--format", help="Output format"),
    compact: bool = typer.Option(_compact_opt(), "--compact", help="Compact JSON output"),
) -> None:
    """Mark an email as unread."""
    try:
        client = _get_client(account)
    except RuntimeError as exc:
        print_error(str(exc), fmt, compact)
        raise typer.Exit(1)
    try:
        client.imap_connect()
        client.select_folder(folder)
        client.mark_unread(uid, folder)
        client.imap_disconnect()
    except Exception as exc:
        print_error(f"Error: {exc}", fmt, compact)
        raise typer.Exit(1)
    print_success(f"Marked {uid} as unread.", fmt, compact)


@app.command("move")
def move_email(
    uid: str = typer.Argument(..., help="Email UID"),
    dest_folder: str = typer.Argument(..., help="Destination IMAP folder"),
    account: Optional[str] = typer.Option(None, "--account", "-a", help="Account alias"),
    folder: str = typer.Option("INBOX", help="Source IMAP folder"),
    fmt: OutputFormat = typer.Option(_fmt_opt(), "--format", help="Output format"),
    compact: bool = typer.Option(_compact_opt(), "--compact", help="Compact JSON output"),
) -> None:
    """Move an email to another folder."""
    try:
        client = _get_client(account)
    except RuntimeError as exc:
        print_error(str(exc), fmt, compact)
        raise typer.Exit(1)
    try:
        client.imap_connect()
        client.select_folder(folder)
        client.move_email(uid, dest_folder, folder)
        client.imap_disconnect()
    except Exception as exc:
        print_error(f"Error: {exc}", fmt, compact)
        raise typer.Exit(1)
    print_success(f"Moved {uid} to '{dest_folder}'.", fmt, compact)


# -- Notes commands --

@notes_app.command("add")
def notes_add(
    message: str = typer.Argument(..., help="Note text to store"),
    tag: Optional[str] = typer.Option(None, help="Optional tag/category"),
    fmt: OutputFormat = typer.Option(_fmt_opt(), "--format", help="Output format"),
    compact: bool = typer.Option(_compact_opt(), "--compact", help="Compact JSON output"),
) -> None:
    """Add a note/reminder for agents."""
    note = add_note(message, tag=tag)
    print_success(f"Note #{note['id']} added.", fmt, compact)


@notes_app.command("list")
def notes_list(
    tag: Optional[str] = typer.Option(None, help="Filter by tag"),
    fmt: OutputFormat = typer.Option(_fmt_opt(), "--format", help="Output format"),
    compact: bool = typer.Option(_compact_opt(), "--compact", help="Compact JSON output"),
) -> None:
    """List agent notes/reminders."""
    notes = get_notes(tag=tag)
    if not notes:
        if fmt != OutputFormat.RAW:
            print_error("No notes found.", fmt, compact)
        raise typer.Exit(0)
    if fmt == OutputFormat.JSON:
        import json, sys
        json.dump(notes, sys.stdout, indent=None if compact else 2, ensure_ascii=False)
        print()
        return
    if fmt == OutputFormat.RAW:
        for n in notes:
            tag_str = n.get("tag", "")
            print(f"{n['id']}\t{n['created']}\t{tag_str}\t{n['message']}")
        return
    from rich.table import Table
    table = Table(title="Agent Notes")
    table.add_column("#", style="cyan")
    table.add_column("Date", style="magenta")
    table.add_column("Tag", style="green")
    table.add_column("Message", style="white")
    for n in notes:
        table.add_row(str(n["id"]), n["created"], n.get("tag", ""), n["message"])
    console = Console()
    console.print(table)


@notes_app.command("remove")
def notes_remove(
    note_id: int = typer.Argument(..., help="Note ID to remove"),
    fmt: OutputFormat = typer.Option(_fmt_opt(), "--format", help="Output format"),
    compact: bool = typer.Option(_compact_opt(), "--compact", help="Compact JSON output"),
) -> None:
    """Remove a note by ID."""
    try:
        remove_note(note_id)
        print_success(f"Note #{note_id} removed.", fmt, compact)
    except ValueError as exc:
        print_error(str(exc), fmt, compact)
        raise typer.Exit(1)


# -- Daemon commands --

@daemon_app.command("start")
def daemon_start(
    fmt: OutputFormat = typer.Option(_fmt_opt(), "--format", help="Output format"),
    compact: bool = typer.Option(_compact_opt(), "--compact", help="Compact JSON output"),
) -> None:
    """Start the email daemon. Keeps IMAP connections warm for faster queries."""
    from email_cli.daemon import start_daemon, is_daemon_running
    if is_daemon_running():
        print_success("Daemon already running.", fmt, compact)
        return
    ok = start_daemon(background=True)
    if ok:
        print_success("Daemon started. IMAP connections will stay warm.", fmt, compact)
    else:
        print_error("Failed to start daemon.", fmt, compact)
        raise typer.Exit(1)


@daemon_app.command("stop")
def daemon_stop(
    fmt: OutputFormat = typer.Option(_fmt_opt(), "--format", help="Output format"),
    compact: bool = typer.Option(_compact_opt(), "--compact", help="Compact JSON output"),
) -> None:
    """Stop the email daemon gracefully."""
    from email_cli.daemon import stop_daemon, is_daemon_running
    if not is_daemon_running():
        print_success("Daemon not running.", fmt, compact)
        return
    ok = stop_daemon()
    if ok:
        print_success("Daemon stopped.", fmt, compact)
    else:
        print_error("Failed to stop daemon.", fmt, compact)
        raise typer.Exit(1)


@daemon_app.command("status")
def daemon_status(
    fmt: OutputFormat = typer.Option(_fmt_opt(), "--format", help="Output format"),
    compact: bool = typer.Option(_compact_opt(), "--compact", help="Compact JSON output"),
) -> None:
    """Show daemon status: running, connected accounts, idle time."""
    from email_cli.daemon import is_daemon_running, send_to_daemon
    if not is_daemon_running():
        if fmt == OutputFormat.JSON:
            json.dump({"running": False}, sys.stdout, ensure_ascii=False)
            print()
        else:
            rprint("[yellow]Daemon not running.[/yellow]")
        return
    resp = send_to_daemon({"method": "status"})
    if not resp:
        if fmt == OutputFormat.JSON:
            json.dump({"running": False, "error": "no response"}, sys.stdout, ensure_ascii=False)
            print()
        else:
            rprint("[red]Daemon not responding.[/red]")
        return
    if fmt == OutputFormat.JSON:
        resp["running"] = True
        json.dump(resp, sys.stdout, indent=None if compact else 2, ensure_ascii=False)
        print()
        return
    rprint(f"[green]Daemon running[/green]  {resp.get('active_connections', 0)} active connections")
    for acc in resp.get("accounts", []):
        idle = acc.get("idle_seconds", 0)
        status = "[green]connected[/green]" if acc.get("connected") else "[red]disconnected[/red]"
        rprint(f"  {status}  [cyan]{acc['name']}[/cyan]  idle: {idle}s")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
