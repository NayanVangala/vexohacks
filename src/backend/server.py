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

import orders
import outbox
import memory
import models
import notes
import payments
import voice

DATA_DIR = Path(os.environ.get("SCENE_DATA_DIR", Path(__file__).parent / "data"))
PORT = int(os.environ.get("PORT", "8001"))

PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")  # HTTPS tunnel; display images need HTTPS

app = FastAPI(title="scene receiver")
(DATA_DIR / "frames").mkdir(parents=True, exist_ok=True)
app.mount("/frames", StaticFiles(directory=DATA_DIR / "frames"), name="frames")
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
}


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
    print(f"audio #{n} {len(data)} B -> {path.name}", flush=True)
    memory.on_audio(path)
    return {"saved": path.name, "kind": "audio"}


@app.post("/activate")
async def activate():
    with _lock:
        _stats["activations"] += 1
        n = _stats["activations"]
    print(f"ACTIVATE #{n}", flush=True)
    return {"activations": n}


def _frame_url(frame: str | None) -> str | None:
    return f"{PUBLIC_BASE_URL}/frames/{frame}" if frame and PUBLIC_BASE_URL else None


@app.post("/ask")
async def ask(text: str | None = Form(None), audio: UploadFile | None = File(None)):
    if audio is not None:
        data = await audio.read()
        if not _is_wav(data):
            _reject(f"audio is not WAV ({len(data)} B)")
        text = await run_in_threadpool(models.transcribe, data)
    if not text or not text.strip():
        raise HTTPException(status_code=422, detail="send `text` or `audio` with speech in it")
    out = await run_in_threadpool(memory.ask, text.strip())  # blocking model calls off the event loop
    print(f"ASK {out['question']!r} -> {out['answer']!r}", flush=True)
    return {**out, "image_url": _frame_url(out["frame"])}


@app.get("/tasks")
async def tasks():
    return memory.open_tasks()


@app.post("/tasks/{task_id}/done")
async def task_done(task_id: int):
    if not memory.set_done(task_id):
        raise HTTPException(status_code=404, detail="no such task")
    return {"done": task_id}


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


@app.post("/notes/stop")
async def notes_stop():
    nid = await run_in_threadpool(memory.finish_notes)  # one model call, off the event loop
    if nid is None:
        raise HTTPException(status_code=409, detail="no notes are being taken")
    return {"notes": nid}


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


class LocationIn(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)


class ReplyIn(BaseModel):
    text: str


@app.post("/location")
async def location(body: LocationIn):
    """The device's position, for "nearest McDonald's". Valid for 30 minutes."""
    memory.set_location(body.lat, body.lng)
    return {"ok": True}


@app.get("/agent")
async def agent_state():
    return memory.agent.state()


@app.post("/agent/reply")
async def agent_reply(body: ReplyIn):
    """Yes/No buttons on the glasses (or a typed answer) for the question the dining agent is waiting on."""
    try:
        memory.agent.reply(body.text)
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
async def dev_location(body: LocationIn):
    _dev_only()
    memory.set_location(body.lat, body.lng)
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
    if not path.startswith(("/capture", "/feed", "/tasks", "/items", "/payments", "/today", "/stats", "/client-log",
                            "/frames", "/ft/", "/offers", "/notes", "/emails", "/events", "/location", "/agent", "/orders", "/order", "/dev")):
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


@app.get("/settings")
async def settings_get():
    """`output` is null until the wearer picks, so the glasses app knows to ask."""
    return {"output": voice.output_mode(default=None), "modes": list(voice.OUTPUT_MODES),
            "speak_on": voice.speak_on(), "speak_targets": list(voice.SPEAK_TARGETS),
            "paused": voice.paused()}


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

    uvicorn.run(app, host="0.0.0.0", port=PORT)
