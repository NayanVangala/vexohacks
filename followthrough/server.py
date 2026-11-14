"""HTTP API for the dashboard. Run:  uvicorn followthrough.server:app --port 8002

Set OPENAI_API_KEY and it works. Without it only the staged demo runs, and it is labelled demo."""
import os
import threading
from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from . import agent
from .config import get_config
from .demo import DEMO_SEGMENTS, FakeLLM
from .executor import execute
from .models import Segment
from .store import Store, parse_text
from .transcribe import transcribe
from .verify import norm

app = FastAPI(title="Follow-Through")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
store = Store(get_config().data_dir)
approve_lock = threading.Lock()   # two taps on Approve must never run an action twice


def llm_for_current_transcript(cfg):
    """Real model when the key is set. Fake model only for the staged demo, otherwise refuse."""
    if store.demo:
        return FakeLLM(cfg)
    if not cfg.openai_api_key:
        raise HTTPException(503, "OPENAI_API_KEY is not set. Add it and restart. Only the staged demo works without it.")
    from .llm import OpenAILLM
    return OpenAILLM(cfg)


@app.get("/api/health")
def health():
    cfg = get_config()
    return {"llm": "openai" if cfg.openai_api_key else "demo only", "model": cfg.model,
            "transcribe_model": cfg.transcribe_model,
            "executor": "configured" if cfg.executor_url else "not set (drafts only)",
            "email_to": cfg.email_to, "calendar_account": cfg.calendar_account, "demo_loaded": store.demo}


@app.get("/api/state")
def state():
    """Everything the dashboard needs in one call."""
    return {"segments": [s.model_dump() for s in store.segments], "items": [i.model_dump() for i in store.ordered()],
            "todos": store.todos, "demo": store.demo, "health": health()}


class TextIn(BaseModel):
    text: str = ""
    segments: list[Segment] = []


@app.post("/api/transcript")
def add_transcript(body: TextIn):
    """Append text (Speaker: line per line) or ready made segments with times."""
    segs = body.segments or parse_text(body.text)
    if not segs:
        raise HTTPException(422, "nothing to add")
    if store.demo:
        store.replace([], demo=False)   # leaving demo mode: do not mix staged and real lines
    store.append(segs)
    return {"segments": len(store.segments)}


@app.post("/api/transcribe")
async def transcribe_audio(audio: UploadFile = File(...)):
    """Audio chunk to timestamped segments appended to the transcript. Needs OPENAI_API_KEY."""
    cfg = get_config()
    if not cfg.openai_api_key:
        raise HTTPException(503, "OPENAI_API_KEY is not set, cannot transcribe.")
    segs = transcribe(await audio.read(), audio.filename or "audio.wav", store.clock, cfg)
    if store.demo:
        store.replace([], demo=False)
    store.append(segs)
    return {"added": len(segs), "segments": len(store.segments)}


@app.post("/api/demo/load")
def load_demo():
    store.replace(DEMO_SEGMENTS, demo=True)
    return {"segments": len(store.segments)}


@app.post("/api/reset")
def reset():
    store.replace([], demo=False)
    store.todos.clear()
    return {"ok": True}


@app.post("/api/run")
def run_agent():
    """Find commitments in the whole transcript so far. Safe to call again as the transcript grows."""
    if not store.segments:
        raise HTTPException(422, "the transcript is empty")
    cfg = get_config()
    try:
        items = agent.run(store.segments, llm_for_current_transcript(cfg), cfg)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(502, f"model call failed: {type(e).__name__}: {e}")
    store.merge(items)
    return {"items": [i.model_dump() for i in store.ordered()]}


def _get(item_id: str):
    it = store.items.get(item_id)
    if not it:
        raise HTTPException(404, "no such item")
    return it


class EditIn(BaseModel):
    title: str | None = None
    due_local: str | None = None
    duration_min: int | None = None
    subject: str | None = None
    body: str | None = None


@app.patch("/api/items/{item_id}")
def edit(item_id: str, body: EditIn):
    """Edit the draft. Editing never makes an unverified quote verified."""
    with store.lock:
        it = _get(item_id)
        if it.status not in ("pending", "needs_review"):
            raise HTTPException(409, f"cannot edit an item that is {it.status}")
        upd = {k: v for k, v in body.model_dump().items() if v is not None}
        if "due_local" in upd:
            upd["due_local"] = agent._valid_time(upd["due_local"])
        it = it.model_copy(update=upd)
        issues = []
        if not it.proof.verified:
            issues.append("quote not found in the transcript")
        if it.action == "calendar" and not it.due_local:
            issues.append("no valid time, a calendar event needs one")
        if it.action in ("email", "reply") and not it.body.strip():
            issues.append("no text")
        it = it.model_copy(update={"issues": issues, "status": "needs_review" if issues else "pending"})
        store.items[item_id] = it
        store.save()
        return it


@app.post("/api/items/{item_id}/approve")
def approve(item_id: str):
    """The only route that runs anything. Only a verified, pending item can be approved."""
    with approve_lock:
        it = _get(item_id)
        if it.status != "pending":
            raise HTTPException(409, f"item is {it.status}, only pending items can be approved")
        done = execute(it, get_config(), store.todos)
        store.items[item_id] = done
        store.save()
        return done


@app.post("/api/items/{item_id}/retry")
def retry(item_id: str):
    """Put a failed item back to pending so it can be approved again. You choose to retry; it never happens by itself."""
    with store.lock:
        it = _get(item_id)
        if it.status != "failed":
            raise HTTPException(409, f"only failed items can be retried, this one is {it.status}")
        store.items[item_id] = it.model_copy(update={"status": "pending", "result": ""})
        store.save()
        return store.items[item_id]


@app.post("/api/items/{item_id}/dismiss")
def dismiss(item_id: str):
    with store.lock:
        it = _get(item_id)
        if it.status in ("done", "drafted"):
            raise HTTPException(409, f"item is already {it.status}")
        store.items[item_id] = it.model_copy(update={"status": "dismissed"})
        store.save()
        return store.items[item_id]


# The built dashboard (dashboard/dist) is served from the same address, so the phone needs no CORS or second server.
_dist = os.path.join(os.path.dirname(__file__), "..", "dashboard", "dist")
if os.path.isdir(_dist):
    app.mount("/", StaticFiles(directory=_dist, html=True), name="dashboard")
