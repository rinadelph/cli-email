"""Agent notes/reminders storage."""

import json
from datetime import datetime
from pathlib import Path
from typing import Optional

NOTES_FILE = Path.home() / ".config" / "email-cli" / "notes.json"


def _ensure_dir() -> None:
    NOTES_FILE.parent.mkdir(parents=True, exist_ok=True)


def _load() -> list[dict]:
    _ensure_dir()
    if not NOTES_FILE.exists():
        return []
    try:
        with open(NOTES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        return []


def _save(notes: list[dict]) -> None:
    _ensure_dir()
    with open(NOTES_FILE, "w", encoding="utf-8") as f:
        json.dump(notes, f, indent=2, ensure_ascii=False)


def add_note(message: str, tag: Optional[str] = None) -> dict:
    """Add a new note and return it."""
    notes = _load()
    note_id = max((n["id"] for n in notes), default=0) + 1
    note = {
        "id": note_id,
        "message": message,
        "tag": tag or "",
        "created": datetime.now().isoformat(),
    }
    notes.append(note)
    _save(notes)
    return note


def get_notes(tag: Optional[str] = None) -> list[dict]:
    """Return all notes, optionally filtered by tag."""
    notes = _load()
    if tag:
        notes = [n for n in notes if n.get("tag") == tag]
    return list(reversed(notes))  # newest first


def remove_note(note_id: int) -> None:
    """Remove a note by ID."""
    notes = _load()
    original_len = len(notes)
    notes = [n for n in notes if n["id"] != note_id]
    if len(notes) == original_len:
        raise ValueError(f"Note #{note_id} not found.")
    _save(notes)
