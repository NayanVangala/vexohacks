"""Scene's task agent: "hey Scene, place an order at McDonald's", "email Priya about the budget", "put this on my
calendar", "search up the best GoPros" all run one real tool-using loop.

It finds the nearest place, asks the wearer to confirm it, looks up that place's menu, builds the cart from what
the wearer says, asks again, and only then proposes an order (a card with Pay). It can never charge anything: Pay
on the glasses is the only thing that starts Stripe. Order lines must come from the menu fetched in this session;
the total is computed in code by orders.propose.

The model call is injectable (`step`), so tests script it. Slow lookups run while the wearer is being asked.
"""

import os
import re
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import lookup
import models
import outbox
# orders and payments import memory, which imports this module: they are imported inside the functions that use them.

TOOL_BUDGET = 8            # tool calls per user turn before the agent gives up
IDLE_S = 600.0
CANCEL = re.compile(r"\b(never mind|nevermind|cancel|forget it|stop)\b", re.I)

def _system() -> str:
    now = datetime.now(ZoneInfo(os.environ.get("TIMEZONE", "America/Los_Angeles")))
    return f"""You are Scene, a voice assistant on someone's smart glasses. It is {now.strftime('%A %Y-%m-%d %H:%M')} local time.
You help with errands using tools: ordering food, emailing, the calendar, and searching the web. Rules:
- Ordering: find_nearby for the restaurant, then ask_user to confirm the nearest place (say its street and distance). If they say no, offer the next one. After they confirm, get_menu, then ask_user what they would like (skip if they already said). Only order items get_menu returned; never invent items or prices. propose_order creates a card they approve; you cannot pay.
- Email: send_email drafts it; the wearer taps Send. Write the subject and a 2 to 4 sentence body from them to the person. If you do not know what it should say, ask_user first. Never claim an email was sent: you only drafted it.
- Calendar: add_calendar_event needs a real day and time (start as YYYY-MM-DDTHH:MM local). If the day or time is missing or vague, ask_user first. Resolve "friday" or "tomorrow" from the date above. It makes a card the wearer approves.
- Search: web_search for facts, products, news. Then answer in one or two spoken sentences; details are already on their screen.
- "What is high protein" or "healthy options": get_menu then rank_menu, answer in one sentence.
- If find_nearby says there is no location, ask_user where they are, then call it again with near set to their answer.
- Every spoken line is under 20 words, casual, no lists. Do not read out URLs."""


TOOLS = [
    {"name": "find_nearby", "description": "Find the 3 nearest places matching a name or type of food.",
     "parameters": {"type": "object", "properties": {"query": {"type": "string"}, "near": {
         "type": "string", "description": "Where, in words, only when the device has no location."}}, "required": ["query"]}},
    {"name": "get_menu", "description": "Look up the menu and prices for one of the places just found.",
     "parameters": {"type": "object", "properties": {"place_index": {"type": "integer", "description": "0-based index into the places"},
                                                     "wanted": {"type": "string", "description": "Item names that must be included, if any."}},
                    "required": ["place_index"]}},
    {"name": "ask_user", "description": "Ask the wearer a question out loud and wait for their answer.",
     "parameters": {"type": "object", "properties": {"question": {"type": "string"}}, "required": ["question"]}},
    {"name": "propose_order", "description": "Create the order card for the wearer to approve. Items must be on the fetched menu.",
     "parameters": {"type": "object", "properties": {"items": {"type": "array", "items": {"type": "object", "properties": {
         "name": {"type": "string"}, "qty": {"type": "integer"}}, "required": ["name", "qty"]}}}, "required": ["items"]}},
    {"name": "send_email", "description": "Draft an email for the wearer to approve and send. It does not send by itself.",
     "parameters": {"type": "object", "properties": {"to": {"type": "string", "description": "Recipient's name"}, "subject": {"type": "string"},
                                                     "body": {"type": "string"}}, "required": ["to", "subject", "body"]}},
    {"name": "add_calendar_event", "description": "Draft a calendar event for the wearer to approve.",
     "parameters": {"type": "object", "properties": {"title": {"type": "string"}, "start": {"type": "string", "description": "YYYY-MM-DDTHH:MM, local time"},
                                                     "duration_min": {"type": "integer"}}, "required": ["title", "start"]}},
    {"name": "web_search", "description": "Search the web and get a short, sourced answer.",
     "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "rank_menu", "description": "Best items on the fetched menu for a goal.",
     "parameters": {"type": "object", "properties": {"goal": {"type": "string", "enum": ["protein", "healthy"]}}, "required": ["goal"]}},
]


def _toks(s: str) -> list[str]:
    return re.sub(r"[^a-z0-9 ]+", " ", s.lower()).split()


def _match(name: str, menu: list[dict]) -> dict | None:
    """Menu item for a spoken name: exact, then every spoken word inside one item's name (only if that is unambiguous)."""
    want = _toks(name)
    exact = [m for m in menu if sorted(_toks(m["name"])) == sorted(want)]
    if exact:
        return exact[0]
    hits = [m for m in menu if want and set(want) <= set(_toks(m["name"]))]
    return hits[0] if len(hits) == 1 else None


def _usd(c: int) -> str:
    return f"${c / 100:.2f}"


class Agent:
    def __init__(self, step=None, say=None, push=None, where=None, sync=False, find=None, menu=None, propose=None,
                 draft_email=None, draft_event=None, search=None):
        self.step = step or models.agent_step
        self.say = say or (lambda text: None)
        self.push = push or (lambda kind, title, body, ref: None)
        self.where = where or (lambda: None)
        self.sync = sync
        self.find = find or (lambda q, w: lookup.find_nearby(q, w))
        self.menu = menu or (lambda place, wanted="": lookup.get_menu(place, wanted))
        self.propose = propose or self._propose
        self.draft_email = draft_email or outbox.draft
        self.draft_event = draft_event or outbox.draft_event
        self.search = search or models.search_web
        self._lock = threading.Lock()
        self._reset()

    @staticmethod
    def _propose(place: dict, lines: list[dict]) -> dict:
        import orders
        return orders.propose(place, lines)

    def _reset(self) -> None:
        self.status, self.question, self.hist = "idle", None, []
        self.places, self.place, self.items, self.calls = [], None, [], 0
        self.sid, self.last, self._spoke, self.utterance = getattr(self, "sid", 0) + 1, time.time(), False, ""

    def state(self) -> dict:
        if self.status in ("waiting", "running") and time.time() - self.last > IDLE_S:
            self.status, self.question = "done", None
        return {"status": self.status, "question": self.question, "session": self.sid}

    def start(self, utterance: str) -> None:
        self._reset()
        self.utterance = utterance
        self.hist = [{"role": "user", "parts": [{"text": utterance}]}]
        self.status = "running"
        self._go()

    def reply(self, text: str) -> None:
        """KeyError when no session is waiting for an answer."""
        if self.state()["status"] != "waiting":
            raise KeyError("no question is waiting")
        if CANCEL.search(text):
            self.status, self.question = "done", None
            self.say("Okay, cancelled.")
            return
        q, self.question, self.status, self.calls, self.last = self.question, None, "running", 0, time.time()
        self.hist.append({"role": "user", "parts": [{"functionResponse": {"name": "ask_user", "response": {"answer": text}}}]})
        self._go()

    def cancel(self) -> None:
        self.status, self.question = "done", None

    def _go(self) -> None:
        if self.sync:
            self._loop()
        else:
            threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self) -> None:
        with self._lock:
            sid = self.sid
            while self.status == "running" and sid == self.sid:
                if self.calls >= TOOL_BUDGET:
                    self.say("Sorry, I couldn't get that done. Try again?")
                    self.status = "done"
                    return
                try:
                    out = self.step(_system(), self.hist, TOOLS)
                except Exception as e:
                    print(f"agent: model call failed: {e!r}", flush=True)
                    self.say("Sorry, I couldn't reach my brain. Try again?")
                    self.status = "done"
                    return
                self.hist.append({"role": "model", "parts": out.get("parts") or [{"text": out.get("text") or ""}]})
                call = out.get("call")
                if not call:
                    if out.get("text"):
                        self.say(out["text"])
                    self.status = "done"
                    return
                self.calls += 1
                result = self._tool(call["name"], call.get("args") or {})
                if self.status == "waiting":
                    return
                self.hist.append({"role": "user", "parts": [{"functionResponse": {"name": call["name"], "response": result}}]})

    # ---- tools ----

    def _tool(self, name: str, a: dict) -> dict:
        fn = getattr(self, "_t_" + name, None)
        if fn is None:
            return {"error": f"unknown tool {name}"}
        try:
            return fn(a)
        except Exception as e:
            from payments import PaymentError
            if isinstance(e, PaymentError):
                return {"error": str(e)}
            print(f"agent: tool {name} failed: {e!r}", flush=True)
            return {"error": f"{name} failed, try again or ask the user"}

    def _t_find_nearby(self, a: dict) -> dict:
        where = self.where() or (a.get("near") or "").strip() or None
        if where is None:
            return {"error": "no location known for this device: ask the user where they are, then call find_nearby with near"}
        if not self._spoke:
            self._spoke = True
            self.say(f"On it, looking for {a.get('query', 'that')} near you.")
        self.places = self.find(a["query"], where)
        if not self.places:
            return {"error": "no places found; ask the user to name the place or area"}
        return {"places": [{"index": i, **p} for i, p in enumerate(self.places)]}

    def _t_get_menu(self, a: dict) -> dict:
        i = a.get("place_index")
        if not isinstance(i, int) or not 0 <= i < len(self.places):
            return {"error": "place_index is not one of the found places"}
        self.place = self.places[i]
        self.items = self.menu(self.place, a.get("wanted") or "")
        if not self.items:
            return {"error": "no menu found for that place; tell the user"}
        return {"place": self.place["name"], "items": [{"name": m["name"], "price": _usd(m["price_cents"]),
                                                        "calories": m["calories"], "protein_g": m["protein_g"]} for m in self.items]}

    def _t_ask_user(self, a: dict) -> dict:
        q = str(a.get("question") or "").strip()
        self.question, self.status = q, "waiting"
        self.say(q)
        self.push("confirm", "Scene asks", q, self.sid)
        return {}

    def _t_rank_menu(self, a: dict) -> dict:
        if not self.items:
            return {"error": "no menu fetched yet: call get_menu first"}
        withp = [m for m in self.items if m["protein_g"] is not None and m["calories"]]
        if a.get("goal") == "healthy":
            key = lambda m: (m["calories"], -m["protein_g"])  # noqa: E731
            pool = [m for m in withp if m["protein_g"] >= 5] or withp
        else:
            key = lambda m: (-m["protein_g"] / m["calories"], -m["protein_g"])  # noqa: E731
            pool = withp
        top = sorted(pool, key=key)[:5]
        return {"items": [{"name": m["name"], "calories": m["calories"], "protein_g": m["protein_g"]} for m in top]}

    def _t_send_email(self, a: dict) -> dict:
        subject, body = str(a.get("subject") or "").strip(), str(a.get("body") or "").strip()
        if not subject or not body:
            return {"error": "subject and body are required: ask the user what the email should say"}
        d = self.draft_email(str(a.get("to") or "").strip(), subject, body, self.utterance)
        self.push("email", f"Email {d['to_name'] or 'them'}?", f"{subject}: {body}", d["id"])
        self.say("Drafted it. Pinch Send on your glasses when you're ready.")
        self.status = "done"
        return {"ok": True}

    def _t_add_calendar_event(self, a: dict) -> dict:
        title, start = str(a.get("title") or "").strip(), str(a.get("start") or "").strip()
        try:
            when = datetime.strptime(start, "%Y-%m-%dT%H:%M")
        except ValueError:
            return {"error": "start must be YYYY-MM-DDTHH:MM in local time with a real day and time: ask the user if unsure"}
        now = datetime.now(ZoneInfo(os.environ.get("TIMEZONE", "America/Los_Angeles"))).replace(tzinfo=None)
        if when < now.replace(microsecond=0) - timedelta(minutes=5):
            return {"error": "that time is in the past: ask the user for a future day and time"}
        if not title:
            return {"error": "title is required"}
        mins = a.get("duration_min")
        d = self.draft_event(title, start, mins if isinstance(mins, int) and mins > 0 else 30, self.utterance)
        self.push("calendar", "Add to calendar?", f"{title} · {when.strftime('%a %b %-d, %-I:%M %p')}", d["id"])
        self.say("Got it. Pinch Add on your glasses to put it on your calendar.")
        self.status = "done"
        return {"ok": True}

    def _t_web_search(self, a: dict) -> dict:
        q = str(a.get("query") or "").strip()
        if not q:
            return {"error": "query is required"}
        if not self._spoke:
            self._spoke = True
            self.say("On it, searching.")
        r = self.search(q)
        names = [s["title"] for s in r.get("sources", [])]
        self.push("answer", q[:60], r["answer"] + (f"\nSources: {', '.join(names)}" if names else ""), None)
        return {"answer": r["answer"], "sources": names}

    def _t_propose_order(self, a: dict) -> dict:
        if not self.place or not self.items:
            return {"error": "get_menu for the chosen place first"}
        lines = []
        for it in a.get("items") or []:
            qty = it.get("qty")
            if not isinstance(qty, int) or not 1 <= qty <= 9:
                return {"error": f"quantity for {it.get('name')} must be 1 to 9"}
            m = _match(str(it.get("name", "")), self.items)
            if m is None:
                return {"error": f"'{it.get('name')}' is not on the menu I found; ask the user to pick something else"}
            lines.append({"name": m["name"], "price_cents": m["price_cents"], "qty": qty})
        if not lines:
            return {"error": "no items given"}
        order = self.propose(self.place, lines)  # raises PaymentError over the cap; _tool turns it into an error result
        summary = ", ".join(f"{l['qty']}x {l['name']}" for l in lines) + f" · {_usd(order['total_cents'])}"
        self.push("order", f"Order from {self.place['name']}?", summary, order["id"])
        self.say(f"Got it. {summary.replace(' · ', ', ')} total. Approve it on your glasses.")
        self.status = "done"
        return {"ok": True, "order_id": order["id"]}
