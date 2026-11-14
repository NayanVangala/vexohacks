# Scene Awareness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The glasses notice a meeting or slides, offer to take notes out loud and on the lens, and on yes produce evidence-checked notes.

**Architecture:** `models.see` also returns a scene. A pure `awareness.py` state machine turns scenes into at most one offer at a time. `notes.py` runs a note session (transcript lines plus one frame per slide change) and builds notes whose action items must quote the transcript or a saved slide. `memory.py` wires them into the existing frame/audio lanes and card feed; `server.py` adds routes; the glasses app renders offer and notes cards.

**Tech Stack:** Python 3 (flat modules in `src/backend`), FastAPI, pytest, vanilla JS in `src/glasses/app.js`.

**Spec:** `docs/superpowers/specs/2026-11-14-scene-awareness-design.md`

## Global Constraints
- Run tests from `src/backend` with `python -m pytest -q`. Tests never call the network: stub `models.*` and `memory.add`.
- Modules are flat imports (`import memory`), no packages.
- Scene types: `meeting|slides|whiteboard|card|menu|none`. A type counts at confidence >= 0.6, needs 3 frames in a row.
- Only `meeting` and `slides` offer notes. Cooldowns: 600 s after an offer is shown, 1800 s after a decline, 60 s before an unanswered offer expires, none while notes are active or another offer is pending.
- Notes auto-stop after 600 s with no `meeting`/`slides` scene.
- Offer copy is casual and spoken: `"{Hey NAME, }looks like you're sitting in on a meeting. Want me to take notes?"` (NAME from env `WEARER_NAME`, first word only; omit the greeting when unset).
- No action item is kept without a verbatim quote found in the transcript or a saved slide's text.
- `SCENE_DEV=1` enables `/dev/scene` and `/dev/line`; without it they return 404.

## Review Focus
- Same scene returned on every frame of a long meeting must produce one offer, not many.
- A frame whose Gemini result has no `scene` key (old prompt, failure) must not crash or reset notes.
- Accepting or declining an offer id that is unknown, expired, or already answered returns 409, never starts a second session.
- Ambiguous speech ("yes no", "maybe", silence) must not answer an offer.
- A static slide must be saved once; a slide with one changed word must not be saved again, a different slide must.
- A model-invented action item (quote not in transcript or slides) is dropped.

---

### Task 1: Awareness state machine

**Files:**
- Create: `src/backend/awareness.py`
- Test: `src/backend/test_awareness.py`

**Interfaces:**
- Produces:
  - `@dataclass Offer(id: int, scene_type: str, text: str, created: float)`
  - `class Awareness` with `feed(scene: dict | None, now: float) -> Offer | None`, `pending(now: float) -> Offer | None` (expires after 60 s), `accept(offer_id: int, now: float) -> Offer` and `decline(offer_id: int, now: float) -> Offer` (both raise `KeyError` if the id is not the pending offer), `set_busy(busy: bool) -> None`, `loggable(scene: dict | None, now: float) -> str | None` (for whiteboard/card/menu: text such as `"Looking at a menu"` at most once per 600 s per type, else `None`).
  - `parse_yes_no(text: str) -> str` returning `"yes"`, `"no"` or `"unclear"` (both or neither word groups present means `"unclear"`).

- [ ] **Step 1: Write failing tests** in `test_awareness.py`: `test_three_in_a_row_offers_once`, `test_different_scene_resets_count`, `test_low_confidence_ignored`, `test_missing_scene_ignored_and_resets_nothing_crashes`, `test_cooldown_after_shown_and_after_decline`, `test_no_offer_while_busy_or_pending`, `test_pending_expires_after_60s`, `test_accept_unknown_or_answered_raises_keyerror`, `test_loggable_once_per_ten_minutes`, `test_parse_yes_no` (params: `"yeah sure"`→yes, `"nope"`→no, `"yes no"`→unclear, `"maybe"`→unclear, `""`→unclear). Drive `now` explicitly; assert the offer text contains `"take notes"` and, with `WEARER_NAME="Srijan Saha"`, starts with `"Hey Srijan, "`.
- [ ] **Step 2: Run** `python -m pytest test_awareness.py -q`; expect import failure.
- [ ] **Step 3: Implement** `awareness.py` per the interfaces; read `WEARER_NAME` at offer creation time, ids from `itertools.count(1)`. Reuse the yes/no word lists from `followthrough/voice.py` (copy the lists, same word-boundary regex).
- [ ] **Step 4: Run** the tests; expect all pass.
- [ ] **Step 5: Commit** `feat(scene): awareness state machine for meeting/slides offers`.

### Task 2: Notes session and evidence-checked build

**Files:**
- Create: `src/backend/notes.py`
- Modify: `src/backend/models.py` (add `NOTES_PROMPT`, `make_notes`)
- Test: `src/backend/test_notes.py`

**Interfaces:**
- Produces in `notes.py`: `norm(s: str) -> str` (lowercase, non-alphanumerics to spaces, collapsed), `slide_changed(prev: str, new: str) -> bool` (False when either is empty or the word-set Jaccard similarity of the normalized texts is >= 0.8), `class Session` with `lines: list[str]`, `slides: list[dict]` (`{"frame": str, "text": str}`), `last_seen: float`; module functions `start(now: float) -> None`, `active() -> bool`, `add_line(text: str) -> None`, `on_scene(scene: dict | None, frame: str | None, now: float) -> bool` (saves a slide when type is `slides` or `whiteboard`, has text, and `slide_changed` vs the last saved; refreshes `last_seen` for `meeting`/`slides`; returns True when idle for 600 s), `stop() -> Session | None`, `build(session: Session, make=models.make_notes) -> dict` returning `{"summary": str, "decisions": list[str], "action_items": list[{"text","quote"}], "slides": list[dict]}`.
- Produces in `models.py`: `make_notes(transcript: str, slide_texts: list[str]) -> dict` using the existing `_think(system, user)`; prompt asks for `{summary, decisions, action_items:[{text, quote}]}` with quotes copied verbatim.
- Consumes: nothing from Task 1.

- [ ] **Step 1: Write failing tests**: `test_slide_changed` (identical→False, one word changed in a 10-word slide→False, different slide→True, empty→False), `test_static_slide_saved_once`, `test_new_slide_saved`, `test_on_scene_none_is_safe` (scene `None` and `{}` do not raise or save), `test_auto_stop_after_600s_without_meeting`, `test_build_drops_invented_action_items` (fake `make` returns one item quoted from the transcript, one quoted from a slide text, one invented; expect first two kept), `test_stop_returns_session_and_deactivates`.
- [ ] **Step 2: Run** `python -m pytest test_notes.py -q`; expect import failure.
- [ ] **Step 3: Implement** `notes.py` and `models.make_notes`; keep a single module-level `_session`.
- [ ] **Step 4: Run** tests; expect pass.
- [ ] **Step 5: Commit** `feat(scene): note session with slide capture and evidence-checked build`.

### Task 3: Wire scene, offers and notes into memory.py

**Files:**
- Modify: `src/backend/models.py:24-31` (`VISION_PROMPT` gains the `scene` field and its rules), `src/backend/memory.py` (`process_frame`, `process_audio`, new functions)
- Test: `src/backend/test_scene_flow.py`

**Interfaces:**
- Consumes: `Awareness`, `Offer`, `parse_yes_no` (Task 1); `notes.*` (Task 2).
- Produces in `memory.py`: module-level `awareness = Awareness()`; `handle_scene(scene: dict | None, frame: str | None) -> None` (feeds notes if active, calls `awareness.set_busy`, logs `loggable` as an `event` memory, and on an offer calls `push_card("offer", "Take notes?", offer.text, ref=offer.id, frame=frame)` then `voice.speak(offer.text)`); `answer_offer(offer_id: int, accept: bool) -> dict` (`KeyError` when not pending; accept calls `notes.start`); `finish_notes() -> int | None` (stops the session, builds notes, adds kept action items as open `task` memories with task cards, adds a `notes` memory whose text is the summary, writes the full JSON to `DATA_DIR/notes/<id>.json`, pushes a `notes` card with `ref` = that id, returns the id); `get_notes(note_id: int) -> dict | None`.
- `process_frame` calls `handle_scene(seen.get("scene"), path.name)` first. `process_audio` (after noise check, before `voice.heard`): if an offer is pending and `parse_yes_no(text)` is not `"unclear"`, call `answer_offer`; if notes are active and the text matches `\b(stop|end|finish)\s+(the\s+)?notes\b`, call `finish_notes`; otherwise when notes are active also `notes.add_line(text)` (in addition to the existing `_transcript` handling). `on_scene` returning True triggers `finish_notes`.
- `VISION_PROMPT` scene rules: `type` is `meeting` (people gathered or a conference room), `slides` (a screen or projector showing slides), `whiteboard`, `card` (business card), `menu`, or `none`; `confidence` 0 to 1; `text` is the headline/slide text exactly as readable, else `""`. Tell the model to prefer `none` when unsure.

- [ ] **Step 1: Write failing tests** with `memory.add`, `memory.push_card`'s speaker (`voice.speak`) and `models.*` monkeypatched: `test_three_meeting_frames_push_one_offer_card_and_speak_once`, `test_accept_starts_notes_and_decline_does_not`, `test_spoken_yes_answers_pending_offer`, `test_spoken_maybe_does_not`, `test_stop_notes_phrase_finishes_and_pushes_notes_card`, `test_finish_adds_only_verified_tasks`, `test_frame_without_scene_key_does_not_crash`, `test_answer_offer_unknown_raises_keyerror`.
- [ ] **Step 2: Run** `python -m pytest test_scene_flow.py -q`; expect failures.
- [ ] **Step 3: Implement** the interfaces above. Reset module state in a test fixture by re-creating `memory.awareness` and calling `notes.stop()`.
- [ ] **Step 4: Run** the whole suite `python -m pytest -q`; expect pass (existing `test_memory.py` still green).
- [ ] **Step 5: Commit** `feat(scene): scene awareness flow wired into memory`.

### Task 4: Server routes and dev hook

**Files:**
- Modify: `src/backend/server.py` (new routes next to the payments routes; update the route-prefix tuple at the request logger so `/offers`, `/notes`, `/dev` are logged)
- Test: `src/backend/test_server_scene.py`

**Interfaces:**
- Consumes: `memory.answer_offer`, `memory.finish_notes`, `memory.get_notes`, `memory.handle_scene`, `notes.add_line`.
- Produces: `POST /offers/{id}/accept` and `/decline` (200 `{"offer": id, "accepted": bool}`; 409 on `KeyError`), `POST /notes/stop` (200 `{"notes": id}`; 409 if no session), `GET /notes/{id}` (404 if unknown), `POST /dev/scene` (JSON `{type, confidence, text}`; 404 unless `SCENE_DEV=1`), `POST /dev/line` (JSON `{text}`, same gate).

- [ ] **Step 1: Write failing tests** (TestClient, memory functions monkeypatched): `test_offer_accept_decline_and_409`, `test_notes_stop_409_without_session`, `test_notes_get_404`, `test_dev_routes_404_without_flag`, `test_dev_scene_feeds_memory_when_flag_set`.
- [ ] **Step 2: Run** `python -m pytest test_server_scene.py -q`; expect failures.
- [ ] **Step 3: Implement** the routes; blocking calls go through `run_in_threadpool` like the payments routes.
- [ ] **Step 4: Run** `python -m pytest -q`; expect all pass.
- [ ] **Step 5: Commit** `feat(scene): offer, notes and dev routes`.

### Task 5: Glasses app cards

**Files:**
- Modify: `src/glasses/app.js` (`spotlight`, `home`, click handler around line 128-145)

**Interfaces:**
- Consumes: routes from Task 4; card kinds `offer` (ref = offer id) and `notes` (ref = notes id).

- [ ] **Step 1:** In `spotlight`, an undismissed `offer` card takes priority right after a pending payment.
- [ ] **Step 2:** In `home`, render `offer` as a card with eyebrow `"Scene noticed"`, the card body, and buttons `data-act="offer-yes"` (primary, "Yes, take notes") and `data-act="offer-no"` ("Not now"), both carrying `data-id` and `data-card`. Render `notes` as an answer-style card titled `"Meeting notes"` with the body and an OK dismiss.
- [ ] **Step 3:** In the click handler, `offer-yes`/`offer-no` POST `/offers/{id}/accept` or `/decline`, dismiss the card, and set `state.note` to `"Taking notes."` or `""`; swallow a 409 (offer already answered).
- [ ] **Step 4: Verify** by running the backend with `SCENE_DEV=1`, posting three `meeting` scenes to `/dev/scene`, opening `/glasses/`, and confirming the offer card appears and Yes starts a session (`POST /notes/stop` then returns a notes id).
- [ ] **Step 5: Commit** `feat(glasses): offer and notes cards`.

### Task 6: README

**Files:**
- Modify: `README.md` (short "Scene awareness" section: what triggers an offer, the `SCENE_DEV` demo recipe with the three curl calls, thresholds and where to tune them).

- [ ] **Step 1:** Write the section; **Step 2:** Commit `docs: scene awareness`.
