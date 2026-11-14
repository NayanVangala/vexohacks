# vexo-bridge

Meta Ray-Ban glasses → iPhone → laptop. Video frames and glasses-mic audio, over local Wi-Fi, to a receiver that saves them to disk. That is all it does.

The bridge plumbing is reused from the **Findr** project (github.com/NayanVangala/findr, `src/backend/ios-bridge`), which streamed real glasses frames and audio to a laptop. It is copied, not rewritten, and was set up the day before VexoHacks; the memory, payments, voice, and glasses-display work was built at the hackathon. Comments citing `CLAUDE-ios.md §x` refer to Findr's design doc, which is not in this repo.

Why the copy and not the Scene bridge: see `docs/BRIDGE-AUDIT.md`.

## Layout

```
ios/VexoBridge.xcodeproj   iOS app (SwiftUI), target VexoBridge, tests VexoBridgeTests
ios/VexoBridge/            WearablesManager, BridgeController, BridgeState, ImageEncoder,
                           AudioChunker, GlassesAudioRecorder, BackendClient, AudioPlayer, views
receiver/                  FastAPI receiver: /capture, /activate, /stats. Saves files, counts.
receiver/memory.py         Glasses memory: objects, tasks, events in sqlite + semantic search
receiver/models.py         Model calls: Gemini (frames), OpenAI (speech, tasks, embeddings, answers)
docs/BRIDGE-AUDIT.md       Findr vs Scene audit
claude/                    Claude Code + ruflo plugin state (.claude, .claude-flow, .swarm, ruvector.db)
```

## Memory

Set `GEMINI_API_KEY` and `OPENAI_API_KEY`, then `cd receiver && .venv/bin/python server.py`.
Without keys the receiver only saves files. Endpoints: `POST /ask` (form `text` or WAV `audio`),
`GET /tasks`, `POST /tasks/{id}/done`, `GET /items`, `GET /frames/<name>`.
Set `PUBLIC_BASE_URL` to an HTTPS tunnel so the glasses display can load thumbnails.

## Payments (Stripe, test mode)

Gemini reads receipts; OpenAI hears "I'll pay you back half". The agent only *proposes*
a request (`GET /payments`). Nothing reaches Stripe until the wearer pinches Approve on the
glasses (`POST /payments/{id}/approve`), which creates a Checkout link; `GET /payments/{id}`
polls until paid. Rails in code: `PAY_MAX_CENTS` (default $50), `PAY_DAILY_CENTS` ($100),
test keys only (`sk_live_` refused), Stripe idempotency key per proposal. Set `STRIPE_SECRET_KEY`.

## Load-bearing constants (do not change without retesting on real glasses)

| What | Value | Where | Why |
|---|---|---|---|
| SDK | `meta-wearables-dat-ios` **exactly 0.7.0** | pbxproj `exactVersion`, Package.resolved | 0.7.0 streams over Bluetooth. 0.8.0+ moves the camera to the glasses' own Wi-Fi (SoftAP) and needs extra entitlements (audit §0). |
| Codec | `.raw` | `WearablesManager.videoCodec` | `.hvc1` kills the session and the HFP link |
| Resolution / fps | `.medium` / 2 fps | `WearablesManager` | Resolution and fps share one Bluetooth budget |
| Upload cadence | 1 frame / 2.5 s, 2.5 s WAV chunks (16 kHz mono) | `BridgeController.frameIntervalSeconds`, `AudioChunker` | |
| Recovery backoff | 2, 5, 10, 20, then 30 s | `WearablesManager.recoveryDelay` | Glasses end sessions on their own (37 s to 11+ min) |

What was stripped from Findr: the voice query path (`/ask-audio`, 5 s query chunker, answer playback), the panic button (`/panic`), and the dormant photo-polling experiment. Everything else is Findr's code with renamed identifiers.

## Checklist before a real-glasses run

- [ ] **Developer Mode ON** for these glasses in the Meta AI app. It turns itself off after glasses firmware updates. Re-check after every update.
- [ ] **Meta AI app installed** on the iPhone, glasses paired and linked in it. Registration needs internet.
- [ ] **Local Network allowed**: iPhone Settings ▸ Privacy & Security ▸ Local Network ▸ Vexo Bridge ON. The app fires a `GET /stats` at launch to raise the prompt. It re-prompts on a new network.
- [ ] **Hotspot, not venue Wi-Fi.** Put the laptop on the iPhone's Personal Hotspot (or the reverse). Venue Wi-Fi often blocks client-to-client traffic and mDNS.
- [ ] **Team ID.** Set to `6KRCDGYC5W` (same team as Findr) on the VexoBridge target. It feeds `MWDAT.TeamID` in Info.plist through `$(DEVELOPMENT_TEAM)`. To change it: target VexoBridge ▸ Signing & Capabilities ▸ Team.
- [ ] iPhone auto-lock: the app disables the idle timer while running, but keep the app in the foreground. There is no background capture.

## Run

Receiver (laptop):

```bash
cd receiver
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python server.py          # 0.0.0.0:8001
.venv/bin/python -m pytest -q       # self-check
curl localhost:8001/stats
```

Files land in `receiver/data/{frames,activation,audio}/` (gitignored).

App: build and run the `VexoBridge` scheme on the iPhone. In the app:
1. Set **Backend** to the laptop, then tap Save URL. The default is `http://Nayans-MacBook-Air.local:8001`. Use `http://<laptop-ip>:8001` if mDNS fails (`ipconfig getifaddr en0`).
2. **Connect glasses** (only shown while registration is `available`), then approve in Meta AI. Camera permission is requested automatically after that.
3. **Start**. Watch the log for `stream state: streaming` and `*** FIRST VIDEO FRAME`. The receiver prints one line per frame and per WAV.
4. **Activate** sends `POST /activate` plus one full-resolution photo tagged `activation`.

Debug builds also have **Use MockDeviceKit** (the phone's back camera stands in for the glasses) and write a durable log to the app's `Documents/vexo-run.log`:

```bash
xcrun devicectl device copy from --device <udid> --domain-type appDataContainer \
  --domain-identifier com.vexohacks.bridge --source Documents/vexo-run.log --destination ./vexo-run.log
```

## If the glasses reject SDK 0.7.0

On the first real run, the eligibility log prints `compat: <value>` per device. If it says `sdkUpdateRequired`, the glasses firmware is too new for 0.7.0. The fallback is SDK 1.x. That needs the `Access Wi-Fi Information` and `Hotspot Configuration` capabilities on the target and the App ID, `NSBonjourServices`, and Local Network allowed, plus the 1.x API (`addCamera`, synchronous `Stream.start()`). That path has not been verified on our hardware.
