"""The dining agent: a function-calling loop that pauses on ask_user. The model is scripted, lookups are fakes."""
import os
import tempfile

os.environ.setdefault("SCENE_DATA_DIR", tempfile.mkdtemp())  # before anything imports memory

import pytest  # noqa: E402

import agent as agent_mod  # noqa: E402
import payments  # noqa: E402

PLACES = [{"name": "McDonald's", "address": "441 Sutter St", "distance_mi": 0.1, "open_now": True},
          {"name": "McDonald's", "address": "609 Market St", "distance_mi": 0.3, "open_now": True}]
MENU = [{"id": "big-mac", "name": "Big Mac", "price_cents": 899, "calories": 580, "protein_g": 25},
        {"id": "fries-small", "name": "Fries (Small)", "price_cents": 419, "calories": 230, "protein_g": 2},
        {"id": "mcchicken", "name": "McChicken", "price_cents": 599, "calories": 400, "protein_g": 14},
        {"id": "qpc", "name": "Quarter Pounder with Cheese", "price_cents": 849, "calories": 520, "protein_g": 30}]


def call(name, **args):
    return {"text": None, "call": {"name": name, "args": args}, "parts": [{"functionCall": {"name": name, "args": args}}]}


def text(t):
    return {"text": t, "call": None, "parts": [{"text": t}]}


class Rig:
    def __init__(self, script, where=(37.78, -122.41)):
        self.script, self.said, self.pushed, self.menu_calls, self.proposed, self.steps = list(script), [], [], [], [], []
        self.agent = agent_mod.Agent(
            step=self.step, say=self.said.append, push=lambda *a: self.pushed.append(a), where=lambda: where, sync=True,
            find=lambda q, w: PLACES, menu=self.menu, propose=self.propose,
            draft_email=self.draft_email, draft_event=self.draft_event, search=self.search)
        self.emails, self.events, self.searches = [], [], []

    def draft_email(self, to_name, subject, body, quote):
        d = {"id": 11, "to_name": to_name, "subject": subject, "body": body, "quote": quote}
        self.emails.append(d)
        return d

    def draft_event(self, title, start, mins, quote):
        if start < "2026-01-01T00:00" or not start[:4].isdigit():
            raise ValueError("bad start")
        d = {"id": 12, "title": title, "start": start, "duration_min": mins, "quote": quote}
        self.events.append(d)
        return d

    def search(self, query):
        self.searches.append(query)
        return {"answer": "The Hero13 Black is the best all-rounder.", "sources": [
            {"title": "digitalcameraworld.com", "url": "https://x.test/1"}, {"title": "techradar.com", "url": "https://x.test/2"}]}

    def step(self, system, history, tools):
        self.system = system
        self.steps.append(history[-1])
        return self.script.pop(0)

    def menu(self, place, wanted=""):
        self.menu_calls.append(place["address"])
        return MENU

    def propose(self, place, lines):
        total = sum(l["price_cents"] * l["qty"] for l in lines)
        if total > 5000:
            raise payments.PaymentError("over cap")
        self.proposed.append((place, lines))
        return {"id": 7, "total_cents": total, "place_name": place["name"], "address": place["address"], "items": lines}


def test_flow_find_confirm_menu_order():
    r = Rig([call("find_nearby", query="McDonald's"),
             call("ask_user", question="The nearest McDonald's is 441 Sutter St, 0.1 miles. Order from there?"),
             call("get_menu", place_index=0),
             call("ask_user", question="What would you like?"),
             call("propose_order", items=[{"name": "Big Mac", "qty": 1}, {"name": "fries small", "qty": 1}])])
    r.agent.start("can you place an order at mcdonalds")
    assert r.agent.state()["status"] == "waiting" and "441 Sutter" in r.agent.state()["question"]
    assert r.said[0].lower().startswith("on it") and r.pushed[-1][0] == "confirm"
    r.agent.reply("yes")
    assert r.agent.state()["status"] == "waiting" and r.menu_calls == ["441 Sutter St"]
    r.agent.reply("a big mac and small fries")
    assert r.agent.state()["status"] == "done"
    place, lines = r.proposed[0]
    assert place["address"] == "441 Sutter St" and [(l["name"], l["qty"], l["price_cents"]) for l in lines] == [
        ("Big Mac", 1, 899), ("Fries (Small)", 1, 419)]
    assert r.pushed[-1][0] == "order" and r.pushed[-1][3] == 7 and "$13.18" in r.pushed[-1][2]


def test_no_at_place_confirmation_does_not_fetch_menu_for_that_place():
    r = Rig([call("find_nearby", query="McDonald's"), call("ask_user", question="Order from 441 Sutter St?"),
             call("ask_user", question="How about 609 Market St, 0.3 miles?"), call("get_menu", place_index=1),
             call("ask_user", question="What would you like?")])
    r.agent.start("order at mcdonalds")
    r.agent.reply("no")
    r.agent.reply("yes")
    assert r.menu_calls == ["609 Market St"]


def test_unknown_item_refused_and_no_order():
    r = Rig([call("find_nearby", query="m"), call("get_menu", place_index=0),
             call("propose_order", items=[{"name": "Sushi platter", "qty": 1}]),
             call("ask_user", question="I don't see that. Anything else?")])
    r.agent.start("order")
    assert r.proposed == [] and "not on the menu" in str(r.steps[-1]).lower()


@pytest.mark.parametrize("items,why", [([{"name": "Big Mac", "qty": 0}], "quantity"),
                                       ([{"name": "Big Mac", "qty": 50}], "quantity"),
                                       ([{"name": "Big Mac", "qty": 9}] * 1 + [{"name": "Quarter Pounder with Cheese", "qty": 9}], "over cap")])
def test_qty_bounds_and_over_cap_refused(items, why):
    r = Rig([call("find_nearby", query="m"), call("get_menu", place_index=0), call("propose_order", items=items),
             call("ask_user", question="Try fewer?")])
    r.agent.start("order")
    assert r.proposed == [] and why in str(r.steps[-1]).lower()


def test_tool_budget_ends_session_with_apology():
    r = Rig([call("find_nearby", query="m")] * 20)
    r.agent.start("order")
    assert r.agent.state()["status"] == "done" and any("couldn't" in s.lower() for s in r.said)
    assert len(r.steps) <= 9


def test_reply_without_waiting_session_raises_keyerror():
    r = Rig([])
    with pytest.raises(KeyError):
        r.agent.reply("yes")


def test_cancel_word_ends_session():
    r = Rig([call("find_nearby", query="m"), call("ask_user", question="Order from 441 Sutter St?")])
    r.agent.start("order")
    r.agent.reply("never mind")
    assert r.agent.state()["status"] == "done" and r.proposed == []


def test_no_location_makes_agent_ask_where():
    r = Rig([call("find_nearby", query="McDonald's"), call("ask_user", question="Where are you right now?"),
             call("find_nearby", query="McDonald's", near="downtown Austin"),
             call("ask_user", question="Nearest is 441 Sutter St. Order from there?")], where=None)
    r.agent.start("order at mcdonalds")
    assert "no location" in str(r.steps[-1]).lower() and r.agent.state()["question"] == "Where are you right now?"
    r.agent.reply("downtown Austin")
    assert r.agent.state()["status"] == "waiting" and "441 Sutter" in r.agent.state()["question"]


def test_rank_menu_protein_orders_by_protein_per_calorie():
    r = Rig([call("find_nearby", query="m"), call("get_menu", place_index=0), call("rank_menu", goal="protein"),
             text("The Quarter Pounder has the most protein.")])
    r.agent.start("what's high protein at mcdonalds")
    top = r.steps[-1]["parts"][0]["functionResponse"]["response"]["items"]
    assert top[0]["name"] == "Quarter Pounder with Cheese" and r.agent.state()["status"] == "done"


def test_second_start_replaces_session():
    r = Rig([call("find_nearby", query="m"), call("ask_user", question="Order from 441 Sutter St?"),
             call("find_nearby", query="m"), call("ask_user", question="Again?")])
    r.agent.start("order")
    r.agent.start("order again")
    assert r.agent.state()["question"] == "Again?"


# ---- general assistant tools: email, calendar, web search ----

def test_send_email_makes_a_draft_card_and_ends_the_session():
    r = Rig([call("send_email", to="Priya", subject="Budget", body="Hi Priya, sending the budget Friday.")])
    r.agent.start("hey scene email Priya about the budget")
    assert r.emails == [{"id": 11, "to_name": "Priya", "subject": "Budget", "body": "Hi Priya, sending the budget Friday.",
                         "quote": "hey scene email Priya about the budget"}]
    kind, title, body, ref = r.pushed[-1]
    assert (kind, title, ref) == ("email", "Email Priya?", 11) and "Budget" in body
    assert r.agent.state()["status"] == "done" and any("send" in s.lower() for s in r.said)


def test_send_email_refuses_empty_subject_or_body():
    r = Rig([call("send_email", to="Priya", subject="", body="hi"), call("ask_user", question="What should it say?")])
    r.agent.start("email priya")
    assert r.emails == [] and "subject" in str(r.steps[-1]).lower() and r.agent.state()["status"] == "waiting"


def test_calendar_event_card_and_asking_for_a_missing_time_first():
    r = Rig([call("ask_user", question="What time?"),
             call("add_calendar_event", title="Dentist", start="2026-11-20T15:00", duration_min=60)])
    r.agent.start("put the dentist on my calendar friday")
    assert r.agent.state()["question"] == "What time?" and r.events == []
    r.agent.reply("3pm")
    assert r.events[0]["title"] == "Dentist" and r.events[0]["start"] == "2026-11-20T15:00" and r.events[0]["duration_min"] == 60
    kind, title, body, ref = r.pushed[-1]
    assert (kind, title, ref) == ("calendar", "Add to calendar?", 12) and "Dentist" in body and "3:00 PM" in body


def test_calendar_event_refuses_bad_or_past_times():
    r = Rig([call("add_calendar_event", title="x", start="friday at 3", duration_min=30),
             call("add_calendar_event", title="x", start="2020-01-01T10:00", duration_min=30),
             call("ask_user", question="When exactly?")])
    r.agent.start("put x on my calendar")
    assert r.events == [] and "past" in str(r.steps[-1]).lower() and "YYYY-MM-DDTHH:MM" in str(r.steps[-2])


def test_web_search_pushes_an_answer_card_with_sources_and_lets_the_model_speak():
    r = Rig([call("web_search", query="best gopro cameras right now"), text("The Hero13 Black is the best all-rounder.")])
    r.agent.start("hey scene search up the best go pro cameras online")
    assert r.searches == ["best gopro cameras right now"]
    kind, title, body, ref = r.pushed[-1]
    assert kind == "answer" and "best gopro" in title.lower() and "Hero13" in body and "digitalcameraworld.com" in body
    assert r.said[-1] == "The Hero13 Black is the best all-rounder." and r.agent.state()["status"] == "done"


def test_web_search_failure_is_reported_to_the_model_not_raised():
    r = Rig([call("web_search", query="x"), text("I couldn't search just now.")])
    r.search = lambda q: (_ for _ in ()).throw(RuntimeError("down"))
    r.agent.search = r.search
    r.agent.start("search x")
    assert "failed" in str(r.steps[-1]).lower() and r.agent.state()["status"] == "done"


def test_system_prompt_has_todays_date_and_lists_every_tool():
    r = Rig([text("ok")])
    r.agent.start("hi")
    import datetime
    assert datetime.date.today().isoformat() in r.system
    names = {t["name"] for t in agent_mod.TOOLS}
    assert {"find_nearby", "get_menu", "ask_user", "propose_order", "rank_menu", "send_email",
            "add_calendar_event", "web_search"} <= names
