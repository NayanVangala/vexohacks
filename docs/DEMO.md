# Scene demo (live: 2 minutes core + 90 seconds of agent add-ons)

One person wears the glasses (**Wearer**), one plays the friend (**Friend**). Laptop on the table,
screen visible to judges. Scene's voice plays through the glasses by default; for a room, set
`SCENE_SPEAK_ON=both` in `src/backend/.env` so the laptop speaks too.

## Before you go up (10 min)

```bash
cd ~/vexohacks/src/backend && ./run.sh                     # Terminal 1: Scene (:8001)
cd ~/vexohacks/src/followthrough && PATH="$HOME/vexohacks/src/backend/.venv/bin:$PATH" ./run.sh   # Terminal 2 (:8002)
pgrep -fl cloudflared                                      # tunnel alive? same URL as in Meta AI
curl -s localhost:8001/stats | head -c 60; echo            # Scene answers
curl -s localhost:8001/inbox; echo                         # [] when nothing is waiting
```

- [ ] Run `scripts/preflight.sh`: it checks every key, every service, the tunnel and Slack/Stripe/Gemini without printing a
      secret, and ends with "N fail, N warn". Fix every FAIL before testing; read the WARNs.
- [ ] Glasses charged and cool. Scene web app **closed** on the glasses (camera can't run while it's open).
- [ ] Phone: Scene app open, backend = tunnel URL, Glasses tab says **Streaming**, Microphone = **iPhone Microphone**.
- [ ] Camera light on. Frames climbing in the Glasses tab.
- [ ] Props on the table: keys, a receipt (any bill with a clear total), wallet, and a laptop screen showing a slide deck.
- [ ] Voice is **on** (the glasses' output mode is "voice" or "both", not "cards"). In "cards" mode Scene is silent.
- [ ] Optional reset for a clean count: stop Scene, `mv src/backend/data src/backend/data.bak`, start again.

Keys and settings (all in `src/backend/.env`, see `.env.example`; Follow-Through has its own `src/followthrough/.env`):

- [ ] `GEMINI_API_KEY` (required), `OPENAI_API_KEY`, `ELEVENLABS_API_KEY` and `ELEVENLABS_VOICE_ID`.
- [ ] `STRIPE_SECRET_KEY=sk_test_...` and `PUBLIC_BASE_URL=https://<tunnel>` (Stripe sends people back there; no trailing slash).
- [ ] `EXECUTOR_URL`, `EXECUTOR_TOKEN`, `DEMO_EMAIL_TO`, `CALENDAR_ACCOUNT` (real email and calendar). For emails to reach real
      contacts the Apps Script also needs the allowlist: `docs/APPS-SCRIPT-UPDATE.md`. Without it every email goes to the demo inbox.
- [ ] `FOLLOWTHROUGH_FORWARD_URL=http://localhost:8002`, or promises said out loud never reach Follow-ups.
- [ ] `SCHEDULING_LINK` (your Calendly), `SLACK_BOT_TOKEN` and `SLACK_CHANNELS` (`docs/SLACK-SETUP.md`), and your people in
      `src/backend/data/contacts.json` (copy `contacts.example.json`; add a `slack` member id to DM someone).
- [ ] `SCENE_NEED_FRAMES=6` (the phone sends a frame every 0.5 s, so the default 3 would offer notes after just 1.5 s).
- [ ] Location for "nearest McDonald's": the phone sends its GPS when allowed; otherwise set `SCENE_LOCATION=lat,lng` to a point near the venue.
- [ ] Rehearse the agent steps once before going up (steps 8 to 10): the first lookups warm the cache, and a run you have already done is instant.

## The core script (2 minutes)

| # | Do / say | What judges see |
|---|---|---|
| 1 | **Wearer:** "Scene is memory for whatever camera you wear. Today: Meta Ray-Ban Display." Looks at keys, puts them on the shelf. Looks at the receipt for 3 s. | Phone: Today count ticks up; Things shows *keys, on the shelf by the door*. |
| 2 | **Friend:** "Lunch was $24. I'll pay you back half." **Wearer:** "Can you also send me the pitch deck by four?" **Friend:** "Yeah, I'll send it by four." | Within ~30 s: a $12 request and a deck task/follow-up appear. |
| 3 | **Wearer:** "Hey Scene, where are my keys?" | Scene speaks the answer (ElevenLabs). Card on the phone with the photo. |
| 4 | Wearer opens **Scene** on the glasses (camera pauses: "the camera is off while you look"). Neural Band: pinch **Approve** on "$12 from your friend". | Phone pops a Stripe QR. Friend scans, pays with 4242 4242 4242 4242. Card flips to **Paid**. |
| 5 | Band: Follow-ups → "Send Nayan the deck", note the exact quote. Pinch **Approve**. Then open the one marked **Couldn't verify**: no Approve button. | "Nothing acts without a pinch, and nothing acts on words nobody said." |
| 6 | Band: **Ask** → "Does anyone owe me money?" (no voice) | Answer card on the lens. "Works with the band alone, for loud rooms or people who can't speak." |
| 7 | Laptop: `scripts/any-camera.sh` | Webcam frames join as a second device. "Any camera that can send a photo is a Scene device. Next: open-source glasses, Omi-style necklaces." |

## Agent add-ons (about 90 seconds, pick what fits)

The pitch for this part: **Scene doesn't just remember, it acts, and it asks first.** Every action below is a draft you
approve with a pinch or tap. While the camera is streaming, answer Scene's questions **out loud** ("yeah", "no", a short
answer); open the Scene web app on the glasses only to pinch Send, Add or Pay (that pauses the camera).

| # | Do / say | What judges see |
|---|---|---|
| 8 | **Notes from what it sees.** Wearer looks at the slide deck on the laptop for ~10 s. Scene says: "Looks like you're sitting in on a meeting. Want me to take notes?" Wearer: "Yeah." Friend talks: "Let's do the demo sync Friday at five." Wearer: "I'll send you the deck by tonight." Wearer: "Stop notes." | A "Taking notes" card with a slide count. After "stop notes": a notes card (summary, decisions, slides) plus an **Email** draft ("Send the deck") and an **Add to calendar** draft ("Demo sync, Fri 5:00 PM"). Phone: Notes tab; Needs you list. Pinch **Send** and **Add it**: the email lands in the inbox, the event on the calendar. |
| 9 | **Do-it-for-me requests.** "Hey Scene, find a time to meet with Maya." Then: "Hey Scene, tell Maya on Slack I'm running ten minutes late." Then: "Hey Scene, search up the best GoPro cameras right now." | Card: **Email Maya**, `To: maya@...`, with your Calendly link in the body. Card: **Slack message to Maya**. Scene speaks a one-sentence answer for the search; the card has sources. Say what is on screen: "each of these is a draft; nothing leaves until I pinch Send." Pinch Send on one and show it arrive. |
| 10 | **Agentic ordering.** "Hey Scene, can you place an order at McDonald's?" Scene: "On it..." then "The nearest McDonald's is on Sutter Street, 0.1 miles. Want to order from there?" Wearer: "Yes." Scene: "What would you like?" Wearer: "A Big Mac and a small fries." | An order card with items and total (prices from the web, labeled "may differ at the store"). Pinch **Pay**: Stripe Checkout opens on the phone (test card 4242...). The order page steps **received, preparing, ready** ("Demo kitchen"). Say: "It looked up the real nearest McDonald's and menu live, confirmed with me twice, and Stripe took the payment in test mode." |

Be honest on stage about what is simulated: the McDonald's kitchen (the order page says "Demo kitchen"), Stripe test mode, and
that email goes to the demo inbox unless the contact is on the Apps Script allowlist.

## If something breaks

| Problem | Do this |
|---|---|
| Camera light won't come on | Close Scene on the glasses. Stop/Start in the phone app. Still no: glasses too hot, put them in the case 5 min and demo from the phone + `SOURCE=test scripts/any-camera.sh`. |
| "hey scene" doesn't answer | Use the band: Ask → preset question. Or type it on the phone's Today tab. Check the output mode is not "cards" (silent). |
| Scene is silent but cards appear | Output mode is "cards", or `SCENE_SPEAK_ON=glasses` and the phone isn't playing it: set `SCENE_SPEAK_ON=both` and restart. |
| No Stripe key / QR | Show the proposal and the "Payments need a Stripe test key" note; the approval gate is the point. |
| Glasses web app grey | Re-open it from the glasses app list; check `tail src/backend/data/glasses.log` for its load line. |
| Tunnel URL changed | Update it in the phone app (Glasses tab), Meta AI (Web apps), and `PUBLIC_BASE_URL` in `.env` (restart the backend). |
| No "take notes" offer | Frames aren't arriving (check `/stats`), or the screen isn't clear. Use the dev route: `SCENE_DEV=1`, then `for i in 1 2 3; do curl -s -X POST :8001/dev/scene -H 'content-type: application/json' -d '{"type":"meeting","confidence":0.9}'; done`. Then `curl -s -X POST :8001/dev/line -H 'content-type: application/json' -d '{"text":"Nayan: lets do the demo sync Friday at five"}'` and `curl -s -X POST :8001/notes/stop`. |
| Offer fires too early or on the wrong scene | Raise `SCENE_NEED_FRAMES` (6 to 10) and restart. |
| Email says "not on the allowlist" or lands in the demo inbox | The contact isn't in the Apps Script `ALLOWED_TO` list, or the script wasn't redeployed: `docs/APPS-SCRIPT-UPDATE.md`. It is safe: nothing goes to the wrong person. |
| Slack says "not_in_channel" or "missing_scope" | `/invite @Scene` in a private channel, or reinstall the Slack app with the scopes in `docs/SLACK-SETUP.md`. |
| Order agent finds a far-away McDonald's | No GPS arrived. Set `SCENE_LOCATION=lat,lng` near the venue and restart, or answer "where are you?" aloud. |
| Order agent is slow or says it couldn't | Gemini hiccup: say it again (cached results are instant). Fallback: show the saved run from the dry run, or use `/dev/say` (below). |
| Whole agent flow without a mic | `SCENE_DEV=1`, then `curl -s -X POST :8001/dev/location -d 'lat=37.7879&lng=-122.4075'` and `curl -s -X POST :8001/dev/say -H 'content-type: application/json' -d '{"text":"can you place an order at mcdonalds"}'`, then `/dev/say` "yes" and "a big mac and a small fries". |
| Spoken yes/no isn't heard | Pinch Yes/No in the glasses app (Needs you), or `curl -s -X POST :8001/agent/reply -d text=yes`. |

## Video shot list (for Devpost, ~2 min)

1. 0:00 Hook: POV through the glasses, keys going on a shelf. Text: "You'll forget this. Scene won't."
2. 0:10 Friend says "I'll pay you back half". Cut to the phone: $12 request appears.
3. 0:25 "Hey Scene, where are my keys?" + spoken answer + the photo.
4. 0:40 On the lens: pinch Approve → phone QR → friend pays → Paid.
5. 0:55 Follow-Through: deck promise with its quote, pinch Approve; the unverifiable one has no button.
6. 1:05 Meeting: Scene sees the slides and asks "Want me to take notes?"; notes with action items; the email and the calendar event it drafted, pinch Send.
7. 1:20 "Hey Scene, tell Maya on Slack I'm late" and "find a time to meet with Maya": drafts, then Send.
8. 1:30 "Hey Scene, order from McDonald's": it finds the nearest one, asks, builds the order, pinch Pay, Stripe, order tracker to Ready.
9. 1:50 Webcam joins as a second device. "Any camera you wear."
10. 2:00 Architecture slide: glasses → phone → laptop; Gemini 4 Hydrogen / OpenAI / Stripe / ElevenLabs / Slack. "It remembers, it acts, and it always asks first."
