"""One check for payments: receipt + "I'll pay you back half" -> proposal -> pinch approve -> paid.
Also the safety rails: caps, test-mode-only keys, double pinch = one Stripe session.

Stripe and models are faked. Run: .venv/bin/python -m pytest -q
"""

import os
import tempfile
from pathlib import Path

os.environ.setdefault("SCENE_DATA_DIR", tempfile.mkdtemp())

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import memory  # noqa: E402
import models  # noqa: E402
import payments  # noqa: E402
import server  # noqa: E402


def test_receipt_to_paid(monkeypatch):
    monkeypatch.setattr(models, "embed", lambda text: [1.0, 0.0])
    monkeypatch.setattr(models, "see", lambda jpeg: {
        "objects": [], "activity": "", "receipt": {"merchant": "Blue Bottle", "total": 24}})
    seen_receipts = []

    def fake_extract(text, receipts=()):
        seen_receipts.extend(receipts)
        return {"tasks": [], "done": [], "owed_to_me": [{"who": "Sam", "amount": 12, "memo": "lunch",
                                                     "quote": "I'll pay you back half"}]}
    monkeypatch.setattr(models, "extract_tasks", fake_extract)

    calls = []

    def fake_stripe(method, path, **kw):
        calls.append((method, path, kw))
        if method == "POST":
            assert kw["headers"]["Idempotency-Key"].startswith("scene-pay-")
            assert kw["data"]["line_items[0][price_data][unit_amount]"] == 1200
            return {"id": "cs_test_1", "url": "https://checkout.stripe.com/c/pay/cs_test_1"}
        return {"payment_status": "paid"}
    monkeypatch.setattr(payments, "_stripe", fake_stripe)

    frame = Path(tempfile.mkdtemp()) / "9.jpg"
    frame.write_bytes(b"\xff\xd8\xff")
    memory.process_frame(frame)
    memory.process_frame(frame)  # same receipt again: logged once
    memory._transcript[:] = ["no worries I'll pay you back half"]
    memory.flush_transcript()
    assert seen_receipts == ["Receipt: Blue Bottle $24.00"]

    c = TestClient(server.app)
    [p] = c.get("/payments").json()
    assert (p["counterparty"], p["amount_cents"], p["status"]) == ("Sam", 1200, "proposed")
    assert calls == []  # agent proposed; nothing reached Stripe yet
    pay_cards = [f for f in c.get("/feed").json() if f["kind"] == "payment"]
    assert pay_cards[-1]["ref"] == p["id"] and pay_cards[-1]["title"] == "Request $12.00 from Sam?"

    a = c.post(f"/payments/{p['id']}/approve").json()
    assert a["status"] == "approved" and a["url"].startswith("https://checkout.stripe.com")
    c.post(f"/payments/{p['id']}/approve")  # double pinch
    assert sum(1 for m, *_ in calls if m == "POST") == 1

    assert c.get(f"/payments/{p['id']}").json()["status"] == "paid"
    assert c.get("/payments").json() == []


def test_safety_rails(monkeypatch):
    monkeypatch.setattr(models, "embed", lambda text: [1.0, 0.0])
    with pytest.raises(payments.PaymentError):
        payments.propose("Sam", payments.MAX_CENTS + 1, "too much")
    with pytest.raises(payments.PaymentError):
        payments.propose("Sam", 10, "below Stripe minimum")

    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_live_nope")
    with pytest.raises(payments.PaymentError, match="test-mode"):
        payments._stripe("GET", "/balance")

    monkeypatch.setattr(payments, "DAILY_CENTS", 1000)
    monkeypatch.setattr(payments, "_stripe", lambda *a, **k: pytest.fail("cap must stop this before Stripe"))
    assert payments.get(payments.propose("", 100, "coffee"))["counterparty"] == "someone"
    pid = payments.propose("Ana", 2000, "concert")
    r = TestClient(server.app).post(f"/payments/{pid}/approve")
    assert r.status_code == 409 and "daily cap" in r.json()["detail"]
