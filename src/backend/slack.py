"""Slack: DMs to saved contacts and posts to allowlisted channels, only after the wearer's tap.

Direct Slack Web API with a bot token (SLACK_BOT_TOKEN, xoxb-...). The model only ever supplies a NAME or a #channel;
the destination is resolved here: a DM goes only to a contact that has a Slack member id in contacts.json, a channel post
only to a channel in SLACK_CHANNELS. Anything else is refused, so a hallucinated id or channel can never be messaged.
"""

import os

import httpx

import contacts

API = "https://slack.com/api"
_channel_ids: dict[str, str] = {}


def _norm(name: str) -> str:
    return str(name or "").strip().lstrip("#").strip().lower()


def channel_allowed(name: str) -> bool:
    ch = _norm(name)
    return bool(ch) and ch in {_norm(c) for c in os.environ.get("SLACK_CHANNELS", "").split(",") if c.strip()}


def target(name) -> dict | None:
    """{kind: dm|channel, label, id} for a person with a Slack id or an allowlisted #channel. None otherwise."""
    n = str(name or "").strip()
    if n.startswith("#"):
        ch = _norm(n)
        return {"kind": "channel", "label": "#" + ch, "id": ch} if channel_allowed(ch) else None
    c = contacts.find(n)
    return {"kind": "dm", "label": c["name"], "id": c["slack"]} if c and c.get("slack") else None


def _hint(err: str) -> str:
    if err in ("not_in_channel", "channel_not_found"):
        return f"Slack said {err}. Invite the Scene bot to that channel (/invite @Scene) and try again."
    if err in ("invalid_auth", "not_authed", "token_revoked", "account_inactive"):
        return f"Slack said {err}. Check SLACK_BOT_TOKEN."
    if err == "missing_scope":
        return "Slack said missing_scope. Reinstall the app with chat:write, chat:write.public, im:write, channels:read, groups:read."
    return f"Slack said {err}."


def _channel_id(name: str, client, headers: dict) -> str | None:
    ch = _norm(name)
    if ch in _channel_ids:
        return _channel_ids[ch]
    cursor = ""
    for _ in range(5):
        r = client.get(f"{API}/conversations.list", headers=headers,
                       params={"types": "public_channel,private_channel", "limit": 1000, "exclude_archived": "true",
                               **({"cursor": cursor} if cursor else {})}).json()
        if not r.get("ok"):
            raise RuntimeError(_hint(r.get("error", "unknown_error")))
        for c in r.get("channels", []):
            _channel_ids[_norm(c.get("name", ""))] = c["id"]
        cursor = (r.get("response_metadata") or {}).get("next_cursor") or ""
        if ch in _channel_ids or not cursor:
            break
    return _channel_ids.get(ch)


def send(tgt: dict, text: str, client: httpx.Client | None = None) -> tuple[bool, str]:
    """Post the message. Returns (ok, result in plain words). Never raises: "sent" only when Slack confirms."""
    token = os.environ.get("SLACK_BOT_TOKEN", "").strip()
    if not token:
        return False, "SLACK_BOT_TOKEN is not set, so nothing was sent."
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json; charset=utf-8"}
    own = client is None
    client = client or httpx.Client(timeout=20)
    try:
        channel = tgt["id"]
        if tgt["kind"] == "channel":
            channel = _channel_id(tgt["id"], client, headers)
            if not channel:
                return False, f"I couldn't find {tgt['label']} in your Slack workspace, so nothing was sent."
        r = client.post(f"{API}/chat.postMessage", headers=headers, json={"channel": channel, "text": text}).json()
        if r.get("ok"):
            return True, f"Slack message sent to {tgt['label']}"
        return False, _hint(r.get("error", "unknown_error")) + " Nothing was sent."
    except Exception as e:  # network down, Slack error from the channel lookup, bad JSON, ...
        return False, f"{type(e).__name__}: {e}. Nothing was sent."
    finally:
        if own:
            client.close()
