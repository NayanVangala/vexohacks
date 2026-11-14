"""Glasses memory: objects, tasks, events in one sqlite table, searched by embedding.

Frames -> models.see (Gemini 4 Hydrogen, every frame) -> object rows + event rows + receipts.
Audio  -> models.transcribe -> "hey scene ..." questions are answered aloud (voice.py);
          everything else joins a rolling transcript -> models.extract_tasks every ~30 s
          -> tasks / done / money owed, each kept ONLY if its quote is really in the transcript.
Ask    -> embed question -> cosine top-k, drop weak hits -> models.answer.
Cards  -> anything the wearer should see (answer, new task, payment to approve) lands in a
          feed the phone polls (GET /feed) and renders on the glasses.

Two worker lanes (frames, audio) so a slow transcription never delays a frame, and vice versa.
"""

import itertools
import json
import math
import os
import queue
import sqlite3
import re
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path

import httpx
import outbox
import models
import notes
import payments
import voice
from awareness import Awareness, parse_yes_no

DATA_DIR = Path(os.environ.get("SCENE_DATA_DIR", Path(__file__).parent / "data"))
ENABLED = bool(os.environ.get("GEMINI_API_KEY"))  # OpenAI optional: models.py falls back to Gemini

# Optional: also feed what the glasses hear to Follow-Through (src/followthrough), which turns
# commitments into calendar events / emails behind an approval. Off unless set, e.g. http://localhost:8002
FOLLOWTHROUGH_URL = os.environ.get("FOLLOWTHROUGH_FORWARD_URL", "").rstrip("/")
FRAME_EVERY_S = float(os.environ.get("SCENE_FRAME_EVERY_S", "0"))  # 0 = every frame (Hydrogen is fast)
MIN_SIM = float(os.environ.get("SCENE_MIN_SIM", "0.25"))  # weaker hits never reach the answer model (lesson:
                                                         # an unrelated memory is worse than none). Tune live.
EXTRACT_EVERY_S = 30.0   # how much transcript to batch before pulling tasks out
DONE_MATCH = 0.5         # cosine needed for "I sent the deck" to close "Send the deck". Tune live.

_db_lock = threading.Lock()
_conn: sqlite3.Connection | None = None
# Frame lane is small on purpose: if Gemini falls behind, drop frames rather than answer about the past.
_lanes = {"frame": queue.Queue(maxsize=3), "audio": queue.Queue(maxsize=50)}
_workers: dict[str, threading.Thread] = {}
_cards: deque = deque(maxlen=50)
_card_ids = itertools.count(1)
_last_frame = 0.0
_last_extract = time.time()
_transcript: list[str] = []
awareness = Awareness()
STOP_NOTES = re.compile(r"\b(stop|end|finish)\s+(the\s+)?notes\b", re.I)
MAX_ANSWER_WORDS = 6   # a spoken yes/no to an offer is short; a long sentence in a meeting is not an answer


# ---------- storage ----------

def db() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        _conn = sqlite3.connect(DATA_DIR / "memory.db", check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("""CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY,
            kind TEXT NOT NULL,          -- object | task | event
            text TEXT NOT NULL,          -- object name, task, or activity sentence
            place TEXT,
            frame TEXT,                  -- filename in data/frames
            ts REAL NOT NULL,
            status TEXT,                 -- open | done (tasks only)
            embedding TEXT               -- JSON list[float]
        )""")
    return _conn


def add(kind: str, text: str, place: str | None = None, frame: str | None = None,
        status: str | None = None) -> int:
    vec = models.embed(f"{text}: {place}" if place else text)
    with _db_lock:
        cur = db().execute(
            "INSERT INTO memories (kind, text, place, frame, ts, status, embedding) VALUES (?,?,?,?,?,?,?)",
            (kind, text, place, frame, time.time(), status, json.dumps(vec)))
        db().commit()
        return cur.lastrowid


def rows(sql: str, *args) -> list[dict]:
    with _db_lock:
        return [dict(r) for r in db().execute(sql, args)]


def _cos(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)) or 1)


def search(vec: list[float], k: int = 5, where: str = "1=1", *args) -> list[tuple[float, dict]]:
    # ponytail: brute-force cosine over every row in Python, fine to ~10k rows; sqlite-vec if it grows.
    scored = []
    for r in rows(f"SELECT * FROM memories WHERE embedding IS NOT NULL AND {where}", *args):
        e = json.loads(r["embedding"])
        if len(e) != len(vec):  # embedded by the other provider (key added/removed): re-embed once, in place
            e = _reembed(r)
        scored.append((_cos(vec, e), r))
    return sorted(scored, key=lambda s: s[0], reverse=True)[:k]


def _reembed(r: dict) -> list[float]:
    vec = models.embed(f"{r['text']}: {r['place']}" if r["place"] else r["text"])
    with _db_lock:
        db().execute("UPDATE memories SET embedding=? WHERE id=?", (json.dumps(vec), r["id"]))
        db().commit()
    return vec


# ---------- ingest (called from server, cheap) ----------

def submit(lane: str, fn, *args) -> None:
    if lane not in _workers:
        _workers[lane] = threading.Thread(target=_run, args=(_lanes[lane],), daemon=True)
        _workers[lane].start()
    try:
        _lanes[lane].put_nowait((fn, args))
    except queue.Full:
        print(f"memory: {lane} lane full, dropping", flush=True)


def _run(q: queue.Queue) -> None:
    while True:
        fn, args = q.get()
        try:
            fn(*args)
        except Exception as e:  # one bad model call must not kill the worker
            print(f"memory: {fn.__name__} failed: {e!r}", flush=True)


def on_frame(path: Path) -> None:
    global _last_frame
    now = time.time()
    if not ENABLED or now - _last_frame < FRAME_EVERY_S:
        return
    _last_frame = now
    submit("frame", process_frame, path)


def on_audio(path: Path) -> None:
    if ENABLED:
        submit("audio", process_audio, path)


# ---------- cards for the glasses ----------

def push_card(kind: str, title: str, body: str, ref: int | None = None, frame: str | None = None) -> dict:
    """kind: answer | task | payment. ref = task or payment id, so the glasses' buttons know what to act on."""
    card = {"id": next(_card_ids), "kind": kind, "title": title, "body": body,
            "ref": ref, "frame": frame, "ts": time.time()}
    _cards.append(card)
    return card


def cards_after(card_id: int) -> list[dict]:
    return [c for c in list(_cards) if c["id"] > card_id]


def _norm(s: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9$.]+", " ", s.lower()).split())


def quoted(item: dict, transcript: str) -> bool:
    """Quote verification: an extracted item is kept only if its quote really was said.
    Stops the model inventing a task, or a payment request, out of small talk."""
    q = _norm(item.get("quote") or "")
    ok = len(q) >= 8 and q in _norm(transcript)
    if not ok:
        print(f"memory: dropped unverified item {item!r}", flush=True)
    return ok


# ---------- ingest (worker thread, calls models) ----------

def process_frame(path: Path) -> None:
    seen = models.see(path.read_bytes())
    handle_scene(seen.get("scene"), path.name)
    for o in seen.get("objects", []):
        name, place = o.get("name", "").strip().lower(), o.get("place", "").strip()
        if not name:
            continue
        last = rows("SELECT id, place FROM memories WHERE kind='object' AND text=? ORDER BY ts DESC LIMIT 1", name)
        if last and last[0]["place"] == place:  # same thing, same spot: refresh, skip an embed call
            with _db_lock:
                db().execute("UPDATE memories SET ts=?, frame=? WHERE id=?", (time.time(), path.name, last[0]["id"]))
                db().commit()
        else:
            add("object", name, place, path.name)
            print(f"memory: {name} @ {place}", flush=True)

    r = seen.get("receipt") or {}
    if r.get("total"):
        text = f"Receipt: {r.get('merchant') or 'unknown'} ${float(r['total']):.2f}"
        last = rows("SELECT text FROM memories WHERE kind='event' AND text=? AND ts > ?", text, time.time() - 3600)
        if not last:  # same receipt seen for many frames: log once
            add("event", text, frame=path.name)
            print(f"memory: {text}", flush=True)

    activity = (seen.get("activity") or "").strip()
    last = rows("SELECT text FROM memories WHERE kind='event' ORDER BY ts DESC LIMIT 1")
    if activity and not (last and last[0]["text"] == activity):
        add("event", activity, frame=path.name)


def process_audio(path: Path) -> None:
    wav = path.read_bytes()
    if voice.is_silent(wav):   # room tone: skip the API call, and the hallucinations it invites
        return
    text = models.transcribe(wav)
    if not text or voice.is_noise(text):
        return
    pending = awareness.pending(time.time())
    if pending and len(text.split()) <= MAX_ANSWER_WORDS and parse_yes_no(text) != "unclear":
        answer_offer(pending.id, parse_yes_no(text) == "yes")
        return
    if notes.active() and STOP_NOTES.search(text):
        finish_notes()
        return
    question = voice.heard(text)
    if question:  # a question to Scene is not a task someone gave you: keep it out of the transcript
        out = ask(question)
        push_card("answer", question, out["answer"], ref=out["memory_id"], frame=out["frame"])
        voice.speak(out["answer"])
        print(f"memory: VOICE {question!r} -> {out['answer']!r}", flush=True)
        return
    if voice.listening():  # "hey scene" heard, question still coming: not part of any conversation
        return
    notes.add_line(text)
    _transcript.append(text)
    if time.time() - _last_extract >= EXTRACT_EVERY_S and _transcript:
        flush_transcript()


# ---------- scene awareness: offers and notes (see docs/superpowers/specs/2026-11-14-scene-awareness-design.md) ----------

def handle_scene(scene: dict | None, frame: str | None) -> None:
    """One analyzed frame's scene: feed the note session, remember menus and the like, maybe offer notes."""
    now = time.time()
    if notes.active() and notes.on_scene(scene, frame, now):
        finish_notes()
    awareness.set_busy(notes.active())
    text = awareness.loggable(scene, now)
    if text:
        add("event", text, frame=frame)
    offer = awareness.feed(scene, now)
    if offer:
        push_card("offer", "Take notes?", offer.text, ref=offer.id, frame=frame)
        voice.speak(offer.text)
        print(f"memory: OFFER {offer.text!r}", flush=True)


def answer_offer(offer_id: int, accept: bool) -> dict:
    """KeyError when that offer is not the one waiting (unknown, expired, or already answered)."""
    now = time.time()
    if accept:
        awareness.accept(offer_id, now)
        notes.start(now)
        awareness.set_busy(True)
    else:
        awareness.decline(offer_id, now)
    return {"offer": offer_id, "accepted": accept}


def finish_notes() -> int | None:
    """End the session and build the notes. Returns the notes memory id, or None when none was running."""
    session = notes.stop()
    awareness.set_busy(False)
    if session is None:
        return None
    try:
        out = notes.build(session, make=models.make_notes)
    except Exception as e:  # the model failed: keep the slides rather than lose the meeting
        print(f"memory: notes build failed: {e!r}", flush=True)
        out = notes.build(session, make=lambda transcript, slides: {})
    for a in out["action_items"]:
        tid = add("task", a["text"], status="open")
        if a.get("action") == "email":  # still a task, but the lens offers to send the email
            d = outbox.draft(a.get("to", ""), a["subject"], a["body"], a["quote"])
            push_card("email", f"Email {d['to_name'] or 'them'}?", f"{d['subject']}: {d['body']}", ref=d["id"])
        elif a.get("action") == "calendar":
            d = outbox.draft_event(a["text"], a["start"], a["duration_min"], a["quote"])
            when = datetime.strptime(a["start"], "%Y-%m-%dT%H:%M").strftime("%a %b %-d, %-I:%M %p")
            push_card("calendar", "Add to calendar?", f"{a['text']} · {when}", ref=d["id"])
        else:
            push_card("task", "New task", a["text"], ref=tid)
    summary = out["summary"] or "Meeting notes"
    nid = add("notes", summary)
    (DATA_DIR / "notes").mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "notes" / f"{nid}.json").write_text(json.dumps(out))
    push_card("notes", "Meeting notes", out["summary"] or "No summary.", ref=nid)
    print(f"memory: NOTES #{nid} ({len(out['action_items'])} tasks, {len(out['slides'])} slides)", flush=True)
    return nid


def get_notes(note_id: int) -> dict | None:
    p = DATA_DIR / "notes" / f"{note_id}.json"
    return json.loads(p.read_text()) if p.is_file() else None


def flush_transcript() -> None:
    global _last_extract
    text = " ".join(_transcript)
    _transcript.clear()
    _last_extract = time.time()
    receipts = [r["text"] for r in rows(
        "SELECT text FROM memories WHERE kind='event' AND text LIKE 'Receipt:%' AND ts > ?", time.time() - 3600)]
    forward_to_followthrough(text)
    out = models.extract_tasks(text, receipts)
    for t in out.get("tasks", []):
        if not quoted(t, text):
            continue
        bits = [b for b in (f"from {t.get('from_whom')}" if t.get("from_whom") else "",
                            f"due {t.get('due')}" if t.get("due") else "") if b]
        task = t["text"] + (f" ({', '.join(bits)})" if bits else "")
        tid = add("task", task, status="open")
        push_card("task", "New task", task, ref=tid)
        print(f"memory: TASK {task}", flush=True)
    for m in out.get("owed_to_me", []):
        if not quoted(m, text):
            continue
        try:
            pid = payments.propose(m["who"], round(float(m["amount"]) * 100), m.get("memo") or "shared expense")
            p = payments.get(pid)
            push_card("payment", f"Request ${p['amount_cents'] / 100:.2f} from {p['counterparty']}?",
                      f"{p['memo']} — \"{m['quote']}\"", ref=pid)
        except (payments.PaymentError, KeyError, ValueError, TypeError) as e:
            print(f"memory: skipped money item {m!r}: {e}", flush=True)
    for item in out.get("done", []):
        if not quoted(item, text):
            continue
        d = item["text"]
        add("event", f"Done: {d}")
        hits = search(models.embed(d), 1, "kind='task' AND status='open'")
        if hits and hits[0][0] >= DONE_MATCH:
            set_done(hits[0][1]["id"])
            print(f"memory: closed task {hits[0][1]['text']!r} ({hits[0][0]:.2f})", flush=True)


def forward_to_followthrough(text: str) -> None:
    """Append the last ~30 s of conversation to Follow-Through and let it look for commitments.
    Never blocks Scene's own extraction: any failure is one log line."""
    if not FOLLOWTHROUGH_URL:
        return
    try:
        with httpx.Client(timeout=60) as c:
            c.post(f"{FOLLOWTHROUGH_URL}/api/transcript", json={"text": text}).raise_for_status()
            c.post(f"{FOLLOWTHROUGH_URL}/api/run").raise_for_status()
    except Exception as e:
        print(f"memory: Follow-Through forward failed: {e!r}", flush=True)


def set_done(task_id: int) -> bool:
    with _db_lock:
        n = db().execute("UPDATE memories SET status='done' WHERE id=? AND kind='task'", (task_id,)).rowcount
        db().commit()
    return n > 0


# ---------- query ----------

def ask(question: str) -> dict:
    now = time.time()
    hits = [(s, r) for s, r in search(models.embed(question), 8) if s >= MIN_SIM]
    # Open tasks are always relevant to "what do I need to do?"-style questions and there are few
    # of them: include them even when the wording doesn't match closely.
    seen = {r["id"] for _, r in hits}
    hits += [(0.0, r) for r in rows("SELECT * FROM memories WHERE kind='task' AND status='open' ORDER BY ts DESC LIMIT 5")
             if r["id"] not in seen]
    if not hits:  # no model call: nothing relevant is better than a confident wrong answer
        return {"question": question, "answer": "I don't have a memory of that.", "memory_id": None, "frame": None}
    mems = [{"id": r["id"], "kind": r["kind"], "text": r["text"], "place": r["place"],
             "status": r["status"], "age_min": round((now - r["ts"]) / 60)} for _, r in hits]
    out = models.answer(question, mems)
    best = next((r for _, r in hits if r["id"] == out.get("memory_id")), None)
    return {"question": question, "answer": out.get("answer", ""),
            "memory_id": best and best["id"], "frame": best and best["frame"]}


def open_tasks(limit: int = 6) -> list[dict]:
    return rows("SELECT id, text, ts FROM memories WHERE kind='task' AND status='open' ORDER BY ts DESC LIMIT ?", limit)


def items(limit: int = 6) -> list[dict]:
    return rows("""SELECT id, text AS name, place, frame, MAX(ts) AS ts FROM memories
                   WHERE kind='object' GROUP BY text ORDER BY ts DESC LIMIT ?""", limit)


def today() -> dict:
    """Home-screen numbers: what Scene took in today, by hour (laptop's local time)."""
    lt = time.localtime()
    midnight = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
    ts = [r["ts"] for r in rows("SELECT ts FROM memories WHERE ts >= ?", midnight)]
    hourly = [0] * 24
    for t in ts:
        hourly[time.localtime(t).tm_hour] += 1
    count = lambda sql: rows(sql)[0]["n"]  # noqa: E731
    return {
        "memories": len(ts),
        "open_tasks": count("SELECT COUNT(*) AS n FROM memories WHERE kind='task' AND status='open'"),
        "things": count("SELECT COUNT(DISTINCT text) AS n FROM memories WHERE kind='object'"),
        "hourly": hourly,
        "now_hour": lt.tm_hour,
    }
