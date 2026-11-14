# Slack setup for Scene (about 3 minutes)

Scene can DM people and post in channels, only after you tap Send. It uses a Slack app (a bot) in YOUR workspace.

## 1. Create the app from a manifest
1. Go to https://api.slack.com/apps > **Create New App** > **From a manifest** > pick your workspace.
2. Choose the **JSON** tab, paste this, **Next**, **Create**:

```json
{
  "display_information": { "name": "Scene", "description": "Scene, your glasses assistant" },
  "features": { "bot_user": { "display_name": "Scene", "always_online": false } },
  "oauth_config": {
    "scopes": { "bot": ["chat:write", "chat:write.public", "im:write", "channels:read", "groups:read", "users:read"] }
  },
  "settings": { "org_deploy_enabled": false, "socket_mode_enabled": false, "token_rotation_enabled": false }
}
```

3. **Install to Workspace** > Allow.
4. **OAuth & Permissions** > copy the **Bot User OAuth Token** (starts with `xoxb-`).

## 2. Configure Scene (`src/backend/.env`)
```
SLACK_BOT_TOKEN=xoxb-...
SLACK_CHANNELS=scene-test          # channels Scene may post in, comma-separated, no #
```
Restart the backend. For a private channel, run `/invite @Scene` in it. Public channels work without inviting
(`chat:write.public`).

## 3. Contacts who can be DMed
Find a person's Slack member ID: click their profile > the three dots > **Copy member ID** (looks like `U012ABCDEF`).
Add it next to their email in `data/contacts.json`, or `POST /contacts` with `name`, `email` and `slack`:
```json
{ "Maya Patel": { "email": "maya@example.com", "slack": "U012ABCDEF" } }
```
A person without a Slack id cannot be messaged on Slack (Scene will say so).

## 4. Try it (nothing is sent until you tap Send)
Say "hey Scene, post in scene-test that the build is ready" or "tell Maya on Slack I'm running late". A card appears with
exactly where it goes; **Send** posts it, and the result says "sent" only when Slack confirms.

Safety: the model only supplies a name or #channel. DMs go only to contacts with a saved Slack id; channel posts only to
`SLACK_CHANNELS`. A raw id or an unlisted channel is refused.
