"""Runs an approved item. This is the only place anything leaves the building.

Honesty rule: status is "done" only when the executor confirmed it. With no executor configured we
build a draft link and say "drafted", never "sent"."""
import json
from datetime import datetime, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo
import httpx
from .config import Config
from .agent import TIME_FORMAT
from .models import Item


def _start_end(item: Item, cfg: Config):
    start = datetime.strptime(item.due_local, TIME_FORMAT).replace(tzinfo=ZoneInfo(cfg.timezone))
    return start, start + timedelta(minutes=item.duration_min)


def describe(item: Item) -> str:
    """Calendar description: one short line on what it is for, then where it came from."""
    heard = f'Heard: "{item.quote}"'
    if item.proof.start is not None:
        heard += f" ({int(item.proof.start) // 60:02d}:{int(item.proof.start) % 60:02d})"
    return (item.summary + "\n\n" if item.summary else "") + heard + "\nAdded by Follow-Through"


def draft_link(item: Item, cfg: Config) -> str:
    """A prefilled Google Calendar or Gmail compose link, used when no executor is set up."""
    if item.action == "calendar":
        s, e = _start_end(item, cfg)
        stamp = lambda d: d.strftime("%Y%m%dT%H%M%S")
        q = urlencode({"action": "TEMPLATE", "text": item.title, "dates": f"{stamp(s)}/{stamp(e)}",
                       "details": describe(item),
                       "ctz": cfg.timezone, "authuser": cfg.calendar_account})
        return "https://calendar.google.com/calendar/render?" + q
    if item.action == "email":
        q = urlencode({"view": "cm", "fs": "1", "to": item.to, "su": item.subject or item.title,
                       "body": item.body, "authuser": cfg.calendar_account})
        return "https://mail.google.com/mail/?" + q
    return ""


def execute(item: Item, cfg: Config, todos: list, client: httpx.Client = None) -> Item:
    """Do the approved action and return the item with its real status. Never raises."""
    try:
        if item.action == "todo":
            todos.append(item.title + (f" (by {item.due_local.replace('T', ' ')})" if item.due_local else ""))
            return item.model_copy(update={"status": "done", "result": "Added to the to-do list"})
        if item.action == "reply":
            return item.model_copy(update={"status": "drafted",
                                           "result": "Reply drafted. Copy it. Nothing was sent."})
        if not cfg.executor_url:
            return item.model_copy(update={"status": "drafted", "link": draft_link(item, cfg),
                                           "result": "No executor set up. Draft link only. Nothing was sent."})
        payload = {"token": cfg.executor_token, "action": item.action, "title": item.title,
                   "details": describe(item)}
        if item.action == "email":
            payload.update(to=item.to, subject=item.subject or item.title, body=item.body)
        else:
            s, e = _start_end(item, cfg)
            payload.update(start=s.isoformat(), end=e.isoformat())
        own = client is None
        client = client or httpx.Client(timeout=30, follow_redirects=True)
        try:
            r = client.post(cfg.executor_url, content=json.dumps(payload),
                            headers={"Content-Type": "text/plain;charset=utf-8"})
            data = r.json()
        finally:
            if own:
                client.close()
        if data.get("ok"):
            return item.model_copy(update={"status": "done", "result": data.get("did", "Done")})
        return item.model_copy(update={"status": "failed", "result": data.get("error", "executor said no")})
    except Exception as e:  # network down, bad JSON, bad time, ...
        return item.model_copy(update={"status": "failed", "result": f"{type(e).__name__}: {e}"})
