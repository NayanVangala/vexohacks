# Bridge audit: Findr (working) vs Scene (never streamed)

Read-only audit, 2026-11-13. Nothing in `~/findr_ios` or `~/scene-*` was modified.

Paths, abbreviated:
- `F/` = `~/findr_ios/src/backend/ios-bridge/FindrBridge/`
- `FP` = `~/findr_ios/src/backend/ios-bridge/FindrBridge.xcodeproj/`
- `S/` = `~/scene-ios-bridge/SceneBridge/`
- `SP` = `~/scene-ios-bridge/SceneBridge.xcodeproj/`
- `DD` = `~/Library/Developer/Xcode/DerivedData/` (the SDK checkouts both projects actually built against)

Scene's symptom, from its own run sheet: `~/scene-fix/glasses-runsheet.md:60`, "stream can report streaming state while no frame callback ever fires… open issue, NO workaround". Scene has no log file, no frame counter and no session error subscription, so there is no captured log of a failing Scene run. Everything below compares code, not logs.

---

## Ranked findings (most likely cause of Scene's dead stream first)

### 0. Revision, 2026-11-13: missing Wi-Fi entitlements on a SoftAP-transport SDK. HIGHEST

Source: Meta's answers on the DAT iOS GitHub (issue #240, and the closed discussion #255 on Findr's own 0.8.0 report). Since 0.8.0, camera frames do not travel over Bluetooth. The SDK first has iOS join the glasses' own Wi-Fi network (SoftAP). If that join never happens, the stream sits in `waitingForDevice`. After about 30 s the SDK's link-setup timeout fires, and that surfaces as `deviceNotConnected`. The join needs two app entitlements, also enabled on the App ID:
- `com.apple.developer.networking.wifi-info` (Access Wi-Fi Information)
- `com.apple.developer.networking.HotspotConfiguration` (Hotspot Configuration)

You can tell whether the join was attempted by watching for an iOS "Join <glasses network>" prompt during the 30 s window. If no prompt appears, the join never fired.

Evidence:
- Scene: SDK 1.0.1 (SoftAP transport). There is no `*.entitlements` file in `~/scene-ios-bridge`, and no `CODE_SIGN_ENTITLEMENTS` in `SP/project.pbxproj`. Its own README predicted this: "If the camera stream dies at ~30s: the DAT camera path has historically needed SoftAP Wi-Fi entitlements" (`~/scene-ios-bridge/README.md:52-54`). It also has `NSBonjourServices` empty (`S/Info.plist:40-41`) and no `NSAllowsLocalNetworking`, which are both socket-layer gates after the join.
- Findr: also no entitlements file. That was fine on 0.7.0, which streams over Bluetooth, and is the same reason Findr's 0.8.0 test failed. So Findr's "0.8.0 regression" (`MWDAT-STREAM-BUG-REPORT.md`) was really a transport change plus missing entitlements, not an SDK bug.

So finding 1 below is reframed: the version matters because it picks the transport. 0.7.0 means Bluetooth with no entitlements. 0.8.0 and later means SoftAP with entitlements. Even with entitlements, #240 shows a later failure at the local-network/socket layer (L2CAP PSM 129). So entitlements are necessary, not proven sufficient.

Implication for Vexo bridge: stay on exactly 0.7.0 (Bluetooth, proven). The risk is that current glasses firmware rejects 0.7.0: watch for `compat: sdkUpdateRequired` in the eligibility log. The fallback is 1.x plus both entitlements, `NSBonjourServices`, and Local Network allowed.

### 1. SDK version: Scene is on 1.0.1, the same post-0.7.0 API shape that broke Findr. HIGH (see 0: the real difference is transport)

| | Findr | Scene |
|---|---|---|
| Package.resolved | `0.7.0`, rev `c40db39` (`FP/project.xcworkspace/xcshareddata/swiftpm/Package.resolved`) | `1.0.1`, rev `0bd3692` (`SP/project.xcworkspace/xcshareddata/swiftpm/Package.resolved`) |
| pbxproj requirement | `kind = exactVersion; version = 0.7.0;` (`FP/project.pbxproj:423-424`) | `kind = upToNextMajorVersion; minimumVersion = 1.0.1;` (`SP/project.pbxproj:311-312`) |
| Checkout actually built | `git describe` = `0.7.0` (DD/FindrBridge-*) | `git describe` = `1.0.1` (DD/SceneBridge-*) |

Findr's own write-up (`~/findr_ios/docs/MWDAT-STREAM-BUG-REPORT.md:1-39`) documents that 0.8.0 on the same phone, glasses and firmware never leaves `waitingForDevice` and dies at exactly 30 s, while 0.7.0 streams. It points at two interface changes: `addCapability` removed, and `Stream.start()/stop()` going from `async` to synchronous (`MWDAT-STREAM-BUG-REPORT.md:212-216`).

1.0.1 keeps the 0.8.0 shape, and changes it further:
- 0.7.0: `final public func start() async` (DD/FindrBridge-*/…/MWDATCamera.swiftmodule/arm64-apple-ios.swiftinterface:295)
- 1.0.1: `final public func start()`, synchronous (DD/SceneBridge-*/…/MWDATCamera.swiftmodule/arm64-apple-ios.swiftinterface:235, :349)
- 1.0.1 replaces `addStream(config:)` with `addCamera(config:) -> Camera?`, and the stream becomes `camera.stream` (same file:39; used at `S/StreamViewModel.swift:89-96`).

Can't tell whether 1.0.1 fixed the 0.8.0 regression. Nobody has run Findr's working code on 1.0.1 or Scene's code on 0.7.0. But Scene is the only one of the two that has never streamed, and it is the only one on the post-0.7.0 line. That makes this the top suspect. The fix is cheap: pin exactly 0.7.0, which is the Phase 1 plan anyway.

### 2. Stream config: 24 fps, medium resolution, plus in-stream PCM audio on the same Bluetooth budget. HIGH-MEDIUM

- Scene: `.raw`, `.pcm(16 kHz mono)`, `.medium`, `frameRate: 24` (`S/StreamViewModel.swift:42-49`).
- Findr: `.raw`, `.medium`, `frameRate: 2`, no in-stream audio (`F/Wearable/WearablesManager.swift:80, 99, 110, 463-467`). The reasoning is at `:81-98`: resolution and frame rate share one Bluetooth budget, so the SDK drops pixels first. At 24 fps the link has the least headroom.
- Findr also proved `.hvc1` worse (`WearablesManager.swift:100-110`). Scene correctly uses `.raw`.
- Findr's 0.8.0 failure reproduced at both 24 and 7 fps (`MWDAT-STREAM-BUG-REPORT.md:64`), so frame rate alone did not cause that regression. But Scene adds an audio codec to the stream. No Findr build ever requested that, and 0.7.0 has no `audioCodec` parameter at all (0.7.0 interface:274 vs 1.0.1 interface:359). Can't tell what that does to stream start-up on real glasses.

### 3. No session state or error subscription, and no device eligibility wait. MEDIUM (cause or blind spot)

The order Findr uses, with evidence (`F/Wearable/WearablesManager.swift`):
1. `AutoDeviceSelector` (:396)
2. Wait for `selector.activeDevice` before creating anything, checking the current value before awaiting the stream (:410, :515-557)
3. Request camera permission now that the link is up (:428)
4. `createSession` (:431)
5. Grab `stateStream()` and `errorStream()` BEFORE `start()` (:436-439)
6. `session.start()` (:442)
7. Check `session.state` first, then await `.started` (:447-459)
8. `addStream` (:468)
9. Attach video, state, error and photo listeners, storing every token in `tokens` (:176, :792-842)
10. `await stream.start()` (:489)

What Scene does (`S/StreamViewModel.swift`):
- No eligibility wait: `createSession` runs immediately (:78-79). The UI does gate on `devicesStream()` being non-empty (`S/Views/ContentView.swift:42-60`), but a device in that list can still be `connecting` or `.undefined` compatibility. Findr measured exactly that (`WearablesManager.swift:417-422, 255-263`).
- Never subscribes to `session.stateStream()` or `session.errorStream()`. It polls `session.state` for 10 s instead (:54-61, :82). Polling avoids the "missed `.started`" deadlock, but any `DeviceSessionError` ("Session ended by device", "Device unavailable") is thrown away.
- Never subscribes to `stream.errorPublisher`. Only video, audio and state are subscribed (:99-124). A 30 s "Device is not connected" error, Findr's exact 0.8.0 signature, would be invisible in Scene.
- Tokens are stored in properties (:31-33). Good: that is not the bug.

Whether this caused the failure or just hid it: can't tell. Either way, Scene could not have diagnosed its own failure.

### 4. Frame handling: possible silent drops, and no way to tell "no callback" from "callback with no image". MEDIUM

- Scene wraps every frame in `Task { @MainActor … videoFrame.makeUIImage() }` at 24 fps and drops nil silently (`S/StreamViewModel.swift:99-108`). The UI says "No frame yet" in both cases (`S/Views/StreamView.swift:15-23`).
- Findr counts and logs on the SDK delivery thread, with no main-actor hop. It logs `*** FIRST VIDEO FRAME` (`F/Wearable/WearablesManager.swift:799-822`) and encodes on that same thread (`F/BridgeController.swift:519-549`, `F/Wearable/ImageEncoder.swift`).
- Holding a 24 fps backlog of `VideoFrame`s on the main actor could starve the SDK's buffer pool, so the SDK stops delivering. Plausible, not proven. Can't tell.
- So the run sheet's "no frame callback ever fires" (runsheet:60) is not something Scene could actually measure. It only knew the preview stayed empty.

### 5. Info.plist differences. MEDIUM for registration, LOW for the stream itself

| Key | Findr (`F/Info.plist`) | Scene (`S/Info.plist`) |
|---|---|---|
| `MWDAT.AppLinkURLScheme` | `findrbridge://` (:14-15) | `scene-bridge`, no `://` (:47-48) |
| `MWDAT.MetaAppID` | `$(META_APP_ID)`, `0` in pbxproj = Developer Mode placeholder (:19-20; `FP/project.pbxproj:306,337`) | literal `YOUR_META_APP_ID` (:45-46) |
| `MWDAT.ClientToken` | `$(CLIENT_TOKEN)` = `0` (:21-22; pbxproj:291,322) | **missing** |
| `MWDAT.TeamID` | `$(DEVELOPMENT_TEAM)` (:23-24); team set in pbxproj:294,325 | **missing**; no `DEVELOPMENT_TEAM` anywhere in `SP/project.pbxproj` |
| `MWDAT.Analytics/CrashReporting OptOut` | absent | present (:49-58). Can't tell whether 1.0.1 honours these. |
| `CFBundleURLTypes` | scheme `findrbridge`, Role Editor, URLName (:30-42) | scheme `scene-bridge` only (:70-78) |
| `LSApplicationQueriesSchemes` | `fb-viewapp` (:45-48) | `fb-viewapp` (:79-82). Same. |
| `UIBackgroundModes` | `processing`, `bluetooth-central`, `bluetooth-peripheral` (:55-60) | `bluetooth-peripheral`, `external-accessory` (:60-64) + `UISupportedExternalAccessoryProtocols com.meta.ar.wearable` (:65-68) |
| `NSBluetoothAlwaysUsageDescription` | present (:61-62) | present (:31-32) |
| `NSLocalNetworkUsageDescription` | present (:68-69) | present (:37-38) |
| `NSBonjourServices` | `_bonjour._tcp` (:70-73) | **empty array** (:40-41), marked "VERIFY" |
| `NSAppTransportSecurity.NSAllowsLocalNetworking` | `true` (:78-82) | **missing**. Only matters for `http://` LAN; Scene's `Config.baseURL` is `https://api.helloscene.xyz/…` (`S/Config.swift:11`), while its run sheet uses `http://10.1.63.33:8000` (runsheet:4). Cleartext to that IP would be blocked by ATS. That breaks uploads, not frames. |
| `UILaunchStoryboardName` | generated (`INFOPLIST_KEY_UILaunchScreen_Generation`, `FP/project.pbxproj:299`) | `LaunchScreen` (:28-29, `SP/project.pbxproj:244`), but **no storyboard exists in the repo**. Can't tell the runtime effect (likely letterboxed launch, not a stream issue). |

The run sheet suggests Scene did get through registration (runsheet:32-35), so the plist gaps probably were not what killed the stream. Can't tell: no logs survive. The missing TeamID/ClientToken and `YOUR_META_APP_ID` are still the first things a new build should not copy.

### 6. Camera permission timing. MEDIUM-LOW

- Findr requests it automatically when registration reaches `.registered` (`F/Wearable/WearablesManager.swift:290-292`), and again once the device link is provably up (:417-428). Two flags (`inFlight` and `granted`) let the request be retried on every re-emit; the reason is at :254-265. Failure is logged with a full eligibility diagnosis (:344-358).
- Scene: manual "Check" and "Request" buttons only (`S/WearablesViewModel.swift:73-91`, `S/Views/ContentView.swift:28-37`). No retry, and errors land in a `lastError` string. Pressing Request before the link is up throws "No wearable devices have been discovered or registered" (Findr hit exactly that), and nothing retries. The run sheet works around this by hand (runsheet:33-34). Whether the stream was ever started with permission actually `.granted`: can't tell.

### 7. No session auto-recovery. LOW for "never started", HIGH for "won't last"

- Findr: `.stopped` triggers rebuild through the same `buildSessionAndStream()`, with backoff of 2, 5, 10, 20, then 30 s, and an `isTearingDown` guard (`F/Wearable/WearablesManager.swift:237-252, 849-859, 950-1001`). Measured: the device ends sessions on its own after 37 s to ~11 min (:231-236).
- Scene: none. It has a manual 2 s gap between stop and start (`S/StreamViewModel.swift:69-77`) and a human retry cap (runsheet:50). It would not explain zero frames, but it would kill any run that did start.

### 8. Backend URL and Local Network. LOW for the stream

- Findr: mDNS hostname `http://Nayans-MacBook-Air.local:8001` (`F/Network/BackendClient.swift:66`), editable in the UI, persisted. A one-time IP-to-hostname migration (:94-111). `waitsForConnectivity = false` (:113-123). A launch-time `GET /stats` raises the Local Network prompt early (`F/BridgeController.swift:196-209`).
- Scene: hard-coded HTTPS cloud URL plus a bearer placeholder `PASTE_DEV_TOKEN_HERE` (`S/Config.swift:11, 14`). There is no early request to raise the Local Network prompt. `ContentView.onAppear` calls `flushPending()` (`S/Views/ContentView.swift:77`), which only hits the network if something is buffered.
- None of this touches the glasses link. It decides whether frames *reach the laptop*.

### 9. AVAudioSession route-change loop. NOT APPLICABLE to Scene

- Findr had it, and fixed it by restarting only on `.oldDeviceUnavailable` and `.newDeviceAvailable`, plus a re-entrancy and 1 s debounce (`F/Wearable/GlassesAudioRecorder.swift:384-399, 425-464`). The reason is at :386-394: in mock mode the loop also stalled video while the stream still read `.streaming`.
- Scene never touches `AVAudioSession` (grep for `AVAudioSession` in `S/` finds nothing). Its audio is in-stream PCM from the SDK. So there is no route-change loop, but also no HFP glasses mic.

### 10. Other differences

- **Configure trap**: both call `Wearables.configure()` in `App.init` (`F/FindrBridgeApp.swift:30-35`, `S/SceneBridgeApp.swift:10-16`). Scene's `@StateObject WearablesViewModel()` (`S/SceneBridgeApp.swift:8`) reads `Wearables.shared` in a stored-property initializer (`S/WearablesViewModel.swift:19`). Findr notes that SwiftUI runs stored-property initializers before `init()`'s body (`F/Wearable/WearablesManager.swift:147-157`). `@StateObject` wraps its initializer in an autoclosure that runs lazily on first body evaluation, so this *probably* runs after configure. Not verified. Findr's `WearablesRuntime.isConfigured` guard (`:20-24, :161-167`) removes the question. Scene has no guard, and on configure failure it calls `assertionFailure`, then continues into a trap.
- **Idle timer**: Findr sets `isIdleTimerDisabled = true` on start (`F/BridgeController.swift:240`). Scene does not, and relies on the manual Auto-Lock setting (runsheet:9).
- **Device selection mismatch**: Scene's UI lets you pick a device (`S/Views/ContentView.swift:47-52`), but `StreamViewModel` ignores it and uses `AutoDeviceSelector` (`S/StreamViewModel.swift:78`). This is harmless with one pair of glasses.
- **Stream lifetime tied to navigation**: `StreamView.onDisappear` stops the stream (`S/Views/StreamView.swift:69`), and the `StreamViewModel` is per-view `@StateObject`. Backing out of the screen kills the session.
- **Diagnostics**: Findr has a durable run log in `Documents/findr-run.log` (`F/BridgeState.swift:61-68`), stdout with `fflush` (:133-146), a 15 s heartbeat (`F/BridgeController.swift:384-415`), frame-size degrade logging (`F/Wearable/WearablesManager.swift:60-73, 812-819`), and link-state probes (:646-674). Scene has none of these. This is the main reason Scene's failure is still unexplained.
- **Meta AI force-quit**: Scene's procedure says to force-quit Meta AI before bridging (runsheet:25-28, `~/scene-ios-bridge/README.md:27-29`). Findr has no such step. Can't tell if it helps or hurts; Meta AI owns registration callbacks and permission prompts.
- **Unit tests**: Findr has `FindrBridgeTests/AudioChunkerTests.swift`. Scene has no test target.
- **Firmware**: Findr's bug report used firmware `66388150085500100` with Meta AI `283.0.0.28.165` (`MWDAT-STREAM-BUG-REPORT.md:8-10`). Scene's run sheet mentions "firmware 129" (runsheet:58-60). Different firmware is a third variable. Can't tell its effect.

---

## What Phase 1 should take from this

1. Pin `meta-wearables-dat-ios` exactly to `0.7.0`. Never `upToNextMajor`.
2. Copy Findr's `WearablesManager` sequence verbatim: eligibility wait, then state and error streams before `start()`, then a state check before awaiting, then `addStream`, then retained tokens, then `await stream.start()`.
3. Keep `.raw` / `.medium` / `2 fps`, with no in-stream audio. Audio stays on HFP via `GlassesAudioRecorder`.
4. Copy Findr's Info.plist block whole: MWDAT with `://`, `0` placeholders and TeamID; Bonjour; `NSAllowsLocalNetworking`.
5. Keep auto-recovery, the run log, the heartbeat and `*** FIRST VIDEO FRAME`. They are the only way to tell which layer failed.
