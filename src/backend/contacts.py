"""Contacts book: name -> email, so "email Maya" can reach Maya. A plain JSON file: SCENE_CONTACTS, else data/contacts.json
(git-ignored; copy contacts.example.json to start). The model only ever supplies a NAME; the address comes from here, so a
hallucinated or injected address can never be used. Sending still needs a tap, and the Apps Script allowlist is the hard lock.
"""

import json
import os
import re
from pathlib import Path

_EMAIL = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[^@\s,;]{2,}$")


def _path() -> Path:
    env = os.environ.get("SCENE_CONTACTS")
    return Path(env) if env else Path(os.environ.get("SCENE_DATA_DIR", Path(__file__).parent / "data")) / "contacts.json"


_SLACK_ID = re.compile(r"^[UW][A-Z0-9]{2,}$")


def _raw() -> dict:
    """name -> email string, or {"email": ..., "slack": member id}. Either shape works in the file."""
    try:
        book = json.loads(_path().read_text())
    except (OSError, ValueError):
        return {}
    return book if isinstance(book, dict) else {}


def _entry(v) -> dict:
    if isinstance(v, dict):
        return {"email": str(v.get("email") or ""), "slack": str(v.get("slack") or "")}
    return {"email": str(v or ""), "slack": ""}


def all() -> dict[str, str]:
    """name -> email."""
    return {str(k): _entry(v)["email"] for k, v in _raw().items()}


def find(name) -> dict | None:
    """{name, email[, slack]} for a full name, or a first name / word that matches exactly one contact. None when unknown or ambiguous."""
    q = str(name or "").strip().lower()
    if not q or "@" in q:
        return None
    book = _raw()
    hit = next((k for k in book if str(k).lower() == q), None)
    if hit is None:
        hits = [k for k in book if q in str(k).lower().split()]
        hit = hits[0] if len(hits) == 1 else None
    if hit is None:
        return None
    e = _entry(book[hit])
    return {"name": str(hit), "email": e["email"], **({"slack": e["slack"]} if e["slack"] else {})}


def add(name: str, email: str, slack: str | None = None) -> None:
    """Add or update a contact. Needs a valid email or a Slack member id (U...). Re-adding keeps what you do not pass."""
    name, email, slack = str(name or "").strip(), str(email or "").strip(), str(slack or "").strip()
    if not name:
        raise ValueError("name is required")
    if slack and not _SLACK_ID.match(slack):
        raise ValueError(f"'{slack}' is not a Slack member id (looks like U012ABCDEF)")
    if email and not _EMAIL.match(email) or not email and not slack:
        raise ValueError(f"'{email}' is not a valid email address")
    book = _raw()
    old = _entry(book.get(name))
    new = {"email": email or old["email"], "slack": slack or old["slack"]}
    book[name] = new if new["slack"] else new["email"]
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(book, indent=2))
