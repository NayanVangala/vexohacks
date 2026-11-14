# Agentic Ordering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** "Hey Scene, place an order at McDonald's" runs a live agent: nearest place, confirm, menu lookup, cart, confirm, Stripe test-mode payment, hosted order tracking.

**Architecture:** `lookup.py` wraps two grounded Gemini calls (Maps for places, Search for menus) into clean JSON with a cache. `orders.py` stores orders and talks to Stripe via `payments._stripe`. `agent.py` is a function-calling loop that can pause on `ask_user`. `memory.py`/`server.py` route dining speech to the agent and serve routes and the order page; the glasses app renders `confirm` and `order` cards.

**Tech Stack:** Python flat modules in `src/backend`, FastAPI, httpx, Gemini REST (`gemini-4-hydrogen`), Stripe REST (test mode), vanilla JS.

**Spec:** `docs/superpowers/specs/2026-11-14-menu-ordering-design.md`

## Global Constraints
- Tests run from `src/backend` with `python -m pytest -q`; never touch the network: stub `models.grounded`, `models.agent_step`, Stripe (`payments._stripe`), `voice.speak`.
- Money is integer cents. Per-order cap `payments.MAX_CENTS`, daily cap `payments.DAILY_CENTS`, minimum 50 cents. Test keys only (`payments._stripe` already refuses others).
- An order line must come from the menu fetched in the same agent session; total computed in code only.
- Agent limits: 8 tool calls per user turn, session idle expiry 600 s, one active session.
- Spoken "on it" line before any lookup; every Yes/No confirm is both spoken and a `confirm` card.
- Kitchen stages from time since paid: received < 10 s, preparing < 40 s, then ready (`KITCHEN_PREP_S`, `KITCHEN_READY_S` env).
- Prices are labeled "may differ at the store"; the page says "Demo kitchen, Stripe test mode".

## Review Focus
- A grounded response with prose around the JSON, a fenced block, or no JSON at all must not crash: no JSON means "couldn't find".
- A menu item with no price is dropped; duplicate ids from two lookups never double a line.
- The model asking for an item not in the fetched menu, qty 0 or 50, or a total over the cap: refused with a spoken reason, no order created.
- User says "no" at the place confirmation: the agent offers the next nearest or ends; it never proceeds to the menu with the rejected place.
- Double Pay tap: one Stripe session. Pay on an already paid or rejected order: 409.
- No location fix and none stated: the agent asks "where are you?" instead of guessing.
- A reply arriving when no session is waiting is ignored (409), not queued.

---

### Task 1: Grounded lookups (`lookup.py`, `models.grounded`)

**Files:** Create `src/backend/lookup.py`, `src/backend/test_lookup.py`; modify `src/backend/models.py` (add `grounded`).

**Interfaces:**
- Produces in `models.py`: `grounded(prompt: str, tool: str, latlng: tuple[float, float] | None = None) -> str` (tool is `"google_maps"` or `"google_search"`; posts `generateContent` with `tools:[{tool:{}}]` and, when latlng is given, `toolConfig.retrievalConfig.latLng`; returns the text of the first candidate).
- Produces in `lookup.py`: `parse_json(text: str) -> object | None` (finds the first JSON array/object in prose or a fenced block, `None` if none); `find_nearby(query: str, where: tuple[float, float] | str | None, ask=models.grounded) -> list[dict]` returning up to 3 `{name, address, distance_mi: float | None, open_now: bool | None}` nearest first; `get_menu(place: dict, ask=models.grounded) -> list[dict]` returning `{id: str, name: str, price_cents: int, calories: int | None, protein_g: int | None}` (id = slug of the name, items without a positive price dropped, duplicates merged); a module-level cache keyed `(kind, key, YYYY-MM-DD)` and `clear_cache()`.

- [ ] **Step 1: Failing tests**: `test_parse_json_variants` (plain, fenced ```json, prose before and after, none→None), `test_find_nearby_normalizes_and_sorts` (fake `ask` returns messy JSON; expect ≤3 places, nearest first, string distances like "0.1 miles" parsed to 0.1), `test_find_nearby_passes_latlng_or_text` (tuple goes as latlng, a string goes into the prompt), `test_find_nearby_no_json_returns_empty`, `test_get_menu_drops_unpriced_and_dedupes`, `test_price_strings_become_cents` ("$5.99"→599, 6→600), `test_cache_hits_once_per_day` (second call does not call `ask`).
- [ ] **Step 2: Run** `python -m pytest test_lookup.py -q`; expect failure.
- [ ] **Step 3: Implement** both modules. Prompts ask for strict JSON only and for the specific location's address in the menu prompt.
- [ ] **Step 4: Run** tests; expect pass. **Step 5: Commit** `feat(order): grounded place and menu lookups`.

### Task 2: Orders and Stripe (`orders.py`)

**Files:** Create `src/backend/orders.py`, `src/backend/test_orders.py`.

**Interfaces:**
- Consumes: `payments._stripe(method, path, **kw)`, `payments.PaymentError`, `payments.MAX_CENTS`, `payments.DAILY_CENTS`, `memory.db()`, `memory._db_lock`, `memory.rows`, `memory.add`.
- Produces: `propose(place: dict, lines: list[dict]) -> dict` where a line is `{name, price_cents, qty}`; raises `payments.PaymentError` when total is outside 50 cents to `MAX_CENTS`; returns the stored order `{id, place_name, address, items(list), total_cents, status, url, session_id, paid_at, ts}`. `get(order_id) -> dict | None`, `approve(order_id) -> dict` (double approve returns the same order; status must be `proposed` else `PaymentError`; daily cap check; Stripe `/checkout/sessions` payment mode, `price_data` line per item, header `Idempotency-Key: scene-order-<id>`, `success_url` = `${PUBLIC_BASE_URL}/order/<id>`), `reject(order_id) -> None`, `refresh(order_id) -> dict` (polls session; `paid` sets status and `paid_at`), `status_view(order_id, now: float | None = None) -> dict` (order plus `stage` in `received|preparing|ready|awaiting_payment|rejected`).

- [ ] **Step 1: Failing tests** (stub `payments._stripe`, `memory.add`, temp DB): `test_propose_total_from_lines`, `test_propose_rejects_over_cap_and_under_minimum`, `test_double_approve_makes_one_stripe_session`, `test_approve_sends_one_line_item_per_item_in_cents`, `test_approve_blocked_when_not_proposed`, `test_refresh_marks_paid_and_sets_paid_at`, `test_kitchen_stage_by_time` (0 s received, 20 s preparing, 60 s ready, unpaid awaiting_payment), `test_daily_cap`.
- [ ] **Step 2: Run** `python -m pytest test_orders.py -q`; expect failure. **Step 3: Implement** (own table created on first use like `payments._init`). **Step 4: Run** tests; expect pass. **Step 5: Commit** `feat(order): orders with Stripe checkout and kitchen stages`.

### Task 3: The agent loop (`agent.py`, `models.agent_step`)

**Files:** Create `src/backend/agent.py`, `src/backend/test_agent.py`; modify `src/backend/models.py` (add `agent_step`).

**Interfaces:**
- Consumes: `lookup.find_nearby`, `lookup.get_menu`, `orders.propose`, `payments.PaymentError`.
- Produces in `models.py`: `agent_step(system: str, history: list[dict], tools: list[dict]) -> dict` returning `{"text": str | None, "call": {"name": str, "args": dict} | None}`; `history` items are Gemini `contents` dicts; sends `functionDeclarations` and returns the first function call or the text.
- Produces in `agent.py`: `class Agent(step=models.agent_step, say=voice.speak, push=memory-free callable(kind, title, body, ref) -> None, where=lambda: (lat,lng) | None, sync=False)` with `start(utterance: str) -> None`, `reply(text: str) -> None` (raises `KeyError` when no session is waiting), `state() -> dict` (`{"status": "idle|running|waiting|done", "question": str | None, "session": int | None}`), `cancel()`. Tools offered to the model: `find_nearby(query)`, `get_menu(place_index)` (index into the last found places), `ask_user(question)`, `propose_order(items:[{name, qty}])`, `rank_menu(goal: "protein"|"healthy")`. `propose_order` matches names to the fetched menu (case-insensitive, exact then contains), rejects unknown names, qty outside 1..9, over-cap totals, and returns a text result to the model on rejection; on success it calls `orders.propose`, then `push("order", "Place order?", summary, order_id)`, and ends the session. Ends on cancel words ("never mind", "cancel"), idle 600 s, or 8 tool calls in one turn.

- [ ] **Step 1: Failing tests** with a scripted `step` (list of calls) and sync mode: `test_flow_find_confirm_menu_order` (find_nearby → ask_user pauses → reply "yes" → get_menu → ask_user "what would you like" → reply "a big mac" → propose_order → order pushed, status done), `test_no_at_place_confirmation_does_not_fetch_menu_for_that_place` (scripted model follows the tool result; assert `get_menu` not called with the rejected place), `test_unknown_item_refused_and_no_order`, `test_qty_bounds_and_over_cap_refused`, `test_tool_budget_ends_session_with_apology`, `test_reply_without_waiting_session_raises_keyerror`, `test_cancel_word_ends_session`, `test_no_location_makes_agent_ask_where`, `test_rank_menu_protein_orders_by_protein_per_calorie`, `test_on_it_is_spoken_before_first_lookup`.
- [ ] **Step 2: Run** `python -m pytest test_agent.py -q`; expect failure. **Step 3: Implement** the loop; system prompt tells the model it must confirm the place and the cart with `ask_user`, never invent items, and keep spoken lines under 20 words. Async mode runs each turn in a worker thread; `sync=True` runs inline for tests. **Step 4: Run** tests; expect pass. **Step 5: Commit** `feat(order): dining agent loop with pause and resume`.

### Task 4: Wiring: location, speech entry, routes, order page

**Files:** Modify `src/backend/memory.py` (dining intent routing, `agent` instance, location store), `src/backend/server.py`; create `src/backend/orders.html`; tests `src/backend/test_order_flow.py`.

**Interfaces:**
- Consumes: Tasks 1 to 3.
- Produces in `memory.py`: `set_location(lat: float, lng: float) -> None`, `get_location() -> tuple[float, float] | None` (None after 1800 s; falls back to env `SCENE_LOCATION="lat,lng"`), `agent = Agent(...)` wired to `voice.speak` and `push_card`, `DINING = re.compile(r"\b(order|place an order|hungry|get me|something to eat|high protein|healthy options|calories)\b", re.I)`; in `process_audio`, a question matching `DINING` calls `agent.start(question)`; while the agent is `waiting`, a short transcript calls `agent.reply(text)` (open questions accept any length up to 25 words, yes/no confirms keep the 6-word rule).
- Produces routes: `POST /location {lat, lng}`, `POST /agent/reply {text}` (409 if not waiting), `GET /agent`, `POST /orders/{id}/approve`, `POST /orders/{id}/reject`, `GET /orders/{id}` (refresh then `status_view`), `GET /order/{id}` (serves `orders.html`), dev `POST /dev/say {text}` and `POST /dev/location` behind `SCENE_DEV=1`. Add `/location /agent /orders /order` to the request-log prefix tuple.
- `orders.html`: single static page; place and address, items and total, "Pay with Stripe" button (the session `url`) while `awaiting_payment`, then three-step progress; label "Demo kitchen, Stripe test mode"; polls `/orders/<id>` every 2 s; readable on a phone.

- [ ] **Step 1: Failing tests**: `test_dining_question_starts_agent_not_memory_ask`, `test_reply_routes_to_waiting_agent_only`, `test_location_expires_and_env_fallback`, `test_order_routes_409_and_404`, `test_order_page_served`, `test_dev_say_requires_flag`.
- [ ] **Step 2: Run**; expect failures. **Step 3: Implement. Step 4: Run** the whole suite. **Step 5: Commit** `feat(order): speech entry, location, order routes and page`.

### Task 5: Glasses cards

**Files:** Modify `src/glasses/app.js`.
- [ ] **Step 1:** In `home`, render `confirm` cards (eyebrow "Scene asks", body, Yes/No buttons → `POST /agent/reply {text:"yes"|"no"}`) and `order` cards (items and total, `Pay` → `POST /orders/{ref}/approve` then note "Pay on your phone" with the order page path, `Not now` → reject). Offers and these cards rank above the Follow-Through hero.
- [ ] **Step 2:** `node --check src/glasses/app.js`. **Step 3: Commit** `feat(glasses): confirm and order cards`.

### Task 6: README and live check

- [ ] **Step 1:** README section "Agentic ordering" (flow, env: `STRIPE_SECRET_KEY` test key, `PUBLIC_BASE_URL`, `SCENE_LOCATION`, kitchen timers, honesty labels).
- [ ] **Step 2:** Live check with real Gemini (needs `SCENE_LOCATION` set to a test point): run `lookup.find_nearby("McDonald's", loc)` and `lookup.get_menu(place)` and confirm real results; run the agent end to end with `/dev/say`; Stripe step needs a test key.
- [ ] **Step 3: Commit** `docs: agentic ordering`.
