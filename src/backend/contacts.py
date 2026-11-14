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


def all() -> dict[str, str]:
    try:
        book = json.loads(_path().read_text())
    except (OSError, ValueError):
        return {}
    return {str(k): str(v) for k, v in book.items()} if isinstance(book, dict) else {}


def find(name) -> dict | None:
    """{name, email} for a full name, or a first name / word that matches exactly one contact. None when unknown or ambiguous."""
    q = str(name or "").strip().lower()
    if not q or "@" in q:
        return None
    book = all()
    for full, email in book.items():
        if full.lower() == q:
            return {"name": full, "email": email}
    hits = [(full, email) for full, email in book.items() if q in full.lower().split()]
    return {"name": hits[0][0], "email": hits[0][1]} if len(hits) == 1 else None


def add(name: str, email: str) -> None:
    name, email = str(name or "").strip(), str(email or "").strip()
    if not name:
        raise ValueError("name is required")
    if not _EMAIL.match(email):
        raise ValueError(f"'{email}' is not a valid email address")
    book = all()
    book[name] = email
    p = _path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(book, indent=2))
