"""Orders: cart -> Stripe Checkout (test mode) -> paid -> kitchen stages. Stripe is faked."""
import os
import tempfile

os.environ.setdefault("SCENE_DATA_DIR", tempfile.mkdtemp())

import pytest  # noqa: E402

import memory  # noqa: E402
import orders  # noqa: E402
import payments  # noqa: E402

PLACE = {"name": "McDonald's", "address": "441 Sutter St, San Francisco, CA"}
LINES = [{"name": "Big Mac", "price_cents": 899, "qty": 1}, {"name": "Fries (Small)", "price_cents": 419, "qty": 2}]


@pytest.fixture(autouse=True)
def fake(monkeypatch):
    monkeypatch.setattr(memory, "add", lambda *a, **k: 1)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://scene.example")
    monkeypatch.setattr(orders, "_now", lambda: 1000.0)
    orders._init()
    with memory._db_lock:
        memory.db().execute("DELETE FROM orders")
        memory.db().execute("DELETE FROM payments")
        memory.db().commit()
    calls = []

    def stripe(method, path, **kw):
        calls.append((method, path, kw))
        if method == "GET":
            return {"payment_status": stripe.status}
        return {"id": "cs_test_1", "url": "https://checkout.stripe.test/cs_test_1"}
    stripe.status = "unpaid"
    monkeypatch.setattr(payments, "_stripe", stripe)
    return calls, stripe


def test_propose_total_from_lines():
    o = orders.propose(PLACE, LINES)
    assert o["total_cents"] == 899 + 2 * 419 and o["status"] == "proposed" and o["items"][1]["qty"] == 2
    assert o["place_name"] == "McDonald's" and o["address"].startswith("441 Sutter")


@pytest.mark.parametrize("lines", [[], [{"name": "x", "price_cents": 10, "qty": 1}],
                                   [{"name": "Feast", "price_cents": 6000, "qty": 1}]])
def test_propose_rejects_empty_under_minimum_and_over_cap(lines):
    with pytest.raises(payments.PaymentError):
        orders.propose(PLACE, lines)


def test_approve_sends_one_line_per_item_in_cents_with_idempotency(fake):
    calls, _ = fake
    o = orders.propose(PLACE, LINES)
    a = orders.approve(o["id"])
    _, path, kw = calls[0]
    assert path == "/checkout/sessions" and kw["headers"]["Idempotency-Key"] == f"scene-order-{o['id']}"
    d = kw["data"]
    assert d["line_items[0][price_data][unit_amount]"] == 899 and d["line_items[1][quantity]"] == 2
    assert d["line_items[1][price_data][unit_amount]"] == 419 and d["success_url"] == f"https://scene.example/order/{o['id']}"
    assert a["status"] == "approved" and a["url"].startswith("https://checkout.stripe.test")


def test_double_approve_makes_one_stripe_session(fake):
    calls, _ = fake
    o = orders.propose(PLACE, LINES)
    orders.approve(o["id"])
    orders.approve(o["id"])
    assert len([c for c in calls if c[0] == "POST"]) == 1


def test_approve_blocked_when_rejected_or_unknown():
    o = orders.propose(PLACE, LINES)
    orders.reject(o["id"])
    with pytest.raises(payments.PaymentError):
        orders.approve(o["id"])
    with pytest.raises(payments.PaymentError):
        orders.approve(999)


def test_daily_cap(monkeypatch):
    monkeypatch.setattr(payments, "DAILY_CENTS", 2000)
    o1 = orders.propose(PLACE, LINES)          # $17.37
    orders.approve(o1["id"])
    o2 = orders.propose(PLACE, LINES)
    with pytest.raises(payments.PaymentError):
        orders.approve(o2["id"])


def test_refresh_marks_paid_and_sets_paid_at(fake):
    _, stripe = fake
    o = orders.approve(orders.propose(PLACE, LINES)["id"])
    assert orders.refresh(o["id"])["status"] == "approved"
    stripe.status = "paid"
    p = orders.refresh(o["id"])
    assert p["status"] == "paid" and p["paid_at"] == 1000.0


def test_kitchen_stage_by_time(fake, monkeypatch):
    _, stripe = fake
    stripe.status = "paid"
    o = orders.approve(orders.propose(PLACE, LINES)["id"])
    assert orders.status_view(o["id"])["stage"] == "awaiting_payment"
    orders.refresh(o["id"])
    assert [orders.status_view(o["id"], now=1000 + s)["stage"] for s in (0, 5, 20, 39, 60)] == [
        "received", "received", "preparing", "preparing", "ready"]
    r = orders.propose(PLACE, LINES)
    orders.reject(r["id"])
    assert orders.status_view(r["id"])["stage"] == "rejected"
