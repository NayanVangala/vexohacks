"""Everything waiting on the wearer, in one list, with ready-made actions. The phone app renders this as-is:
each action is an HTTP call it can make with one tap, so new kinds of approval need no new phone code.
"""

import os
import time

import memory
import orders
import outbox
import payments


def _act(label: str, path: str, json: dict | None = None, primary: bool = False) -> dict:
    a = {"label": label, "method": "POST", "path": path, "primary": primary}
    return {**a, "json": json} if json is not None else a


def _usd(c: int) -> str:
    return f"${c / 100:.2f}"


def items() -> list[dict]:
    """Newest first. Each item: {type, id, title, body, ts, actions[], link?}."""
    now = time.time()
    base = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    out = []
    offer = memory.awareness.pending(now)
    if offer:
        out.append({"type": "offer", "id": offer.id, "title": "Take notes?", "body": offer.text, "ts": offer.created,
                    "actions": [_act("Yes, take notes", f"/offers/{offer.id}/accept", primary=True),
                                _act("Not now", f"/offers/{offer.id}/decline")]})
    st = memory.agent.state()
    if st["status"] == "waiting":
        out.append({"type": "question", "id": st["session"], "title": "Scene asks", "body": st["question"], "ts": now,
                    "actions": [_act("Yes", "/agent/reply", {"text": "yes"}, primary=True), _act("No", "/agent/reply", {"text": "no"})]})
    for d in outbox.pending():
        if d["kind"] == "email":
            title, body = outbox.card_text(d)
            out.append({"type": "email", "id": d["id"], "title": title, "ts": d["ts"], "body": body,
                        "actions": [_act("Send", f"/emails/{d['id']}/send", primary=True), _act("Not now", f"/emails/{d['id']}/dismiss")]})
        else:
            out.append({"type": "event", "id": d["id"], "title": "Add to calendar?", "ts": d["ts"],
                        "body": f"{d['title']} · {d['start'].replace('T', ' ')} ({d['duration_min']} min)",
                        "actions": [_act("Add it", f"/events/{d['id']}/add", primary=True), _act("Not now", f"/events/{d['id']}/dismiss")]})
    for o in orders.recent(10):
        summary = ", ".join(f"{i['qty']}x {i['name']}" for i in o["items"]) + f" · {_usd(o['total_cents'])}"
        if o["status"] == "proposed":
            out.append({"type": "order", "id": o["id"], "title": f"Order from {o['place_name']}?", "body": summary, "ts": o["ts"],
                        "actions": [_act("Pay", f"/orders/{o['id']}/approve", primary=True), _act("Not now", f"/orders/{o['id']}/reject")]})
        elif o["status"] == "approved":
            out.append({"type": "order", "id": o["id"], "title": f"Pay for your {o['place_name']} order", "body": summary, "ts": o["ts"],
                        "actions": [], "link": f"{base}/order/{o['id']}"})
    for p in payments.pending():
        if p["status"] == "proposed":
            out.append({"type": "payment", "id": p["id"], "title": f"Request {_usd(p['amount_cents'])} from {p['counterparty']}?",
                        "body": p["memo"], "ts": p["ts"],
                        "actions": [_act("Approve", f"/payments/{p['id']}/approve", primary=True), _act("Not now", f"/payments/{p['id']}/reject")]})
    return sorted(out, key=lambda i: -i["ts"])
