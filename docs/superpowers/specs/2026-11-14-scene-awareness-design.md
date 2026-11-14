# Scene awareness: the glasses notice the situation and offer to help

## Intent
Scene's glasses stream video and audio all day. Today every frame is read for objects, receipts and
a one-line activity, but nothing reacts to the *situation* the wearer is in. We want Scene to notice
"you just sat down in a meeting / are looking at slides" and offer, out loud and on the lens:
"Hey Srijan, looks like you're sitting in on a meeting. Want me to take notes?" A yes starts a note
session that uses both what is heard and what is seen (slides), and ends with notes plus action
items. This is the visual selling point of the demo (Meta Ray-Ban Display), on top of the audio-only
Follow-Through promises we already have.

## Goals
- Spot a meeting or slides within about 10 seconds of the frames showing it, with no extra model calls
  per frame.
- Offer once, politely, in the casual voice. Never nag.
- On yes, collect transcript and one frame per slide, then produce notes with the slides attached.
- Anything that becomes a task or a visible promise keeps the proof rule: no item without verbatim
  evidence (spoken words in the transcript, or text read off a saved slide).
- Demo-able without glasses: a dev hook injects a scene so the flow can be shown from a laptop.

## Non-goals
- No acting on whiteboards, business cards, menus. They are detected and stored as memories only
  (menus matter later for the Stripe ordering project, which is a separate design).
- No wiring of Follow-Through's calendar or email approval flow in this version. Action items become
  Scene tasks through the existing path.
- No local pre-filter or second model tier.

## Anti-goals (a failure even if it technically works)
- Offering again and again, or offering mid-sentence while another card is open.
- Notes that include invented items. A dropped item is better than a made-up one.

## Constraints
- Reuse the frame loop in `src/backend/memory.py` (a frame about every 2.5 s, Gemini via `models.see`,
  frame lane drops frames when behind). Cards go to the glasses through `push_card`; speech through
  `voice.speak`; approvals mirror the existing `/payments/{id}/approve` routes.
- Gemini model name is unconfirmed until a key is set (`GEMINI_MODEL`, default `gemini-4-hydrogen`).
- Hackathon: small, testable pieces; the trigger logic must be pure code so it can be unit tested.

## Approaches considered
- **A. Extend the per-frame vision call (chosen).** `see` also returns the scene type and readable
  text; a small state machine decides when to offer. No extra calls; as fast as Gemini.
- B. Cheap local trigger plus Gemini only on interesting frames. Faster and cheaper but more code and
  tuning; too risky for the time.
- C. Build in the standalone Follow-Through webcam app. Simple but skips the glasses.

## Design
**1. Scene in the vision call.** `VISION_PROMPT` gains
`"scene": {"type": "meeting|slides|whiteboard|card|menu|none", "confidence": 0-1, "text": str}`.
`text` is the readable headline or slide text, copied as seen. `process_frame` passes the scene to the
awareness module and, when the type is `slides`, to the active note session.

**2. `awareness.py` (new, pure logic).** `Awareness.feed(scene, frame, now)` returns an `Offer` or
`None`.
- A type counts only at confidence 0.6 or more. The same type must appear on 3 analyzed frames in a
  row (a different type or `none` resets the count).
- `meeting` and `slides` can offer "take notes". Other types are stored as an `event` memory
  ("Looking at a menu") at most once per 10 minutes and offer nothing.
- Cooldowns: 10 minutes after an offer is shown, 30 minutes after a decline, none while a note
  session is active or another offer is pending. An unanswered offer expires after 60 seconds.
- The offer text is casual, one fixed template per type.

**3. Offer flow.** The offer becomes a card (`kind: "offer"`, `ref` = offer id), is spoken with
`voice.speak`, and is answered by `POST /offers/{id}/accept` or `/decline` (glasses button or pinch).
While an offer is pending, a short audio transcript that is a clear yes or no also answers it (same
rule as the Follow-Through voice parser: a mumble or an ambiguous answer changes nothing).

**4. `notes.py` (new): the note session.**
- Start: begins collecting transcript lines with times, and key frames. A key frame is saved when the
  scene is `slides` or `whiteboard` and its `text` differs from the last saved one (normalized
  comparison), so a static slide is saved once.
- Stop: voice "stop notes", `POST /notes/stop`, or 10 minutes with no meeting or slides scene.
- Finish: one model call turns transcript plus slide texts into
  `{summary, decisions, action_items[{text, quote, source}], slides[{frame, text}]}`. Every action item
  must carry a verbatim `quote`, kept only if it appears in the transcript or in a saved slide's text
  (the existing `quoted()` rule extended to slide texts). Kept items become tasks and task cards.
  The notes are stored as a memory (`kind: "notes"`), pushed as a `notes` card, and readable at
  `GET /notes/{id}`.

**5. Demo hook.** With `SCENE_DEV=1`, `POST /dev/scene` injects a scene dict as if a frame had
returned it, so the whole flow can be shown without a camera.

**6. Tests.** Unit tests for `Awareness` (3-in-a-row, reset, cooldowns, expiry, decline), for slide
change detection, and for evidence checking against slide text. Server tests for the offer routes and
the dev hook with Gemini stubbed.

## Left to the builder
Exact thresholds (tune live), card layout on the glasses display, wording of the offer templates (keep
them casual), and where the notes endpoint output is rendered in the existing web app.
