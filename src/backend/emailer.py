"""Email drafts the wearer approves with a tap. This is the only place Scene sends anything.

Same executor as Follow-Through (the Apps Script web app, EXECUTOR_URL). Demo safety: the recipient is
ALWAYS DEMO_EMAIL_TO, never the person named in the notes. Honesty rule: "sent" only when the executor
confirms; with no executor we make a Gmail draft link and say nothing was sent.
"""

import itertools
import json
import os
from urllib.parse import urlencode

import httpx

_ids = itertools.count(1)
_drafts: dict[int, dict] = {}


def draft(to_name: str, subject: str, body: str, quote: str) -> dict:
    d = {"id": next(_ids), "to_name": to_name, "to": os.environ.get("DEMO_EMAIL_TO", ""), "subject": subject,
         "body": body, "quote": quote, "status": "proposed", "result": "", "link": ""}
    _drafts[d["id"]] = d
    return d


def get(draft_id: int) -> dict:
    return _drafts[draft_id]          # KeyError when unknown


def _proposed(draft_id: int) -> dict:
    d = get(draft_id)
    if d["status"] != "proposed":
        raise ValueError(f"draft {draft_id} is already {d['status']}")
    return d


def dismiss(draft_id: int) -> dict:
    d = _proposed(draft_id) if draft_id in _drafts else get(draft_id)
    d["status"] = "dismissed"
    return d


def send(draft_id: int, client: httpx.Client | None = None) -> dict:
    """Send an approved draft. KeyError if unknown, ValueError if already sent or dismissed. Never raises on send trouble."""
    d = _proposed(draft_id)
    url, token = os.environ.get("EXECUTOR_URL", ""), os.environ.get("EXECUTOR_TOKEN", "")
    if not url:
        q = urlencode({"view": "cm", "fs": "1", "to": d["to"], "su": d["subject"], "body": d["body"],
                       "authuser": os.environ.get("CALENDAR_ACCOUNT", "")})
        d.update(status="drafted", link="https://mail.google.com/mail/?" + q,
                 result="No executor set up. Draft link only. Nothing was sent.")
        return d
    payload = {"token": token, "action": "email", "title": d["subject"], "to": d["to"], "subject": d["subject"],
               "body": d["body"], "details": f'Heard: "{d["quote"]}"\nAdded by Scene'}
    own = client is None
    client = client or httpx.Client(timeout=30, follow_redirects=True)
    try:
        data = client.post(url, content=json.dumps(payload),
                           headers={"Content-Type": "text/plain;charset=utf-8"}).json()
        if data.get("ok"):
            d.update(status="sent", result=data.get("did", "Sent"))
        else:
            d.update(status="failed", result=data.get("error", "executor said no"))
    except Exception as e:  # network down, bad JSON, ...
        d.update(status="failed", result=f"{type(e).__name__}: {e}")
    finally:
        if own:
            client.close()
    return d
