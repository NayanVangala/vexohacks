"""Scene receiver: the laptop side of the glasses bridge.

Exists only to prove data arrives: saves every frame and WAV the phone sends,
counts them, and reports the counts. No processing, no models, no storage
beyond plain files.

Wire format is the iOS bridge's (BackendClient.swift):
  POST /capture   multipart, exactly one file part: `photo` (JPEG) or `audio` (WAV),
                  optional form field `source` ("activation" for the Activate photo)
  POST /activate  empty body
  GET  /stats     JSON counters

Memory (memory.py, needs GEMINI_API_KEY; OPENAI_API_KEY optional; without Gemini the receiver just saves files):
  POST /ask              form `text` or `audio` (WAV) -> answer card
  GET  /tasks            open tasks        POST /tasks/{id}/done
  GET  /items            last place of each object
  GET  /feed?after=N     cards for the glasses (answers, new tasks, payments to approve)
  GET  /unknown-face    who-was-that prompt after a conversation (unknown face saved during it)
  POST /unknown-face/enroll {name}  enroll the saved frame under that name; /unknown-face/dismiss discards it
  GET  /payments ...     see payments.py; approve is the human-in-the-loop gate
  GET  /frames/<name>    saved frames (thumbnails; set PUBLIC_BASE_URL to an HTTPS tunnel for the display)

Run:  .venv/bin/python server.py        (0.0.0.0:8001, the bridge's default port)
"""

import os
import threading
import time
from pathlib import Path

import httpx
from fastapi import FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import contacts
import inbox
import lead
import orders
import outbox
import memory
import models
import notes
import payments
import voice
from awareness import parse_yes_no

DATA_DIR = Path(os.environ.get("SCENE_DATA_DIR", Path(__file__).parent / "data"))
PORT = int(os.environ.get("PORT", "8001"))

PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")  # HTTPS tunnel; display images need HTTPS

app = FastAPI(title="scene receiver")
(DATA_DIR / "frames").mkdir(parents=True, exist_ok=True)
app.mount("/frames", StaticFiles(directory=DATA_DIR / "frames"), name="frames")
# Enrolled face photos (data/faces). Served so the phone can show who is enrolled; no matching runs on them.
(DATA_DIR / "faces").mkdir(parents=True, exist_ok=True)
app.mount("/faces", StaticFiles(directory=DATA_DIR / "faces"), name="faces")
# The Meta Ray-Ban Display web app (src/glasses/). Same origin as the API, so the glasses need
# only this one HTTPS URL (the tunnel): https://<tunnel>/glasses/
GLASSES_DIR = Path(__file__).resolve().parent.parent / "glasses"
if GLASSES_DIR.is_dir():
    app.mount("/glasses", StaticFiles(directory=GLASSES_DIR, html=True), name="glasses")

_lock = threading.Lock()
_started = time.time()
_stats = {
    "frames": 0,
    "activation_photos": 0,
    "audio_chunks": 0,
    "activations": 0,
    "rejected": 0,
    "bytes": 0,
    "last_frame_at": None,
    "last_audio_at": None,
    "last_sound_at": None,   # last audio chunk that was NOT room tone (voice.is_silent)
}

# ---------- Display Audio Mode ----------
# The Ray-Ban Display web app and the DAT camera stream cannot run at the same
# time, and opening the web app disrupts the audio session. But the glasses mic
# is a separate path from the camera (glasses mic -> phone app -> POST /capture
# audio), so while the display is open Scene can still listen, just without new frames.
# The lens itself is the only reliable signal the display is open: app.js
# beacons POST /display/heartbeat on load and every 15 s, and /display/close
# on pagehide. The mode expires 25 s after the last heartbeat, so a crashed
# or closed web app falls back to normal capture on its own.
_display_lock = threading.Lock()
_display_last_seen = 0.0
DISPLAY_HEARTBEAT_TTL_S = 25.0


def display_audio_mode() -> bool:
    with _display_lock:
        return time.time() - _display_last_seen < DISPLAY_HEARTBEAT_TTL_S


def _display_age_s() -> float | None:
    with _display_lock:
        return None if _display_last_seen == 0.0 else round(time.time() - _display_last_seen, 1)


def _is_jpeg(b: bytes) -> bool:
    return b[:3] == b"\xff\xd8\xff"


def _is_wav(b: bytes) -> bool:
    return len(b) >= 12 and b[:4] == b"RIFF" and b[8:12] == b"WAVE"


def _save(kind: str, ext: str, data: bytes) -> Path:
    # Server-side names only: the client's filename is untrusted input.
    d = DATA_DIR / kind
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{time.time_ns()}.{ext}"
    path.write_bytes(data)
    return path


def _reject(msg: str):
    with _lock:
        _stats["rejected"] += 1
    print(f"REJECT {msg}", flush=True)
    raise HTTPException(status_code=415, detail=msg)


@app.post("/capture")
async def capture(
    photo: UploadFile | None = File(None),
    audio: UploadFile | None = File(None),
    source: str | None = Form(None),
):
    if (photo is None) == (audio is None):
        raise HTTPException(status_code=422, detail="send exactly one of `photo` or `audio`")

    now = time.time()
    if photo is not None:
        data = await photo.read()
        if not _is_jpeg(data):
            _reject(f"photo is not JPEG ({len(data)} B)")
        activation = source == "activation"
        path = _save("activation" if activation else "frames", "jpg", data)
        with _lock:
            _stats["activation_photos" if activation else "frames"] += 1
            _stats["bytes"] += len(data)
            _stats["last_frame_at"] = now
            n = _stats["activation_photos" if activation else "frames"]
        print(f"{'ACTIVATION PHOTO' if activation else 'frame'} #{n} {len(data)} B -> {path.name}", flush=True)
        if not activation:
            # Display Audio Mode: the display is open, so there should be no
            # new frames anyway; if one sneaks in, save it but do not spend a
            # Gemini vision call on it. Audio keeps flowing (see below).
            # Sync the sticky memory flag from the TTL-based heartbeat every
            # time, so an expired heartbeat resumes frame processing.
            memory.set_display_audio_mode(display_audio_mode())
            memory.on_frame(path)
        return {"saved": path.name, "kind": "activation" if activation else "frame"}

    data = await audio.read()
    if not _is_wav(data):
        _reject(f"audio is not WAV ({len(data)} B)")
    path = _save("audio", "wav", data)
    with _lock:
        _stats["audio_chunks"] += 1
        _stats["bytes"] += len(data)
        _stats["last_audio_at"] = now
        n = _stats["audio_chunks"]
    # Real sound (not room tone) resets the mic-alive clock. The lens shows
    # "Mic silent" when this goes stale in Display Audio Mode; silent WAVs
    # still arrive when the mic is dead, so last_audio_at alone cannot tell.
    try:
        if not voice.is_silent(data):
            with _lock:
                _stats["last_sound_at"] = now
    except Exception:
        pass
    print(f"audio #{n} {len(data)} B -> {path.name}", flush=True)
    # Keep the sticky Display Audio Mode flag fresh on every audio chunk too, not
    # only on frames: while the display is open there are no frames to sync it, and
    # a heartbeat that expired (display closed/crashed) must stop live transcript
    # cards until the next heartbeat. set_display_audio_mode only prints when the
    # value changes, so this stays quiet.
    memory.set_display_audio_mode(display_audio_mode())
    memory.on_audio(path)
    return {"saved": path.name, "kind": "audio"}


@app.post("/activate")
async def activate():
    with _lock:
        _stats["activations"] += 1
        n = _stats["activations"]
    print(f"ACTIVATE #{n}", flush=True)
    return {"activations": n}


@app.post("/display/heartbeat")
async def display_heartbeat():
    """The glasses web app beacons this on load and every 15 s while open.
    While the heartbeat is fresh, Scene is in Display Audio Mode: frames are
    paused (no Gemini vision), but the glasses mic keeps listening."""
    global _display_last_seen
    with _display_lock:
        _display_last_seen = time.time()
    memory.set_display_audio_mode(True)
    voice.note_display_seen()  # also feeds voice.display_open() for /settings + /stats
    return {"display_audio_mode": True}


@app.post("/display/close")
async def display_close():
    """Best effort (pagehide beacon); the heartbeat TTL is the real expiry."""
    global _display_last_seen
    with _display_lock:
        _display_last_seen = 0.0
    memory.set_display_audio_mode(False)
    memory.clear_focus()  # display closed: "this" no longer means anything on it
    voice.note_display_seen(now=0.0)  # reset the voice.py display heuristic
    print("DISPLAY closed: frames resume", flush=True)
    return {"display_audio_mode": False}


@app.get("/display")
async def display_state():
    now = time.time()
    with _lock:
        last_sound = _stats["last_sound_at"]
    return {
        "display_audio_mode": display_audio_mode(),
        "display_last_seen_age_s": _display_age_s(),
        # Seconds since real (non-silent) audio arrived; None until any sound.
        "audio_age_s": None if last_sound is None else round(now - last_sound, 1),
    }


@app.post("/focus")
async def focus_set(request: Request):
    """The glasses web app posts the item the wearer is looking at (a Follow-
    Through promise, a Needs-you card, a task): that item becomes what "this"
    means when they ask "Hey Scene, tell me more about this?". Re-posted on
    every display heartbeat while it is still on screen, so the 5 min expiry
    in memory.get_focus only bites once they look away / the display closes."""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="send a JSON focus item")
    if not isinstance(body, dict) or not str(body.get("id") or "").strip() \
            or not str(body.get("title") or "").strip():
        raise HTTPException(status_code=422, detail="focus needs `id` and `title`")
    return {"focus": memory.set_focus(body)}


@app.get("/focus")
async def focus_get():
    return {"focus": memory.get_focus()}


@app.post("/focus/clear")
async def focus_clear():
    memory.clear_focus()
    return {"focus": None}


def _frame_url(frame: str | None) -> str | None:
    return f"{PUBLIC_BASE_URL}/frames/{frame}" if frame and PUBLIC_BASE_URL else None


@app.post("/ask")
async def ask(text: str | None = Form(None), audio: UploadFile | None = File(None)):
    _t0 = time.time()
    _transcribe_ms = 0.0
    if audio is not None:
        data = await audio.read()
        if not _is_wav(data):
            _reject(f"audio is not WAV ({len(data)} B)")
        text = await run_in_threadpool(models.transcribe, data)
        _transcribe_ms = (time.time() - _t0) * 1000
    if not text or not text.strip():
        raise HTTPException(status_code=422, detail="send `text` or `audio` with speech in it")
    text = text.strip()
    # Action questions go to the agent first, never to memory.ask: /ask used to send
    # "book a table..." / "email Priya..." straight to memory.ask, which could only
    # answer "I don't have a memory of that." dining_turn is the same router the voice
    # path uses (corrections, the code-first schedule draft, agent waiting reply,
    # agent.start), so /ask and voice stay in agreement. Pure recall falls through
    # to memory.ask below. Lead target / frame handling is untouched for recall.
    if await run_in_threadpool(memory.dining_turn, text, True):
        print(f"ASK {text!r} -> agent (routed_to=agent) transcribe_ms={_transcribe_ms:.0f}", flush=True)
        return {"question": text,
                "answer": "On it. Check your glasses for the next step.",
                "memory_id": None, "frame": None, "target": None,
                "routed_to": "agent", "agent": memory.agent.state(), "image_url": None}
    out = await run_in_threadpool(memory.ask, text)  # blocking model calls off the event loop
    print(f"ASK {out['question']!r} -> {out['answer']!r} transcribe_ms={_transcribe_ms:.0f} answer_ms={(time.time() - _t0) * 1000 - _transcribe_ms:.0f}", flush=True)
    # The phone decodes target {name, place, frame, age_min} for Lead me there; frame_path is a
    # laptop filesystem path and must never leave the server.
    target = {k: v for k, v in (out.get("target") or {}).items() if k != "frame_path"}
    return {**out, "target": target or None, "image_url": _frame_url(out["frame"])}


@app.get("/tasks")
async def tasks():
    return memory.open_tasks()


@app.post("/tasks/{task_id}/done")
async def task_done(task_id: int):
    if not memory.set_done(task_id):
        raise HTTPException(status_code=404, detail="no such task")
    return {"done": task_id}


# ---------- Face enrollment + live recognition (Gemini Hydrogen vision) ----------
# POST /faces/enroll stores one JPEG per name in data/faces/<name>.jpg.
# memory.match_face() compares frames against those photos via Gemini Hydrogen
# vision (confidence >= 0.7); a matched name becomes the speaker on cards in
# that window, otherwise speaker comes from `from_whom` in the transcript.
@app.post("/faces/enroll", status_code=201)
async def face_enroll(name: str | None = Form(None), photo: UploadFile | None = File(None)):
    if not name or not name.strip():
        raise HTTPException(status_code=422, detail="send `name`")
    if photo is None:
        raise HTTPException(status_code=422, detail="send a `photo` JPEG")
    data = await photo.read()
    if not _is_jpeg(data):
        _reject(f"photo is not JPEG ({len(data)} B)")
    try:
        out = await run_in_threadpool(memory.enroll_face, name.strip(), data)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    base = PUBLIC_BASE_URL or ""
    return {**out, "image_url": f"{base}/faces/{out['photo']}"}


@app.get("/faces")
async def faces_list():
    base = PUBLIC_BASE_URL or ""
    return {"recognition": "hydrogen_vision",
            "note": "Face matching runs via Gemini Hydrogen vision on frames (confidence >= 0.7). A matched name becomes the speaker on cards in that window; otherwise speaker comes from from_whom in the transcript, or Unknown.",
            "faces": [{**f, "image_url": f"{base}/faces/{f['photo']}"} for f in memory.list_faces()]}


# ---------- Conversation Mode (see memory.py) ----------
# The display/phone reads the state and can start/stop it. While active Scene only
# listens, transcribes and takes notes: no spoken answers, no chime-out, no
# interrupting speech. Live tasks created in this mode are drafts (GET /tasks,
# /inbox): nothing sends without a tap.
@app.get("/conversation")
async def conversation_get():
    """Conversation Mode state: mode, started_at, last_speech_at, speaker."""
    return memory.conversation_state()


@app.post("/conversation/start")
async def conversation_start(request: Request):
    """Start Conversation Mode from the display/phone. Optional JSON/form {speaker}."""
    f = await _fields(request)
    speaker = str(f.get("speaker") or "").strip() or None
    started = memory.start_conversation(source="api", speaker=speaker)
    return {**memory.conversation_state(), "started": started}


@app.post("/conversation/stop")
async def conversation_stop():
    """Stop Conversation Mode from the display/phone."""
    stopped = memory.stop_conversation(reason="api")
    return {**memory.conversation_state(), "stopped": stopped}


# ---------- Unknown face: enroll AFTER the conversation (see memory.py) ----------
# GET /unknown-face: is a who-was-that prompt waiting, and which saved frame is it for.
# POST /unknown-face/enroll {name}: enroll the saved frame under that name (the only
# enroll path for an unknown face; no name, no enroll). POST /unknown-face/dismiss:
# discard the saved frame, never ask again for that conversation. The phone never
# uploads a photo: the frame already lives in data/frames on the laptop.
@app.get("/unknown-face")
async def unknown_face_get():
    """Who-was-that prompt state: {pending, frame, frame_url, asked_at}."""
    state = memory.unknown_face_state()
    if state.get("frame"):
        state = {**state, "frame_url": _frame_url(state["frame"])}
    return state


@app.post("/unknown-face/enroll")
async def unknown_face_enroll(request: Request):
    """Enroll the saved unknown frame under `name` (form or JSON). 422 with no name /
    bad name, 404 with no prompt waiting."""
    f = await _fields(request)
    name = str(f.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="send `name`")
    try:
        out = await run_in_threadpool(memory.enroll_unknown_face, name)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    except FileNotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e))
    base = PUBLIC_BASE_URL or ""
    return {**out, "image_url": f"{base}/faces/{out['photo']}"}


@app.post("/unknown-face/dismiss")
async def unknown_face_dismiss():
    """Discard the saved unknown frame without enrolling. Never asks again for that conversation."""
    return await run_in_threadpool(memory.dismiss_unknown_face)


@app.get("/payments")
async def payments_pending():
    return payments.pending()


@app.get("/payments/{pid}")
async def payment_status(pid: int):
    try:
        return await run_in_threadpool(payments.refresh, pid)
    except payments.PaymentError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/payments/{pid}/approve")
async def payment_approve(pid: int):
    """Called only by the glasses' Approve button: the human-in-the-loop gate."""
    try:
        return await run_in_threadpool(payments.approve, pid)
    except payments.PaymentError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.post("/payments/{pid}/reject")
async def payment_reject(pid: int):
    try:
        payments.reject(pid)
    except payments.PaymentError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"rejected": pid}


@app.post("/offers/{offer_id}/accept")
async def offer_accept(offer_id: int):
    """Glasses' Yes button on an offer card (take notes)."""
    try:
        return await run_in_threadpool(memory.answer_offer, offer_id, True)
    except KeyError:
        raise HTTPException(status_code=409, detail="that offer is not waiting for an answer")


@app.post("/offers/{offer_id}/decline")
async def offer_decline(offer_id: int):
    try:
        return await run_in_threadpool(memory.answer_offer, offer_id, False)
    except KeyError:
        raise HTTPException(status_code=409, detail="that offer is not waiting for an answer")


# ---------- Lead me there (lead.py + memory.py) ----------
# The glasses pinch Yes/No and Stop, and the phone's "Lead me there" button, call these.
# The voice flow is untouched: spoken yes/no inside the offer window still goes through
# memory._lead_audio -> lead.try_answer, and lead steps still push cards and speak.

def _lead_target_public(t: dict | None) -> dict | None:
    """Target safe to send to a client: name/place/frame/age, never the laptop file path."""
    if not t:
        return None
    return {"name": t.get("name"), "place": t.get("place"), "frame": t.get("frame"),
            "age_min": t.get("age_min")}


def _lead_target_from_row(r: dict) -> dict:
    now = time.time()
    return {"name": r["text"], "place": r.get("place") or "",
            "frame": r.get("frame"),
            "frame_path": str(DATA_DIR / "frames" / r["frame"]) if r.get("frame") else None,
            "age_min": round((now - r["ts"]) / 60)}


@app.post("/lead/answer")
async def lead_answer(request: Request):
    """Glasses pinch Yes/No (or phone Yes) on a lead offer. Form or JSON {text: yes|no}.
    200 on start/declined, 409 when no offer is waiting (expired or never asked)."""
    f = await _fields(request)
    text = str(f.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="text is required (yes or no)")
    ans = parse_yes_no(text)
    if ans == "unclear":
        raise HTTPException(status_code=422, detail="answer yes or no")
    r = lead.try_answer(ans, from_tap=True)
    if r == "start":
        return {"result": "started", "target": _lead_target_public(lead.get_target())}
    if r == "declined":
        return {"result": "declined"}
    if r == "ignore":
        return {"result": "ignored", "detail": "Scene is still asking; try again in a moment"}
    raise HTTPException(status_code=409, detail="no lead offer is waiting (it expired or was never asked)")


@app.post("/lead/stop")
async def lead_stop():
    """Glasses/phone Stop button while leading (or while an offer is open). Always 200."""
    was = lead.mode()
    lead.stop()
    return {"stopped": True, "was": was}


@app.post("/lead/start")
async def lead_start(request: Request):
    """Start the lead flow for a thing the glasses already saw: create the offer, which the
    wearer then answers with POST /lead/answer (or out loud). Form or JSON, one of:
      memory_id (an object row id from /items), name (+ optional place/frame/age_min),
      question (e.g. "Where are my keys?" - answered via memory.ask).
    409 when there is no photo to lead from, the sighting is older than 180 min, or a lead
    is already running. Never invents a place: everything comes from a stored sighting."""
    f = await _fields(request)
    if lead.mode() == "active":
        raise HTTPException(status_code=409, detail="a lead is already running, stop it first")
    target = None
    if f.get("memory_id") is not None:
        try:
            mid = int(f["memory_id"])
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="memory_id must be an object id")
        rows_ = memory.rows("SELECT * FROM memories WHERE id=? AND kind='object'", mid)
        if not rows_:
            raise HTTPException(status_code=404, detail="no such object memory")
        target = _lead_target_from_row(rows_[0])
    elif f.get("question") or f.get("text"):
        out = await run_in_threadpool(memory.ask, str(f.get("question") or f.get("text")).strip())
        target = out.get("target")
        if not target:
            raise HTTPException(status_code=409, detail="no recent sighting with a photo to lead from")
    elif f.get("name"):
        name = str(f["name"]).strip().lower()
        rows_ = memory.rows("SELECT * FROM memories WHERE kind='object' AND text=? ORDER BY ts DESC LIMIT 1", name)
        if rows_:
            target = _lead_target_from_row(rows_[0])
        else:
            if not f.get("frame"):
                raise HTTPException(status_code=404, detail=f"never saw {name}")
            try:
                age_min = int(f["age_min"])
            except (KeyError, TypeError, ValueError):
                raise HTTPException(status_code=422, detail="age_min is required when no stored sighting exists")
            target = {"name": name, "place": str(f.get("place") or ""),
                      "frame": str(f["frame"]),
                      "frame_path": str(DATA_DIR / "frames" / str(f["frame"])),
                      "age_min": age_min}
    else:
        raise HTTPException(status_code=422, detail="send memory_id, name, or question")
    if not target.get("frame") or not (DATA_DIR / "frames" / str(target["frame"])).is_file():
        raise HTTPException(status_code=409, detail="no photo to lead from")
    if target.get("age_min", 9999) > memory.LEAD_MAX_AGE_MIN:
        raise HTTPException(status_code=409,
                            detail=f"too long ago to lead from ({target['age_min']} min ago), it may have moved")
    spoken = (f"{target['name']} is {target['place']}. Want me to lead you there?".strip()
              if target.get("place") else f"Want me to lead you to {target['name']}?")
    lead.offer(target, spoken)
    return {"offer": "created", "mode": "offer", "target": _lead_target_public(target),
            "expires_in_s": round(len(spoken.split()) / 2.6 + 1.5 + 15)}


@app.post("/notes/stop")
async def notes_stop():
    nid = await run_in_threadpool(memory.finish_notes)  # one model call, off the event loop
    if nid is None:
        raise HTTPException(status_code=409, detail="no notes are being taken")
    return {"notes": nid}


@app.post("/session/stop")
async def session_stop():
    """The wearer stopped streaming: save everything heard so far, synchronously
    (end the conversation, flush the transcript tail, finish any notes session).
    The phone should call this when Stop is tapped. Even if it never does, the
    flush watchdog + startup recovery cover the same tail -- this just makes it
    immediate instead of ~10 s later."""
    return await run_in_threadpool(memory.stop_streaming)


@app.get("/session")
async def session():
    """Is a note session running (for the glasses' "Taking notes" card)."""
    # Polled every 2 s by the phone, so this also keeps the sticky memory flag
    # in sync with the heartbeat TTL when no frames are arriving at all.
    memory.set_display_audio_mode(display_audio_mode())
    return {"notes": notes.status(), "display_audio_mode": display_audio_mode(),
            "display_last_seen_age_s": _display_age_s(),
            "conversation": memory.conversation_state()}


@app.get("/notes")
async def notes_recent(limit: int = 20):
    """Recent meeting notes for the phone (summary and counts). The full notes are /notes/<id>."""
    return memory.notes_list(limit)


@app.get("/activity")
async def activity(limit: int = 50, today: bool = True):
    """What Scene saw, heard and did (today by default), newest first."""
    return memory.activity(limit, today)


@app.get("/inbox")
async def inbox_items():
    """Everything waiting on the wearer, each with ready-made actions the phone can call with one tap."""
    return await run_in_threadpool(inbox.items)


@app.get("/orders")
async def orders_recent(limit: int = 20):
    return await run_in_threadpool(orders.recent, limit)


@app.get("/notes/{note_id}")
async def notes_get(note_id: int):
    out = memory.get_notes(note_id)
    if out is None:
        raise HTTPException(status_code=404, detail="no such notes")
    return out


@app.post("/emails/{draft_id}/send")
async def email_send(draft_id: int):
    """The glasses' Send button: the human-in-the-loop gate. Goes to DEMO_EMAIL_TO only."""
    try:
        return await run_in_threadpool(outbox.send, draft_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="no such draft")
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.post("/emails/{draft_id}/dismiss")
async def email_dismiss(draft_id: int):
    try:
        return outbox.dismiss(draft_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="no such draft")
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.post("/slack/{draft_id}/send")
async def slack_send(draft_id: int):
    """The glasses' Send button for a Slack draft. Posts only when Slack confirms."""
    try:
        return await run_in_threadpool(outbox.send, draft_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="no such draft")
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.post("/slack/{draft_id}/dismiss")
async def slack_dismiss(draft_id: int):
    try:
        return outbox.dismiss(draft_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="no such draft")
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.post("/events/{draft_id}/add")
async def event_add(draft_id: int):
    """The glasses' Add to calendar button. Creates the event only when the executor confirms."""
    try:
        return await run_in_threadpool(outbox.send, draft_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="no such event")
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.post("/events/{draft_id}/dismiss")
async def event_dismiss(draft_id: int):
    try:
        return outbox.dismiss(draft_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="no such event")
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.post("/reservations/{draft_id}/confirm")
async def reservation_confirm(draft_id: int):
    """The glasses'/phone's Confirm button on a reservation draft: confirms the DRAFT only.
    No table is booked: there is no restaurant booking API."""
    try:
        d = outbox.get(draft_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="no such reservation draft")
    if d.get("kind") != "reservation":
        raise HTTPException(status_code=409, detail="that draft is not a reservation")
    try:
        d = await run_in_threadpool(outbox.send, draft_id)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    memory.push_card("reservation", "Reservation draft confirmed",
                     f"Draft for {d['restaurant']} at {d['start'].replace('T', ' ')} confirmed. "
                     "No table was booked: call the restaurant to book.", ref=draft_id)
    return d


@app.post("/reservations/{draft_id}/dismiss")
async def reservation_dismiss(draft_id: int):
    try:
        d = outbox.get(draft_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="no such reservation draft")
    if d.get("kind") != "reservation":
        raise HTTPException(status_code=409, detail="that draft is not a reservation")
    try:
        return outbox.dismiss(draft_id)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))


async def _fields(request: Request) -> dict:
    """The body as a dict, whether it was sent as JSON (our tools) or form-encoded (the glasses and phone apps)."""
    if request.headers.get("content-type", "").startswith("application/json"):
        try:
            body = await request.json()
        except ValueError:
            raise HTTPException(status_code=422, detail="bad JSON")
        return body if isinstance(body, dict) else {}
    return dict(await request.form())


def _coords(f: dict) -> tuple[float, float]:
    try:
        lat, lng = float(f["lat"]), float(f["lng"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=422, detail="lat and lng are required numbers")
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        raise HTTPException(status_code=422, detail="lat or lng out of range")
    return lat, lng


class ReplyIn(BaseModel):
    text: str


@app.post("/location")
async def location(request: Request):
    """The device's position, for "nearest McDonald's". Valid for 30 minutes. JSON or form."""
    memory.set_location(*_coords(await _fields(request)))
    return {"ok": True}


@app.get("/contacts")
async def contacts_list():
    return contacts.all()


@app.post("/contacts")
async def contacts_add(request: Request):
    """Add or update a contact (name, email). JSON or form. Sending to them still needs a tap AND the Apps Script allowlist."""
    f = await _fields(request)
    try:
        contacts.add(f.get("name"), f.get("email"), f.get("slack"))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {"ok": True}


@app.get("/agent")
async def agent_state():
    return memory.agent.state()


@app.post("/agent/reply")
async def agent_reply(request: Request):
    """Yes/No buttons on the glasses (or a typed answer) for the question the dining agent is waiting on. JSON or form."""
    text = str((await _fields(request)).get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=422, detail="text is required")
    try:
        memory.agent.reply(text)
    except KeyError:
        raise HTTPException(status_code=409, detail="no question is waiting")
    return memory.agent.state()


@app.post("/orders/{order_id}/approve")
async def order_approve(order_id: int):
    """The glasses' Pay button: the human-in-the-loop gate. Creates the Stripe Checkout Session (test mode)."""
    try:
        return await run_in_threadpool(orders.approve, order_id)
    except payments.PaymentError as e:
        raise HTTPException(status_code=409, detail=str(e))


@app.post("/orders/{order_id}/reject")
async def order_reject(order_id: int):
    try:
        orders.reject(order_id)
    except payments.PaymentError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return {"rejected": order_id}


@app.get("/orders/{order_id}")
async def order_status(order_id: int):
    try:
        await run_in_threadpool(orders.refresh, order_id)
        return orders.status_view(order_id)
    except payments.PaymentError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.get("/order/{order_id}", response_class=HTMLResponse)
async def order_page(order_id: int):
    return (Path(__file__).parent / "orders.html").read_text()


class SceneIn(BaseModel):
    type: str
    confidence: float
    text: str = ""
    frame: str | None = None   # optional saved frame filename, so a dev slide can be attached to the notes


class LineIn(BaseModel):
    text: str


def _dev_only() -> None:
    if os.environ.get("SCENE_DEV") != "1":
        raise HTTPException(status_code=404)


@app.post("/dev/scene")
async def dev_scene(body: SceneIn):
    """Demo without glasses: pretend a frame came back with this scene. Needs SCENE_DEV=1."""
    _dev_only()
    await run_in_threadpool(memory.handle_scene, body.model_dump(exclude={"frame"}), body.frame)
    return {"ok": True}


@app.post("/dev/say")
async def dev_say(body: ReplyIn):
    """Demo without a mic: say this to Scene as if it were heard (answers a waiting question or starts a request)."""
    _dev_only()
    if not await run_in_threadpool(memory.dining_turn, body.text, True):
        raise HTTPException(status_code=422, detail="not a dining request and no question is waiting")
    return memory.agent.state()


@app.post("/dev/location")
async def dev_location(request: Request):
    _dev_only()
    memory.set_location(*_coords(await _fields(request)))
    return {"ok": True}


@app.post("/dev/line")
async def dev_line(body: LineIn):
    """Demo without a mic: add a spoken line to the active note session. Needs SCENE_DEV=1."""
    _dev_only()
    notes.add_line(body.text)
    return {"ok": True}


@app.get("/feed")
async def feed(after: int = 0):
    return [{**c, "image_url": _frame_url(c["frame"])} for c in memory.cards_after(after)]


@app.middleware("http")
async def log_visitors(request: Request, call_next):
    """Record every non-API visitor (path + user agent): shows whether the glasses fetch anything at all."""
    response = await call_next(request)
    path = request.url.path
    if path.startswith("/glasses"):
        # The glasses' WebView otherwise keeps an old app.js after an update.
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    if not path.startswith(("/capture", "/feed", "/tasks", "/items", "/payments", "/today", "/stats", "/client-log", "/display", "/focus",
                            "/frames", "/ft/", "/offers", "/notes", "/emails", "/events", "/location", "/agent", "/orders", "/order", "/inbox", "/activity", "/session", "/contacts", "/slack", "/dev", "/lead", "/faces", "/unknown-face", "/reservations", "/conversation", "/interrupt", "/chime")):
        with open(DATA_DIR / "glasses.log", "a") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {request.method} {path} -> {response.status_code} "
                    f"ua={request.headers.get('user-agent', '?')[:160]}\n")
    return response


@app.get("/")
async def root():
    """Opening the bare tunnel URL lands on the glasses app."""
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/glasses/")


# Follow-Through (src/followthrough, its own server on :8002) behind the same origin, so the glasses
# app reaches it through the one tunnel URL: fetch('/ft/api/state') -> http://localhost:8002/api/state.
FOLLOWTHROUGH_URL = os.environ.get("FOLLOWTHROUGH_URL", "http://localhost:8002").rstrip("/")
_ft = httpx.AsyncClient(timeout=60)


@app.api_route("/ft/{path:path}", methods=["GET", "POST", "PATCH", "DELETE"])
async def followthrough_proxy(path: str, request: Request):
    try:
        r = await _ft.request(request.method, f"{FOLLOWTHROUGH_URL}/{path}", params=request.query_params,
                              content=await request.body(),
                              headers={"content-type": request.headers.get("content-type", "application/json")})
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Follow-Through isn't reachable at {FOLLOWTHROUGH_URL} ({type(e).__name__}). "
                                 "Start it: cd src/followthrough && ./run.sh")
    if request.method == "POST" and 200 <= r.status_code < 300:
        # A tap handled this promise (approve / dismiss / retry, from phone or glasses):
        # stop the voice loop asking about it, or a later "no" / "skip" would dismiss an
        # already-approved item and show "Skipped". Never breaks the proxy if it fails.
        parts = path.split("/")
        if len(parts) == 4 and parts[0] == "api" and parts[1] == "items" and parts[3] in ("approve", "dismiss", "retry"):
            try:
                memory.ft_cancel(parts[2])
            except Exception as e:
                print(f"server: ft_cancel failed: {e!r}", flush=True)
    return Response(r.content, status_code=r.status_code, media_type=r.headers.get("content-type"))


@app.get("/favicon.ico")
async def favicon():
    """Meta AI fetches this for the web app's icon in the glasses' app list."""
    from fastapi.responses import FileResponse
    return FileResponse(GLASSES_DIR / "icon.png", media_type="image/png")


@app.post("/client-log")
async def client_log(request: Request):
    """The glasses web app has no devtools: it beacons load info and errors here."""
    body = (await request.body())[:500].decode("utf-8", "replace")
    print(f"GLASSES {body}", flush=True)
    with open(DATA_DIR / "glasses.log", "a") as f:   # so it can be read without the server terminal
        f.write(f"{time.strftime('%H:%M:%S')} {request.client.host if request.client else '?'} {body}\n")
    return {"ok": True}


@app.get("/speech")
async def speech_list(after: int = 0):
    """Lines Scene wants said; the phone plays each on the glasses' speakers."""
    return voice.utterances_after(after)


@app.get("/speech/{utterance_id}")
async def speech_audio(utterance_id: int):
    out = await run_in_threadpool(voice.audio_for, utterance_id)
    if out is None:
        raise HTTPException(status_code=404, detail="no such line")
    data, media_type = out
    return Response(data, media_type=media_type)


@app.get("/interrupt")
async def interrupt_state():
    """Barge-in sequence number: the phone polls this every 0.5 s and stops its
    own player when the number changes (the backend already cleared its queue
    and killed the laptop player when the wearer talked over Jessica)."""
    return {"interrupt_id": voice.interrupt_seq()}


@app.post("/chime/local")
async def chime_local():
    """The phone played the fast local chime on speech onset (~100-250 ms in,
    before any transcription). Tell voice.py so its chime-echo filter drops
    the tone the mic picks up, and chime() skips queuing a second ping for
    the same wake word. Fire-and-forget from the phone; never blocks audio."""
    voice.note_local_chime()
    return {"ok": True}


@app.get("/settings")
async def settings_get():
    """`output` is null until the wearer picks, so the glasses app knows to ask."""
    now = time.time()
    with _lock:
        last_sound = _stats["last_sound_at"]
    return {"output": voice.output_mode(default=None), "modes": list(voice.OUTPUT_MODES),
            "speak_on": voice.speak_on(), "speak_targets": list(voice.SPEAK_TARGETS),
            "paused": voice.paused(), "display_audio_mode": display_audio_mode(),
            "audio_age_s": None if last_sound is None else round(now - last_sound, 1),
            "display_open": voice.display_open(), "display_seen_ago_s": voice.display_seen_ago_s(),
            "conversation": memory.conversation_state()}


@app.post("/settings")
async def settings_set(output: str | None = Form(None), speak_on: str | None = Form(None),
                       paused: str | None = Form(None)):
    """Change any subset: output (cards|voice|both), speak_on (glasses|laptop|both), paused (true|false)."""
    try:
        if output is not None:
            voice.set_output_mode(output)
        if speak_on is not None:
            voice.set_speak_on(speak_on)
        if paused is not None:
            voice.set_paused(paused.lower() in ("1", "true", "yes", "on"))
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    print(f"SETTINGS {dict(output=output, speak_on=speak_on, paused=paused)}", flush=True)
    return await settings_get()


@app.get("/today")
async def today():
    return memory.today()


@app.get("/items")
async def items():
    return [{**i, "image_url": _frame_url(i["frame"])} for i in memory.items()]


@app.get("/stats")
async def stats():
    now = time.time()
    with _lock:
        s = dict(_stats)
    for k in ("last_frame_at", "last_audio_at"):
        s[k.replace("_at", "_age_s")] = None if s[k] is None else round(now - s[k], 1)
    s["display_open"] = voice.display_open()
    s["display_seen_ago_s"] = voice.display_seen_ago_s()
    s["uptime_s"] = round(now - _started, 1)
    s["data_dir"] = str(DATA_DIR)
    return s


def _check_models() -> None:
    """Say loudly, at startup, which keys and models are live. A wrong model name otherwise
    shows up only as one 404 per frame in the log."""
    for k in ("GEMINI_API_KEY", "OPENAI_API_KEY", "STRIPE_SECRET_KEY", "ELEVENLABS_API_KEY"):
        print(f"  {k:20} {'set' if os.environ.get(k) else 'MISSING'}", flush=True)
    if not memory.ENABLED:
        print("  memory OFF: needs GEMINI_API_KEY. Receiver only saves files.", flush=True)
        return
    try:
        names = models.gemini_models()
    except Exception as e:
        print(f"  !! could not list Gemini models: {e!r}", flush=True)
        return
    print(f"  gemini-4 models on this key: {[n for n in names if n.startswith('gemini-4')] or 'none'}", flush=True)
    for var, name in (("GEMINI_MODEL", models.GEMINI_MODEL), ("GEMINI_EMBED_MODEL", models.GEMINI_EMBED_MODEL)):
        if name not in names:
            print(f"  !! {var}={name} is NOT available on this key. Pick one from the list.", flush=True)
    print(f"  brain: {'OpenAI ' + models.OPENAI_CHAT_MODEL if os.environ.get('OPENAI_API_KEY') else 'Gemini ' + models.GEMINI_TEXT_MODEL}", flush=True)


if __name__ == "__main__":
    import uvicorn

    _check_models()
    # Restart recovery: flush anything the previous run was killed with still
    # unflushed (transcript tail, a whole conversation, a notes session, failed
    # Follow-Through forwards) so a backend restart never clears what was heard.
    memory.recover_pending()

    uvicorn.run(app, host="0.0.0.0", port=PORT)
