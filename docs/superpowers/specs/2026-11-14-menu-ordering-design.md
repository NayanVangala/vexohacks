# Menu awareness and agentic ordering (Stripe)

## Intent
At a restaurant, or anywhere, the wearer says "hey Scene, can you place an order at McDonald's". Scene runs
a real agent: it finds the nearest McDonald's, confirms the location with the wearer, looks up that
location's menu, builds the order from what the wearer asks for, confirms it, and pays through Stripe.
Food questions ("what's high protein here?") use the same lookups. Nothing is preloaded: places and menus
are looked up live. McDonald's is the demo; the agent works for any restaurant name.

## Goals
- A tool-using agent loop that can pause to ask the wearer a question and resume on the answer (spoken yes/no,
  a spoken item list, or a button).
- Flow for the demo: "place an order at McDonald's" -> Scene says "On it, looking" -> "The nearest McDonald's is
  441 Sutter St, about 0.1 miles, open now. Order from there?" -> yes -> "What would you like?" -> "a Big Mac and
  small fries" -> order card (items, prices, total, Pay / Not now) -> Pay -> Stripe Checkout (test mode) ->
  hosted order page moves received, preparing, ready.
- Location comes from the wearer's device; if none is known the agent asks "where are you?".
- Honest about what is simulated: the kitchen, and the payment is Stripe test mode. Prices are looked up from the
  web and shown as "may differ at the store".

## Non-goals
- No real McDonald's/DoorDash order, delivery address, tip or tax. No live Stripe keys.
- No browser-driving agent, no Stripe Issuing.
- No preloaded menu data. (Lookups are cached per restaurant and area so a repeat is instant.)

## Anti-goals
- The agent charging anything without two human confirmations (the place, then the cart with Pay).
- A line item the lookup did not return: items and prices must come from this session's menu lookup, and the
  total is computed in code from them. An item that matches nothing is refused out loud.
- An order charged twice.

## Constraints
- Reuse `payments.py` safety: test-mode keys only (`sk_live_` refused), per-order cap `PAY_MAX_CENTS`, daily cap
  `PAY_DAILY_CENTS`, one Checkout Session per approved order (idempotency key), approval only from the glasses'
  button or the dev route.
- Gemini tool use: the loop uses function calling; place and menu lookups are separate grounded calls (Google Maps
  and Google Search tools, verified live on this key with model `gemini-4-hydrogen`). Grounded calls cannot be mixed
  with function calling in one request, so each lookup is its own tool function wrapping a grounded call that
  returns JSON.
- Grounded lookups take seconds: Scene speaks an immediate "On it" and a short status line before each slow step.
- Voice and cards reuse `voice.py` and `memory.push_card`.

## Approaches considered
- A. Stripe Checkout for the order with a simulated kitchen. B. A plus a hosted order-tracking page (chosen).
  C. Stripe virtual card plus a browser agent on DoorDash (fragile, bot-blocked, needs Issuing approval).
- Agent shape: a function-calling loop with pause/resume (chosen) over a fixed scripted flow, so it handles
  "order at Chipotle", "actually make that two", and "no, a different one".

## Design
**1. `agent.py` (new): the dining agent.** One `Session` at a time: message history, `state` (`running`,
`waiting`, `done`), pending question, and the current place and menu. `start(utterance)` begins a session in a
worker thread; `reply(text_or_choice)` resumes a waiting session. The loop calls Gemini with function declarations and
executes returned calls until the model stops or calls `ask_user`. A session ends after `propose_order`, a cancel,
10 minutes idle, or 8 tool calls without progress. Tools:
- `find_nearby(query)` -> `[{name, address, distance_mi, open_now}]` (grounded Maps call, location from `/location`).
- `get_menu(place)` -> `[{id, name, price_cents, calories, protein_g}]` (grounded Search call). Items are normalized in
  code (stable ids, price in cents), anything without a price is dropped. Cached per `(place, day)`.
- `ask_user(question)` -> speaks the question, pushes a confirm card (Yes / No buttons), pauses; the answer returns
  as the tool result.
- `propose_order(place, items:[{id, qty}])` -> validates ids against this session's menu and qty 1 to 9, computes the
  total, applies the caps, creates the order, pushes the `order` card. The only way an order exists.
- `rank_menu(goal)` for "high protein" and "healthy" questions, from the fetched menu, so answers are consistent.

**2. Location.** `POST /location {lat, lng, accuracy?}` stores the latest fix with a time (valid 30 minutes). Sources:
the iPhone app or glasses web app (browser geolocation) when available, `SCENE_LOCATION=lat,lng` env for the demo,
and `POST /dev/location`. With no fix the agent asks the wearer where they are and passes the answer to the Maps call
as text.

**3. Entry points.** `voice.heard` questions that start with an ordering or dining intent (order, place an order,
get me, hungry, "what's high protein") start or resume the agent instead of `memory.ask`. While a session is
`waiting`, a short spoken answer or a tap resumes it (same short-answer rule as offers, 6 words max except when the
agent asked an open question like "what would you like?"). `POST /dev/say {text}` injects a spoken line.

**4. `orders.py` (new).** Table `orders` in the memory DB: items (JSON), total, place name and address, status
(`proposed`, `approved`, `paid`, `rejected`), session id, url, timestamps. `approve(id)` creates the Stripe
Checkout Session (`payment` mode, `price_data` lines, idempotency key `order-<id>`, `success_url` = the order
page); `refresh(id)` marks `paid` from Stripe; `status_view(id)` adds a kitchen stage from time since paid
(received 0 to 10 s, preparing 10 to 40 s, ready after; env tunable). Reuses the Stripe helper from `payments.py`.

**5. Routes and page.** `POST /orders/{id}/approve`, `/reject`; `GET /orders/{id}`; `GET /order/{id}` serves
`orders.html`: place and address, items and total, a "Pay with Stripe" button until paid, then the three-step
progress, labeled "Demo kitchen, Stripe test mode"; polls every 2 s. Agent endpoints for the glasses: `POST
/agent/reply {text|choice}`, `GET /agent` (state and pending question).

**6. Glasses.** `confirm` card (the agent's question with Yes / No) and `order` card (Pay / Not now). Pay shows
"Pay on your phone" with the order page URL; later updates show paid, preparing, ready.

**7. Tests.** Agent loop with a fake model (scripted tool calls): pauses on `ask_user` and resumes; refuses an item
not in the menu; refuses over-cap; ends after the tool budget; one session at a time. `find_nearby` and `get_menu`
parsing from recorded grounded responses (fixtures). Orders: total from items only, double approve makes one
session, refresh marks paid, kitchen stage by time. Location expiry. Routes. Gemini and Stripe stubbed. One manual
live check of the full McDonald's flow.

## Left to the builder
Prompt wording, the order page's look, cache storage, the exact function-calling and grounding request shapes (verify
live), and thresholds.
