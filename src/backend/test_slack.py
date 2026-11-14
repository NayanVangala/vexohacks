"""Slack: DMs to saved contacts and posts to allowlisted channels, after a tap. Slack is faked."""
import json
import os
import tempfile

os.environ.setdefault("SCENE_DATA_DIR", tempfile.mkdtemp())

import pytest  # noqa: E402

import contacts  # noqa: E402
import slack  # noqa: E402


class Resp:
    def __init__(self, data, status=200):
        self.data, self.status_code = data, status

    def json(self):
        return self.data


class FakeSlack:
    """httpx.Client stand-in: records calls and replies from a script keyed by Slack method."""
    def __init__(self, replies=None):
        self.replies, self.calls = replies or {}, []

    def post(self, url, headers=None, json=None, **kw):
        method = url.rsplit("/", 1)[-1]
        self.calls.append((method, headers, json))
        return Resp(self.replies.get(method, {"ok": True}))

    def get(self, url, headers=None, params=None, **kw):
        method = url.rsplit("/", 1)[-1]
        self.calls.append((method, headers, params))
        return Resp(self.replies.get(method, {"ok": True}))


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    monkeypatch.setenv("SLACK_CHANNELS", "demo-team, General")
    p = tmp_path / "contacts.json"
    p.write_text(json.dumps({"Maya Patel": {"email": "maya@example.com", "slack": "U0MAYA"}, "Sam Lee": "sam@example.com",
                             "Jo Kim": {"slack": "U0JO"}}))
    monkeypatch.setenv("SCENE_CONTACTS", str(p))
    slack._channel_ids.clear()


def test_contacts_accept_a_slack_id_next_to_the_email_and_stay_backward_compatible():
    assert contacts.find("maya") == {"name": "Maya Patel", "email": "maya@example.com", "slack": "U0MAYA"}
    assert contacts.find("sam") == {"name": "Sam Lee", "email": "sam@example.com"}          # no slack key when there is none
    assert contacts.find("jo")["slack"] == "U0JO" and contacts.find("jo")["email"] == ""
    assert contacts.all()["Maya Patel"] == "maya@example.com"                                 # all() still name -> email


def test_add_contact_can_set_a_slack_id():
    contacts.add("Al Ng", "al@example.com", slack="U0AL")
    assert contacts.find("al")["slack"] == "U0AL"
    contacts.add("Al Ng", "al@example.com")                                                    # re-adding keeps the id
    assert contacts.find("al")["slack"] == "U0AL"
    with pytest.raises(ValueError):
        contacts.add("Bad", "bad@example.com", slack="not an id")


def test_channel_allowlist_is_case_insensitive_and_ignores_hash():
    assert slack.channel_allowed("#Demo-Team") and slack.channel_allowed("general")
    assert not slack.channel_allowed("random") and not slack.channel_allowed("") and not slack.channel_allowed("#")


def test_target_for_a_contact_dm_a_channel_and_refusals():
    assert slack.target("Maya") == {"kind": "dm", "label": "Maya Patel", "id": "U0MAYA"}
    assert slack.target("#demo-team") == {"kind": "channel", "label": "#demo-team", "id": "demo-team"}
    for bad in ("Sam", "#random", "U0RAWID", "zed", "", "C012345", "@maya"):
        assert slack.target(bad) is None, bad                    # no slack id / not allowlisted / raw ids never accepted


def test_send_dm_posts_to_the_user_id_with_the_bot_token():
    f = FakeSlack({"chat.postMessage": {"ok": True, "channel": "D1", "ts": "1.2"}})
    ok, result = slack.send({"kind": "dm", "label": "Maya Patel", "id": "U0MAYA"}, "running late", client=f)
    method, headers, body = f.calls[0]
    assert ok and result == "Slack message sent to Maya Patel"
    assert method == "chat.postMessage" and headers["Authorization"] == "Bearer xoxb-test"
    assert body == {"channel": "U0MAYA", "text": "running late"}


def test_send_channel_resolves_the_channel_id_once_and_posts_there():
    f = FakeSlack({"conversations.list": {"ok": True, "channels": [{"id": "C9", "name": "random"}, {"id": "C1", "name": "demo-team"}]}})
    ok, result = slack.send({"kind": "channel", "label": "#demo-team", "id": "demo-team"}, "build is ready", client=f)
    assert ok and "#demo-team" in result
    assert [c[0] for c in f.calls] == ["conversations.list", "chat.postMessage"] and f.calls[1][2]["channel"] == "C1"
    slack.send({"kind": "channel", "label": "#demo-team", "id": "demo-team"}, "again", client=f)
    assert [c[0] for c in f.calls].count("conversations.list") == 1


def test_send_reports_slacks_error_honestly_and_never_raises():
    f = FakeSlack({"chat.postMessage": {"ok": False, "error": "not_in_channel"}})
    ok, result = slack.send({"kind": "dm", "label": "Maya Patel", "id": "U0MAYA"}, "hi", client=f)
    assert not ok and "not_in_channel" in result and "invite" in result.lower()
    boom = FakeSlack()
    boom.post = lambda *a, **k: (_ for _ in ()).throw(ConnectionError("down"))
    ok, result = slack.send({"kind": "dm", "label": "x", "id": "U1"}, "hi", client=boom)
    assert not ok and "down" in result


def test_send_without_a_token_says_so(monkeypatch):
    monkeypatch.delenv("SLACK_BOT_TOKEN")
    ok, result = slack.send({"kind": "dm", "label": "Maya Patel", "id": "U0MAYA"}, "hi", client=FakeSlack())
    assert not ok and "SLACK_BOT_TOKEN" in result


def test_unknown_channel_in_the_workspace_is_reported():
    f = FakeSlack({"conversations.list": {"ok": True, "channels": []}})
    ok, result = slack.send({"kind": "channel", "label": "#demo-team", "id": "demo-team"}, "hi", client=f)
    assert not ok and "demo-team" in result and not any(c[0] == "chat.postMessage" for c in f.calls)
