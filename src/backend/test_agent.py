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


CONTACTS = {"maya": {"name": "Maya Patel", "email": "maya@example.com"}}


def call(_tool, **args):
    return {"text": None, "call": {"name": _tool, "args": args}, "parts": [{"functionCall": {"name": _tool, "args": args}}]}


def text(t):
    return {"text": t, "call": None, "parts": [{"text": t}]}


class Rig:
    def __init__(self, script, where=(37.78, -122.41)):
        self.script, self.said, self.pushed, self.menu_calls, self.proposed, self.steps = list(script), [], [], [], [], []
        self.agent = agent_mod.Agent(
            step=self.step, say=self.said.append, push=lambda *a: self.pushed.append(a), where=lambda: where, sync=True,
            find=lambda q, w: PLACES, menu=self.menu, propose=self.propose,
            draft_email=self.draft_email, draft_event=self.draft_event, search=self.search,
            find_contact=lambda n: CONTACTS.get((n or "").strip().lower()),
            draft_slack=self.draft_slack, slack_target=self.slack_target)
        self.emails, self.events, self.searches = [], [], []

    def draft_email(self, to_name, subject, body, quote, to=None):
        d = {"id": 11, "to_name": to_name, "subject": subject, "body": body, "quote": quote, "to": to or "demo@example.com",
             "to_is_contact": bool(to)}
        self.emails.append(d)
        return d

    def slack_target(self, name):
        n = (name or "").strip().lower()
        return {"maya": {"kind": "dm", "label": "Maya Patel", "id": "U0MAYA"},
                "#demo-team": {"kind": "channel", "label": "#demo-team", "id": "demo-team"}}.get(n)

    def draft_slack(self, target, text, quote):
        d = {"id": 21, "target": target, "text": text, "quote": quote}
        self.slacks = getattr(self, "slacks", []) + [d]
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
                         "quote": "hey scene email Priya about the budget", "to": "demo@example.com", "to_is_contact": False}]
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


# ---- contacts and the scheduling link ----

def test_email_to_a_saved_contact_uses_their_real_address_and_the_card_shows_it():
    r = Rig([call("send_email", to="Maya", subject="Deck", body="Hi Maya, deck attached.")])
    r.agent.start("email maya the deck")
    assert r.emails[0]["to"] == "maya@example.com" and r.emails[0]["to_name"] == "Maya Patel"
    kind, title, body, ref = r.pushed[-1]
    assert title == "Email Maya Patel?" and "maya@example.com" in body


def test_email_to_an_unknown_name_goes_to_the_demo_inbox_and_the_card_says_so():
    r = Rig([call("send_email", to="Priya", subject="Hi", body="Hello.")])
    r.agent.start("email priya")
    assert r.emails[0]["to"] == "demo@example.com" and "no contact saved" in r.pushed[-1][2].lower()


def test_the_model_can_never_choose_an_email_address():
    r = Rig([call("send_email", to="evil@attacker.com", subject="Hi", body="Hello.")])
    r.agent.start("email someone")
    assert r.emails[0]["to"] == "demo@example.com" and r.emails[0]["to_is_contact"] is False


def test_lookup_contact_tool():
    r = Rig([call("lookup_contact", name="maya"), call("lookup_contact", name="zed"), text("ok")])
    r.agent.start("who is maya")
    assert "maya@example.com" in str(r.steps[1]) and "no contact" in str(r.steps[2]).lower()


def test_scheduling_link_email_contains_the_link_and_goes_to_the_contact(monkeypatch):
    monkeypatch.setenv("SCHEDULING_LINK", "https://calendly.com/me/30min")
    monkeypatch.setenv("WEARER_NAME", "Srijan Saha")
    r = Rig([call("send_scheduling_link", to="Maya", note="Want to find a time for the demo?")])
    r.agent.start("find a time to meet with maya")
    e = r.emails[0]
    assert e["to"] == "maya@example.com" and "https://calendly.com/me/30min" in e["body"] and "demo" in e["body"]
    assert e["body"].startswith("Hi Maya") and e["body"].rstrip().endswith("Srijan") and e["subject"]
    assert r.pushed[-1][0] == "email" and r.agent.state()["status"] == "done"


def test_scheduling_link_missing_is_reported(monkeypatch):
    monkeypatch.delenv("SCHEDULING_LINK", raising=False)
    r = Rig([call("send_scheduling_link", to="Maya"), text("I don't have a scheduling link set up.")])
    r.agent.start("send maya my calendly")
    assert r.emails == [] and "scheduling link" in str(r.steps[-1]).lower()


# ---- Slack ----

def test_slack_dm_to_a_contact_makes_a_card_and_ends_the_session():
    r = Rig([call("send_slack", to="Maya", message="Running ten minutes late.")])
    r.agent.start("hey scene tell maya on slack I'm running late")
    assert r.slacks[0]["target"]["id"] == "U0MAYA" and r.slacks[0]["text"] == "Running ten minutes late."
    kind, title, body, ref = r.pushed[-1]
    assert (kind, title, body, ref) == ("slack", "Slack message to Maya Patel?", "Running ten minutes late.", 21)
    assert r.agent.state()["status"] == "done" and any("send" in s.lower() for s in r.said)


def test_slack_channel_post_card_title():
    r = Rig([call("send_slack", to="#demo-team", message="Build is ready.")])
    r.agent.start("post in demo team that the build is ready")
    assert r.pushed[-1][1] == "Post in #demo-team?"


def test_slack_refuses_unknown_people_channels_and_raw_ids_and_empty_messages():
    r = Rig([call("send_slack", to="zed", message="hi"), call("send_slack", to="#random", message="hi"),
             call("send_slack", to="U0RAWID", message="hi"), call("send_slack", to="Maya", message="  "),
             call("ask_user", question="Who should I message?")])
    r.agent.start("slack zed")
    assert not getattr(r, "slacks", []) and "no slack contact" in str(r.steps[1]).lower()
    assert "message is required" in str(r.steps[-1]).lower() and r.agent.state()["status"] == "waiting"


# ---- ordering: nearest place is picked, never confirmed ----

def test_find_nearby_picks_nearest_without_a_confirmation_question():
    """Two McDonald's, no ask_user at all: find -> menu (nearest, index 0 by default)
    -> propose. The order card says which street was picked and the 8pm time."""
    r = Rig([call("find_nearby", query="McDonald's"),
             call("get_menu"),
             call("propose_order", items=[{"name": "Big Mac", "qty": 1}])])
    r.agent.start("order a big mac from mcdonalds at 8pm")
    assert r.agent.state()["status"] == "done"
    assert not any(p[0] == "confirm" for p in r.pushed)  # no "Order from 441 Sutter St?" question
    place, lines = r.proposed[0]
    assert place["address"] == "441 Sutter St" and lines == [{"name": "Big Mac", "price_cents": 899, "qty": 1}]
    kind, title, body, ref = r.pushed[-1]
    assert kind == "order" and "441 Sutter St" in body and "0.1 mi, closest" in body
    assert "for 8:00 PM" in body and "$8.99" in body


def test_order_time_label():
    assert agent_mod._order_time_label("order a big mac from mcdonalds at 8pm") == "8:00 PM"
    assert agent_mod._order_time_label("order a big mac from mcdonalds at 8am") == "8:00 AM"
    assert agent_mod._order_time_label("order 2 big macs") is None  # a quantity is not a time
    assert agent_mod._order_time_label("order a big mac at 7, no actually 8pm") == "8:00 PM"
