"""Reservation drafts: routing, the propose_reservation tool, and the "7, no actually 8" correction.

A reservation is a DRAFT only: there is no restaurant booking API, so confirming it must
never claim a table was booked. Run: .venv/bin/python -m pytest -q test_reservation.py
"""
import os
import tempfile
from datetime import datetime, timedelta

os.environ.setdefault("SCENE_DATA_DIR", tempfile.mkdtemp())

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import agent as agent_mod  # noqa: E402
import memory  # noqa: E402
import outbox  # noqa: E402
import server  # noqa: E402
import voice  # noqa: E402


@pytest.fixture(autouse=True)
def _isolate():
    """Drafts and cards are shared module state: clear them so this file cannot pollute other tests."""
    outbox._drafts.clear()
    memory.clear_cards()
    yield
    outbox._drafts.clear()
    memory.clear_cards()


def _future(hour: int) -> str:
    """Today at `hour` (24h), or tomorrow if that already passed."""
    now = datetime.now()
    t = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if t <= now:
        t += timedelta(days=1)
    return t.strftime("%Y-%m-%dT%H:%M")


def call(tool, **args):
    return {"text": None, "call": {"name": tool, "args": args}, "parts": [{"functionCall": {"name": tool, "args": args}}]}


def text(t):
    return {"text": t, "call": None, "parts": [{"text": t}]}


class Rig:
    def __init__(self, script):
        self.script, self.said, self.pushed, self.steps = list(script), [], [], []
        self.agent = agent_mod.Agent(step=self.step, say=self.said.append,
                                     push=lambda *a: self.pushed.append(a),
                                     where=lambda: (37.78, -122.41), sync=True,
                                     draft_reservation=outbox.draft_reservation)

    def step(self, system, history, tools):
        self.system = system
        self.steps.append(history[-1])
        return self.script.pop(0)


def test_dining_regex_routes_reservation_requests():
    for s in ["book a reservation for me at 7", "book a table at Chez Panisse",
              "can you reserve a table for 2 tonight", "find a restaurant near me"]:
        assert memory.DINING.search(s), s
    assert not memory.DINING.search("where are my keys")


def test_propose_reservation_drafts_a_card_and_books_nothing():
    r = Rig([call("propose_reservation", restaurant="Chez Panisse", start=_future(19), party_size=2)])
    r.agent.start("book a reservation at Chez Panisse at 7")
    assert r.agent.state()["status"] == "done"
    drafts = [d for d in outbox.pending() if d["kind"] == "reservation"]
    assert len(drafts) == 1
    d = drafts[0]
    assert (d["restaurant"], d["start"], d["party_size"]) == ("Chez Panisse", _future(19), 2)
    kind, title, body, ref = r.pushed[-1]
    assert kind == "reservation" and ref == d["id"] and "no table booked" in body.lower()
    assert "haven't booked" in " ".join(r.said).lower() and "Chez Panisse" in title


def test_propose_reservation_refuses_a_missing_restaurant():
    r = Rig([call("propose_reservation", restaurant="", start=_future(19)),
             call("ask_user", question="Which restaurant?")])
    r.agent.start("book a reservation at 7")
    assert outbox.pending() == [] and "restaurant" in str(r.steps[-1]).lower()
    assert r.agent.state()["status"] == "waiting"


def test_correction_7_to_8_updates_the_same_draft(monkeypatch):
    spoken = []
    monkeypatch.setattr(voice, "speak", spoken.append)
    d = outbox.draft_reservation("Chez Panisse", _future(19), 2, "book a reservation at 7")
    assert memory.dining_turn("no actually 8") is True
    drafts = [x for x in outbox.pending() if x["kind"] == "reservation"]
    assert len(drafts) == 1 and drafts[0]["id"] == d["id"]
    assert drafts[0]["start"] == _future(20) and drafts[0]["changed_from"] == _future(19)
    assert "changed it to" in " ".join(spoken).lower()
    # Correcting to the time it already has is a no-op, not a second draft.
    assert memory.dining_turn("no actually 8") is True
    assert len([x for x in outbox.pending() if x["kind"] == "reservation"]) == 1


def test_correction_without_a_draft_does_nothing():
    assert memory.dining_turn("no actually 8") is False
    assert memory.dining_turn("actually 8") is False


def test_confirm_and_inbox_for_a_reservation_draft():
    d = outbox.draft_reservation("Chez Panisse", _future(19), 2, "book a reservation at 7")
    c = TestClient(server.app)
    items = [i for i in c.get("/inbox").json() if i["type"] == "reservation"]
    assert len(items) == 1 and items[0]["id"] == d["id"]
    assert [a["label"] for a in items[0]["actions"]] == ["Confirm", "Not now"]
    assert items[0]["actions"][0]["path"] == f"/reservations/{d['id']}/confirm"
    r = c.post(f"/reservations/{d['id']}/confirm")
    assert r.status_code == 200 and r.json()["status"] == "drafted"
    assert "No table was booked" in r.json()["result"]
    assert c.post(f"/reservations/{d['id']}/confirm").status_code == 409  # already confirmed
    assert c.post("/reservations/999999/confirm").status_code == 404
