"""Emailer: draft -> human Send -> executor. Recipient is always the demo address. No network."""
import json

import pytest

import emailer


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
    emailer._drafts.clear()
    monkeypatch.setenv("EXECUTOR_URL", "https://exec.example/run")
    monkeypatch.setenv("EXECUTOR_TOKEN", "tok")
    monkeypatch.setenv("DEMO_EMAIL_TO", "ultra@example.com")
    monkeypatch.setenv("CALENDAR_ACCOUNT", "me@example.com")


def make():
    return emailer.draft("Priya", "Budget", "Hi Priya, sending the budget Friday.", "send the budget to Priya")


def test_draft_is_proposed_and_sends_nothing():
    d = make()
    assert d["status"] == "proposed" and d["to"] == "ultra@example.com" and d["to_name"] == "Priya"


def test_send_goes_to_demo_address_only_and_marks_sent_when_executor_confirms():
    c = FakeClient()
    d = emailer.send(make()["id"], client=c)
    url, payload = c.calls[0]
    assert url == "https://exec.example/run" and payload["to"] == "ultra@example.com"
    assert payload["token"] == "tok" and payload["action"] == "email" and payload["subject"] == "Budget"
    assert "send the budget to Priya" in payload["details"]
    assert d["status"] == "sent" and d["result"] == "Emailed ultra@example.com"


def test_send_failed_when_executor_says_no_or_network_down():
    assert emailer.send(make()["id"], client=FakeClient({"ok": False, "error": "quota"}))["status"] == "failed"
    d = emailer.send(make()["id"], client=FakeClient(boom=ConnectionError("down")))
    assert d["status"] == "failed" and "down" in d["result"]


def test_without_executor_only_a_draft_link_and_honest_status(monkeypatch):
    monkeypatch.delenv("EXECUTOR_URL")
    d = emailer.send(make()["id"])
    assert d["status"] == "drafted" and d["link"].startswith("https://mail.google.com/") and "Nothing was sent" in d["result"]


def test_cannot_send_twice_or_after_dismiss_and_unknown_id():
    i = make()["id"]
    emailer.send(i, client=FakeClient())
    with pytest.raises(ValueError):
        emailer.send(i, client=FakeClient())
    j = make()["id"]
    emailer.dismiss(j)
    with pytest.raises(ValueError):
        emailer.send(j, client=FakeClient())
    with pytest.raises(KeyError):
        emailer.send(999)
    with pytest.raises(KeyError):
        emailer.dismiss(999)
