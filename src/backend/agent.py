"""The dining agent: "hey Scene, place an order at McDonald's" runs a real tool-using loop.

It finds the nearest place, asks the wearer to confirm it, looks up that place's menu, builds the cart from what
the wearer says, asks again, and only then proposes an order (a card with Pay). It can never charge anything: Pay
on the glasses is the only thing that starts Stripe. Order lines must come from the menu fetched in this session;
the total is computed in code by orders.propose.

The model call is injectable (`step`), so tests script it. Slow lookups run while the wearer is being asked.
"""

import re
import threading
import time

import lookup
import models
import orders
import payments

TOOL_BUDGET = 8            # tool calls per user turn before the agent gives up
IDLE_S = 600.0
CANCEL = re.compile(r"\b(never mind|nevermind|cancel|forget it|stop)\b", re.I)

SYSTEM = """You are Scene, a voice assistant on someone's smart glasses, helping them order food.
Work step by step with the tools. Rules:
- To order: call find_nearby for the restaurant, then ask_user to confirm the nearest place (say its street and distance). If they say no, offer the next one.
- After they confirm a place, call get_menu for it, then ask_user what they would like (skip this if they already said).
- Never invent menu items or prices. Only order items that get_menu returned. If they ask for something missing, say so and ask.
- Before propose_order the wearer must have said what they want. propose_order creates a card they approve; you cannot pay.
- Questions like "what is high protein" or "healthy options": use get_menu then rank_menu and answer in one short sentence.
- If find_nearby says there is no location, ask_user where they are, then call find_nearby again with near set to their answer.
- Every spoken line must be under 20 words, casual, no lists. Do not repeat the menu."""

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
    def __init__(self, step=None, say=None, push=None, where=None, sync=False, find=None, menu=None, propose=None):
        self.step = step or models.agent_step
        self.say = say or (lambda text: None)
        self.push = push or (lambda kind, title, body, ref: None)
        self.where = where or (lambda: None)
        self.sync = sync
        self.find = find or (lambda q, w: lookup.find_nearby(q, w))
        self.menu = menu or (lambda place, wanted="": lookup.get_menu(place, wanted))
        self.propose = propose or orders.propose
        self._lock = threading.Lock()
        self._reset()

    def _reset(self) -> None:
        self.status, self.question, self.hist = "idle", None, []
        self.places, self.place, self.items, self.calls = [], None, [], 0
        self.sid, self.last, self._spoke = getattr(self, "sid", 0) + 1, time.time(), False

    def state(self) -> dict:
        if self.status in ("waiting", "running") and time.time() - self.last > IDLE_S:
            self.status, self.question = "done", None
        return {"status": self.status, "question": self.question, "session": self.sid}

    def start(self, utterance: str) -> None:
        self._reset()
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
                    out = self.step(SYSTEM, self.hist, TOOLS)
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
        try:
            return getattr(self, "_t_" + name)(a)
        except AttributeError:
            return {"error": f"unknown tool {name}"}
        except payments.PaymentError as e:
            return {"error": str(e)}
        except Exception as e:
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
