"""Money the agent noticed -> Stripe, with a human pinch between the agent and the money.

The agent (OpenAI, via memory.flush_transcript) can only PROPOSE a payment request.
Nothing touches Stripe until approve(), which only the glasses' Approve button calls.
Safety lives here in code, not in a prompt:
  - per-request cap and daily cap
  - test-mode keys only (sk_live_ refused)
  - one Checkout Session per proposal (Stripe idempotency key), so a double pinch can't double-charge
  - every step written to memory, so "what money did I request today?" is answerable
"""

import os
import time

import httpx

import memory

MAX_CENTS = int(os.environ.get("PAY_MAX_CENTS", "5000"))      # $50 per request
DAILY_CENTS = int(os.environ.get("PAY_DAILY_CENTS", "10000"))  # $100 per day
SUCCESS_URL = os.environ.get("PAY_SUCCESS_URL", "https://example.com/paid")


class PaymentError(Exception):
    pass


def _init() -> None:
    with memory._db_lock:
        memory.db().execute("""CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY,
            counterparty TEXT NOT NULL,
            amount_cents INTEGER NOT NULL,
            memo TEXT NOT NULL,
            status TEXT NOT NULL,     -- proposed | approved | paid | rejected
            session_id TEXT,
            url TEXT,
            ts REAL NOT NULL
        )""")
        memory.db().commit()


def _stripe(method: str, path: str, **kw) -> dict:
    key = os.environ.get("STRIPE_SECRET_KEY", "")
    if not key.startswith(("sk_test_", "rk_test_")):
        raise PaymentError("refusing: STRIPE_SECRET_KEY must be a test-mode key")
    r = httpx.request(method, f"https://api.stripe.com/v1{path}", auth=(key, ""), timeout=20, **kw)
    if r.status_code >= 400:
        raise PaymentError(f"stripe {r.status_code}: {r.json().get('error', {}).get('message', r.text)}")
    return r.json()


def _usd(cents: int) -> str:
    return f"${cents / 100:.2f}"


def get(pid: int) -> dict | None:
    _init()
    r = memory.rows("SELECT * FROM payments WHERE id=?", pid)
    return r[0] if r else None


def pending(limit: int = 6) -> list[dict]:
    _init()
    return memory.rows("SELECT * FROM payments WHERE status IN ('proposed','approved') ORDER BY ts DESC LIMIT ?", limit)


def propose(counterparty: str, amount_cents: int, memo: str) -> int:
    _init()
    counterparty = (counterparty or "").strip() or "someone"  # name often never said aloud
    if not 50 <= amount_cents <= MAX_CENTS:  # Stripe's minimum charge is $0.50
        raise PaymentError(f"amount {_usd(amount_cents)} outside $0.50–{_usd(MAX_CENTS)}")
    with memory._db_lock:
        pid = memory.db().execute(
            "INSERT INTO payments (counterparty, amount_cents, memo, status, ts) VALUES (?,?,?,?,?)",
            (counterparty, amount_cents, memo, "proposed", time.time())).lastrowid
        memory.db().commit()
    memory.add("event", f"Proposed: request {_usd(amount_cents)} from {counterparty} for {memo}")
    print(f"PAY proposed #{pid}: {_usd(amount_cents)} from {counterparty} ({memo})", flush=True)
    return pid


def approve(pid: int) -> dict:
    """Only the human pinch calls this."""
    p = get(pid)
    if p is None:
        raise PaymentError("no such payment")
    if p["status"] == "approved":  # double pinch: same link back, no second session
        return p
    if p["status"] != "proposed":
        raise PaymentError(f"payment is {p['status']}")
    day_start = time.time() - 86400
    spent = memory.rows("SELECT COALESCE(SUM(amount_cents),0) AS s FROM payments "
                        "WHERE status IN ('approved','paid') AND ts > ?", day_start)[0]["s"]
    if spent + p["amount_cents"] > DAILY_CENTS:
        raise PaymentError(f"daily cap {_usd(DAILY_CENTS)} reached ({_usd(spent)} already)")

    s = _stripe("POST", "/checkout/sessions", headers={"Idempotency-Key": f"scene-pay-{pid}"}, data={
        "mode": "payment",
        "success_url": SUCCESS_URL,
        "line_items[0][quantity]": 1,
        "line_items[0][price_data][currency]": "usd",
        "line_items[0][price_data][unit_amount]": p["amount_cents"],
        "line_items[0][price_data][product_data][name]": f"{p['memo']} (to settle with {p['counterparty']})",
        "metadata[scene_payment_id]": pid,
    })
    with memory._db_lock:
        memory.db().execute("UPDATE payments SET status='approved', session_id=?, url=? WHERE id=?",
                            (s["id"], s["url"], pid))
        memory.db().commit()
    memory.add("event", f"Approved: requested {_usd(p['amount_cents'])} from {p['counterparty']} for {p['memo']}")
    print(f"PAY approved #{pid} -> {s['url']}", flush=True)
    return get(pid)


def reject(pid: int) -> None:
    p = get(pid)
    if p is None or p["status"] != "proposed":
        raise PaymentError("only a proposed payment can be rejected")
    with memory._db_lock:
        memory.db().execute("UPDATE payments SET status='rejected' WHERE id=?", (pid,))
        memory.db().commit()
    memory.add("event", f"Rejected: request {_usd(p['amount_cents'])} from {p['counterparty']}")


def refresh(pid: int) -> dict:
    """Poll Stripe instead of webhooks: no public URL or signing secret needed for the demo."""
    p = get(pid)
    if p is None:
        raise PaymentError("no such payment")
    if p["status"] == "approved":
        s = _stripe("GET", f"/checkout/sessions/{p['session_id']}")
        if s.get("payment_status") == "paid":
            with memory._db_lock:
                memory.db().execute("UPDATE payments SET status='paid' WHERE id=?", (pid,))
                memory.db().commit()
            memory.add("event", f"Paid: {p['counterparty']} paid {_usd(p['amount_cents'])} for {p['memo']}")
            print(f"PAY paid #{pid}", flush=True)
            p = get(pid)
    return p
