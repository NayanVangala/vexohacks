# Verification

Status as of 2026-11-13. "Verified" means the command was run here and its output checked.

## Verified

| Check | Command | Result |
|---|---|---|
| Unit tests (AudioChunker) | `xcodebuild -scheme VexoBridge -destination 'platform=iOS Simulator,name=iPhone 17 Pro' test` | `Executed 12 tests, with 0 failures`, `TEST SUCCEEDED` |
| Debug build, device | `xcodebuild -scheme VexoBridge -configuration Debug -destination generic/platform=iOS -allowProvisioningUpdates build` | `BUILD SUCCEEDED`, signed `Apple Development … (4C36R4ZXC4)`, profile `iOS Team Provisioning Profile: com.vexohacks.bridge` |
| Release build, device | same with `-configuration Release` | `BUILD SUCCEEDED`, same signing |
| Debug and Release build, simulator | `-destination 'platform=iOS Simulator,…'` | `BUILD SUCCEEDED` |
| SDK pin | Package.resolved, pbxproj | `0.7.0`, `kind = exactVersion` |
| Built Info.plist | `plutil -extract MWDAT json` on the Release .app | `{"MetaAppID":"0","AppLinkURLScheme":"vexobridge://","TeamID":"6KRCDGYC5W","ClientToken":"0"}` |
| Receiver self-check | `cd src/backend && .venv/bin/python -m pytest -q` | `1 passed` |
| MockDeviceKit end to end, 2.5 min | see below | 62 frames / 153.3 s, median interval 2.52 s, max 2.54 s, 0 gaps > 5 s. 61 WAVs / 150 s, 2.50 s each, 152.5 s audio total. No errors, no recovery events. |

`-allowProvisioningUpdates` registered bundle ID `com.vexohacks.bridge` and a development profile on team `6KRCDGYC5W`.

### MockDeviceKit run (Simulator, iPhone 17 Pro, iOS 26)

```bash
# 1. HEVC feed. Use Apple's encoder. libx265 crashes the SDK (see "Found" below).
ffmpeg -f lavfi -i testsrc2=size=720x1280:rate=30:duration=60 \
  -c:v hevc_videotoolbox -b:v 2M -tag:v hvc1 -pix_fmt yuv420p build/mock_feed_vt.mov

# 2. Receiver
cd src/backend && .venv/bin/python server.py

# 3. App, unattended (Debug-only launch args)
U=<simulator udid>
xcrun simctl install $U build/dd/Build/Products/Debug-iphonesimulator/VexoBridge.app
xcrun simctl privacy $U grant microphone com.vexohacks.bridge
xcrun simctl launch --console-pty $U com.vexohacks.bridge \
  -autostartMock YES -backend_base_url http://localhost:8001 \
  -mockFeedPath "$PWD/build/mock_feed_vt.mov"

# 4. Summary
cd src/backend && .venv/bin/python check_run.py
```

Log from that run:
```
eligible — selector activeDevice: eb1c99421a3fe2e80b51027ff2fcb856
session state: started
stream started: raw medium @2fps
stream state: streaming
*** FIRST VIDEO FRAME — generation 1
frame size: 720x1280 (frame 1, generation 1)
HB gen=1 rx=4506 posted=60 | pcm=1500 audio_posted=59 | stream=streaming rec=true phase=active
```
The mock ignores the 2 fps request: rx climbs at the file's 30 fps. The 2.5 s throttle in `FramePipeline` is what sets the upload cadence. Audio in the Simulator is the Mac's built-in mic. The last 5 WAVs had RMS 6–64 (Int16), so they are real signal, not zeros.

### Found during verification

- **A libx265-encoded mock feed crashes the app.** The process aborts about 3 s after `streaming` with `[err] Aborting due to potential buffer overflow, writing size 2420 to destination 960 in: checked_memcpy`, inside MWDAT 0.7.0's native media code. No crash report is written. libx265 embeds an approximately 2 KB encoder-settings block in the stream. The same content encoded with `hevc_videotoolbox` streams cleanly. Cause inferred from this one A/B, not proven at byte level. Mock-only: real glasses do not feed the SDK x265 files.

## Not verified (needs hardware)

- Real glasses: registration, camera permission, `.streaming`, real frames on disk, HFP glasses mic, session auto-recovery, Activate photo.
- Activation photo path in the mock: not exercised, because no mock captured image was set.
- Whether glasses firmware still accepts SDK 0.7.0.

## Real-glasses run: exact steps

### Before

1. Work through the README checklist: Developer Mode ON, Meta AI installed and glasses linked, hotspot.
2. Laptop on the iPhone hotspot. Get the laptop address: `ipconfig getifaddr en0`.
3. Receiver: `cd ~/vexohacks/src/backend && rm -rf data && .venv/bin/python server.py`. Leave this terminal visible.
4. iPhone plugged into the Mac. Developer Mode ON on the iPhone (Settings ▸ Privacy & Security ▸ Developer Mode).
5. Xcode: open `src/ios/VexoBridge.xcodeproj`, scheme VexoBridge, choose your iPhone, Run (Debug build, so you get the heartbeat and run log). The first install may need Settings ▸ General ▸ VPN & Device Management ▸ trust the developer.

Logs: the Xcode console shows every `[vexo]` line. The app's on-screen log keeps the last 50.

### 1. Launch and backend

In the app, set Backend to `http://<laptop-ip>:8001` (or keep the `.local` default), then tap Save URL. Allow the Local Network prompt.

Expect: `backend reachable at http://…:8001`, and a `GET /stats` in the receiver terminal.

If instead you see `backend unreachable` → Settings ▸ Privacy & Security ▸ Local Network ▸ Vexo Bridge ON, check the IP, check the hotspot.

### 2. Registration and camera permission

Expect `registration: available — tap Connect…`, then tap **Connect glasses**. Meta AI opens; approve Vexo Bridge. Back in the app:
```
handled callback URL
registration: registered
camera permission: denied            (or notDetermined)
camera permission after request: granted
```
Meta AI opens a second time for the camera grant. The header should read `Reg: registered · Cam: granted`.

If `registration UNAVAILABLE` → Meta AI is missing, or Developer Mode is off (it resets after firmware updates).

If `camera permission: failed — will retry` → normal when the link is still connecting. It retries on the next Start.

### 3. Start: the line that decides everything

Tap **Start**.
```
eligible — selector activeDevice: <id>
probe on <name> [id <id>] — entry link: connected compat: compatible     ← must say compatible
session state: started
stream started: raw medium @2fps
stream state: streaming
*** FIRST VIDEO FRAME — generation 1
frame size: 504x896 (frame 1, generation 1)                                ← may later drop to 360x640: expected
*** HFP PORT FOUND: <glasses> — glasses mic is live (§11.3)
```
Note: the `probe on …` line is only logged when the device was not eligible at the instant Start ran. If eligibility was immediate, `compat` is not printed. That is fine as long as frames arrive.

Receiver terminal: `frame #N … B` about every 2.5 s, and `audio #N` every 2.5 s.

Failure signatures:
- `compat: sdkUpdateRequired` → firmware too new for 0.7.0. Stop. Fallback is SDK 1.x plus Wi-Fi entitlements (audit §0).
- `stream state: waitingForDevice` for 30 s, then `Device is not connected` → the 0.8.0-style failure. On 0.7.0 that would be new; capture the full log.
- `!! no eligible device after 10.0 seconds` plus its `!! cause:` lines → follow the `fix:` line printed under it.
- `no Bluetooth HFP port — using iPhone Microphone` → video is unaffected; audio is the phone mic.

### 4. Two continuous minutes

Leave it running for 2+ minutes in the foreground. Watch `HB gen=1 rx=… posted=…` every 15 s: `posted` should climb by about 6 per heartbeat. Then:
```bash
cd ~/vexohacks/src/backend && .venv/bin/python check_run.py
```
Pass: frames median about 2.5 s, `gaps >5s: 0` (or a few, explained by a logged event), frame sizes 504x896 or 360x640.

### 5. Activate

Tap **Activate**. Receiver: `ACTIVATE #1` then `ACTIVATION PHOTO #1 … B`. App: no `capturePhoto: …` failure line.

### 6. Forced drop and auto-recovery

Force the device to end the session. In order of preference: fold the glasses arms shut for about 10 s, then unfold. If that only pauses: phone Bluetooth OFF for 10 s, then ON.
```
session state: stopped
!! session ended by device — auto-recovery starting
recovery: attempt 1 in 2.0 seconds
...
recovery: session rebuilt on attempt N
stream state: streaming
*** FIRST VIDEO FRAME — generation 2
HB gen=2 rx=…                                   ← gen climbed, frames flowing again
```
The header shows RECONNECTING (amber) during recovery.

`session paused — holding, not restarting` is expected when the glasses are taken off. That is a pause, not a drop: put them back on, and tap the side if needed.

The backoff caps at 30 s per attempt and keeps trying while the bridge is Started.

### 7. Collect

Pull the full run log, then run `check_run.py` again:
```bash
xcrun devicectl list devices      # get <udid>
xcrun devicectl device copy from --device <udid> --domain-type appDataContainer \
  --domain-identifier com.vexohacks.bridge --source Documents/vexo-run.log --destination ./vexo-run.log
```
Send back: `vexo-run.log`, the `check_run.py` output, and the glasses firmware version from Meta AI ▸ device settings.
