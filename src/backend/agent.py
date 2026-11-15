"""Scene's task agent: "hey Scene, place an order at McDonald's", "email Priya about the budget", "put this on my
calendar", "search up the best GoPros" all run one real tool-using loop.

It finds the nearest place and picks it itself (never asks the wearer to confirm which location: when several
places share a city, the nearest one is the answer, and the card says which street it picked), looks up that
place's menu, builds the cart from what the wearer says, and only then proposes an order (a card with Pay).
It can never charge anything: Pay on the glasses is the only thing that starts Stripe. Order lines must come
from the menu fetched in this session; the total is computed in code by orders.propose.

The model call is injectable (`step`), so tests script it. Slow lookups run while the wearer is being asked.
"""

import os
import re
import threading
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import contacts
import lookup
import models
import outbox
import slack
import voice
# orders and payments import memory, which imports this module: they are imported inside the functions that use them.

TOOL_BUDGET = 8            # tool calls per user turn before the agent gives up
IDLE_S = 600.0
CANCEL = re.compile(r"\b(never mind|nevermind|cancel|forget it|stop)\b", re.I)

def _system() -> str:
    now = datetime.now(ZoneInfo(os.environ.get("TIMEZONE", "America/Los_Angeles")))
    return f"""You are Scene, a voice assistant on someone's smart glasses. It is {now.strftime('%A %Y-%m-%d %H:%M')} local time.
You help with errands using tools: ordering food, emailing, the calendar, and searching the web. Rules:
- Ordering: find_nearby for the restaurant, then pick the NEAREST place yourself (place_index 0, already sorted nearest first). Never ask the wearer to confirm which location: if several places share a city, the nearest one is the answer. Say its street and distance in one short line so they know which one you picked, then get_menu, then ask_user what they would like only if they did not already say an item. Only order items get_menu returned; never invent items or prices. propose_order creates a card they approve; you cannot pay. If the wearer named a time ("at 8pm"), that time goes on the order card; it does not need a question.
- Email and contacts: lookup_contact checks the saved contacts. send_email takes the person's NAME only (never an email address); the app looks up their address. send_scheduling_link emails someone the wearer's scheduling link so they can pick a time ("find a time to meet with Maya"). send_email drafts it; the wearer taps Send. Write the subject and a 2 to 4 sentence body from them to the person. If you do not know what it should say, ask_user first. Never claim an email was sent: you only drafted it.
- Slack: send_slack drafts a Slack message; the wearer taps Send. `to` is a person's NAME (they must have a Slack id saved) or a #channel from the allowed list. Never put an id or address in `to`. Write the message in the wearer's voice, one or two sentences.
- Calendar: add_calendar_event needs a real day and time (start as YYYY-MM-DDTHH:MM local). If the day or time is missing or vague, ask_user first. Resolve "friday" or "tomorrow" from the date above. It makes a card the wearer approves. If the wearer corrects a time in the same sentence ("7, no actually 8"), use the corrected time (8) and ignore the earlier one. Complete "schedule <thing> at <time>" requests are drafted in code before you see them; you only see ones missing a time or day, so ask_user for that.
- Reservations: propose_reservation only drafts a request card the wearer confirms; it never books a table, so never claim a table was booked. It needs a restaurant name and a real day and time (start as YYYY-MM-DDTHH:MM local). If either is missing or vague, ask_user first. If the wearer corrects a time in the same sentence ("7, no actually 8"), use the corrected time (8) and ignore the earlier one.
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
     "parameters": {"type": "object", "properties": {"to": {"type": "string", "description": "Recipient's NAME, never an email address"}, "subject": {"type": "string"},
                                                     "body": {"type": "string"}}, "required": ["to", "subject", "body"]}},
    {"name": "send_slack", "description": "Draft a Slack DM to a saved contact or a post to an allowed #channel. The wearer taps Send.",
     "parameters": {"type": "object", "properties": {"to": {"type": "string", "description": "A person's NAME or a #channel, never an id"},
                                                     "message": {"type": "string"}}, "required": ["to", "message"]}},
    {"name": "lookup_contact", "description": "Check whether a person is in the saved contacts.",
     "parameters": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "send_scheduling_link", "description": "Draft an email to a person with the wearer's scheduling link so they can pick a time to meet. The wearer taps Send.",
     "parameters": {"type": "object", "properties": {"to": {"type": "string", "description": "Person's name, never an email address"},
                                                     "note": {"type": "string", "description": "One friendly sentence about what the meeting is for"}}, "required": ["to"]}},
    {"name": "add_calendar_event", "description": "Draft a calendar event for the wearer to approve.",
     "parameters": {"type": "object", "properties": {"title": {"type": "string"}, "start": {"type": "string", "description": "YYYY-MM-DDTHH:MM, local time"},
                                                     "duration_min": {"type": "integer"}}, "required": ["title", "start"]}},
    {"name": "propose_reservation", "description": "Draft a restaurant reservation REQUEST for the wearer to confirm. It does not book a table: there is no booking API.",
     "parameters": {"type": "object", "properties": {"restaurant": {"type": "string", "description": "Restaurant name, as the wearer said it"},
                                                     "start": {"type": "string", "description": "YYYY-MM-DDTHH:MM, local time"},
                                                     "party_size": {"type": "integer", "description": "How many people, if the wearer said"}}, "required": ["restaurant", "start"]}},
    {"name": "web_search", "description": "Search the web and get a short, sourced answer.",
     "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}},
    {"name": "rank_menu", "description": "Best items on the fetched menu for a goal.",
     "parameters": {"type": "object", "properties": {"goal": {"type": "string", "enum": ["protein", "healthy"]}}, "required": ["goal"]}},
]


def _tok_norm(tok: str) -> str:
    """Plural-tolerant token: "Macs" matches the menu's "Mac", "nuggets" matches
    "nugget". Applied to both sides of a match, so it can only conflate items
    that differ by a trailing s alone."""
    return tok[:-1] if len(tok) > 3 and tok.endswith("s") and not tok.endswith("ss") else tok


def _toks(s: str) -> list[str]:
    return [_tok_norm(t) for t in re.sub(r"[^a-z0-9 ]+", " ", s.lower()).split()]


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


_ORDER_TIME_RE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b|\bat\s+(\d{1,2})(?::(\d{2}))?\b(?!\s*(?:am|pm)\b)", re.I)  # bare "at 8" must not steal the "8" of "at 8pm"


def _order_time_label(utterance: str) -> str | None:
    """"8:00 PM" from "order a Big Mac ... at 8pm", else None. A bare hour only counts
    after "at" (so "2 Big Macs" is a quantity, not a time); an am/pm always counts."""
    best = None
    for m in _ORDER_TIME_RE.finditer(utterance or ""):
        if m.group(1) is not None:
            h, minute, ampm = int(m.group(1)), int(m.group(2) or 0), m.group(3).lower()
        else:
            h, minute, ampm = int(m.group(4)), int(m.group(5) or 0), None
        if not 1 <= h <= 12 or minute > 59:
            continue
        if ampm:
            h24 = h % 12 + (12 if ampm == "pm" else 0)
        elif h < 12:
            h24 = h + 12  # a bare dinner hour is PM, same rule as the reservation correction
        else:
            h24 = h
        best = (m.start(), h24, minute)
    if best is None:
        return None
    now = datetime.now(ZoneInfo(os.environ.get("TIMEZONE", "America/Los_Angeles")))
    return now.replace(hour=best[1], minute=best[2], second=0, microsecond=0).strftime("%-I:%M %p")


class Agent:
    def __init__(self, step=None, say=None, push=None, where=None, sync=False, find=None, menu=None, propose=None,
                 draft_email=None, draft_event=None, search=None, find_contact=None,
                 draft_slack=None, slack_target=None, draft_reservation=None):
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
        self.find_contact = find_contact or contacts.find
        self.draft_slack = draft_slack or outbox.draft_slack
        self.draft_reservation = draft_reservation or outbox.draft_reservation
        self.slack_target = slack_target or slack.target
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
        # Nearest first, and already picked: when several places share a city, the
        # nearest one is the answer and the wearer is never asked to confirm which
        # location. place_index 0 is that place; the card says which street it is.
        self.places.sort(key=lambda p: (p.get("distance_mi") is None, p.get("distance_mi") or 0))
        self.place = self.places[0]
        return {"places": [{"index": i, **p} for i, p in enumerate(self.places)],
                "picked": {"index": 0, **self.places[0]},
                "note": "place_index 0 is already picked (nearest). Do not ask the user to confirm it; say its street and distance, then call get_menu."}

    def _t_get_menu(self, a: dict) -> dict:
        i = a.get("place_index")
        if i is None:
            i = 0  # nearest place is already picked by find_nearby; no confirm step
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

    def _draft_email(self, name: str, subject: str, body: str) -> dict:
        """The recipient is resolved here from the contacts book, from a NAME only: the model can never pick an address."""
        c = self.find_contact(name)
        shown = c["name"] if c else ("someone" if "@" in name else name)
        d = self.draft_email(shown, subject, body, self.utterance, to=c["email"] if c else None)
        title, text = outbox.card_text({**d, "to_name": shown})
        self.push("email", title, text, d["id"])
        self.say("Drafted it. Pinch Send on your glasses when you're ready.")
        self.status = "done"
        return {"ok": True}

    def _t_send_email(self, a: dict) -> dict:
        subject, body = str(a.get("subject") or "").strip(), str(a.get("body") or "").strip()
        if not subject or not body:
            return {"error": "subject and body are required: ask the user what the email should say"}
        return self._draft_email(str(a.get("to") or "").strip(), subject, body)

    def _t_send_slack(self, a: dict) -> dict:
        message, to = str(a.get("message") or "").strip(), str(a.get("to") or "").strip()
        if not message:
            return {"error": "message is required: ask the user what to say"}
        t = self.slack_target(to)
        if t is None:
            return {"error": f"no Slack contact or allowed channel matches '{to}': tell the user, or offer email"}
        d = self.draft_slack(t, message, self.utterance)
        title, text = outbox.card_text({"kind": "slack", "target": t, "text": message})
        self.push("slack", title, text, d["id"])
        self.say("Drafted it. Pinch Send on your glasses when you're ready.")
        self.status = "done"
        return {"ok": True}

    def _t_lookup_contact(self, a: dict) -> dict:
        c = self.find_contact(str(a.get("name") or ""))
        return {"found": True, **c} if c else {"error": f"no contact named '{a.get('name')}': offer to send it to the demo inbox or ask them to add the contact"}

    def _t_send_scheduling_link(self, a: dict) -> dict:
        link = os.environ.get("SCHEDULING_LINK", "").strip()
        if not link:
            return {"error": "no scheduling link is set up (SCHEDULING_LINK): tell the user"}
        name = str(a.get("to") or "").strip()
        c = self.find_contact(name)
        first = (c["name"] if c else name).split()[0] if (c or name) and "@" not in (c["name"] if c else name) else "there"
        note = str(a.get("note") or "").strip() or "I'd like to find a time for us to meet."
        wearer = os.environ.get("WEARER_NAME", "").split()[:1]
        body = f"Hi {first},\n\n{note}\n\nYou can pick a time that works for you here: {link}\n\nThanks,\n{wearer[0] if wearer else ''}".rstrip()
        return self._draft_email(name, "Finding a time to meet", body)

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
        voice.chime_out()   # the draft is made and the answer is done: the done ping
        self.status = "done"
        return {"ok": True}

    def _t_propose_reservation(self, a: dict) -> dict:
        """Draft a reservation request card. Never a booking: no restaurant booking API exists,
        and the card and the spoken line both say so."""
        restaurant = str(a.get("restaurant") or "").strip()
        start = str(a.get("start") or "").strip()
        if not restaurant:
            return {"error": "restaurant is required: ask the user which restaurant"}
        try:
            when = datetime.strptime(start, "%Y-%m-%dT%H:%M")
        except ValueError:
            return {"error": "start must be YYYY-MM-DDTHH:MM in local time with a real day and time: ask the user if unsure"}
        now = datetime.now(ZoneInfo(os.environ.get("TIMEZONE", "America/Los_Angeles"))).replace(tzinfo=None)
        if when < now.replace(microsecond=0) - timedelta(minutes=5):
            return {"error": "that time is in the past: ask the user for a future day and time"}
        size = a.get("party_size")
        party = size if isinstance(size, int) and size > 0 else None
        d = self.draft_reservation(restaurant, start, party, self.utterance)
        when_s = when.strftime("%a %b %-d, %-I:%M %p")
        party_s = f"party of {party}" if party else "party size not given"
        self.push("reservation", f"Reservation at {restaurant}?",
                  f"Draft reservation · {when_s} · {party_s} — tap Confirm, no table booked yet.", d["id"])
        self.say("Drafted it. Pinch Confirm on your glasses when you're ready. I haven't booked a table.")
        self.status = "done"
        return {"ok": True, "draft_id": d["id"]}

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
        if not self.place and self.places:
            self.place = self.places[0]  # nearest place is already picked; never ask which
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
        # The card says which location was picked (street + distance), and the time
        # the wearer named, so a "Newark at 8pm" order never silently drops either.
        dist = self.place.get("distance_mi")
        dist_s = f", {dist} mi, closest" if dist is not None else ", closest"
        when = _order_time_label(self.utterance)
        when_s = f" · for {when}" if when else ""
        summary = (", ".join(f"{l['qty']}x {l['name']}" for l in lines) + f" · {_usd(order['total_cents'])}{when_s}"
                   f" · {self.place['name']}, {self.place['address']}{dist_s} — tap Approve to pay.")
        self.push("order", f"Order from {self.place['name']}?", summary, order["id"])
        self.say(f"Got it. {summary.replace(' · ', ', ')} total. Approve it on your glasses.")
        self.status = "done"
        return {"ok": True, "order_id": order["id"]}
