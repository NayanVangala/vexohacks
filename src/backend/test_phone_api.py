"""What the phone app needs: cards that survive a restart, notes and activity lists, one "needs you" inbox, orders list."""
import json
import os
import tempfile
import time

os.environ.setdefault("SCENE_DATA_DIR", tempfile.mkdtemp())

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import awareness  # noqa: E402
import memory  # noqa: E402
import models  # noqa: E402
import orders  # noqa: E402
import outbox  # noqa: E402
import payments  # noqa: E402
import server  # noqa: E402

c = TestClient(server.app)


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.setattr(models, "embed", lambda t: [0.0] * 4)
    memory.clear_cards()
    outbox._drafts.clear()
    orders._init()
    with memory._db_lock:
        for t in ("memories", "orders", "payments"):
            memory.db().execute(f"DELETE FROM {t}")
        memory.db().commit()
    monkeypatch.setattr(memory, "awareness", awareness.Awareness())
    yield


def test_cards_survive_a_restart_and_ids_never_go_backwards():
    a = memory.push_card("task", "New task", "send deck", ref=3)
    memory._conn = None                                    # a restart: the process forgets its connection
    assert [x["id"] for x in memory.cards_after(0)] == [a["id"]]
    b = memory.push_card("notes", "Meeting notes", "x", ref=9)
    assert b["id"] > a["id"]                               # a phone holding lastCardId=a keeps seeing new cards
    assert [x["id"] for x in memory.cards_after(a["id"])] == [b["id"]]


def test_cards_after_returns_at_most_the_newest_50_in_order():
    ids = [memory.push_card("answer", f"q{i}", "a")["id"] for i in range(60)]
    got = [x["id"] for x in memory.cards_after(0)]
    assert got == ids[-50:]


def test_outbox_drafts_survive_a_restart_and_pending_lists_only_proposed():
    d = outbox.draft("Priya", "Budget", "Hi.", "send the budget to Priya")
    e = outbox.draft_event("Demo sync", "2026-11-20T17:00", 30, "q")
    outbox.dismiss(e["id"])
    outbox._drafts.clear()
    outbox._load()
    assert outbox.get(d["id"])["subject"] == "Budget" and outbox.get(e["id"])["status"] == "dismissed"
    assert [x["id"] for x in outbox.pending()] == [d["id"]]
    assert outbox.draft("x", "y", "z", "q")["id"] > e["id"]


def test_notes_list_newest_first_with_counts():
    (memory.DATA_DIR / "notes").mkdir(parents=True, exist_ok=True)
    for summary, items, slides in (("First sync.", 1, 0), ("Second sync.", 2, 3)):
        nid = memory.add("notes", summary)
        (memory.DATA_DIR / "notes" / f"{nid}.json").write_text(json.dumps({
            "summary": summary, "decisions": ["d"], "action_items": [{}] * items, "slides": [{}] * slides}))
        time.sleep(0.01)
    got = c.get("/notes").json()
    assert [n["summary"] for n in got] == ["Second sync.", "First sync."]
    assert (got[0]["action_items"], got[0]["slides"], got[0]["decisions"]) == (2, 3, 1)


def test_activity_is_todays_events_tasks_and_notes_newest_first():
    memory.add("event", "Viewing a presentation slide.")
    memory.add("task", "Send the deck to Nayan", status="open")
    memory.add("object", "keys", place="shelf")           # objects are not activity
    memory.add("notes", "Sync.")
    got = c.get("/activity").json()
    assert [a["kind"] for a in got] == ["notes", "task", "event"] and got[1]["status"] == "open"
    assert c.get("/activity?limit=1").json()[0]["kind"] == "notes"


def test_inbox_lists_everything_waiting_on_the_wearer_with_ready_made_actions(monkeypatch):
    for _ in range(3):
        memory.handle_scene({"type": "meeting", "confidence": 0.9, "text": ""}, "f.jpg")
    em = outbox.draft("Priya", "Budget", "Hi Priya.", "send the budget")
    ev = outbox.draft_event("Demo sync", "2026-11-20T17:00", 45, "q")
    monkeypatch.setattr(payments, "_stripe", lambda *a, **k: {"id": "cs", "url": "https://stripe.test/cs"})
    o = orders.propose({"name": "McDonald's", "address": "441 Sutter"}, [{"name": "Big Mac", "price_cents": 899, "qty": 1}])
    pid = payments.propose("Sam", 1200, "lunch")
    monkeypatch.setattr(memory.agent, "state", lambda: {"status": "waiting", "question": "Order from 441 Sutter?", "session": 1})
    got = {i["type"]: i for i in c.get("/inbox").json()}
    assert set(got) == {"offer", "question", "email", "event", "order", "payment"}
    paths = lambda t: [(a["method"], a["path"]) for a in got[t]["actions"]]  # noqa: E731
    assert paths("offer")[0][1].endswith("/accept") and paths("offer")[1][1].endswith("/decline")
    assert got["question"]["actions"][0]["json"] == {"text": "yes"} and got["question"]["actions"][1]["json"] == {"text": "no"}
    assert paths("email") == [("POST", f"/emails/{em['id']}/send"), ("POST", f"/emails/{em['id']}/dismiss")]
    assert paths("event") == [("POST", f"/events/{ev['id']}/add"), ("POST", f"/events/{ev['id']}/dismiss")]
    assert paths("order") == [("POST", f"/orders/{o['id']}/approve"), ("POST", f"/orders/{o['id']}/reject")]
    assert paths("payment") == [("POST", f"/payments/{pid}/approve"), ("POST", f"/payments/{pid}/reject")]
    assert "$8.99" in got["order"]["body"] and got["email"]["title"] == "Email Priya?"


def test_inbox_drops_answered_items_and_shows_an_approved_unpaid_order_as_a_link(monkeypatch):
    em = outbox.draft("Priya", "Budget", "Hi.", "q")
    outbox.dismiss(em["id"])
    monkeypatch.setattr(payments, "_stripe", lambda *a, **k: {"id": "cs", "url": "https://stripe.test/cs"})
    o = orders.propose({"name": "McDonald's", "address": "441 Sutter"}, [{"name": "Big Mac", "price_cents": 899, "qty": 1}])
    orders.approve(o["id"])
    items = c.get("/inbox").json()
    assert [i["type"] for i in items] == ["order"] and items[0]["actions"] == []
    assert items[0]["link"].endswith(f"/order/{o['id']}")


def test_orders_list_newest_first():
    place = {"name": "McDonald's", "address": "441 Sutter"}
    o1 = orders.propose(place, [{"name": "Big Mac", "price_cents": 899, "qty": 1}])
    o2 = orders.propose(place, [{"name": "Fries", "price_cents": 419, "qty": 2}])
    got = c.get("/orders").json()
    assert [o["id"] for o in got] == [o2["id"], o1["id"]] and got[0]["total_cents"] == 838 and got[0]["stage"] == "awaiting_approval"
