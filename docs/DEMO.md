# Scene demo (2 minutes, live)

One person wears the glasses (**Wearer**), one plays the friend (**Friend**). Laptop on the table,
screen visible to judges, sound on (answers are spoken from the laptop).

## Before you go up (5 min)

```bash
cd ~/vexohacks/src/backend && ./run.sh                     # Terminal 1: Scene (:8001)
cd ~/vexohacks/src/followthrough && PATH="$HOME/vexohacks/src/backend/.venv/bin:$PATH" ./run.sh   # Terminal 2 (:8002)
pgrep -fl cloudflared                                      # tunnel alive? same URL as in Meta AI
curl -s localhost:8001/stats | head -c 60; echo            # Scene answers
```

- [ ] Glasses charged and cool. Scene web app **closed** on the glasses (camera can't run while it's open).
- [ ] Phone: Scene app open, backend = tunnel URL, Glasses tab says **Streaming**, Microphone = **iPhone Microphone**.
- [ ] Camera light on. Frames climbing in the Glasses tab.
- [ ] Props on the table: keys, a receipt (any bill with a clear total), wallet.
- [ ] Optional reset for a clean count: stop Scene, `mv src/backend/data src/backend/data.bak`, start again.

## The script

| # | Do / say | What judges see |
|---|---|---|
| 1 | **Wearer:** "Scene is memory for whatever camera you wear. Today: Meta Ray-Ban Display." Looks at keys, puts them on the shelf. Looks at the receipt for 3 s. | Phone: Today count ticks up; Things shows *keys, on the shelf by the door*. |
| 2 | **Friend:** "Lunch was $24. I'll pay you back half." **Wearer:** "Can you also send me the pitch deck by four?" **Friend:** "Yeah, I'll send it by four." | Within ~30 s: a $12 request and a deck task/follow-up appear. |
| 3 | **Wearer:** "Hey Scene, where are my keys?" | Laptop speaks the answer (ElevenLabs). Card on the phone with the photo. |
| 4 | Wearer opens **Scene** on the glasses (camera pauses: "the camera is off while you look"). Neural Band: pinch **Approve** on "$12 from your friend". | Phone pops a Stripe QR. Friend scans, pays with 4242 4242 4242 4242. Card flips to **Paid**. |
| 5 | Band: Follow-ups → "Send Nayan the deck", note the exact quote. Pinch **Approve**. Then open the one marked **Couldn't verify**: no Approve button. | "Nothing acts without a pinch, and nothing acts on words nobody said." |
| 6 | Band: **Ask** → "Does anyone owe me money?" (no voice) | Answer card on the lens. "Works with the band alone, for loud rooms or people who can't speak." |
| 7 | Laptop: `scripts/any-camera.sh` | Webcam frames join as a second device. "Any camera that can send a photo is a Scene device. Next: open-source glasses, Omi-style necklaces." |

Optional if time: point the glasses at a slide deck for ~10 s, Scene asks "Want me to take notes?",
say "yeah", talk for a bit, then `curl -s -X POST :8001/notes/stop` and show the notes.

## If something breaks

| Problem | Do this |
|---|---|
| Camera light won't come on | Close Scene on the glasses. Stop/Start in the phone app. Still no: glasses too hot, put them in the case 5 min and demo from the phone + `SOURCE=test scripts/any-camera.sh`. |
| "hey scene" doesn't answer | Use the band: Ask → preset question. Or type it on the phone's Today tab. |
| No Stripe key / QR | Show the proposal and the "Payments need a Stripe test key" note; the approval gate is the point. |
| Glasses web app grey | Re-open it from the glasses app list; check `tail src/backend/data/glasses.log` for its load line. |
| Tunnel URL changed | Update it in the phone app (Glasses tab) and Meta AI (Web apps). |

## Video shot list (for Devpost, ~2 min)

1. 0:00 Hook: POV through the glasses, keys going on a shelf. Text: "You'll forget this. Scene won't."
2. 0:10 Friend says "I'll pay you back half". Cut to the phone: $12 request appears.
3. 0:25 "Hey Scene, where are my keys?" + spoken answer + the photo.
4. 0:40 On the lens: pinch Approve → phone QR → friend pays → Paid.
5. 1:00 Follow-Through: deck promise with its quote, pinch Approve; the unverifiable one has no button.
6. 1:20 Meeting: Scene offers to take notes; notes with action items.
7. 1:35 Webcam joins as a second device. "Any camera you wear."
8. 1:50 Architecture slide: glasses → phone → laptop; Gemini 4 Hydrogen / OpenAI / Stripe / ElevenLabs.
