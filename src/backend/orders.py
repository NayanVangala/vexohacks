"""Food orders: a cart the agent proposed -> a human pinch -> a Stripe Checkout Session (test mode) -> paid ->
a simulated kitchen. Safety is the same as payments.py: test keys only, per-order and daily caps, one Checkout
Session per order (idempotency key), approval only from the glasses' button.

Honesty: the payment is real Stripe test mode; the kitchen is a demo (stages are computed from time since paid).
"""

import json
import os
import time

import memory
import payments

PREP_S = float(os.environ.get("KITCHEN_PREP_S", "10"))     # received for this long, then preparing
READY_S = float(os.environ.get("KITCHEN_READY_S", "40"))   # preparing until this long after payment, then ready


def _now() -> float:
    return time.time()


def _usd(cents: int) -> str:
    return f"${cents / 100:.2f}"


def _init() -> None:
    payments._init()
    with memory._db_lock:
        memory.db().execute("""CREATE TABLE IF NOT EXISTS orders (
            id INTEGER PRIMARY KEY,
            place_name TEXT NOT NULL,
            address TEXT NOT NULL,
            items TEXT NOT NULL,         -- JSON [{name, price_cents, qty}]
            total_cents INTEGER NOT NULL,
            status TEXT NOT NULL,        -- proposed | approved | paid | rejected
            session_id TEXT,
            url TEXT,
            paid_at REAL,
            ts REAL NOT NULL
        )""")
        memory.db().commit()


def _row(r: dict | None) -> dict | None:
    return None if r is None else {**r, "items": json.loads(r["items"])}


def get(order_id: int) -> dict | None:
    _init()
    r = memory.rows("SELECT * FROM orders WHERE id=?", order_id)
    return _row(r[0] if r else None)


def propose(place: dict, lines: list[dict]) -> dict:
    """lines: [{name, price_cents, qty}]. The total is computed here, from the lines only."""
    _init()
    items = [{"name": str(l["name"]), "price_cents": int(l["price_cents"]), "qty": int(l["qty"])} for l in lines]
    total = sum(i["price_cents"] * i["qty"] for i in items)
    if not items or any(i["qty"] < 1 or i["price_cents"] < 1 for i in items):
        raise payments.PaymentError("the order is empty or has a bad line")
    if not 50 <= total <= payments.MAX_CENTS:
        raise payments.PaymentError(f"order total {_usd(total)} outside $0.50 to {_usd(payments.MAX_CENTS)}")
    with memory._db_lock:
        oid = memory.db().execute(
            "INSERT INTO orders (place_name, address, items, total_cents, status, ts) VALUES (?,?,?,?,?,?)",
            (place["name"], place["address"], json.dumps(items), total, "proposed", _now())).lastrowid
        memory.db().commit()
    memory.add("event", f"Proposed order {_usd(total)} at {place['name']}, {place['address']}")
    print(f"ORDER proposed #{oid}: {_usd(total)} at {place['name']}", flush=True)
    return get(oid)


def approve(order_id: int) -> dict:
    """Only the human pinch calls this. A second call returns the same order without a second session."""
    o = get(order_id)
    if o is None:
        raise payments.PaymentError("no such order")
    if o["status"] in ("approved", "paid"):
        return o
    if o["status"] != "proposed":
        raise payments.PaymentError(f"order is {o['status']}")
    spent = memory.rows("SELECT COALESCE(SUM(total_cents),0) AS s FROM orders WHERE status IN ('approved','paid') AND ts > ?",
                        _now() - 86400)[0]["s"]
    spent += memory.rows("SELECT COALESCE(SUM(amount_cents),0) AS s FROM payments WHERE status IN ('approved','paid') AND ts > ?",
                         _now() - 86400)[0]["s"]
    if spent + o["total_cents"] > payments.DAILY_CENTS:
        raise payments.PaymentError(f"daily cap {_usd(payments.DAILY_CENTS)} reached ({_usd(spent)} already)")
    data = {"mode": "payment",
            "success_url": f"{os.environ.get('PUBLIC_BASE_URL', '').rstrip('/')}/order/{order_id}",
            "metadata[scene_order_id]": order_id}
    for n, it in enumerate(o["items"]):
        data[f"line_items[{n}][quantity]"] = it["qty"]
        data[f"line_items[{n}][price_data][currency]"] = "usd"
        data[f"line_items[{n}][price_data][unit_amount]"] = it["price_cents"]
        data[f"line_items[{n}][price_data][product_data][name]"] = it["name"]
    s = payments._stripe("POST", "/checkout/sessions", headers={"Idempotency-Key": f"scene-order-{order_id}-{int(o['ts'])}"}, data=data)
    with memory._db_lock:
        memory.db().execute("UPDATE orders SET status='approved', session_id=?, url=? WHERE id=?",
                            (s["id"], s["url"], order_id))
        memory.db().commit()
    memory.add("event", f"Approved order {_usd(o['total_cents'])} at {o['place_name']}")
    print(f"ORDER approved #{order_id} -> {s['url']}", flush=True)
    return get(order_id)


def reject(order_id: int) -> None:
    o = get(order_id)
    if o is None or o["status"] != "proposed":
        raise payments.PaymentError("only a proposed order can be rejected")
    with memory._db_lock:
        memory.db().execute("UPDATE orders SET status='rejected' WHERE id=?", (order_id,))
        memory.db().commit()


def refresh(order_id: int) -> dict:
    """Poll Stripe (no webhooks, no public URL needed for the demo)."""
    o = get(order_id)
    if o is None:
        raise payments.PaymentError("no such order")
    if o["status"] == "approved":
        s = payments._stripe("GET", f"/checkout/sessions/{o['session_id']}")
        if s.get("payment_status") == "paid":
            with memory._db_lock:
                memory.db().execute("UPDATE orders SET status='paid', paid_at=? WHERE id=?", (_now(), order_id))
                memory.db().commit()
            memory.add("event", f"Paid order {_usd(o['total_cents'])} at {o['place_name']}")
            print(f"ORDER paid #{order_id}", flush=True)
            o = get(order_id)
    return o


def status_view(order_id: int, now: float | None = None) -> dict:
    """The order plus where the demo kitchen is: awaiting_approval | awaiting_payment | received | preparing | ready | rejected."""
    o = get(order_id)
    if o is None:
        raise payments.PaymentError("no such order")
    now = _now() if now is None else now
    if o["status"] == "paid":
        t = now - o["paid_at"]
        stage = "received" if t < PREP_S else "preparing" if t < READY_S else "ready"
    else:
        stage = {"proposed": "awaiting_approval", "approved": "awaiting_payment", "rejected": "rejected"}[o["status"]]
    return {**o, "stage": stage}
