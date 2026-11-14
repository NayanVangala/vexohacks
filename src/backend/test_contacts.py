"""Contacts book: name -> email, so Scene can email real people (still only after a tap, and only if the Apps Script allows it)."""
import json
import os
import tempfile
from pathlib import Path

os.environ.setdefault("SCENE_DATA_DIR", tempfile.mkdtemp())

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import contacts  # noqa: E402
import server  # noqa: E402


@pytest.fixture(autouse=True)
def book(monkeypatch, tmp_path):
    p = tmp_path / "contacts.json"
    p.write_text(json.dumps({"Maya Patel": "maya@example.com", "Sam Lee": "sam@example.com", "Sam Wu": "samwu@example.com"}))
    monkeypatch.setenv("SCENE_CONTACTS", str(p))
    return p


def test_find_by_full_name_first_name_and_case():
    assert contacts.find("Maya Patel") == {"name": "Maya Patel", "email": "maya@example.com"}
    assert contacts.find("maya")["email"] == "maya@example.com"
    assert contacts.find("  MAYA  ")["name"] == "Maya Patel"
    assert contacts.find("Sam Wu")["email"] == "samwu@example.com"


def test_ambiguous_or_unknown_or_address_like_names_find_nothing():
    assert contacts.find("sam") is None                      # two Sams: do not guess
    assert contacts.find("priya") is None
    assert contacts.find("evil@attacker.com") is None        # an address is never accepted as a name
    assert contacts.find("") is None and contacts.find(None) is None


def test_add_validates_email_and_persists(book):
    contacts.add("Jo Kim", "jo@example.com")
    assert json.loads(book.read_text())["Jo Kim"] == "jo@example.com"
    assert contacts.find("jo")["email"] == "jo@example.com"
    for bad in ("", "not-an-email", "a@b", "x y@z.com", "a@b.com, c@d.com"):
        with pytest.raises(ValueError):
            contacts.add("Bad", bad)
    with pytest.raises(ValueError):
        contacts.add("", "ok@example.com")


def test_missing_or_corrupt_file_is_an_empty_book(book):
    book.write_text("{not json")
    assert contacts.all() == {} and contacts.find("maya") is None
    book.unlink()
    assert contacts.all() == {}


def test_example_file_in_repo_loads_and_uses_placeholder_addresses(monkeypatch):
    monkeypatch.setenv("SCENE_CONTACTS", str(Path(__file__).parent / "contacts.example.json"))
    book = contacts.all()
    assert book and all(e.endswith("@example.com") for e in book.values())


def test_routes_list_and_add():
    c = TestClient(server.app)
    assert c.get("/contacts").json()["Maya Patel"] == "maya@example.com"
    assert c.post("/contacts", data={"name": "Jo Kim", "email": "jo@example.com"}).status_code == 200
    assert c.post("/contacts", json={"name": "Al", "email": "al@example.com"}).status_code == 200
    assert c.get("/contacts").json()["Al"] == "al@example.com"
    assert c.post("/contacts", data={"name": "Bad", "email": "nope"}).status_code == 422
