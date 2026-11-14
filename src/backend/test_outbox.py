"""Emailer: draft -> human Send -> executor. Recipient is always the demo address. No network."""
import json

import pytest

import outbox


class FakeClient:
    def __init__(self, reply=None, boom=None):
        self.reply, self.boom, self.calls = reply or {"ok": True, "did": "Emailed ultra@example.com"}, boom, []

    def post(self, url, content=None, headers=None):
        self.calls.append((url, json.loads(content)))
        if self.boom:
            raise self.boom
        return type("R", (), {"json": lambda s: self.reply})()


@pytest.fixture(autouse=True)
def env(monkeypatch):
    outbox._drafts.clear()
    monkeypatch.setenv("EXECUTOR_URL", "https://exec.example/run")
    monkeypatch.setenv("EXECUTOR_TOKEN", "tok")
    monkeypatch.setenv("DEMO_EMAIL_TO", "ultra@example.com")
    monkeypatch.setenv("CALENDAR_ACCOUNT", "me@example.com")


def make():
    return outbox.draft("Priya", "Budget", "Hi Priya, sending the budget Friday.", "send the budget to Priya")


def test_draft_is_proposed_and_sends_nothing():
    d = make()
    assert d["status"] == "proposed" and d["to"] == "ultra@example.com" and d["to_name"] == "Priya"


def test_send_goes_to_demo_address_only_and_marks_sent_when_executor_confirms():
    c = FakeClient()
    d = outbox.send(make()["id"], client=c)
    url, payload = c.calls[0]
    assert url == "https://exec.example/run" and payload["to"] == "ultra@example.com"
    assert payload["token"] == "tok" and payload["action"] == "email" and payload["subject"] == "Budget"
    assert "send the budget to Priya" in payload["details"]
    assert d["status"] == "sent" and d["result"] == "Emailed ultra@example.com"


def test_send_failed_when_executor_says_no_or_network_down():
    assert outbox.send(make()["id"], client=FakeClient({"ok": False, "error": "quota"}))["status"] == "failed"
    d = outbox.send(make()["id"], client=FakeClient(boom=ConnectionError("down")))
    assert d["status"] == "failed" and "down" in d["result"]


def test_without_executor_only_a_draft_link_and_honest_status(monkeypatch):
    monkeypatch.delenv("EXECUTOR_URL")
    d = outbox.send(make()["id"])
    assert d["status"] == "drafted" and d["link"].startswith("https://mail.google.com/") and "Nothing was sent" in d["result"]


def test_cannot_send_twice_or_after_dismiss_and_unknown_id():
    i = make()["id"]
    outbox.send(i, client=FakeClient())
    with pytest.raises(ValueError):
        outbox.send(i, client=FakeClient())
    j = make()["id"]
    outbox.dismiss(j)
    with pytest.raises(ValueError):
        outbox.send(j, client=FakeClient())
    with pytest.raises(KeyError):
        outbox.send(999)
    with pytest.raises(KeyError):
        outbox.dismiss(999)


def make_event(start="2026-11-20T17:00", mins=45):
    return outbox.draft_event("Demo sync with Nayan", start, mins, "let's sync Friday at five")


def test_event_draft_is_proposed_and_validates_start():
    d = make_event()
    assert d["kind"] == "calendar" and d["status"] == "proposed" and d["title"] == "Demo sync with Nayan"
    for bad in ("", "friday at five", "2026-13-40T10:00"):
        with pytest.raises(ValueError):
            outbox.draft_event("x", bad, 30, "q")


def test_event_send_builds_zoned_start_and_end_and_marks_done_on_confirm(monkeypatch):
    monkeypatch.setenv("TIMEZONE", "America/Los_Angeles")
    c = FakeClient({"ok": True, "did": "Calendar event created: Demo sync with Nayan"})
    d = outbox.send(make_event()["id"], client=c)
    _, p = c.calls[0]
    assert p["action"] == "calendar" and p["title"] == "Demo sync with Nayan"
    assert p["start"] == "2026-11-20T17:00:00-08:00" and p["end"] == "2026-11-20T17:45:00-08:00"
    assert "let's sync Friday at five" in p["details"]
    assert d["status"] == "sent" and d["result"].startswith("Calendar event created")


def test_event_without_executor_gives_calendar_link(monkeypatch):
    monkeypatch.delenv("EXECUTOR_URL")
    d = outbox.send(make_event()["id"])
    assert d["status"] == "drafted" and d["link"].startswith("https://calendar.google.com/") and "Nothing was" in d["result"]


def test_draft_to_a_contact_sends_to_that_address_and_flags_it(monkeypatch):
    c = FakeClient({"ok": True, "did": "Email sent to maya@example.com", "to": "maya@example.com"})
    d = outbox.draft("Maya", "Hi", "Body", "q", to="maya@example.com")
    assert d["to"] == "maya@example.com" and d["to_is_contact"] is True
    r = outbox.send(d["id"], client=c)
    assert c.calls[0][1]["to"] == "maya@example.com" and r["status"] == "sent" and r["result"] == "Email sent to maya@example.com"


def test_draft_without_a_contact_goes_to_the_demo_address():
    d = outbox.draft("Priya", "Hi", "Body", "q")
    assert d["to"] == "ultra@example.com" and d["to_is_contact"] is False


def test_send_is_honest_when_the_executor_used_a_different_recipient():
    c = FakeClient({"ok": True, "did": "Email sent to ultra@example.com", "to": "ultra@example.com"})
    d = outbox.draft("Maya", "Hi", "Body", "q", to="maya@example.com")
    r = outbox.send(d["id"], client=c)
    assert r["status"] == "sent" and "ultra@example.com" in r["result"] and "not maya@example.com" in r["result"]


def test_slack_draft_card_and_send_use_the_slack_module(monkeypatch):
    import slack
    tgt = {"kind": "dm", "label": "Maya Patel", "id": "U0MAYA"}
    d = outbox.draft_slack(tgt, "running ten minutes late", "tell maya I'm late")
    assert d["kind"] == "slack" and d["status"] == "proposed"
    assert outbox.card_text(d) == ("Slack message to Maya Patel?", "running ten minutes late")
    ch = outbox.draft_slack({"kind": "channel", "label": "#demo-team", "id": "demo-team"}, "build is ready", "q")
    assert outbox.card_text(ch)[0] == "Post in #demo-team?"
    monkeypatch.setattr(slack, "send", lambda t, text, client=None: (True, "Slack message sent to Maya Patel"))
    r = outbox.send(d["id"])
    assert r["status"] == "sent" and r["result"] == "Slack message sent to Maya Patel"
    monkeypatch.setattr(slack, "send", lambda t, text, client=None: (False, "Slack said not_in_channel"))
    r2 = outbox.send(ch["id"])
    assert r2["status"] == "failed" and "not_in_channel" in r2["result"]
    with pytest.raises(ValueError):
        outbox.send(d["id"])                                   # already sent: no second message
