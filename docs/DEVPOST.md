# Scene: Devpost draft

**Tagline:** Memory for whatever camera you wear. It remembers where you left things, what you promised, and who owes you, and acts only when you pinch.

## Inspiration
Everyone loses keys, forgets what they told someone they'd do, and never collects the $12 from lunch.
Smart glasses already see and hear all of it. We wanted them to *remember* it and help, without
doing anything behind your back.

## What it does
- **Remembers where things are.** Gemini 4 Hydrogen looks at every frame from the glasses and keeps
  where your keys, wallet, laptop are. "Hey Scene, where are my keys?" is answered out loud with the photo.
- **Turns conversation into follow-through.** "Can you send me the deck by four?" becomes a task;
  Follow-Through drafts the email or calendar event and proves it with the exact quote.
- **Collects what you're owed, safely.** It reads the receipt, hears "I'll pay you back half", and
  *proposes* a $12 request. Nothing reaches Stripe until you pinch Approve on the glasses; your friend
  scans a QR and pays.
- **Notices the situation.** In a meeting or looking at slides, Scene offers to take notes and returns a
  summary, decisions, slides, and action items, plus a drafted follow-up email and calendar event.
- **An agent that does errands.** "Hey Scene, place an order at McDonald's" runs a real tool-using agent:
  it finds the nearest McDonald's (Gemini with Google Maps), asks you to confirm it, reads that location's
  live menu and prices (Gemini with Google Search), builds the cart from what you say, and asks again with an
  order card. One pinch creates a Stripe Checkout (test mode) and a live order page walks received, preparing,
  ready. The same agent handles "email Maya", "find a time to meet with Maya" (sends your Calendly link),
  "tell Maya on Slack I'm late", "put the dentist on my calendar Friday" and "search up the best GoPros".
  Every one is a draft you approve first; the model only ever supplies a name, and the address comes from your
  contacts and an allowlist.
- **On the lens, with the Neural Band.** A Ray-Ban Display web app shows what needs you; the band moves
  focus and pinches to act. Every action, even asking questions, works without voice.
- **Open to any camera.** Two HTTP calls are the whole device API; a 60-line script turns a webcam or
  Raspberry Pi into a Scene device. Meta Ray-Ban Display is our first integration; open-source glasses
  and camera necklaces like Omi are next.

## How we built it
- **Glasses → phone:** Meta Wearables Device Access Toolkit (iOS, Swift) streams camera frames; the
  iPhone app (SwiftUI) uploads frames and audio to the laptop and is the home screen for Today, Tasks,
  Things, and approvals with a Stripe QR.
- **On the glasses:** a Meta Ray-Ban Display Web App (vanilla JS, 600×600, ~20 KB), served by our backend
  through an HTTPS tunnel, driven by the Neural Band.
- **Backend (FastAPI):** Gemini 4 Hydrogen on every frame (objects, places, receipts, scene type);
  OpenAI Whisper + GPT for transcription, tasks, money owed, and answers; embeddings in SQLite for
  semantic search; ElevenLabs for spoken answers; Stripe Checkout for payments and food orders.
- **Task agent:** a Gemini function-calling loop (find places, get menu, ask you, propose order, send email,
  Slack message, calendar event, web search). It can pause on a question and resume on your spoken answer.
  Place and menu lookups use Gemini's Google Maps and Google Search tools; email and calendar go through a
  Google Apps Script with a recipient allowlist; Slack through a bot with a channel allowlist.
- **Follow-Through:** a separate agent that extracts commitments with a strict schema, verifies every
  quote in code, and only executes after approval.

## Safety by design (code, not prompts)
- Every task, payment, and follow-up must carry a **verbatim quote** checked against the transcript;
  invented items are dropped or shown as "Couldn't verify" with no Approve button.
- The agent only **proposes** money; a human pinch approves. $50 per request, $100 per day, test keys
  only, idempotent so a double pinch can't double charge.
- The agent can only **draft**: email, Slack, calendar and orders wait for your pinch. Order lines must come from
  the menu it just fetched and totals are computed in code. Recipients are resolved from your contacts, never
  from model output, and the Apps Script refuses any address not on its allowlist.
- We are honest about what is simulated: the McDonald's kitchen is a demo page, and Stripe is test mode.
- Weak search matches are dropped: no memory beats a confident wrong one.
- The camera pauses whenever you're looking at the display.

## Challenges we ran into
- **The glasses won't share.** The Ray-Ban Display refuses a phone camera session while a glasses web
  app is open, so Scene has a capture mode and a review mode.
- **Silent mic.** While the camera streams, the glasses' Bluetooth mic delivers all-zero audio (Whisper
  "heard" only "you"). We measured it and moved audio to the iPhone mic.
- **Venue Wi-Fi blocks devices from talking to each other.** We put the backend behind a Cloudflare
  tunnel, which also gave the glasses the HTTPS they require.
- **Grounded Gemini calls were too slow for a live agent.** Maps and Search lookups took about 40 seconds on the
  big model, and the public OpenStreetMap servers timed out, so we moved lookups to a fast model (about 2 s),
  cache them per place and day, and keep the slow, more accurate model one setting away.
- **Switching AI providers broke search.** Old memories had Gemini embeddings; search now re-embeds
  them automatically.

## Accomplishments we're proud of
Real frames from real glasses reaching Gemini 4 Hydrogen, which placed the laptop "on the white desk in
front of the grey wall" correctly on the first try, and a pinch on the lens that runs a real approval.

## What we learned
On-device constraints (one experience at a time, one Bluetooth radio) shape the product more than the
models do. Approval gates and quote checks are what make an agent trustworthy enough to wear.

## What's next
Open-source smart glasses and camera necklaces (Omi) through the same two-call API, on-device redaction
of faces and screens, and shared memories for families and caregivers.

## Built with
Meta Ray-Ban Display, Meta Neural Band, Meta Wearables Device Access Toolkit, Meta Wearables Web Apps,
Gemini 4 Hydrogen (vision, Google Maps and Search grounding, function calling), OpenAI (GPT, Whisper, embeddings),
ElevenLabs, Stripe Checkout, Slack API, Google Apps Script (Gmail and Calendar), Calendly, Swift/SwiftUI,
SceneKit, Python, FastAPI, SQLite, Cloudflare Tunnel, ffmpeg.

## Tracks
Best Overall · Best Use of Gemini 4 · Best Economic Agent (Stripe) · Best ElevenLabs Project
