# Phone app API (what the iPhone app can call)

All JSON, snake_case (the app's decoder converts). Base URL = the tunnel. Everything below already exists on `main`.

## Poll these

| Call | Returns | Use on the phone |
|---|---|---|
| `GET /feed?after=<lastCardId>` | `[{id, kind, title, body, ref, frame, ts}]`, oldest first, at most 50 | "Heard and seen". Cards are saved in the DB now, so `lastCardId` stays valid across a backend restart. New kinds: `offer`, `notes`, `email`, `calendar`, `confirm`, `order` (old: `answer`, `task`, `payment`). Unknown kinds must still render as a plain row. |
| `GET /inbox` | `[{type, id, title, body, ts, actions[], link?}]`, newest first | **One "Needs you" screen.** Types: `offer`, `question`, `email`, `event`, `order`, `payment`. Render `title` + `body` and one button per `actions[]`. |
| `GET /notes` | `[{id, summary, ts, decisions, action_items, slides}]` newest first | Meeting notes list. |
| `GET /notes/<id>` | `{summary, decisions[], action_items[{text, quote, action?, ...}], slides[{frame, text}]}` | Notes detail. Slide image = `GET /frames/<frame>`. |
| `GET /activity?limit=50&today=true` | `[{id, kind, text, status, frame, ts}]`, newest first. kind = `event` (seen, approved, paid, ...), `task` (status `open`/`done`), `notes` | "What Scene did today". |
| `GET /orders` | `[{id, place_name, address, items[{name, price_cents, qty}], total_cents, status, stage, url, ts}]` | Orders. `stage`: awaiting_approval, awaiting_payment, received, preparing, ready, rejected. Open `<base>/order/<id>` in Safari to pay or watch progress. |
| `GET /session` | `{notes: {active, started, slides, lines}}` | "Taking notes" indicator. `POST /notes/stop` ends it. |
| `GET /agent` | `{status: idle\|running\|waiting\|done, question, session}` | Optional: show the ordering agent's question live. |
| existing | `/tasks`, `/items`, `/payments`, `/today`, `/stats` | unchanged |

## Actions (an `actions[]` entry is a ready-made call)

```json
{"label": "Send", "method": "POST", "path": "/emails/3/send", "primary": true}
{"label": "Yes", "method": "POST", "path": "/agent/reply", "json": {"text": "yes"}, "primary": true}
```
Send `method` to `base + path`. When `json` is present send it as the body, either as JSON (`Content-Type: application/json`) or form-encoded, which is what the app's existing `post(form:)` does; `/agent/reply` and `/location` accept both.
A 409 means it was already answered or expired: just refresh the list. Each action response is the updated object.

## Send this

- `POST /location` `{"lat": 37.78, "lng": -122.41}` from CoreLocation every few minutes (valid 30 min). The ordering agent uses it
  for "nearest McDonald's"; without it the agent asks "where are you?".

## Notes
- Offers expire after 60 s; the agent's question waits 10 minutes; email/calendar drafts and orders stay until answered.
- Email always goes to the demo address, never the person named. Orders are Stripe test mode with a demo kitchen.
