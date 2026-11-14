# Scene

Memory for camera wearables. Your glasses (or any camera you wear) see and hear your day; Scene
remembers where you left things, what people asked you to do, and money you're owed, and acts on
it with a tap, a pinch, or your voice.

- **"Hey Scene, where are my keys?"** answered out loud, with the frame where they were last seen.
- **Tasks from conversation.** "Can you send me the deck by 5?" becomes a task, and closes itself
  when you later say "I sent the deck".
- **Money, safely.** Gemini reads the receipt, hears "I'll pay you back half", and *proposes* a
  $12 request. Nothing reaches Stripe until you approve it; then the other person scans a QR on
  your phone and pays.

Built at VexoHacks (Nov 14, 2026). The glasses-to-laptop streaming bridge was set up before the
event; memory, payments, voice, the glasses display, the phone app, and the open device API were
built at the hackathon.

## Devices: open by design

Scene doesn't care what device it talks to. Anything that can send a photo and a short audio clip
over HTTP is a Scene device:

```
POST /capture   multipart photo = one JPEG frame        every ~2.5 s
POST /capture   multipart audio = one WAV chunk         16 kHz mono, ~2.5 s
```

| Device | Status |
|---|---|
| **Meta Ray-Ban Display + Neural Band** | Working today (`src/ios/`): camera, mic, cards on the in-lens display, pinch to approve. This is our demo device. |
| **Any camera: laptop webcam, USB camera, Raspberry Pi, glasses dev kits** | Working today through `scripts/any-camera.sh` (ffmpeg + curl, about 60 lines). Point it at the server and it streams like the glasses do. |
| **Open-source smart glasses** | Next: same two calls from their companion app. No backend changes needed. |
| **Camera necklaces (e.g. Omi)** | Planned: same contract; a pendant camera is just another frame source. |

```bash
scripts/any-camera.sh                      # this Mac's webcam + mic becomes a Scene device
SOURCE=test scripts/any-camera.sh          # synthetic frames, no camera needed
INPUT="-f v4l2 -i /dev/video0" scripts/any-camera.sh   # Linux / Raspberry Pi camera
```

## On the glasses: Scene web app

`src/glasses/index.html` is a Meta Ray-Ban Display **Web App** (600×600, black is transparent,
D-pad focus + pinch to act, about 10 KB, no dependencies). The backend serves it at `/glasses/`, so
it shares an origin with the API. It shows what needs you (a payment to approve, a new task, the
latest answer) and your Tasks and Things.

**Works with the Meta Neural Band alone.** The band's D-pad gestures move focus and its pinch
selects, so every action works without voice: approve or dismiss a payment, mark a task done,
browse Recent, and **ask questions with a pinch** (preset questions built from what Scene has seen,
like "Where did I leave my wallet?" or "Does anyone owe me money?"). If the mic fails or the room
is loud, Scene still works.

Add it once on the phone: Meta AI → Devices → Display Glasses settings → App connections →
Web apps → Add a web app → `https://<your tunnel>/glasses/` (any public HTTPS URL to the backend).
The phone bridge then only streams camera and mic, and the glasses app draws the cards.

## Built to be accessible

- **Hands-free by default.** Ask out loud ("hey scene"), hear the answer spoken back, approve with
  a pinch. No phone screen needed for the everyday loop.
- **Voice is optional.** Everything, including asking questions, also works with the Neural Band
  alone, for loud rooms, a failed mic, or people who can't or don't want to speak.
- **For people who forget.** Where things are, what was promised, and what's owed, kept for you and
  searchable by meaning, not exact words.
- **Calm interface.** Large type, high-contrast numbers, one primary action per screen. All motion
  respects iOS Reduce Motion.
- **Bring your own hardware.** Not locked to one brand of glasses: any camera that can send a photo works.

## How it works

```
Ray-Ban Display ──Bluetooth──▶ iPhone app ──Wi-Fi──▶ laptop receiver   ◀── any camera (any-camera.sh)
  camera 1 frame / 2.5 s          VexoBridge            FastAPI (src/backend/)
  mic 2.5 s chunks                feed, tasks,          Gemini 4 Hydrogen: every frame
  display cards ◀── pinch ──      things, QR            OpenAI: speech, tasks, answers
                                                        SQLite + embeddings: search
                                                        Stripe: pinch-approved requests
```

| Sponsor | Job |
|---|---|
| **Gemini 4 Hydrogen** | Sees every frame in real time: objects and where they are, what you're doing, receipts |
| **OpenAI** | Whisper transcription, task and money extraction, embeddings, answers (Gemini covers all of this when no OpenAI key is set) |
| **Stripe** | Checkout links for money you're owed, only after a human pinch |
| **ElevenLabs** | Spoken answers to "hey scene" (macOS `say` without a key) |

### Safety rails (in code, not prompts)

- Every task, done item, and money item must carry a **verbatim quote** that is checked against the
  transcript. Anything the model invents is dropped.
- The agent can only **propose** payments. `POST /payments/{id}/approve` is called only by a pinch
  on the glasses or a tap on the phone.
- Caps: `PAY_MAX_CENTS` ($50 per request), `PAY_DAILY_CENTS` ($100 per day). Test keys only:
  `sk_live_` is refused. One Stripe idempotency key per proposal, so a double pinch can't double-charge.
- Every step (proposed, approved, rejected, paid) is written to memory, so "what money did I request
  today?" has an answer.
- Retrieval drops weak matches: no memory is better than a confident wrong one.

## Layout

```
src/ios/VexoBridge/        iPhone app (SwiftUI): Today, Tasks, Things, Glasses tabs
  Views/HomeView.swift       phone screens + payment QR
  Network/VexoAPI.swift      API types + MemoryStore (polls /feed, drives phone and glasses)
  Wearable/GlassesDisplay.swift   cards on the Ray-Ban Display, pinch buttons
  Wearable/WearablesManager.swift glasses session, camera stream, recovery
src/backend/               laptop backend (FastAPI)
  server.py                  /capture, /ask, /feed, /tasks, /items, /payments, /frames
  models.py                  Gemini 4 + OpenAI calls (plain REST)
  memory.py                  ingest lanes, SQLite store, semantic search, quote checks
  voice.py                   "hey scene" wake word, spoken answers
  payments.py                Stripe Checkout behind the approval gate
src/glasses/               Scene web app for the Ray-Ban Display (served at /glasses/)
src/followthrough/         Follow-Through agent + web dashboard (laptop web app)
scripts/                   start-scene.command (one-click start), any-camera.sh (any camera as a device),
                           render-icon.swift (app icon)
claude/                    Claude Code + ruflo workspace state
```

## Run

**Quick start:** double-click `scripts/start-scene.command` on the laptop, open the Scene app on the iPhone,
tap **Start**. The receiver advertises itself over Bonjour (`_scene._tcp`) and the app finds it,
so there's no IP to type.

Receiver by hand (laptop):

```bash
cd src/backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp .env.example .env        # add GEMINI_API_KEY; OpenAI, Stripe (sk_test_), ElevenLabs optional
./run.sh                    # 0.0.0.0:8001, prints which keys and gemini-4 models are live
.venv/bin/python -m pytest -q
```

iPhone: build and run the `VexoBridge` scheme. In the **Glasses** tab set the backend to
`http://<laptop-ip>:8001` (`ipconfig getifaddr en0`), tap **Connect glasses**, approve in Meta AI,
then **Start** on the Now tab.

API:

| Method | Path | |
|---|---|---|
| POST | `/capture` | multipart `photo` (JPEG) or `audio` (WAV), from the phone |
| POST | `/ask` | form `text` or WAV `audio` → answer + frame |
| GET | `/feed?after=N` | cards for the phone and glasses |
| GET | `/tasks` · POST `/tasks/{id}/done` | open tasks |
| GET | `/items` | last place each object was seen |
| GET | `/payments` · POST `/payments/{id}/approve` · `/reject` · GET `/payments/{id}` | the money flow |
| GET | `/frames/<name>` | saved frames (set `PUBLIC_BASE_URL` to an HTTPS tunnel for the glasses) |

## Before a real-glasses run

- [ ] **Developer Mode ON** for the glasses in the Meta AI app (it turns off after firmware updates).
- [ ] **Local Network allowed** for Scene in iPhone Settings ▸ Privacy & Security.
- [ ] **Hotspot, not venue Wi-Fi**, if the phone can't reach the laptop (venue Wi-Fi often blocks it).
- [ ] Keep the app in the foreground; there is no background capture.

Load-bearing settings (retest on real glasses before changing): Meta Wearables DAT SDK **exactly
0.7.0** (camera over Bluetooth; `DAMEnabled` in Info.plist for the display), `.raw` codec, `.medium`
at 2 fps, 1 frame / 2.5 s upload, recovery backoff 2 → 30 s (glasses end sessions on their own).
