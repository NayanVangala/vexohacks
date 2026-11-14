"""Offer, notes and dev routes. memory is stubbed: these tests are about the HTTP contract."""
import os
import tempfile

os.environ.setdefault("SCENE_DATA_DIR", tempfile.mkdtemp())

from fastapi.testclient import TestClient  # noqa: E402

import memory  # noqa: E402
import notes  # noqa: E402
import server  # noqa: E402

c = TestClient(server.app)


def test_offer_accept_decline_and_409(monkeypatch):
    def answer(oid, accept):
        if oid != 7:
            raise KeyError(oid)
        return {"offer": oid, "accepted": accept}
    monkeypatch.setattr(memory, "answer_offer", answer)
    assert c.post("/offers/7/accept").json() == {"offer": 7, "accepted": True}
    assert c.post("/offers/7/decline").json() == {"offer": 7, "accepted": False}
    assert c.post("/offers/8/accept").status_code == 409
    assert c.post("/offers/8/decline").status_code == 409


def test_notes_stop_409_without_session_and_ok_with(monkeypatch):
    monkeypatch.setattr(memory, "finish_notes", lambda: None)
    assert c.post("/notes/stop").status_code == 409
    monkeypatch.setattr(memory, "finish_notes", lambda: 42)
    assert c.post("/notes/stop").json() == {"notes": 42}


def test_notes_get_404_and_200(monkeypatch):
    monkeypatch.setattr(memory, "get_notes", lambda i: {"summary": "x"} if i == 1 else None)
    assert c.get("/notes/1").json() == {"summary": "x"}
    assert c.get("/notes/2").status_code == 404


def test_dev_routes_404_without_flag(monkeypatch):
    monkeypatch.delenv("SCENE_DEV", raising=False)
    assert c.post("/dev/scene", json={"type": "meeting", "confidence": 0.9, "text": ""}).status_code == 404
    assert c.post("/dev/line", json={"text": "hi"}).status_code == 404


def test_dev_scene_and_line_when_flag_set(monkeypatch):
    monkeypatch.setenv("SCENE_DEV", "1")
    seen = []
    monkeypatch.setattr(memory, "handle_scene", lambda scene, frame: seen.append((scene, frame)))
    r = c.post("/dev/scene", json={"type": "meeting", "confidence": 0.9, "text": ""})
    assert r.status_code == 200 and seen == [({"type": "meeting", "confidence": 0.9, "text": ""}, None)]
    assert c.post("/dev/scene", json={"type": "meeting"}).status_code == 422     # confidence is required
    notes.start(0)
    assert c.post("/dev/line", json={"text": "we ship friday"}).status_code == 200
    assert notes.stop().lines == ["we ship friday"]


def test_email_send_and_dismiss_routes(monkeypatch):
    import emailer
    emailer._drafts.clear()
    d = emailer.draft("Priya", "Budget", "Hi.", "send the budget to Priya")
    monkeypatch.setattr(emailer, "send", lambda i: {**emailer._drafts[i], "status": "sent", "result": "Emailed"})
    assert c.post(f"/emails/{d['id']}/send").json()["status"] == "sent"
    assert c.post("/emails/999/send").status_code == 404
    assert c.post(f"/emails/{d['id']}/dismiss").json()["status"] == "dismissed"
    assert c.post(f"/emails/{d['id']}/dismiss").status_code == 409
    assert c.post("/emails/999/dismiss").status_code == 404
