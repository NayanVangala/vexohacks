"""Email and calendar drafts the wearer approves with a tap. This is the only place Scene acts outside itself.

Same executor as Follow-Through (the Apps Script web app, EXECUTOR_URL). Demo safety: the recipient is
ALWAYS DEMO_EMAIL_TO, never the person named in the notes. Honesty rule: "sent" only when the executor
confirms; with no executor we make a Gmail draft link and say nothing was sent.
"""

import itertools
import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import httpx

DATA_DIR = Path(os.environ.get("SCENE_DATA_DIR", Path(__file__).parent / "data"))
_ids = itertools.count(1)
_drafts: dict[int, dict] = {}


def _save() -> None:
    """Drafts survive a restart (the phone may still be showing one)."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "drafts.json").write_text(json.dumps(_drafts))


def _load() -> None:
    global _ids
    p = DATA_DIR / "drafts.json"
    _drafts.clear()
    if p.is_file():
        try:
            _drafts.update({int(k): v for k, v in json.loads(p.read_text()).items()})
        except (ValueError, OSError):
            pass
    _ids = itertools.count(max(_drafts, default=0) + 1)


def pending() -> list[dict]:
    """Drafts still waiting for the wearer's yes, newest first."""
    return [d for d in sorted(_drafts.values(), key=lambda d: -d["id"]) if d["status"] == "proposed"]


def draft(to_name: str, subject: str, body: str, quote: str, to: str | None = None) -> dict:
    """`to` is a saved contact's address; without one the email goes to DEMO_EMAIL_TO."""
    d = {"id": next(_ids), "kind": "email", "to_name": to_name, "to": to or os.environ.get("DEMO_EMAIL_TO", ""), "to_is_contact": bool(to), "subject": subject,
         "body": body, "quote": quote, "status": "proposed", "result": "", "link": "", "ts": time.time()}
    _drafts[d["id"]] = d
    _save()
    return d


TIME_FORMAT = "%Y-%m-%dT%H:%M"   # local time in TIMEZONE, same as Follow-Through


def draft_event(title: str, start_local: str, duration_min: int, quote: str) -> dict:
    """ValueError when start_local is not YYYY-MM-DDTHH:MM, so a vague time never becomes an event."""
    datetime.strptime(start_local, TIME_FORMAT)
    d = {"id": next(_ids), "kind": "calendar", "title": title, "start": start_local,
         "duration_min": int(duration_min) or 30, "quote": quote, "status": "proposed", "result": "", "link": "",
         "changed_from": "", "ts": time.time()}
    _drafts[d["id"]] = d
    _save()
    return d


def update_event(draft_id: int, start_local: str) -> dict:
    """Move a pending calendar draft to a new time, keeping the old one in `changed_from`.
    ValueError when the draft is not a proposed calendar event, or the time is not YYYY-MM-DDTHH:MM.
    Mirrors update_reservation: a "no actually 8" correction updates the draft in place."""
    d = _proposed(draft_id)
    if d.get("kind") != "calendar":
        raise ValueError(f"draft {draft_id} is not a calendar event")
    datetime.strptime(start_local, TIME_FORMAT)
    d["changed_from"] = d["start"]
    d["start"] = start_local
    d["ts"] = time.time()
    _save()
    return d


def draft_reservation(restaurant: str, start_local: str, party_size: int | None, quote: str) -> dict:
    """A reservation REQUEST draft, not a booking: there is no restaurant booking API.
    ValueError when the restaurant is missing or start_local is not YYYY-MM-DDTHH:MM, so a
    vague time never becomes a draft. `changed_from` is the previous start when a correction
    moved the time ("7, no actually 8"), else ""."""
    if not (restaurant or "").strip():
        raise ValueError("restaurant is required: ask the wearer which restaurant")
    datetime.strptime(start_local, TIME_FORMAT)
    d = {"id": next(_ids), "kind": "reservation", "restaurant": restaurant.strip(), "start": start_local,
         "party_size": party_size if isinstance(party_size, int) and party_size > 0 else None,
         "quote": quote, "status": "proposed", "result": "", "link": "", "changed_from": "", "ts": time.time()}
    _drafts[d["id"]] = d
    _save()
    return d


def update_reservation(draft_id: int, start_local: str) -> dict:
    """Move a pending reservation draft to a new time, keeping the old one in `changed_from`.
    ValueError when the draft is not a proposed reservation, or the time is not YYYY-MM-DDTHH:MM."""
    d = _proposed(draft_id)
    if d.get("kind") != "reservation":
        raise ValueError(f"draft {draft_id} is not a reservation")
    datetime.strptime(start_local, TIME_FORMAT)
    d["changed_from"] = d["start"]
    d["start"] = start_local
    d["ts"] = time.time()
    _save()
    return d


def _times(d: dict) -> tuple[datetime, datetime]:
    start = datetime.strptime(d["start"], TIME_FORMAT).replace(tzinfo=ZoneInfo(os.environ.get("TIMEZONE", "America/Los_Angeles")))
    return start, start + timedelta(minutes=d["duration_min"])


def draft_slack(target: dict, text: str, quote: str) -> dict:
    """A Slack DM or channel post waiting for the wearer's tap. `target` comes from slack.target(), never from the model."""
    d = {"id": next(_ids), "kind": "slack", "target": target, "text": text, "quote": quote, "status": "proposed",
         "result": "", "link": "", "ts": time.time()}
    _drafts[d["id"]] = d
    _save()
    return d


def card_text(d: dict) -> tuple[str, str]:
    """Title and body for a draft's card: who it really goes to, so the tap is informed."""
    if d.get("kind") == "slack":
        t = d["target"]
        return (f"Post in {t['label']}?" if t["kind"] == "channel" else f"Slack message to {t['label']}?"), d["text"]
    title = f"Email {d['to_name'] or 'them'}?"
    if d.get("to_is_contact"):
        who = f"To: {d['to']} · "
    elif d["to_name"]:
        who = f"No contact saved for {d['to_name']}: goes to your demo inbox · "
    else:
        who = ""
    return title, f"{who}{d['subject']}: {d['body']}"


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
    _save()
    return d


def send(draft_id: int, client: httpx.Client | None = None) -> dict:
    """Send an approved draft. KeyError if unknown, ValueError if already sent or dismissed. Never raises on send trouble."""
    d = _proposed(draft_id)
    if d["kind"] == "reservation":
        # Confirming a reservation draft only confirms the DRAFT. There is no restaurant
        # booking API, so nothing is booked and nothing is sent to the restaurant.
        d.update(status="drafted",
                 result="Draft reservation confirmed. No table was booked: there is no restaurant booking API, so call the restaurant to book.")
        _save()
        return d
    if d["kind"] == "slack":
        import slack
        ok, result = slack.send(d["target"], d["text"], client=client)
        d.update(status="sent" if ok else "failed", result=result)
        _save()
        return d
    url, token = os.environ.get("EXECUTOR_URL", ""), os.environ.get("EXECUTOR_TOKEN", "")
    details = f'Heard: "{d["quote"]}"\nAdded by Scene'
    account = os.environ.get("CALENDAR_ACCOUNT", "")
    if d["kind"] == "calendar":
        s0, e0 = _times(d)
        payload = {"token": token, "action": "calendar", "title": d["title"], "details": details,
                   "start": s0.isoformat(), "end": e0.isoformat()}
        stamp = lambda t: t.strftime("%Y%m%dT%H%M%S")  # noqa: E731
        link = "https://calendar.google.com/calendar/render?" + urlencode({
            "action": "TEMPLATE", "text": d["title"], "dates": f"{stamp(s0)}/{stamp(e0)}", "details": details,
            "ctz": os.environ.get("TIMEZONE", "America/Los_Angeles"), "authuser": account})
    else:
        payload = {"token": token, "action": "email", "title": d["subject"], "to": d["to"], "subject": d["subject"],
                   "body": d["body"], "details": details}
        link = "https://mail.google.com/mail/?" + urlencode({"view": "cm", "fs": "1", "to": d["to"], "su": d["subject"],
                                                              "body": d["body"], "authuser": account})
    if not url:
        d.update(status="drafted", link=link, result="No executor set up. Draft link only. Nothing was sent.")
        _save()
        return d
    own = client is None
    client = client or httpx.Client(timeout=30, follow_redirects=True)
    try:
        data = client.post(url, content=json.dumps(payload),
                           headers={"Content-Type": "text/plain;charset=utf-8"}).json()
        if data.get("ok"):
            result = data.get("did", "Sent")
            if d["kind"] == "email" and data.get("to") and data["to"].lower() != d["to"].lower():
                result += f" (not {d['to']}: not on the allowlist)"   # the Apps Script fell back to the demo address
            d.update(status="sent", result=result)
        else:
            d.update(status="failed", result=data.get("error", "executor said no"))
    except Exception as e:  # network down, bad JSON, ...
        d.update(status="failed", result=f"{type(e).__name__}: {e}")
    finally:
        if own:
            client.close()
    _save()
    return d


_load()
