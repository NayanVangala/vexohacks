"""Glasses memory: objects, tasks, events in one sqlite table, searched by embedding.

Frames -> models.see (Gemini 4 Hydrogen, every frame) -> object rows + event rows + receipts.
Audio  -> models.transcribe -> "hey scene ..." questions are answered aloud (voice.py);
          everything else joins a rolling transcript -> models.extract_tasks every ~30 s
          -> tasks / done / money owed, each kept ONLY if its quote is really in the transcript.
          In Conversation Mode the extract runs on the WHOLE conversation transcript so far
          (see _conv_transcript): a request and its day/time arrive in different 2.5 s chunks.
Ask    -> embed question -> cosine top-k, drop weak hits -> models.answer.
Cards  -> anything the wearer should see (answer, new task, payment to approve) lands in a
          feed the phone polls (GET /feed) and renders on the glasses.
Unknown faces -> during a conversation a clear face no enrolled photo matches saves its
          frame only, silently. When the conversation ends Scene asks once who it was,
          and a name (spoken, or typed on the phone) enrolls that saved frame via
          enroll_face(). Skip / "no one" discards it. Never enrolls without a name,
          never enrolls silently, never asks mid-conversation.

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
from datetime import datetime, timedelta
from pathlib import Path

import agent as agent_mod
import contacts
import httpx
import outbox
import lead
import lookup
import models
import notes
import payments
import voice
from awareness import Awareness, parse_yes_no

DATA_DIR = Path(os.environ.get("SCENE_DATA_DIR", Path(__file__).parent / "data"))
notes.set_data_dir(DATA_DIR)  # notes sessions persist to data/notes-session.json
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
# "extract" holds the slow transcript work (task extraction, Follow-Through's GPT-4o run, often
# 5-15 s) so it never sits in front of a "hey scene" question on the audio lane.
_lanes = {"frame": queue.Queue(maxsize=3), "audio": queue.Queue(maxsize=50), "extract": queue.Queue(maxsize=20),
          "lead": queue.Queue(maxsize=2)}
_workers: dict[str, threading.Thread] = {}
_embed_cache: dict[int, tuple[list[float], float]] = {}   # memory id -> (embedding vector, norm): parsing 1536 floats per row on every ask was the slow part of search()
_last_frame = 0.0
_last_extract = time.time()
_transcript: list[str] = []
# Rolling transcript of the CURRENT Conversation Mode session only, never cleared
# by the 30 s flush: speech arrives in ~2.5 s chunks and one sentence routinely
# spans several of them ("block out a time..." / "on Tuesday at 2 p.m. Actually,
# no. Make it" / "Tuesday at 5 p.m. for math class."), so extracting from a single
# chunk drops the day/time and invents fragment tasks. Every extract during a
# conversation (live, 30 s flush, and the final extract at conversation end) runs
# on this whole transcript instead. Cleared when the conversation ends; capped so
# a very long conversation does not grow the prompt without bound.
_conv_transcript: list[str] = []
_CONV_TRANSCRIPT_MAX_CHUNKS = 200
awareness = Awareness()
STOP_NOTES = re.compile(r"\b(stop|end|finish)\s+(the\s+)?notes\b", re.I)
LEAD_MAX_AGE_MIN = float(os.environ.get("SCENE_LEAD_MAX_AGE_MIN", "180"))  # older sightings: say where, don't offer to walk
MAX_ANSWER_WORDS = 6   # a spoken yes/no to an offer is short; a long sentence in a meeting is not an answer
_STOP_WORDS = re.compile(r"\b(stop|cancel|never ?mind)\b", re.I)  # heard even over our own voice
# Spoken to a Follow-Through question, these mean "ask me later", not "dismiss this promise
# forever". The glasses Later button already means exactly this; the voice path used to treat
# "skip" / "not now" as a hard no, which is how an already-approved promise ended up "Skipped".
_LATER_WORDS = re.compile(r"\b(skip|stop|not now|later)\b", re.I)
_silent_dropped = 0       # silent chunks since the last log line (throttled: they arrive every ~2.5 s)
_last_silent_log = 0.0


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
            speaker TEXT,                -- who said/asked it (from_whom); NULL = unknown, never guessed
            embedding TEXT               -- JSON list[float]
        )""")
        # Cards live in the DB so the phone's lastCardId stays valid across a backend restart.
        _conn.execute("""CREATE TABLE IF NOT EXISTS cards (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            kind TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL,
            ref INTEGER, frame TEXT, speaker TEXT, ts REAL NOT NULL
        )""")
        # Face enrollment (stub, NOT recognition): just stores a name + reference photo.
        _conn.execute("""CREATE TABLE IF NOT EXISTS faces (
            name TEXT PRIMARY KEY,       -- display name, e.g. Srijan
            photo TEXT NOT NULL,         -- filename in data/faces
            ts REAL NOT NULL
        )""")
        # Durable pending transcript (the restart fix): every heard line is
        # written here the moment it is heard, and deleted only after
        # _process_transcript has run extract on it. The RAM lists _transcript
        # / _conv_transcript used to be the only copy, and the 30 s flush only
        # fired on a NEW audio chunk -- stop streaming and the tail never
        # flushed, restart the backend and kill -9 took the tail, the whole
        # Conversation Mode transcript, and any queued extract job with it.
        _conn.execute("""CREATE TABLE IF NOT EXISTS pending_transcript (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            text TEXT NOT NULL,
            speaker TEXT,
            conv INTEGER NOT NULL DEFAULT 0,  -- 1 = part of the rolling conversation transcript
            ts REAL NOT NULL
        )""")
        _ensure_speaker_columns(_conn)
    return _conn


def _ensure_speaker_columns(conn: sqlite3.Connection) -> None:
    """Existing DBs were created before the speaker column existed; add it if missing."""
    for table in ("memories", "cards"):
        cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if "speaker" not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN speaker TEXT")
    # Detail view: a task keeps the exact quote it came from, verbatim, so the
    # phone can show what was actually said. Older rows have no quote.
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(memories)")}
    if "quote" not in cols:
        conn.execute("ALTER TABLE memories ADD COLUMN quote TEXT")
    conn.commit()


# ---------- durable pending transcript + flush watchdog ----------
# One commit per ~2.5 s chunk is cheap; losing a whole session tail is not.
_PENDING_FLUSH_IDLE_S = 10.0    # flush the tail this long after the last heard line
_PENDING_WATCHDOG_EVERY_S = 5.0
_last_speech_at: float | None = None
_watchdog_started = False
_watchdog_lock = threading.Lock()


def _pending_append(text: str, speaker: str | None, conv: bool) -> None:
    """Write one heard line to disk the moment it is heard."""
    with _db_lock:
        db().execute("INSERT INTO pending_transcript (text, speaker, conv, ts) VALUES (?,?,?,?)",
                     (text, speaker, 1 if conv else 0, time.time()))
        db().commit()


def _pending_rows(conv: bool | None = None) -> list[dict]:
    sql = "SELECT id, text, speaker, conv, ts FROM pending_transcript"
    args: list = []
    if conv is not None:
        sql += " WHERE conv=?"
        args.append(1 if conv else 0)
    return rows(sql + " ORDER BY id", *args)


def _pending_delete(ids: list[int]) -> None:
    """Only ever called after _process_transcript has run extract on the batch:
    until then the rows are the recovery copy."""
    if not ids:
        return
    with _db_lock:
        db().execute(f"DELETE FROM pending_transcript WHERE id IN ({','.join('?' * len(ids))})", list(ids))
        db().commit()


def _ft_retry_path() -> Path:
    return DATA_DIR / "pending_ft.jsonl"


def _queue_ft_retry(text: str) -> None:
    """A Follow-Through forward that failed (FT down) is queued on disk instead
    of being one log line and gone: it is replayed on the next startup."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(_ft_retry_path(), "a") as f:
            f.write(json.dumps({"text": text, "ts": time.time()}) + "\n")
    except Exception as e:  # pragma: no cover - disk failure only
        print(f"memory: could not queue FT retry: {e!r}", flush=True)


def _replay_pending_ft() -> int:
    """Replay queued FT forwards. Returns how many succeeded. Failed ones stay
    queued for the next startup; a batch already covered by another run is a
    harmless duplicate post (FT re-runs over its whole transcript)."""
    p = _ft_retry_path()
    if not p.is_file():
        return 0
    try:
        lines = p.read_text().splitlines()
    except OSError:
        return 0
    done, rest = 0, []
    for line in lines:
        try:
            text = json.loads(line)["text"]
        except Exception:
            continue
        if forward_to_followthrough(text, queue_retry=False):
            done += 1
        else:
            rest.append(line)
    try:
        if rest:
            p.write_text("\n".join(rest) + "\n")
        else:
            p.unlink()
    except OSError:
        pass
    return done


def _flush_watchdog() -> None:
    """Flush the tail after streaming stops: no new chunk arrives to trigger
    the 30 s flush, so without this the last lines sit in RAM until a restart
    kills them. Also ends a conversation on silence (that check used to run
    only on incoming chunks/frames, i.e. never after streaming stops)."""
    while True:
        time.sleep(_PENDING_WATCHDOG_EVERY_S)
        try:
            now = time.time()
            if _conv_active:
                _maybe_end_conversation(now)
            if _transcript and _last_speech_at and now - _last_speech_at >= _PENDING_FLUSH_IDLE_S:
                flush_transcript(background=True)
        except Exception as e:  # never let the watchdog thread die silently
            print(f"memory: flush watchdog failed: {e!r}", flush=True)


def _ensure_watchdog() -> None:
    global _watchdog_started
    with _watchdog_lock:
        if _watchdog_started:
            return
        _watchdog_started = True
    threading.Thread(target=_flush_watchdog, daemon=True).start()


def recover_pending() -> dict:
    """Startup: reload lines the backend was killed with still unflushed and
    flush them now, so nothing heard is lost to a restart. A pending
    conversation is treated as finished (the wearer stopped streaming when the
    backend died): its whole transcript is the extract text, and its rows are
    deleted once extract has run, so a second restart never re-extracts it.
    Also resumes an in-progress notes session and replays failed FT forwards."""
    _ensure_watchdog()
    _seed_seen_quotes()
    try:
        notes.recover()
    except Exception as e:
        print(f"memory: notes recovery failed: {e!r}", flush=True)
    pend = _pending_rows()
    ft_done = _replay_pending_ft()
    if not pend:
        print(f"memory: RECOVER pending=0 ft_replayed={ft_done}", flush=True)
        return {"pending": 0, "ft_replayed": ft_done}
    global _transcript, _conv_transcript
    _transcript = [r["text"] for r in pend if not r["conv"]]
    _conv_transcript = [r["text"] for r in pend if r["conv"]]
    extract_text = " ".join([r["text"] for r in pend])
    print(f"memory: RECOVER pending={len(pend)} conv_lines={len(_conv_transcript)} ft_replayed={ft_done}: flushing now", flush=True)
    flush_transcript(background=True, extract_text=extract_text or None, end_of_conversation=True)
    return {"pending": len(pend), "ft_replayed": ft_done}


def stop_streaming() -> dict:
    """The wearer stopped streaming (POST /session/stop): save everything heard
    so far, synchronously, before the phone / backend goes away. The pending
    rows stay on disk until extract has run on them, so even a restart in the
    next second cannot lose them -- recover_pending() picks them up."""
    out: dict = {"flushed_chars": 0, "notes_id": None, "conversation_stopped": False}
    if _conv_active:
        out["conversation_stopped"] = stop_conversation(reason="streaming_stop")
    if _transcript:
        out["flushed_chars"] = len(" ".join(_transcript))
        flush_transcript(background=False)
    if notes.active():
        out["notes_id"] = finish_notes()
    return out


def add(kind: str, text: str, place: str | None = None, frame: str | None = None,
        status: str | None = None, speaker: str | None = None, quote: str | None = None) -> int:
    """speaker is who said/asked it, only when the backend actually knows (from_whom from
    extract_tasks). None means unknown: never invent a name. quote is the exact words the
    task came from, verbatim, or None when there is none."""
    vec = models.embed(f"{text}: {place}" if place else text)
    with _db_lock:
        cur = db().execute(
            "INSERT INTO memories (kind, text, place, frame, ts, status, speaker, embedding, quote) VALUES (?,?,?,?,?,?,?,?,?)",
            (kind, text, place, frame, time.time(), status, speaker, json.dumps(vec), quote))
        db().commit()
        return cur.lastrowid


def rows(sql: str, *args) -> list[dict]:
    with _db_lock:
        return [dict(r) for r in db().execute(sql, args)]


def _cos(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    return dot / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)) or 1)


def search(vec: list[float], k: int = 5, where: str = "1=1", *args) -> list[tuple[float, dict]]:
    # Brute-force cosine over every row in Python, fine to ~10k rows; sqlite-vec if it grows.
    # Two things used to make this slow on every ask: json.loads of every 1536-float
    # embedding (parsed once now, cached by row id), and _reembed() firing a live
    # embedding API call per row when the provider changed -- a row whose embedding
    # is the wrong length is skipped instead (it stays in the DB, it just never wins
    # until it is re-embedded offline). With ~1000 memories this was ~0.5-1 s per ask.
    qnorm = math.sqrt(sum(x * x for x in vec)) or 1
    scored, skipped = [], 0
    for r in rows(f"SELECT * FROM memories WHERE embedding IS NOT NULL AND {where}", *args):
        cached = _embed_cache.get(r["id"])
        if cached is None:
            e = json.loads(r["embedding"])
            if len(e) != len(vec):
                skipped += 1
                continue
            cached = (e, math.sqrt(sum(x * x for x in e)) or 1)
            _embed_cache[r["id"]] = cached
        e, en = cached
        scored.append((sum(x * y for x, y in zip(vec, e)) / (qnorm * en), r))
    if skipped:
        print(f"memory: search skipped {skipped} rows with stale-length embeddings (provider changed?)", flush=True)
    return sorted(scored, key=lambda s: s[0], reverse=True)[:k]


def _reembed(r: dict) -> list[float]:
    vec = models.embed(f"{r['text']}: {r['place']}" if r["place"] else r["text"])
    with _db_lock:
        db().execute("UPDATE memories SET embedding=? WHERE id=?", (json.dumps(vec), r["id"]))
        db().commit()
    _embed_cache.pop(r["id"], None)   # the cached vector for this row is now stale
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


# ---------- Display Audio Mode (see server.py) ----------
# While the glasses web app is open, the DAT camera cannot run, so there are
# no new frames to see. Audio still arrives via the glasses mic (audio is
# glasses-only, nothing from the phone) and on_audio() below is untouched by
# this flag. Set by server.py from the lens heartbeat.
DISPLAY_AUDIO_MODE = False


def set_display_audio_mode(value: bool) -> None:
    global DISPLAY_AUDIO_MODE
    if value != DISPLAY_AUDIO_MODE:
        DISPLAY_AUDIO_MODE = value
        print(f"memory: display audio mode {'ON (frames paused, glasses mic listening)' if value else 'OFF (frames resume)'}", flush=True)


# ---------- display focus: "tell me more about this" ----------
# The glasses web app posts the Follow-Through / Needs-you item the wearer is
# looking at (opened, or D-pad-focused on a list) to POST /focus, and re-posts
# it on every display heartbeat while it is still on screen. The backend keeps
# only that one item, in memory, so "this" in a spoken question resolves to
# exactly that item. It expires after 5 min, on display close, on approve /
# dismiss, or when another item takes focus. Nothing is ever guessed: with no
# fresh focus, Scene says so instead of naming a different item.
FOCUS_TTL_S = 300.0
_focus: dict | None = None
_focus_lock = threading.Lock()


def set_focus(item: dict) -> dict:
    """Store the display's current item (id + title required; quote, speaker,
    status, action, due, body, context lines when the display knows them).
    Returns the stored copy."""
    global _focus
    clean = {"id": str(item["id"])[:64], "kind": str(item.get("kind") or "promise")[:32],
             "title": str(item["title"])[:200], "ts": time.time()}
    for f in ("quote", "speaker", "status", "action", "due_local", "body"):
        v = item.get(f)
        clean[f] = str(v)[:1000] if v is not None else None
    ctx = item.get("context")
    clean["context"] = [str(c)[:200] for c in ctx][:4] if isinstance(ctx, list) else []
    with _focus_lock:
        _focus = clean
    print(f"memory: FOCUS {clean['id']!r} {clean['title']!r}", flush=True)
    return dict(clean)


def clear_focus() -> None:
    global _focus
    with _focus_lock:
        _focus = None


def get_focus() -> dict | None:
    """The focused item, or None when nothing was ever focused or it expired."""
    with _focus_lock:
        f = _focus
    if f is None or time.time() - f["ts"] > FOCUS_TTL_S:
        if f is not None:
            clear_focus()
        return None
    return dict(f)


# ---------- Conversation Mode ----------
# While on, Scene only listens, transcribes and takes notes: no spoken answers,
# no chime-out, no interrupting speech. Starts by wake command ("Scene, listen" /
# "Scene, take notes"), by API (POST /conversation/start), or automatically (see
# _conversation_on_speech / process_frame). Ends on 30 s of silence, on no face in
# frames for a while, on "Hey Scene" (which then answers normally), or by API.
# Detection is heuristic: there is no diarization, so "someone other than Nayan"
# is approximated by conversational speech not addressed to Scene (a face match,
# when present, names the speaker and lowers the bar to one chunk).
CONV_SILENCE_END_S = float(os.environ.get("SCENE_CONV_SILENCE_S", "30"))
CONV_NO_FACE_END_S = float(os.environ.get("SCENE_CONV_NO_FACE_S", "120"))
CONV_AUTO_CHUNKS = int(os.environ.get("SCENE_CONV_AUTO_CHUNKS", "2"))
CONV_FACE_SPEAKER_TTL_S = 60.0   # a face match names the speaker for chunks in this window
CONV_SPEECH_GAP_S = 10.0         # a gap this long breaks "continuous" speech
FACE_MATCH_EVERY_S = float(os.environ.get("SCENE_FACE_EVERY_S", "5"))
FACE_MATCH_MIN_CONF = 0.7        # Gemini must be at least this sure to name someone

_conv_active = False
_conv_started_at: float | None = None
_conv_last_speech_at: float | None = None
_conv_speaker: str | None = None
_conv_source: str | None = None
_conv_other_streak = 0
_conv_last_chunk_at: float | None = None
_last_person_seen_at: float | None = None   # any person in a frame (face match or vision scene)
_last_face_at: float | None = None
_last_face_name: str | None = None
_last_face_match_at = 0.0
_seen_task_quotes: set[str] = set()   # normalised quotes already made into a (live or 30 s) task
_seen_quotes_seeded = False


def _seed_seen_quotes() -> None:
    """The RAM-only _seen_task_quotes set is lost on restart, so recover_pending()
    re-extracts the same batch and re-creates tasks _upsert_task cannot match
    (the model paraphrases the core). Seed it from the open tasks already in
    the DB, so a restart never re-creates them."""
    global _seen_quotes_seeded
    if _seen_quotes_seeded:
        return
    _seen_quotes_seeded = True
    try:
        for r in rows("SELECT quote FROM memories WHERE kind='task' AND status='open'"):
            q = _norm(r.get("quote") or "")
            if q:
                _seen_task_quotes.add(q)
    except Exception:
        pass

_CONV_START_RE = re.compile(
    r"\b(?:hey|hi|ok|okay)[\s,.]+scene[\s,.]+(?:listen|take notes)\b"
    r"|\bscene[\s,.]+(?:listen|take notes)\b", re.I)


def _conv_start_command(text: str) -> bool:
    return bool(_CONV_START_RE.search(text or ""))


def conversation_state() -> dict:
    _maybe_end_conversation(time.time())
    now = time.time()
    return {
        "active": _conv_active,
        "mode": "conversation" if _conv_active else "normal",
        "started_at": _conv_started_at,
        "last_speech_at": _conv_last_speech_at,
        "speaker": _conv_speaker,
        "source": _conv_source,
        "silence_s": None if _conv_last_speech_at is None else round(now - _conv_last_speech_at, 1),
        "last_face_at": _last_face_at,
        "unknown_face": unknown_face_state(),
    }


def start_conversation(source: str = "wake", speaker: str | None = None) -> bool:
    global _conv_active, _conv_started_at, _conv_last_speech_at, _conv_speaker, _conv_source, _conv_other_streak, _conv_transcript
    if _conv_active:
        return False
    # Auto-started conversations were already underway for a chunk or two before
    # the mode switched on: keep those chunks in the rolling transcript, or the
    # request they started is lost to the extract. Wake/API starts have no prior
    # conversation to keep.
    # A new conversation starts clean: an unknown face from the last one is gone,
    # never carried over and never asked about twice.
    _reset_unknown_face()
    # A new conversation is a new dedup window: the same words said again in
    # a later conversation are a new commitment, not a re-post.
    with _ft_sent_lock:
        _ft_sent_lines.clear()
    _conv_transcript = list(_transcript) if source in ("auto", "face") else []
    _conv_active = True
    _conv_started_at = time.time()
    _conv_last_speech_at = _conv_started_at
    _conv_speaker = speaker or _current_speaker()
    _conv_source = source
    _conv_other_streak = 0
    print(f"memory: CONVERSATION started source={source} speaker={_conv_speaker}", flush=True)
    push_card("notes", "Listening", "Conversation mode: taking notes, no spoken answers.", speaker=_conv_speaker)
    return True


def stop_conversation(reason: str = "manual") -> bool:
    global _conv_active, _conv_started_at, _conv_last_speech_at, _conv_speaker, _conv_source, _conv_other_streak, _conv_transcript
    if not _conv_active:
        return False
    _conv_active = False
    # End-of-conversation flush, immediately: the buffered transcript used to sit
    # here until the NEXT chunk arrived (the 30 s flush only fires on new speech),
    # so the tail of a conversation -- including a corrected time ("Tuesday at 2,
    # no, make it 5") -- reached Follow-Through minutes late or never. Flush now,
    # on the extract lane, so stop_conversation itself never blocks on a model call.
    # The flush extracts from the WHOLE conversation transcript (_conv_transcript),
    # not just the unflushed 30 s tail: _transcript may already have been flushed,
    # and only the full transcript has the day/time that arrived with earlier chunks.
    _conv_text = " ".join(_conv_transcript)
    _conv_transcript = []
    _unflushed_chars = len(" ".join(_transcript))
    print(f"memory: CONVERSATION stopped reason={reason} lasted_s={round(time.time() - (_conv_started_at or time.time()), 1)} unflushed_chars={_unflushed_chars} conv_chars={len(_conv_text)}", flush=True)
    if _transcript or _conv_text.strip():
        print(f"memory: CONV_END_FLUSH chars={_unflushed_chars} conv_chars={len(_conv_text)} reason={reason}", flush=True)
        # end_of_conversation=True: the durable conv rows are deleted only
        # after this flush's extract has run, so a kill in between still
        # recovers them on the next startup instead of losing the conversation.
        flush_transcript(background=True, extract_text=_conv_text or None, end_of_conversation=True)
    # After-conversation enroll, never mid-talk and never on a "wake" end: the wearer
    # just asked Scene something, and that question comes first. The saved frame stays
    # for the next silence / manual end. _arm_unknown_face_prompt() asks once (one
    # spoken line + one card), only when this conversation actually saw an unknown face.
    if reason != "wake":
        _arm_unknown_face_prompt()
    _conv_started_at = None
    _conv_last_speech_at = None
    _conv_speaker = None
    _conv_source = None
    _conv_other_streak = 0
    return True


def _maybe_end_conversation(now: float) -> bool:
    """Silence 30 s, or no face/person in frames for a while (only when a person was
    actually seen during this session). Returns True when it ended the mode."""
    if not _conv_active:
        return False
    if _conv_last_speech_at is not None and now - _conv_last_speech_at >= CONV_SILENCE_END_S:
        return stop_conversation(reason="silence")
    if (_last_person_seen_at is not None and _conv_started_at is not None
            and _last_person_seen_at >= _conv_started_at
            and now - _last_person_seen_at >= CONV_NO_FACE_END_S):
        return stop_conversation(reason="no_face")
    return False


def _current_speaker(now: float | None = None) -> str | None:
    """Last face-matched name, while it is still fresh. None = Unknown, never guessed."""
    now = time.time() if now is None else now
    if _last_face_name and _last_face_at and now - _last_face_at <= CONV_FACE_SPEAKER_TTL_S:
        return _last_face_name
    return None


def _conversation_on_speech(text: str, speaker: str | None = None) -> None:
    """One real conversational chunk (past every hallucination gate, not a question or
    command): update last-speech, and auto-start after 2 such chunks in a row, or 1
    when a face match already named the speaker. Nayan's own chunks (face-matched
    to 'Nayan') do not count toward the streak."""
    global _conv_other_streak, _conv_last_chunk_at, _conv_last_speech_at
    now = time.time()
    if _conv_active:
        _conv_last_speech_at = now
        return
    _conv_last_speech_at = now
    if len((text or "").split()) < 4:
        return
    if _conv_last_chunk_at is not None and now - _conv_last_chunk_at > CONV_SPEECH_GAP_S:
        _conv_other_streak = 0
    _conv_last_chunk_at = now
    if speaker and speaker.lower() == "nayan":
        return
    _conv_other_streak += 1
    need = 1 if speaker else CONV_AUTO_CHUNKS
    if _conv_other_streak >= need:
        start_conversation(source="auto", speaker=speaker)


def on_frame(path: Path) -> None:
    global _last_frame
    if DISPLAY_AUDIO_MODE:
        return   # display is open: no vision, no lead steps (they need frames)
    now = time.time()
    if ENABLED and not voice.paused() and lead.due(now):
        submit("lead", _lead_step, path)      # its own lane: never waits behind a slow see()
    if not ENABLED or now - _last_frame < FRAME_EVERY_S or voice.paused():
        return
    _last_frame = now
    submit("frame", process_frame, path)


def on_audio(path: Path) -> None:
    if ENABLED and not voice.paused():
        submit("audio", process_audio, path)


# ---------- cards for the glasses ----------

def push_card(kind: str, title: str, body: str, ref: int | None = None, frame: str | None = None,
              speaker: str | None = None) -> dict:
    """kind: answer | task | payment | offer | notes | email | calendar | confirm | order. ref = the id the buttons act on.
    speaker is who said it, only when the backend actually knows (from_whom). None = unknown: the lens/phone
    then show no name. `from_whom` is the same value under the name the lens/phone already read."""
    ts = time.time()
    with _db_lock:
        cid = db().execute("INSERT INTO cards (kind, title, body, ref, frame, speaker, ts) VALUES (?,?,?,?,?,?,?)",
                           (kind, title, body, ref, frame, speaker, ts)).lastrowid
        db().commit()
    return {"id": cid, "kind": kind, "title": title, "body": body, "ref": ref, "frame": frame,
            "speaker": speaker, "from_whom": speaker, "ts": ts}


def clear_cards() -> None:
    with _db_lock:
        db().execute("DELETE FROM cards")
        db().commit()


# ---------- face enrollment + live recognition (Gemini Hydrogen vision) ----------
# Enrolling stores one JPEG per name in data/faces/<name>.jpg (POST /faces/enroll).
# match_face() sends the frame plus every enrolled reference photo to Gemini
# Hydrogen vision and asks which enrolled person, if any, is in the frame. Only a
# clear match (confidence >= FACE_MATCH_MIN_CONF) returns a name; anything else
# is None, so no card ever claims a face match that did not happen. There is no
# face-embedding model in this repo: recognition is one Gemini vision call per
# throttled frame (see process_frame), so it is slower and less accurate than a
# dedicated face recogniser, and it only runs while frames are flowing (never
# while the display is open: Display Audio Mode pauses frames).
FACES_DIR = DATA_DIR / "faces"
_FACE_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z '\-]{0,49}$")


def _enrolled_references() -> list[tuple[str, bytes]]:
    refs = []
    for r in rows("SELECT name, photo FROM faces ORDER BY name"):
        p = FACES_DIR / r["photo"]
        try:
            refs.append((r["name"], p.read_bytes()))
        except OSError:
            print(f"memory: enrolled face {r['name']!r} photo missing ({p.name}), skipping", flush=True)
    return refs


def match_face(frame_bytes: bytes) -> str | None:
    """Name of the enrolled person in this frame, or None. Gemini Hydrogen vision
    compares the frame against every enrolled reference photo; only a match at
    confidence >= FACE_MATCH_MIN_CONF counts. No enrolled faces, a Gemini error,
    or an unclear match all return None, never a guess."""
    refs = _enrolled_references()
    if not refs:
        return None
    try:
        out = models.identify_face(frame_bytes, refs)
    except Exception as e:
        print(f"memory: face match failed: {e!r}", flush=True)
        return None
    name, conf = out.get("name"), float(out.get("confidence") or 0.0)
    if name and conf >= FACE_MATCH_MIN_CONF:
        print(f"memory: FACE {name} conf={conf:.2f}", flush=True)
        return name
    return None


def enroll_face(name: str, photo_bytes: bytes) -> dict:
    """Store one enrollment photo for `name`. Raises ValueError on a bad name or photo."""
    name = (name or "").strip()
    if not _FACE_NAME_RE.match(name):
        raise ValueError("name must be 1-50 letters, spaces, apostrophes or hyphens, starting with a letter")
    if not photo_bytes or photo_bytes[:3] != b"\xff\xd8\xff":
        raise ValueError("photo must be a JPEG")
    safe = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") + ".jpg"
    FACES_DIR.mkdir(parents=True, exist_ok=True)
    (FACES_DIR / safe).write_bytes(photo_bytes)
    ts = time.time()
    with _db_lock:
        db().execute("INSERT INTO faces (name, photo, ts) VALUES (?,?,?) "
                     "ON CONFLICT(name) DO UPDATE SET photo=excluded.photo, ts=excluded.ts",
                     (name, safe, ts))
        db().commit()
    return {"name": name, "photo": safe, "ts": ts, "recognition": "hydrogen_vision",
            "note": "Enrollment photo stored. Face matching runs via Gemini Hydrogen vision on frames (confidence >= 0.7); a matched name becomes the speaker on cards in that window, otherwise Unknown."}


def list_faces() -> list[dict]:
    """Enrolled faces, with their photo filenames. Recognition is hydrogen_vision."""
    return [{"name": r["name"], "photo": r["photo"], "ts": r["ts"], "recognition": "hydrogen_vision"}
            for r in rows("SELECT name, photo, ts FROM faces ORDER BY name")] 


# ---------- unknown face: enroll AFTER the conversation, never during it ----------
# Nayan said yes to auto-enroll, but only after the conversation ends.
# During a conversation, process_frame() notes the first clear face no enrolled
# photo matched (_note_unknown_face): it saves the frame filename only, and says
# nothing. When the conversation ends (silence / no-face / manual / API / streaming
# stop, never a "wake" end), stop_conversation() arms the prompt once: one spoken
# line ("Who was that? I saw an unfamiliar face.") + one feed card, both pointing
# at the saved frame. A spoken name then enrolls that frame via enroll_face();
# "skip" / "no one" / tap Skip discards it. No name, no enroll, ever; no silent
# enroll; the prompt expires after UNKNOWN_FACE_ANSWER_WINDOW_S and the frame is
# dropped without asking again for that conversation. If Nayan himself is not
# enrolled, his own face counts as "unknown" too: enroll "Nayan" once and it stops.
UNKNOWN_FACE_ANSWER_WINDOW_S = float(os.environ.get("SCENE_UNKNOWN_FACE_WINDOW_S", "120"))
_UNKNOWN_FACE_PROMPT = "Who was that? I saw an unfamiliar face."
_unknown_face: dict | None = None      # saved during a conversation: {frame, ts}
_unknown_pending: dict | None = None   # armed at conversation end: {frame, asked_at, until}
_SKIP_RE = re.compile(r"^(skip|no one|no-one|nobody|nope|no|never mind|not now|later|don't know|dont know|i don't know|unknown)\.?$", re.I)
_NAME_PREFIX_RE = re.compile(r"^(that was|that's|that is|his name is|her name is|their name is|it's|it is|this is|meet)\s+", re.I)


def _reset_unknown_face() -> None:
    """Drop any saved unknown frame and any armed prompt: a new conversation starts clean."""
    global _unknown_face, _unknown_pending
    _unknown_face = None
    _unknown_pending = None


def _note_unknown_face(seen: dict, frame: str, face_name: str | None) -> None:
    """Save the best frame of an unknown face, during a conversation only. Silent: never asks mid-talk."""
    global _unknown_face
    if not _conv_active or face_name is not None or _current_speaker() is not None:
        return
    if _unknown_face is not None:
        return  # first clear frame wins; later ones may be turned away or blurry
    face = seen.get("face") or {}
    if not (face.get("present") and face.get("clear")):
        return
    _unknown_face = {"frame": frame, "ts": time.time()}
    print(f"memory: UNKNOWN FACE saved frame={frame} (ask after the conversation, not now)", flush=True)


def _expire_unknown_pending() -> bool:
    """Drop an expired who-was-that prompt (and its frame) without asking again. True when it expired."""
    global _unknown_pending, _unknown_face
    if _unknown_pending is not None and time.time() > _unknown_pending["until"]:
        print(f"memory: UNKNOWN FACE prompt expired, discarding frame={_unknown_pending['frame']} (not asked again)", flush=True)
        _unknown_pending = None
        _unknown_face = None
        return True
    return False


def unknown_face_state() -> dict:
    """What the phone polls: is there a who-was-that prompt waiting, and which saved frame is it for?
    `frame_url` is filled in by server.py (it knows PUBLIC_BASE_URL); here it is always None."""
    _expire_unknown_pending()
    if _unknown_pending is None:
        return {"pending": False, "frame": None, "frame_url": None, "asked_at": None}
    return {"pending": True, "frame": _unknown_pending["frame"],
            "frame_url": None, "asked_at": _unknown_pending["asked_at"]}


def _arm_unknown_face_prompt() -> bool:
    """At conversation end only: ask once who the saved unknown face was. One card + one spoken line."""
    global _unknown_pending
    if _unknown_face is None or _unknown_pending is not None:
        return False
    frame = _unknown_face["frame"]
    now = time.time()
    _unknown_pending = {"frame": frame, "asked_at": now, "until": now + UNKNOWN_FACE_ANSWER_WINDOW_S}
    push_card("unknown_face", "Who was that?", "I saw an unfamiliar face. Say their name, or tap to enter it.", frame=frame)
    voice.speak(_UNKNOWN_FACE_PROMPT)
    print(f"memory: UNKNOWN FACE asking who that was frame={frame} window_s={UNKNOWN_FACE_ANSWER_WINDOW_S:.0f}", flush=True)
    return True


def _unknown_name_from(text: str) -> str | None:
    """A name-shaped reply to the who-was-that question, or None. Strips "that was ..." prefixes."""
    t = (text or "").strip().strip(" .!?")
    t = _NAME_PREFIX_RE.sub("", t).strip()
    if not t or len(t.split()) > 3:
        return None
    if not _FACE_NAME_RE.match(t):
        return None
    # Never enroll a word that is really a command, a wake word, or a question.
    if voice.WAKE.search(t) or _conv_start_command(t):
        return None
    return t


def _unknown_try_answer(text: str) -> bool:
    """True when this chunk answered the who-was-that question (spoken name or skip)."""
    global _unknown_pending, _unknown_face
    if _unknown_pending is None:
        return False
    if _expire_unknown_pending():
        return False
    now = time.time()
    if now < _unknown_pending["asked_at"] + 2.0:
        return True  # our own question still on the air: never hear it as the answer
    t = (text or "").strip()
    if not t or len(t.split()) > 4:
        return False  # ordinary speech: not an answer, the prompt stays until it expires
    if _SKIP_RE.match(t):
        frame = _unknown_pending["frame"]
        _unknown_pending = None
        _unknown_face = None
        voice.speak("Okay, skipped.")
        print(f"memory: UNKNOWN FACE skipped, discarded frame={frame}", flush=True)
        return True
    name = _unknown_name_from(t)
    if name is None:
        return False
    try:
        out = enroll_unknown_face(name)
    except (ValueError, FileNotFoundError) as e:
        print(f"memory: UNKNOWN FACE enroll failed {name!r}: {e!r}", flush=True)
        voice.speak("I couldn't save that name. Try again on the phone.")
        return True
    voice.speak(f"Got it, {name}.")
    print(f"memory: UNKNOWN FACE enrolled {name!r} from frame={out['frame']}", flush=True)
    return True


def enroll_unknown_face(name: str) -> dict:
    """Enroll the saved unknown frame under `name`. The only enroll path for an unknown
    face: it requires a name AND an armed post-conversation prompt (a saved frame from a
    conversation that already ended). Raises ValueError on a bad name, FileNotFoundError
    when there is no armed prompt (already enrolled, skipped, or expired). Never enrolls
    without a name, never enrolls silently: the caller speaks / shows the result."""
    global _unknown_pending, _unknown_face
    _expire_unknown_pending()
    name = (name or "").strip()
    if not _FACE_NAME_RE.match(name):
        raise ValueError("name must be 1-50 letters, spaces, apostrophes or hyphens, starting with a letter")
    if _unknown_pending is None:
        raise FileNotFoundError("no unknown face waiting for a name (already enrolled, skipped, or expired)")
    frame = _unknown_pending["frame"]
    path = DATA_DIR / "frames" / Path(frame).name  # basename only: the frame name never escapes data/frames
    try:
        photo = path.read_bytes()
    except OSError:
        raise FileNotFoundError(f"saved frame {frame} is gone")
    out = enroll_face(name, photo)
    _unknown_pending = None
    _unknown_face = None
    push_card("answer", f"Enrolled: {name}", f"I'll recognise {name} next time.", frame=frame)
    return {**out, "frame": frame}


def dismiss_unknown_face() -> dict:
    """Discard the saved unknown frame without enrolling: "skip" / "no one" / tap Skip. Never asks again for that conversation."""
    global _unknown_pending, _unknown_face
    frame = (_unknown_pending or _unknown_face or {}).get("frame")
    _unknown_pending = None
    _unknown_face = None
    if frame:
        print(f"memory: UNKNOWN FACE dismissed, discarded frame={frame}", flush=True)
    return {"dismissed": True, "frame": frame}


# ---------- dining agent: location and spoken entry ----------

_loc: tuple[float, float, float] | None = None   # lat, lng, when
LOC_VALID_S = 1800
# Task requests go to the agent: food, email, calendar, reminders, web search. Everything else is a memory question.
# "send" / "message" / "slack" / "text" / "pay" were missing: "send Priya a message" and
# "text Sam" fell through to memory.ask and came back "I don't have a memory of that."
DINING = re.compile(r"\b(order|place an order|hungry|get me|something to eat|high protein|healthy options|calories"
                    r"|e-?mail|calendar|schedule|remind|reminder|search|look up|google"
                    r"|book|booking|reservation|reserve|restaurant|table|appointment"
                    r"|send|message|slack|text|pay|buy)\b", re.I)


def is_action_request(text: str) -> bool:
    """True when `text` is an action for the agent (book / schedule / order / remind / send /
    email / calendar / ...), not a recall question for memory.ask. Pure recall ("where did I
    leave X", "what did Y ask me") is excluded even if it mentions email: asking what
    someone emailed is recall, sending an email is an action."""
    if not text:
        return False
    if _QUESTION_TO_SCENE.search(text):
        return False
    return bool(DINING.search(text))
MAX_REPLY_WORDS = 20   # a spoken answer to the agent; longer is probably someone else's conversation


def set_location(lat: float, lng: float) -> None:
    global _loc
    _loc = (lat, lng, time.time())


def get_location() -> tuple[float, float] | None:
    """The device's latest fix (30 minutes), else SCENE_LOCATION="lat,lng" from the env, else None."""
    if _loc and time.time() - _loc[2] <= LOC_VALID_S:
        return _loc[0], _loc[1]
    env = os.environ.get("SCENE_LOCATION", "")
    try:
        lat, lng = (float(x) for x in env.split(","))
        return lat, lng
    except ValueError:
        return None


# ---------- reservation corrections ("7, no actually 8") ----------
# A reservation draft is live after the agent proposes one and before the wearer confirms
# or dismisses it. A fresh utterance that only corrects the time updates that ONE draft in
# place (old time kept in `changed_from`) instead of starting a new agent session, which
# would reset the agent and make a second draft. No restaurant or time is ever invented here.
_RESERVATION_CORRECTION = re.compile(
    r"\b(?:no[ ,]+)?actually\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b"
    r"|\bmake (?:it|that)\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b"
    r"|\bchange (?:it|that) to\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", re.I)
_BARE_TIME = re.compile(r"^\W*(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\W*$", re.I)


def _time_today(hour: str, minute: str | None, ampm: str | None) -> str | None:
    """A spoken hour as YYYY-MM-DDTHH:MM today. A bare 1-11 for dinner is PM (7 -> 19:00);
    an explicit am/pm wins; 12 stays noon. None when the hour is not a clock time."""
    h, m = int(hour), int(minute or 0)
    if not 1 <= h <= 12 or m > 59:
        return None
    if ampm:
        h = h % 12 + (12 if ampm.lower() == "pm" else 0)
    elif h < 12:
        h += 12
    return f"{datetime.now().strftime('%Y-%m-%d')}T{h:02d}:{m:02d}"


def _correction_time(text: str) -> str | None:
    m = _RESERVATION_CORRECTION.search(text)
    if m:
        for i in (1, 4, 7):   # one of the three alternatives matched; the others are None
            if m.group(i) is not None:
                return _time_today(m.group(i), m.group(i + 1), m.group(i + 2))
    return None


def _pending_reservation() -> dict | None:
    for d in outbox.pending():
        if d.get("kind") == "reservation":
            return d
    return None


def _reservation_when(start: str) -> str:
    return datetime.strptime(start, "%Y-%m-%dT%H:%M").strftime("%a %b %-d, %-I:%M %p")


def _correct_reservation(text: str) -> bool:
    """True when this utterance only corrected a pending reservation draft's time."""
    d = _pending_reservation()
    if d is None:
        return False
    new_start = _correction_time(text)
    if new_start is None and len(text.split()) <= 4:   # a bare "8" / "at 8" while a draft waits
        m = _BARE_TIME.match(text)
        new_start = _time_today(m.group(1), m.group(2), m.group(3)) if m else None
    if new_start is None:
        return False
    if new_start == d["start"]:
        voice.speak(f"It's already set for {_reservation_when(new_start)}.")
        return True
    old_start = d["start"]
    outbox.update_reservation(d["id"], new_start)
    when_s, old_s = _reservation_when(new_start), _reservation_when(old_start)
    party_s = f"party of {d['party_size']}" if d.get("party_size") else "party size not given"
    push_card("reservation", f"Reservation at {d['restaurant']}?",
              f"Draft reservation · {when_s} · {party_s} · changed from {old_s} — tap Confirm, no table booked yet.",
              ref=d["id"])
    voice.speak(f"Got it, changed it to {when_s}. Draft updated, tap Confirm when you're ready.")
    print(f"memory: RESERVATION corrected {d['restaurant']} {old_s} -> {when_s}", flush=True)
    return True


# ---------- direct reservations ("reservation for Giovanni's at 7pm tonight") ----------
# Parsed in code, not by the model, same as _schedule_direct / _order_direct: a
# complete reservation never asks a follow-up question, and never becomes a
# Follow-Through promise. Live failure 2026-11-14: "Can you make an appointment
# for Giovanni" + "Restaurant at 7 p.m. tonight." arrived as two chunks. The
# first chunk has no DINING word ("appointment" was not in the regex) and the
# wake window had expired by processing time, so it fell through to the
# transcript, the 30 s extract made it a task, and Follow-Through asked about
# it later. Nothing was ever spoken. So: "appointment" is a DINING word, a
# complete reservation is drafted in code, and a restaurant-without-a-time
# leaves a 60 s buffer so the time arriving in the next chunk completes it.
_RESERVATION_REQUEST_RE = re.compile(
    r"\b(reservation|appointment|booking|reserve|book(?:ing)?(?: a table)?|get a table)\b", re.I)
_PARTIAL_RESERVATION_TTL_S = 60.0
_pending_reservation_req: dict | None = None   # {"restaurant": str, "ts": float}


def _parse_reservation(text: str) -> dict | None:
    """{"restaurant", "start", "time_label", "party_size"} for a reservation /
    appointment request. Either of restaurant / start may be None (restaurant
    named but no time yet, or a time-only continuation chunk like "Restaurant
    at 7 p.m. tonight."). None when the text has no reservation wording at all."""
    if not _RESERVATION_REQUEST_RE.search(text or ""):
        # Time-only continuation ("Restaurant at 7 p.m. tonight."): no reservation
        # word at all, so it is only meaningful while a partial request is buffered;
        # _reservation_direct enforces that, this parser just extracts the time.
        if not (re.search(r"\b(restaurant|table)\b", text or "", re.I) and _TIME_FIND.search(text or "")):
            return None
    t = (text or "").strip()
    m = re.match(r"\W*(?:hey|hi|ok|okay)[\s,.]+scene[\s,.]+", t, re.I)
    if m:
        t = t[m.end():]
    # Strip the request verbs, leaving "<restaurant> at <time>".
    t = re.sub(r"(?i)^(?:can you|could you|please)\s+", "", t).strip()
    t = re.sub(r"(?i)^(?:we|i|you|they|he|she)\s+(?:should|need to|have to|want to|will|would)\s+", "", t).strip()
    t = re.sub(r"(?i)^(?:i need|i want|i'?d like|i would like)\s+(?:a\s+)?(?:reservation|appointment|table)\s+(?:for|at)\s+", "", t).strip()
    t = re.sub(r"(?i)^(make|get|book|place)\s+(?:an?\s+|the\s+)?(?:reservation|appointment|booking)(?:\s+for|\s+at)?\s+", "", t).strip()
    t = re.sub(r"(?i)^(book|reserve|get)\s+(?:a\s+table\s+)?(?:at|for)\s+", "", t).strip()
    t = re.sub(r"(?i)^(?:a\s+)?(?:reservation|appointment)\s+(?:for|at)\s+", "", t).strip()
    # Last clock time wins; day defaults to today, "tonight" is today.
    times = list(_TIME_FIND.finditer(t))
    start = time_label = None
    rest = t
    if times:
        last = times[-1]
        day, explicit_day = _schedule_date(t)
        start = _time_on_date(day, last.group(1), last.group(2), last.group(3))
        if start is not None and datetime.strptime(start, "%Y-%m-%dT%H:%M") < datetime.now() - timedelta(minutes=5):
            if explicit_day:
                start = None
            else:
                start = _time_on_date(day + timedelta(days=1), last.group(1), last.group(2), last.group(3))
        if start is not None:
            time_label = datetime.strptime(start, "%Y-%m-%dT%H:%M").strftime("%-I:%M %p")
            rest = t[:last.start()].strip(" ,")
    rest = re.sub(r"(?i)\s+(?:at|for)\s*$", "", rest).strip(" ,.")
    rest = re.sub(r"(?i)^(for|at)\s+", "", rest).strip()
    if rest.lower() in ("restaurant", "a restaurant", "the restaurant", "table", "a table", "the table", ""):
        rest = None
    party = None
    pm = re.search(r"(?i)\bparty of (\d+)\b|\bfor (\d+) (?:people|person|guests)\b", text or "")
    if pm:
        party = int(pm.group(1) or pm.group(2))
    return {"restaurant": rest, "start": start, "time_label": time_label, "party_size": party}


def _reservation_direct(text: str, wake: bool = False) -> bool:
    """True when a reservation / appointment request was handled in code: a complete
    one is drafted + spoken, a restaurant-without-a-time is buffered and the wearer
    is asked what time (spoken, never silent). Only runs after the wake word, except
    for a continuation chunk while a partial request is buffered: the time arriving
    in the next chunk completes the request the wearer already started. Anything
    incomplete with no buffer falls through to the agent, which asks out loud."""
    global _pending_reservation_req
    parsed = _parse_reservation(text)
    if parsed is None:
        return False
    now = time.time()
    buf = _pending_reservation_req
    if buf is not None and now - buf["ts"] > _PARTIAL_RESERVATION_TTL_S:
        buf, _pending_reservation_req = None, None
    if not wake and buf is None:
        return False   # conversation mentioning a reservation is not a command
    restaurant = parsed["restaurant"] or (buf or {}).get("restaurant")
    start, time_label = parsed["start"], parsed["time_label"]
    if restaurant and start:
        _pending_reservation_req = None
        try:
            d = outbox.draft_reservation(restaurant, start, parsed["party_size"], text)
        except ValueError as e:
            voice.speak("Sorry, I couldn't draft that reservation.")
            print(f"memory: RESERVATION draft failed {e!r}", flush=True)
            return True
        when_s = _reservation_when(start)
        push_card("reservation", f"Reservation at {restaurant}?",
                  f"Draft reservation · {when_s} — tap Confirm, no table booked yet.", ref=d["id"])
        voice.speak(f"Got it. Draft reservation at {restaurant} at {when_s}. "
                    "Pinch Confirm on your glasses when you're ready. I haven't booked a table.")
        voice.chime_out()
        print(f"memory: RESERVATION direct {restaurant!r} -> {when_s}", flush=True)
        return True
    if restaurant and not start:
        _pending_reservation_req = {"restaurant": restaurant, "ts": now}
        voice.speak(f"What time do you want the reservation at {restaurant}?")
        voice.chime_out()
        print(f"memory: RESERVATION partial, waiting for time {restaurant!r}", flush=True)
        return True
    return False


# ---------- voice scheduling ("schedule dinner at 7", "schedule gym for friday at 6") ----------
# Parsed in code, not by the model, so a correction in the same sentence always lands:
# every time in the utterance is found and the LAST one wins ("7, no actually 8" -> 8),
# same rule the reservation correction uses. A calendar draft is created and waits for a
# tap on Add to confirm, like every other calendar card; nothing is ever put on the
# calendar without a tap. No time or day is ever invented: a request with no usable time
# falls through to the agent, which asks "when?" out loud.
_TIME_FIND = re.compile(r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", re.I)
_DAY_FIND = re.compile(r"\b(today|tomorrow|tonight|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", re.I)
_WEEKDAYS = {"monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6}


def _time_on_date(day, hour: str, minute: str | None, ampm: str | None) -> str | None:
    """A spoken hour as YYYY-MM-DDTHH:MM on `day` (a date). Bare 1-11 is PM (7 -> 19:00),
    an explicit am/pm wins, 12 stays noon. None when the hour is not a clock time."""
    h, m = int(hour), int(minute or 0)
    if not 1 <= h <= 12 or m > 59:
        return None
    if ampm:
        h = h % 12 + (12 if ampm.lower() == "pm" else 0)
    elif h < 12:
        h += 12
    return f"{day.isoformat()}T{h:02d}:{m:02d}"


def _schedule_date(text: str) -> tuple:
    """(date, explicit) for the LAST day word in the text; no day word means today, not explicit."""
    days = _DAY_FIND.findall(text)
    today = datetime.now().date()
    if not days:
        return today, False
    w = days[-1].lower()
    if w in ("today", "tonight"):
        return today, True
    if w == "tomorrow":
        return today + timedelta(days=1), True
    offset = (_WEEKDAYS[w] - today.weekday()) % 7   # same weekday means today
    return today + timedelta(days=offset), True


def _schedule_title(rest: str, last_time_start: int) -> str:
    """What is being scheduled: everything before the time, minus the time/day
    clause and any correction fragment."""
    title = rest[:last_time_start]
    title = re.sub(r"(?i)\b(today|tomorrow|tonight|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b", "", title)
    title = re.sub(r"(?i)\s*,?\s*(?:no,?\s*)?actually\s*$", "", title)
    title = re.sub(r"(?i)\s+at\s+\d{1,2}(?::\d{2})?\s*(?:am|pm)?\s*,?\s*$", "", title)
    title = re.sub(r"(?i)\s+at\s*$", "", title)
    title = re.sub(r"(?i)\s+(?:for|on)\s*$", "", title)
    title = re.sub(r"(?i)^a\s+", "", title.strip(" ,"))
    return title.strip(" ,")


def _parse_schedule(text: str) -> tuple[str, str] | None:
    """("title", "YYYY-MM-DDTHH:MM") for a complete schedule request, else None.
    Complete means a title and a clock time; the day defaults to today. The LAST time
    in the utterance wins, so "dinner at 7, no actually 8" schedules 8. A bare time
    that already passed today with no day given rolls to tomorrow (the card shows the
    date, so the wearer sees it); a time in the past on an explicit day is not a
    request, it is a mistake, so it returns None and the agent asks."""
    t = text.strip()
    m = re.match(r"\W*(?:hey|hi|ok|okay)[\s,.]+scene[\s,.]+", t, re.I)
    if m:
        t = t[m.end():]
    m = re.match(r"schedule\s+", t, re.I)
    if not m:
        return None
    rest = t[m.end():].strip()
    if not rest:
        return None
    times = list(_TIME_FIND.finditer(rest))
    if not times:
        return None
    last = times[-1]
    day, explicit_day = _schedule_date(rest)
    start = _time_on_date(day, last.group(1), last.group(2), last.group(3))
    if start is None:
        return None
    title = _schedule_title(rest, last.start())
    if len(title) < 2 or title.lower() in ("it", "that", "this", "something"):
        return None
    now = datetime.now()
    if datetime.strptime(start, "%Y-%m-%dT%H:%M") < now - timedelta(minutes=5):
        if explicit_day:
            return None   # an explicit day in the past: ask, don't guess another day
        start = _time_on_date(day + timedelta(days=1), last.group(1), last.group(2), last.group(3))
    return title, start


def _pending_calendar() -> dict | None:
    for d in outbox.pending():
        if d.get("kind") == "calendar":
            return d
    return None


def _correction_hms(text: str) -> tuple[str, str | None, str | None] | None:
    """(hour, minute, ampm) from a correction phrase ("no actually 8", "make it 8",
    "change it to 8", or a bare "8"), else None. Same shapes as the reservation correction."""
    m = _RESERVATION_CORRECTION.search(text)
    if m:
        for i in (1, 4, 7):   # one of the three alternatives matched; the others are None
            if m.group(i) is not None:
                return m.group(i), m.group(i + 1), m.group(i + 2)
    if len(text.split()) <= 4:   # a bare "8" / "at 8" while a draft waits
        m = _BARE_TIME.match(text)
        if m:
            return m.group(1), m.group(2), m.group(3)
    return None


def _correct_schedule(text: str) -> bool:
    """True when this utterance only corrected a pending calendar draft's time.
    The correction changes the TIME, never the day: the new time lands on the draft's
    own date, and the old time is kept in `changed_from`, like reservations."""
    d = _pending_calendar()
    if d is None:
        return False
    hms = _correction_hms(text)
    if hms is None:
        return False
    day = datetime.strptime(d["start"][:10], "%Y-%m-%d").date()
    new_start = _time_on_date(day, *hms)
    if new_start is None:
        return False
    when_s = datetime.strptime(new_start, "%Y-%m-%dT%H:%M").strftime("%a %b %-d, %-I:%M %p")
    if new_start == d["start"]:
        voice.speak(f"It's already set for {when_s}.")
        voice.chime_out()
        return True
    old_start = d["start"]
    outbox.update_event(d["id"], new_start)
    old_s = datetime.strptime(old_start, "%Y-%m-%dT%H:%M").strftime("%a %b %-d, %-I:%M %p")
    push_card("calendar", "Add to calendar?",
              f"{d['title']} · {when_s} · changed from {old_s}", ref=d["id"])
    voice.speak(f"Got it, changed it to {when_s}. Draft updated, tap Add when you're ready.")
    voice.chime_out()
    print(f"memory: SCHEDULE corrected {d['title']} {old_s} -> {when_s}", flush=True)
    return True


def _schedule_direct(text: str, wake: bool = False) -> bool:
    """True when a complete "schedule ..." request was drafted in code. Only runs
    after the wake word (like every other DINING route): a schedule request heard
    without "hey scene" is conversation, not a command. Incomplete requests return
    False and fall through to the agent, which asks "when?" out loud."""
    if not wake:
        return False
    parsed = _parse_schedule(text)
    if parsed is None:
        return False
    title, start = parsed
    d = outbox.draft_event(title, start, 30, text)
    when_s = datetime.strptime(start, "%Y-%m-%dT%H:%M").strftime("%a %b %-d, %-I:%M %p")
    push_card("calendar", "Add to calendar?", f"{title} · {when_s}", ref=d["id"])
    voice.speak(f"Got it, {title} at {when_s}. Pinch Add on your glasses to put it on your calendar.")
    voice.chime_out()
    print(f"memory: SCHEDULE {title!r} -> {when_s}", flush=True)
    return True


# ---------- direct orders ("order a Big Mac from McDonald's at 8pm from the Newark location") ----------
# Parsed in code, not by the model, same as the schedule draft: a complete order never
# asks a follow-up question. The nearest place is picked, never confirmed: places come
# back sorted nearest first, and a city (Newark) only narrows that list to the places in
# that city, so two Newark McDonald's resolve to the closest one to the wearer. The
# draft card is the order: nothing charges until the wearer taps Approve (Pay), same as
# every other order, and Stripe test mode + caps are untouched. No time, restaurant or
# item is ever invented: an incomplete parse, a menu without the item, or no location
# to measure closeness from all fall through to the agent, which asks like before.
_QTY_WORDS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5}
_ORDER_TIME_RE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b|\bat\s+(\d{1,2})(?::(\d{2}))?\b(?!\s*(?:am|pm)\b)", re.I)  # bare "at 8" must not steal the "8" of "at 8pm"


def _order_time(text: str) -> tuple[str | None, str | None, int, int]:
    """(start "YYYY-MM-DDTHH:MM", label "8:00 PM", time-phrase start, time-phrase end)
    for the LAST clock time in `text`, else (None, None, -1, -1). An am/pm always counts;
    a bare hour only counts after "at", so "2 Big Macs" is a quantity, not a time. A bare
    1-11 is PM (dinner), same rule as the reservation correction; a past time with no
    day given rolls to tomorrow, same as scheduling."""
    best = None
    for m in _ORDER_TIME_RE.finditer(text):
        if m.group(1) is not None:
            h, minute, ampm = int(m.group(1)), int(m.group(2) or 0), m.group(3).lower()
        else:
            h, minute, ampm = int(m.group(4)), int(m.group(5) or 0), None
        if not 1 <= h <= 12 or minute > 59:
            continue
        best = (m.start(), m.end(), h, minute, ampm)
    if best is None:
        return None, None, -1, -1
    mstart, mend, h, minute, ampm = best
    day, explicit_day = _schedule_date(text)
    start = _time_on_date(day, str(h), str(minute) if minute else None, ampm)
    if start is None:
        return None, None, -1, -1
    if datetime.strptime(start, "%Y-%m-%dT%H:%M") < datetime.now() - timedelta(minutes=5):
        if explicit_day:
            return None, None, -1, -1
        start = _time_on_date(day + timedelta(days=1), str(h), str(minute) if minute else None, ampm)
    return start, datetime.strptime(start, "%Y-%m-%dT%H:%M").strftime("%-I:%M %p"), mstart, mend


def _parse_order_items(s: str) -> list[dict]:
    """[{"name", "qty"}] from "a Big Mac and fries" / "two Big Macs"."""
    items = []
    for chunk in re.split(r"\s+and\s+|,\s*", s.strip()):
        chunk = chunk.strip()
        if not chunk:
            continue
        m = re.match(r"(\d+|a|an|one|two|three|four|five)\s+(.+)$", chunk, re.I)
        if m:
            qty = int(m.group(1)) if m.group(1).isdigit() else _QTY_WORDS[m.group(1).lower()]
            name = m.group(2).strip()
        else:
            qty, name = 1, chunk
        name = re.sub(r"^(a|an|the)\s+", "", name, flags=re.I).strip()
        if name:
            items.append({"name": name, "qty": qty})
    return items


def _parse_order(text: str) -> dict | None:
    """{"restaurant", "items", "city", "start", "time_label"} for a complete
    "order <item> from <restaurant> [at <time>] [in/from <city>]" request, else None."""
    t = text.strip()
    m = re.match(r"\W*(?:hey|hi|ok|okay)[\s,.]+scene[\s,.]+", t, re.I)
    if m:
        t = t[m.end():]
    m = re.match(r"order\s+", t, re.I)
    if not m:
        return None
    rest = t[m.end():].strip()
    if not rest:
        return None
    start, time_label, time_start, time_end = _order_time(rest)
    if time_end >= 0:   # take the time phrase out so it is never part of a name
        rest = (rest[:time_start] + rest[time_end:]).strip()
    city = None
    m = re.search(r"\bfrom the ([A-Za-z]+) location\b", rest, re.I)
    if m:
        city = m.group(1)
        rest = (rest[:m.start()] + rest[m.end():]).strip(" ,")
    else:
        m = re.search(r"\b([A-Za-z]+) location\b", rest, re.I)
        if m:
            city = m.group(1)
            rest = (rest[:m.start()] + rest[m.end():]).strip(" ,")
    rest = re.sub(r"\s+(?:at|for)\s*$", "", rest, flags=re.I).strip()  # "McDonald's at" once the time was cut out
    parts = re.split(r"\bfrom\b|\bat\b", rest, maxsplit=1, flags=re.I)
    if len(parts) != 2 or not parts[0].strip() or not parts[1].strip():
        return None
    restaurant = parts[1].strip()
    m = re.search(r"\bin ([A-Za-z]+)$", restaurant, re.I)
    if m:   # "McDonald's in Newark" puts the city after the restaurant
        city = city or m.group(1)
        restaurant = restaurant[:m.start()].strip()
    restaurant = re.sub(r"\s+location$", "", restaurant, flags=re.I).strip()
    if not restaurant:
        return None
    items = _parse_order_items(parts[0])
    if not items:
        return None
    return {"restaurant": restaurant, "items": items, "city": city,
            "start": start, "time_label": time_label}


def _order_direct(text: str, wake: bool = False) -> bool:
    """True when a complete order was drafted in code. Only runs after the wake word:
    an order heard without "hey scene" is conversation, not a command."""
    if not wake:
        return False
    parsed = _parse_order(text)
    if parsed is None:
        return False
    where = get_location()  # (lat, lng) from the phone GPS, else SCENE_LOCATION env, else None
    city = parsed["city"]
    if where is None and not city:
        return False  # no reference point to pick the closest from: the agent asks where
    places = lookup.find_nearby(parsed["restaurant"], where if where is not None else city)
    if city:
        in_city = [p for p in places if city.lower() in (p.get("address") or "").lower()]
        if in_city:
            places = in_city
    if not places:
        return False
    places = sorted(places, key=lambda p: (p.get("distance_mi") is None, p.get("distance_mi") or 0))
    place = places[0]  # nearest, never confirmed: two Newark locations resolve to this one
    menu = lookup.get_menu(place, ", ".join(i["name"] for i in parsed["items"]))
    lines = []
    for it in parsed["items"]:
        matched = agent_mod._match(it["name"], menu)
        if matched is None:
            return False  # not on this menu: the agent asks what they would like instead
        lines.append({"name": matched["name"], "price_cents": matched["price_cents"], "qty": it["qty"]})
    import orders
    try:
        order = orders.propose(place, lines)
    except payments.PaymentError as e:
        voice.speak(str(e))
        return True
    total = sum(l["price_cents"] * l["qty"] for l in lines)
    summary = ", ".join(f"{l['qty']}x {l['name']}" for l in lines) + f" · ${total / 100:.2f}"
    dist = place.get("distance_mi")
    dist_s = f"{dist} mi" if dist is not None else "distance unknown"
    if where is not None:
        picked_s = f"closest to you ({dist_s})"
    else:
        picked_s = f"closest to {city} ({dist_s}) — no GPS fix, picked from {city}"
    when_s = f" · for {parsed['time_label']}" if parsed.get("time_label") else ""
    push_card("order", f"Order from {place['name']}?",
              f"{summary}{when_s} · {place['name']}, {place['address']} — {picked_s}. Tap Approve to pay.",
              ref=order["id"])
    voice.speak(f"Got it. {summary.replace(' · ', ', ')} from {place['name']}{when_s}, {picked_s}. "
                "Approve it on your glasses.")
    voice.chime_out()
    print(f"memory: ORDER direct {summary} at {place['address']} ({picked_s})", flush=True)
    return True


# ---------- "tell me more about this" (display focus) ----------
# The wearer is looking at a Follow-Through / Needs-you item on the display and
# asks about "this". Resolved in code, not by the model, against the stored
# focus only: no model call, nothing invented, and with no fresh focus Scene
# says so instead of naming a different item. The spoken line is short (under
# 30 words); the exact quote and full detail go on the answer card.
_TELL_MORE_RE = re.compile(
    r"\b(tell me more|tell me about|what('| i)s this|what is this|explain this|more about this|about this one)\b", re.I)
_TELL_MORE_THIS_NOUN_RE = re.compile(r"\bthis\s+([a-z]+)", re.I)
_TELL_MORE_THIS_OK = ("about", "one", "promise", "item", "task", "thing")
_APPROVE_OUTCOME = {
    "calendar": "adds it to your calendar",
    "email": "drafts the email for you to send",
    "reply": "drafts the reply for you to send",
    "todo": "adds it to your to-do list",
    "task": "marks it done",
}


def _focus_spoken(f: dict) -> str:
    """Spoken answer, under 30 words: title + who said it + status + what
    approving does. The exact quote is long, so it lives on the card, not here."""
    title = " ".join(str(f.get("title") or "This").split()[:8])
    speaker = (f.get("speaker") or "").strip()
    who = f"{speaker} asked for this." if speaker else "No speaker recorded."
    status = (f.get("status") or "pending").strip()
    outcome = _APPROVE_OUTCOME.get((f.get("action") or "").lower(), "marks it done")
    return " ".join(f"{title}. {who} Status {status}. Approving it {outcome}.".split()[:30])


def _focus_card_body(f: dict) -> str:
    quote = (f.get("quote") or "").strip()
    lines = [f"\u201c{quote}\u201d" if quote else "No exact quote saved for this one."]
    desc = (f.get("body") or "").strip()
    if desc and desc != quote:
        lines.append(desc)
    lines += [c for c in (f.get("context") or []) if c]
    status = (f.get("status") or "pending").strip()
    outcome = _APPROVE_OUTCOME.get((f.get("action") or "").lower(), "marks it done")
    due = f.get("due_local")
    lines.append(f"Status: {status}" + (f" \u00b7 due {due}" if due else "") + f". Approving it {outcome}.")
    return "\n".join(lines)


def _tell_me_more_direct(text: str) -> bool:
    """True when a 'tell me more about this' question was handled: the focused
    display item is described aloud (short) and in full on a card. 'This' must
    be the focused item: 'tell me more about this restaurant' names something
    else, so it falls through to the normal ask path instead."""
    if not _TELL_MORE_RE.search(text or "") or "this" not in (text or "").lower():
        return False
    m = _TELL_MORE_THIS_NOUN_RE.search(text or "")
    if m and m.group(1).lower() not in _TELL_MORE_THIS_OK:
        return False
    f = get_focus()
    if f is None:
        voice.speak("I am not sure which one you mean. Open one on the display first.")
        voice.chime_out()
        push_card("answer", "Tell me more",
                  "No item is focused on the display. Open a promise on the display first, then ask again.")
        print("memory: TELL-MORE no focus", flush=True)
        return True
    spoken = _focus_spoken(f)
    push_card("answer", f"About: {f['title']}", _focus_card_body(f), speaker=f.get("speaker"))
    voice.speak(spoken)
    voice.chime_out()
    print(f"memory: TELL-MORE {f['id']!r} {f['title']!r}", flush=True)
    return True


def dining_turn(text: str, wake: bool = False) -> bool:
    """Route speech to the dining agent: an answer to its waiting question, or (after the wake word) a new request."""
    if wake and _conv_start_command(text):   # "Scene, listen" / "Scene, take notes" via /ask or /dev/say
        start_conversation(source="wake")
        return True
    if wake and _conv_active and voice.WAKE.search(text):
        stop_conversation(reason="wake")   # fall through: the question is answered normally
    if wake and _tell_me_more_direct(text):   # "tell me more about this" -> the focused display item
        return True
    if _correct_reservation(text):   # a time correction for a waiting draft, never a new session
        return True
    if _reservation_direct(text, wake=wake):   # a complete reservation, drafted in code + spoken; never a Follow-Through promise
        return True
    if _correct_schedule(text):   # same: a time correction for a waiting calendar draft
        return True
    if wake and _schedule_direct(text, wake=True):   # a complete schedule request, drafted in code
        return True
    if wake and _order_direct(text, wake=True):   # a complete order, drafted in code, nearest place picked
        return True
    if agent.state()["status"] == "waiting" and len(text.split()) <= MAX_REPLY_WORDS:
        agent.reply(text)
        return True
    if wake and DINING.search(text):
        agent.start(text)
        return True
    return False


_FROM_RE = re.compile(r"\(from ([^,\)]+)")


def _speaker_from_text(text: str) -> str | None:
    """Legacy fallback only: task text already embeds "(from Srijan, due ...)" from extract_tasks.
    Returns that name, or None. Never invents one."""
    m = _FROM_RE.search(text or "")
    return m.group(1).strip() or None if m else None


def cards_after(card_id: int) -> list[dict]:
    """Cards newer than card_id, oldest first, at most the newest 50.
    Every card carries `speaker` (and `from_whom`, same value) when the backend knows who
    said it, else None. Legacy cards fall back to the "(from ...)" in their body text."""
    out = []
    for c in rows("SELECT * FROM (SELECT * FROM cards WHERE id > ? ORDER BY id DESC LIMIT 50) ORDER BY id", card_id):
        speaker = c.get("speaker") or _speaker_from_text(c.get("body") or "")
        out.append({**c, "speaker": speaker, "from_whom": speaker})
    return out



agent = agent_mod.Agent(say=voice.speak, where=lambda: get_location(),
                        push=lambda kind, title, body, ref: push_card(kind, title, body, ref=ref))


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

def _add_many(items: list[tuple]) -> list[int]:
    """Insert several new memories, embedding them in parallel first.
    Each item is (kind, text, place, frame, status, speaker). Embed calls are network
    round trips that release the GIL, so 3-4 of them in parallel cost ~1 call instead of
    ~4 serially; the sqlite inserts stay serial under _db_lock. Falls back to serial
    add() if the pool fails, so a threading error never loses a memory."""
    if not items:
        return []
    try:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=4) as ex:
            vecs = list(ex.map(lambda it: models.embed(f"{it[1]}: {it[2]}" if it[2] else it[1]), items))
    except Exception as e:  # pragma: no cover - pool failure only
        print(f"memory: parallel embed failed ({e!r}), falling back to serial", flush=True)
        return [add(kind, text, place, frame, status, speaker)
                for kind, text, place, frame, status, speaker in items]
    ids = []
    with _db_lock:
        for (kind, text, place, frame, status, speaker), vec in zip(items, vecs):
            cur = db().execute(
                "INSERT INTO memories (kind, text, place, frame, ts, status, speaker, embedding) VALUES (?,?,?,?,?,?,?,?)",
                (kind, text, place, frame, time.time(), status, speaker, json.dumps(vec)))
            ids.append(cur.lastrowid)
        db().commit()
    return ids


_PERSON_RE = re.compile(r"\b(people|person|someone|man|woman|guy|group|meeting)\b", re.I)


def _person_in_scene(seen: dict) -> bool:
    """A person is in this frame even when no enrolled face matched: vision scene
    type "meeting", or people words in the activity/scene text. Presence only,
    never a name."""
    scene = seen.get("scene") or {}
    if scene.get("type") == "meeting":
        return True
    text = f"{seen.get('activity') or ''} {scene.get('text') or ''}"
    return bool(_PERSON_RE.search(text))


def process_frame(path: Path) -> None:
    global _last_face_at, _last_face_name, _last_face_match_at, _last_person_seen_at
    _t0 = time.time()
    _maybe_end_conversation(_t0)
    frame_bytes = path.read_bytes()
    seen = models.see(frame_bytes)
    _see_ms = (time.time() - _t0) * 1000
    handle_scene(seen.get("scene"), path.name)
    # Live face recognition, throttled: one extra Gemini vision call per frame would
    # double the vision cost, so match at most every FACE_MATCH_EVERY_S, and only
    # when someone enrolled a face (match_face returns None fast when none did).
    # The matched name becomes this frame's speaker and the speaker for transcript
    # chunks in the next 60 s (see _current_speaker).
    face_name: str | None = None
    if time.time() - _last_face_match_at >= FACE_MATCH_EVERY_S:
        _last_face_match_at = time.time()
        face_name = match_face(frame_bytes)
        if face_name:
            _last_face_name, _last_face_at = face_name, time.time()
            _last_person_seen_at = time.time()
    elif _current_speaker():
        face_name = _current_speaker()
    # Unknown face, during a conversation only: a clear face in this frame that no
    # enrolled photo matched. Save the frame (first clear one wins: later frames
    # may be turned away) and say nothing: never interrupt a live conversation to
    # ask who someone is. The ask happens once, when the conversation ends.
    _note_unknown_face(seen, path.name, face_name)
    # A person in the scene even when no enrolled face matched: still a person
    # present, which feeds Conversation Mode auto-start/end, but never a name.
    if _person_in_scene(seen):
        _last_person_seen_at = time.time()
        if face_name and not _conv_active and _conv_last_speech_at is not None \
                and time.time() - _conv_last_speech_at <= CONV_SPEECH_GAP_S:
            start_conversation(source="face", speaker=face_name)
    # Collect new rows first, then embed them in parallel: the old code called add()
    # per object / receipt / activity, each doing its own serial embed call after the
    # vision call, so one busy frame cost vision + up to 3-4 serial embeds.
    pending: list[tuple] = []
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
            pending.append(("object", name, place, path.name, None, face_name))

    r = seen.get("receipt") or {}
    if r.get("total"):
        text = f"Receipt: {r.get('merchant') or 'unknown'} ${float(r['total']):.2f}"
        last = rows("SELECT text FROM memories WHERE kind='event' AND text=? AND ts > ?", text, time.time() - 3600)
        if not last:  # same receipt seen for many frames: log once
            pending.append(("event", text, None, path.name, None, face_name))

    activity = (seen.get("activity") or "").strip()
    last = rows("SELECT text FROM memories WHERE kind='event' ORDER BY ts DESC LIMIT 1")
    if activity and not (last and last[0]["text"] == activity):
        pending.append(("event", activity, None, path.name, None, face_name))
    if pending:
        _add_many(pending)
        for kind, text, place, _frame, _s, _sp in pending:
            print(f"memory: {text} @ {place}" if kind == "object" else f"memory: {text}", flush=True)
    _total_ms = (time.time() - _t0) * 1000
    if _see_ms > 1500 or _total_ms > 2000:  # throttled: only slow frames log
        print(f"memory: FRAME_TIMING see_ms={_see_ms:.0f} total_ms={_total_ms:.0f} new_rows={len(pending)}", flush=True)


# Questions only Scene would be asked ("Where is my wallet?"). If the wake word was missed, these
# are answered anyway and never become promises or tasks. No trailing "?" required: Whisper often
# returns no question mark, and a question split across chunks fails the old `.*\?\s*$` either way.
_QUESTION_TO_SCENE = re.compile(
    r"^\W*(where('?s| is| are| did i (leave|put)| was)\b.*\bmy\b|did i (leave|put|send|pay)\b|what did .{0,20}(ask|tell) me\b)",
    re.I)


def process_audio(path: Path) -> None:
    """One 2.5 s chunk from the glasses mic (audio source is the glasses only, nothing
    from the phone). Hallucinations die here: a chunk needs real speech energy, a
    non-noise transcript, and (for questions/tasks) plausible length, or it never
    becomes a question, a task, a card, or a spoken answer."""
    global _silent_dropped, _last_silent_log, _last_speech_at
    _ensure_watchdog()
    _maybe_end_conversation(time.time())   # silence / no-face expiry, checked on every chunk
    if _ft_queue:
        _ft_ask_next()         # a question timed out unanswered: move on to the next one
    now = time.time()
    try:
        queue_ms = (now - path.stat().st_mtime) * 1000   # how long this chunk sat in the audio lane behind a slow STT/answer; >1 s means the lane is backed up
        chunk_end = path.stat().st_mtime   # capture clock: when this chunk's audio finished being captured
    except OSError:
        queue_ms = 0.0
        chunk_end = now
    wav = path.read_bytes()
    peak = voice.peak_window_rms(wav)
    if voice.is_silent(wav):   # room tone: skip the API call, and the hallucinations it invites
        _silent_dropped += 1
        if now - _last_silent_log >= 10:   # throttled: silent chunks arrive every ~2.5 s
            print(f"memory: silent chunks dropped x{_silent_dropped} (peak RMS {peak:.1f})", flush=True)
            _silent_dropped, _last_silent_log = 0, now
        return
    _t_transcribe0 = time.time()
    # Accuracy priming: real names Gemini may hear (contacts + enrolled faces) and
    # the last lines of this session, so it spells names right instead of inventing
    # a similar word. Names/context are hints only, never words to insert.
    _names = list(contacts.all().keys())
    try:
        _names += [f.get("name", "") for f in list_faces()]
    except Exception:
        pass
    text = models.transcribe(wav, names=_names, context=" / ".join(_transcript[-2:]),
                             items=models.COMMON_ITEMS)
    _transcribe_ms = (time.time() - _t_transcribe0) * 1000
    _stt = models.transcribe_source()  # "gemini" or "whisper": prove which engine heard it
    if not text:
        print(f"memory: empty transcript source={_stt} peak_rms={peak:.1f}", flush=True)
        return
    print(f"memory: HEARD {text!r} source={_stt} peak_rms={peak:.1f}", flush=True)
    # Barge-in: the wearer started talking while Jessica was still speaking. Stop
    # her NOW (phone queue + laptop player), before any gate that would drop this
    # chunk as "our own voice" -- the time-window own_voice check below cannot
    # tell her voice from the wearer's, so content has to decide: a wake word, a
    # stop word, or real speech that is not her echo, not noise, not a loop, and
    # not a chime tone. A chunk that fails these is still dropped below as before,
    # so a hallucination never stops her. The new words then flow on normally.
    _is_barge = bool(voice.WAKE.search(text) or _STOP_WORDS.search(text))
    if not _is_barge and not voice.just_chimed(now) and len(text.split()) >= 2 \
            and not voice.is_noise(text) and not voice.is_repetitive(text) \
            and not voice.is_non_english(text) and not voice.is_own_answer(text, now):
        _is_barge = True
    if voice.speaking(now) and _is_barge:
        voice.interrupt()
        print(f"memory: BARGE-IN {text!r}", flush=True)
    # A time arriving for a buffered reservation request is the wearer's answer to
    # Scene's own "What time?" question: it must reach dining_turn even while her
    # voice is still on the air (speak() holds the mic for ~6 s), or the request
    # dies silent in the own-voice drop below.
    if _pending_reservation_req is not None:
        _res = _parse_reservation(text)
        if _res is not None and _res.get("start") and dining_turn(text):
            return
    # Scene's own spoken answer reaching the mic is not the wearer talking. A real "stop" or a
    # fresh wake word still gets through, so the wearer can interrupt us.
    if voice.own_voice(now) and not voice.WAKE.search(text) and not _STOP_WORDS.search(text):
        print(f"memory: dropped own voice {text!r}", flush=True)
        return
    # Scene's last spoken answer echoing back late through the glasses mic (phone
    # playback can lag the air-time estimate by seconds): same words, not the wearer.
    if voice.is_own_answer(text, now):
        print(f"memory: dropped own answer echo {text!r}", flush=True)
        return
    # The wake chime is a tone, not speech: a chunk right after it with no wake word and no
    # question in it is the ping, not the wearer.
    if voice.just_chimed(now) and not voice.WAKE.search(text) and len(text.split()) <= 4 \
            and not _QUESTION_TO_SCENE.search(text):
        print(f"memory: dropped chime echo {text!r}", flush=True)
        return
    if _lead_audio(text):
        return
    pending = awareness.pending(now)
    if pending and len(text.split()) <= MAX_ANSWER_WORDS and parse_yes_no(text) != "unclear":
        answer_offer(pending.id, parse_yes_no(text) == "yes")
        return
    if _ft_waiting is not None and _ft_try_answer(text):
        return
    # A spoken name (or "skip" / "no one") answering the who-was-that question
    # armed when the last conversation ended. Only a name-shaped short reply is
    # consumed; ordinary speech falls through and the prompt stays until it expires.
    if _unknown_try_answer(text):
        return
    if notes.active() and STOP_NOTES.search(text):
        # finish_notes() runs models.make_notes (a full LLM call) plus an embed per
        # action item: on the audio lane it stalls the next question by seconds.
        # Run it on the extract lane instead; /notes/stop still calls it directly
        # (it needs the notes id back). The notes card lands a few seconds later.
        submit("extract", finish_notes)
        return
    if dining_turn(text):
        return
    if voice.listening():
        # "hey scene" was already heard: this chunk is the question arriving, not a new line.
        # The hallucination filters below do not apply to it.
        pass
    else:
        # Hallucination filter: noise, loops, repeats, and one-word fragments never become a
        # question, a task, a card, or a spoken answer.
        if voice.is_noise(text):
            print(f"memory: dropped noise {text!r}", flush=True)
            return
        if voice.is_repetitive(text):
            print(f"memory: dropped looped hallucination {text!r}", flush=True)
            return
        if voice.is_non_english(text):
            print(f"memory: dropped non-English hallucination {text!r}", flush=True)
            return
        # Near-duplicate BEFORE is_duplicate: is_duplicate overwrites the last-text
        # record, so checking after it would compare the chunk against itself.
        if voice.is_near_duplicate(text, now):
            print(f"memory: dropped near-duplicate {text!r}", flush=True)
            return
        if voice.is_duplicate(text, now):
            print(f"memory: dropped duplicate {text!r}", flush=True)
            return
        if len(text.split()) < 2:
            print(f"memory: dropped one-word fragment {text!r}", flush=True)
            return
    # NOTE: no "display open" warning here on purpose. Display Audio Mode (server.py
    # heartbeat) already lets the glasses mic listen while the display is open, so the
    # old "close the display, voice may not work" warning is wrong now.
    if _conv_start_command(text):
        # "Scene, listen" / "Scene, take notes": start listening, never a question.
        start_conversation(source="wake")
        return
    if _conv_active and voice.WAKE.search(text):
        # "Hey Scene" after a pause ends Conversation Mode; the question below is
        # then answered normally, with speech and chime-out.
        stop_conversation(reason="wake")
    if not voice.WAKE.search(text) and not voice.listening() and not _conv_active and _QUESTION_TO_SCENE.search(text):
        if voice.is_priming_echo(text):
            # The old Whisper prompt's example, invented on noise: never answer it without a wake word.
            print(f"memory: dropped priming echo {text!r}", flush=True)
            return
        if peak < voice.CLEAR_SPEECH_RMS:
            print(f"memory: dropped quiet no-wake question {text!r} peak_rms={peak:.1f}", flush=True)
            return
        text = "hey scene, " + text   # "hey scene" got lost in the last chunk; this is still a question to Scene
    if voice.WAKE.search(text) and not voice.listening():
        voice.chime()          # "I heard you", before any model work
    # The wake window is measured on the chunk's capture clock, not on processing
    # time: Gemini takes ~3.5 s per chunk on the serial audio lane, so a question
    # spoken 5 s after "hey scene" was already past the 6 s window by the time it
    # was transcribed, and fell through to the transcript unanswered (Giovanni's,
    # 2026-11-14). chunk_end is when this chunk's audio finished being captured.
    question = voice.heard(text, now=chunk_end)
    if question and dining_turn(question, wake=True):
        return
    if question and _conv_active:
        # Defensive: the wake check above should already have ended the mode, but a
        # question must never be answered aloud over a conversation.
        stop_conversation(reason="wake")
    if question:  # a question to Scene is not a task someone gave you: keep it out of the transcript
        _t_ask0 = time.time()
        out = ask(question)
        _answer_ms = (time.time() - _t_ask0) * 1000
        said = out["answer"] + (" Want me to lead you there?" if _can_lead(out) else "")
        push_card("answer", question, said, ref=out["memory_id"], frame=out["frame"])
        _t_voice0 = time.time()
        if not _conv_active:   # Conversation Mode: no spoken answers, card only
            voice.speak(said)
        _voice_ms = (time.time() - _t_voice0) * 1000
        # One line per spoken question (not per 2.5 s chunk): queue = lane wait,
        # transcribe = Gemini audio (Whisper is fallback only), search/llm split
        # the ask() call, answer = the whole ask(), voice = enqueueing speak
        # (near 0 now that laptop playback runs on its own thread). The real
        # ElevenLabs time logs separately as `voice: TTS_MS=...` when the synth
        # finishes, and the phone's 0.5 s poll + MP3 download happen after that.
        # Add queue_ms + transcribe_ms + answer_ms + TTS_MS + ~0.5 s poll for the
        # full end-to-end time from chunk arrival to sound on the glasses.
        print(f"memory: TIMING queue_ms={queue_ms:.0f} transcribe_ms={_transcribe_ms:.0f} search_ms={out.get('search_ms', 0):.0f} llm_ms={out.get('llm_ms', 0):.0f} answer_ms={_answer_ms:.0f} voice_ms={_voice_ms:.0f} total_ms={(time.time() - now) * 1000:.0f}", flush=True)
        # The answer is done (spoken, or shown as a card in cards mode): the
        # descending chime-out, queued behind the spoken answer. The lead offer
        # question is already inside `said`, so this still lands after it.
        if not _conv_active:   # Conversation Mode: no chime-out either
            voice.chime_out()
        if _can_lead(out):
            lead.offer(out["target"], said)
        print(f"memory: VOICE {question!r} -> {said!r}", flush=True)
        return
    if voice.listening():  # "hey scene" heard, question still coming: not part of any conversation
        return
    if DISPLAY_AUDIO_MODE:
        # Live transcript for the glasses: what someone just said, as a bubble on
        # Home within seconds of speaking -- not after the ~30 s extract. This point
        # is past every hallucination gate above (silence, own voice, chime echo,
        # noise, loops, duplicates, one-word fragments, priming echo), so silence
        # and noise never become a live bubble. Transcript-only: never a task or a
        # promise (the 30 s extract below still does that, separately). Speaker is
        # "Unknown" here: per-chunk diarization does not exist, and a name is only
        # used when the transcript actually says it (from_whom in the extract).
        # Gated on the display being open: with the display closed there is no lens
        # to show it on, and the feed would flood with a card every ~2.5 s.
        push_card("transcript", text, "", speaker=_current_speaker() or "Unknown")
    notes.add_line(text)
    _transcript.append(text)
    _last_speech_at = time.time()
    # Durable the moment it is heard: if streaming stops now and the backend
    # restarts, recover_pending() still has this line. Deleted only after
    # _process_transcript has run extract on it.
    _pending_append(text, _current_speaker(), conv=False)
    # Conversation Mode: this chunk is real speech from a conversation. Update the
    # auto-start streak / last-speech clock, append it to the rolling conversation
    # transcript, and run the live task extract on the WHOLE transcript so far,
    # never on this chunk alone: a sentence spans several 2.5 s chunks, and the
    # day/time usually arrives in a later chunk than the request itself.
    speaker = _current_speaker()
    _conversation_on_speech(text, speaker)
    if _conv_active:
        if not _conv_transcript or _conv_transcript[-1] != text:
            _conv_transcript.append(text)
            del _conv_transcript[:max(0, len(_conv_transcript) - _CONV_TRANSCRIPT_MAX_CHUNKS)]
            _pending_append(text, speaker, conv=True)
        submit("extract", _live_extract_chunk, " ".join(_conv_transcript), speaker)
    if time.time() - _last_extract >= EXTRACT_EVERY_S and _transcript:
        flush_transcript(background=True)


# ---------- lead me there (see lead.py) ----------

def _can_lead(out: dict) -> bool:
    """Offer only for a thing seen recently enough to still be there, with a photo to compare against."""
    t = out.get("target")
    return bool(t and t.get("frame") and t.get("age_min", 9999) <= LEAD_MAX_AGE_MIN)


def _lead_audio(text: str) -> bool:
    """True if this chunk was part of the lead flow (a yes/no, a stop, or Scene's own voice) and ends here."""
    if lead.wants_stop(text):
        lead.stop()
        push_card("answer", "Stopped", "Okay, no more directions.")
        voice.speak("Okay.")
        return True
    r = lead.try_answer(text)
    if r == "start":
        t = lead.get_target()
        push_card("answer", f"Leading you to {t['name']}", "Walk slowly. I'll tell you which way to turn.", frame=t.get("frame"))
        voice.speak("Okay. Walk slowly and I'll tell you which way to turn.")
        return True
    if r == "declined":
        voice.speak("Okay.")
        return True
    if r == "ignore":
        return True
    # while leading, only "hey scene" talk and "stop" matter; our own spoken steps must not become promises
    return lead.active() and not voice.WAKE.search(text) and not voice.listening()


def _lead_step(path: Path) -> None:
    def say(text: str, arrived: bool) -> None:
        t = lead.get_target() or {}
        if arrived:
            push_card("answer", f"Found: {t.get('name', 'it')}", text, frame=t.get("frame"))
        elif not voice.speaks():
            push_card("answer", f"Leading you to {t.get('name', 'it')}", text)
        voice.speak(text)
        print(f"memory: LEAD {text!r}", flush=True)
    lead.step(path, say)


# ---------- scene awareness: offers and notes (see docs/superpowers/specs/2026-11-14-scene-awareness-design.md) ----------

def handle_scene(scene: dict | None, frame: str | None) -> None:
    """One analyzed frame's scene: feed the note session, remember menus and the like, maybe offer notes."""
    now = time.time()
    if notes.active() and notes.on_scene(scene, frame, now):
        # Same as the voice "stop the notes" path: make_notes + an embed per action
        # item must not block the frame lane. The extract lane builds the notes.
        submit("extract", finish_notes)
    awareness.set_busy(notes.active())
    text = awareness.loggable(scene, now)
    if text:
        add("event", text, frame=frame)
    offer = awareness.feed(scene, now)
    if offer:
        push_card("offer", "Take notes?", offer.text, ref=offer.id, frame=frame)
        if not _conv_active:   # Conversation Mode: card only, never interrupt speech
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
            c = contacts.find(a.get("to", ""))
            d = outbox.draft(c["name"] if c else a.get("to", ""), a["subject"], a["body"], a["quote"], to=c["email"] if c else None)
            title, text = outbox.card_text(d)
            push_card("email", title, text, ref=d["id"])
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


def notes_list(limit: int = 20) -> list[dict]:
    """Recent meeting notes for the phone: newest first, with counts. Full notes are GET /notes/<id>."""
    out = []
    for r in rows("SELECT id, text, ts FROM memories WHERE kind='notes' ORDER BY ts DESC, id DESC LIMIT ?", limit):
        n = get_notes(r["id"]) or {}
        out.append({"id": r["id"], "summary": n.get("summary") or r["text"], "ts": r["ts"],
                    "decisions": len(n.get("decisions", [])), "action_items": len(n.get("action_items", [])),
                    "slides": len(n.get("slides", []))})
    return out


def activity(limit: int = 50, today: bool = True) -> list[dict]:
    """What Scene took in and did, newest first: events (seen, approved, paid), tasks and notes. Objects are not activity."""
    lt = time.localtime()
    since = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1)) if today else 0
    return rows("SELECT id, kind, text, status, frame, ts FROM memories WHERE kind IN ('event','task','notes') AND ts >= ? "
                "ORDER BY ts DESC, id DESC LIMIT ?", since, limit)


def get_notes(note_id: int) -> dict | None:
    p = DATA_DIR / "notes" / f"{note_id}.json"
    return json.loads(p.read_text()) if p.is_file() else None


def flush_transcript(background: bool = False, extract_text: str | None = None,
                     end_of_conversation: bool = False) -> None:
    global _last_extract
    text = " ".join(_transcript)
    _transcript.clear()
    _last_extract = time.time()
    # The durable rows this batch covers. They are deleted only at the END of
    # _process_transcript, after extract has run on them -- never here, or a
    # kill while the extract lane is still working would lose the batch.
    # Mid-conversation the plain rows stay: the conversation transcript still
    # needs them, and the end flush (end_of_conversation=True) deletes both
    # sets. Outside a conversation the plain rows go with this flush.
    pending_ids: list[int] = []
    if not _conv_active or end_of_conversation:
        pending_ids = [r["id"] for r in _pending_rows(conv=False)]
    if end_of_conversation:
        pending_ids += [r["id"] for r in _pending_rows(conv=True)]
    # In a conversation, extract from the WHOLE conversation transcript, not just
    # this 30 s batch: the day/time for a task in this batch may have been said in
    # an earlier batch. Outside a conversation the batch is the transcript.
    # extract_text overrides (the end-of-conversation flush passes the whole
    # conversation even after the mode has switched off). When the batch itself is
    # empty but a conversation transcript was passed, that transcript is the batch
    # too, so the end flush still forwards it to Follow-Through.
    if extract_text is None and _conv_active and _conv_transcript:
        extract_text = " ".join(_conv_transcript)
    if not text.strip() and not (extract_text or "").strip():
        return
    if not text.strip():
        text = extract_text or ""
    # Timing log: the next run's log should show FLUSH -> EXTRACT_TIMING ->
    # FT_FORWARD_TIMING, so the delay from last speech to Follow-Through item
    # is measurable instead of guessed.
    print(f"memory: FLUSH chars={len(text)} background={background} pending_ids={len(pending_ids)}", flush=True)
    if background:
        submit("extract", _process_transcript, text, extract_text, pending_ids)
    else:
        _process_transcript(text, extract_text, pending_ids)


# A task needs a verb + a real object. "Sit down" (verb + particle, no object)
# and a bare "send a reminder" (no recipient, content, or time) are misheard
# fragments or small talk, not tasks. Log, 2026-11-14: "Sit down." became a task.
_PARTICLE_WORDS = {"down", "up", "in", "out", "over", "back", "around", "away", "on", "off"}
_PRONOUN_WORDS = {"it", "that", "this", "them", "here", "there"}
_GENERIC_TASK_RE = re.compile(r"^(send|set|make|leave|give|drop)\s+(a\s+|an\s+|the\s+)?(reminder|message|note)s?$", re.I)


def _is_actionable_task(task_text: str, due: str = "") -> bool:
    """True when `task_text` is a real thing to do: a verb plus an object with
    something concrete in it (a person, a thing, a time/day). False for fragments
    like "sit down" and a bare "send a reminder". The verbatim-quote gate still
    applies separately; this only stops unactionable text from becoming a task."""
    words = re.findall(r"[a-z]+", (task_text or "").lower())
    if len(words) < 2:
        return False
    if len(words) == 2 and words[1] in _PARTICLE_WORDS | _PRONOUN_WORDS:
        return False
    if _GENERIC_TASK_RE.match((task_text or "").strip()) and not (due or "").strip():
        return False
    return True


def _upsert_task(task: str, speaker: str | None, quote: str | None,
                 insert: bool = True) -> tuple[int | None, bool, bool]:
    """Insert `task`, or update the open task it enriches instead of duplicating
    it. A request's day/time usually arrives in a later chunk than the request
    ("block out a time..." then "Tuesday at 5 p.m. for math class"), so an early
    extract creates the timeless task and a later extract returns the same task
    with its time: same core text (the new core equals or extends the old one,
    ignoring the trailing "(from ..., due ...)" suffix) means the same task,
    updated in place, card body included. Never shortens a task: a shorter new
    core matches (same task, paraphrased) but is kept as-is, never written over
    the longer one. Returns (task id, created, changed). `insert=False`
    only ever updates, never creates (a quote seen before, for a task that is
    gone/closed, returns (None, False, False))."""
    new_core = _norm(re.sub(r"\s*\([^)]*\)\s*$", "", task))
    for r in rows("SELECT id, text, speaker, quote FROM memories WHERE kind='task' AND status='open' ORDER BY ts DESC"):
        old_core = _norm(re.sub(r"\s*\([^)]*\)\s*$", "", r["text"] or ""))
        if not old_core or not new_core:
            continue
        if new_core != old_core and not new_core.startswith(old_core):
            # Same task in different words ("Send a deck by 10 p.m. today"
            # vs "Send the deck by tonight"): the model paraphrases the same
            # promise across extracts. Token overlap >= 85% of the shorter
            # core means the same task, never a second row.
            _new_toks = set(new_core.split())
            _old_toks = set(old_core.split())
            if not _new_toks or not _old_toks:
                continue
            if len(_new_toks & _old_toks) / min(len(_new_toks), len(_old_toks)) < 0.85:
                # Same deadline, same words except for time words: "10 p.m.
                # today" and "tonight" are the same clock reading in
                # different words. Only time words and articles may differ --
                # a different object word (deck vs slides) keeps two tasks
                # distinct even at the same deadline.
                _due = lambda s: (re.search(r"due ([^,)]+)", s or "") or [None, None])[1]
                _time_words = {"tonight", "today", "tomorrow", "morning", "afternoon",
                               "evening", "night", "monday", "tuesday", "wednesday",
                               "thursday", "friday", "saturday", "sunday", "week",
                               "p", "m", "am", "pm", "p.m.", "a.m.", "at", "by", "on", "for"} | {str(n) for n in range(13)}
                _diff = (_new_toks ^ _old_toks) - _time_words - {"the", "a", "an"}
                if not (_due(task) and _due(task) == _due(r["text"]) and not _diff
                        and len(_new_toks & _old_toks) >= 3):
                    continue
        # Normalised no-change: "hang out..." vs "Hang out..." is the same
        # task, not an update -- without this every re-extract rewrote the row
        # and card and looked like a new item arriving.
        if (_norm(r["text"] or "") == _norm(task)
                and _norm(r.get("speaker") or "") == _norm(speaker or "")
                and _norm(r.get("quote") or "") == _norm(quote or "")):
            return r["id"], False, False
        # Never shorten a task in place: a shorter paraphrase of a task that
        # already has its time is the same task, not a replacement for it.
        if len(_norm(task)) < len(_norm(r["text"] or "")):
            return r["id"], False, False
        with _db_lock:
            db().execute("UPDATE memories SET text=?, speaker=?, quote=? WHERE id=?",
                         (task, speaker, quote, r["id"]))
            db().execute("UPDATE cards SET body=?, speaker=? WHERE kind='task' AND ref=?",
                         (task, speaker, r["id"]))
            db().commit()
        print(f"memory: TASK updated {task!r} (was {r['text']!r})", flush=True)
        return r["id"], False, True
    if not insert:
        return None, False, False
    return add("task", task, status="open", speaker=speaker, quote=quote), True, True


def _create_task_from_extract(t: dict, transcript: str, speaker_fallback: str | None,
                              log_label: str = "TASK") -> bool:
    """One extracted task dict -> an open task row + card, or nothing. Shared by
    the live extract and the 30 s / end-of-conversation extract so both apply the
    same gates: the verbatim-quote gate, the actionable gate (a verb + a real
    object: "sit down" and a bare "send a reminder" never become tasks), dedup
    by quote, and enrich-in-place when a later extract adds the day/time to a
    task already created from an earlier chunk. Returns True when a task was
    created or enriched (so the live path can forward it to Follow-Through)."""
    _seed_seen_quotes()
    if not quoted(t, transcript):
        return False
    text = (t.get("text") or "").strip()
    due = (t.get("due") or "").strip()
    if not _is_actionable_task(text, due):
        print(f"memory: dropped unactionable task {t!r}", flush=True)
        return False
    q = _norm(t.get("quote") or "")
    if not q:
        return False
    who = (t.get("from_whom") or "").strip() or speaker_fallback
    bits = [b for b in (f"from {who}" if who else "", f"due {due}" if due else "") if b]
    task = text + (f" ({', '.join(bits)})" if bits else "")
    quote = t.get("quote") or None
    if q in _seen_task_quotes:
        _tid, _created, changed = _upsert_task(task, who or None, quote, insert=False)
        return changed
    _seen_task_quotes.add(q)
    tid, created, changed = _upsert_task(task, who or None, quote, insert=True)
    if created:
        push_card("task", "New task", task, ref=tid, speaker=who or None)
        print(f"memory: {log_label} {task}", flush=True)
    return changed


def _live_extract_chunk(transcript: str, speaker: str | None) -> None:
    """Live task extract on the FULL rolling conversation transcript so far: same
    extract_tasks + quote gate as the 30 s extract, but immediately, so a promise
    becomes a draft task (visible in Needs you / GET /tasks) within seconds, not
    after 30 s. Drafts only: nothing sends without a tap. Runs on the extract
    lane, never the audio lane, so it never stalls the next chunk.

    `transcript` is the whole conversation so far, NOT one 2.5 s chunk: a request
    and its day/time arrive in different chunks, and a single chunk both drops
    the time ("block out a time..." with no Tuesday 5 p.m.) and invents fragment
    tasks ("Sit down."). The caller (process_audio) passes " ".join(_conv_transcript).

    A verified task also goes to Follow-Through NOW, on its own thread -- the
    live path used to stop at the local task/card, so Follow-Through only ever
    learned about a promise from the 30 s flush (which itself failed whenever
    Follow-Through was down). Only chunks that actually produced a verified
    task are forwarded, so a chatty conversation does not fire an /api/run
    (a full-transcript GPT-4o pass) every ~2.5 s; the end-of-conversation
    flush still sends the full transcript for anything the live pass missed."""
    _t_live0 = time.time()
    try:
        out = models.extract_tasks(transcript)
    except Exception as e:
        print(f"memory: live extract failed: {e!r}", flush=True)
        return
    made = 0
    for t in out.get("tasks", []):
        if _create_task_from_extract(t, transcript, speaker, log_label="LIVE TASK"):
            made += 1
    print(f"memory: LIVE_EXTRACT_TIMING extract_ms={(time.time() - _t_live0) * 1000:.0f} tasks={made}", flush=True)
    if made and FOLLOWTHROUGH_URL:
        # Speaker prefix so Follow-Through's transcript carries who said it
        # (its parse_text turns "Srijan: ..." into a speaker segment); the
        # quote gate above already passed, this changes nothing about it.
        ft_text = f"{speaker}: {transcript}" if speaker else transcript
        threading.Thread(target=forward_to_followthrough, args=(ft_text,), daemon=True).start()


def _process_transcript(text: str, extract_text: str | None = None,
                        pending_ids: list[int] | None = None) -> None:
    """`text` is the batch just flushed (what Follow-Through is forwarded, and what
    the reservation backstop below checks). `extract_text` is the transcript task
    extraction runs on: the whole conversation transcript in Conversation Mode (a
    task's day/time may have been said in an earlier batch), else the batch itself.
    `pending_ids` are the durable pending_transcript rows this batch covers: they
    are deleted ONLY at the end of this function, after extract has run on them.
    If extract throws, the exception propagates (the extract-lane worker logs
    it) and the rows stay, so the next startup's recover_pending() retries the
    batch instead of losing it."""
    if extract_text is None:
        extract_text = text
    # Backstop: a reservation request that slipped past dining (no wake word, split
    # chunks) must never become a Follow-Through promise or an extract task. The
    # dining path above is the real fix; this only stops a short, self-contained
    # request from being forwarded as a commitment.
    if len(text) < 160 and _RESERVATION_REQUEST_RE.search(text) and _TIME_FIND.search(text):
        print(f"memory: skipped reservation request in transcript extract {text!r}", flush=True)
        _pending_delete(pending_ids or [])  # batch was handled, its rows can go
        return
    # ponytail: runs on the extract lane while the audio lane keeps going; the shared state it touches
    # (sqlite under _db_lock, Follow-Through's question queue) is small. Add locks if it grows.
    receipts = [r["text"] for r in rows(
        "SELECT text FROM memories WHERE kind='event' AND text LIKE 'Receipt:%' AND ts > ?", time.time() - 3600)]
    # Follow-Through's GPT-4o run (often 5-15 s, httpx timeout 60) used to run BEFORE
    # extract_tasks, so every task / payment / done item waited on it. Run it in parallel
    # on its own thread; extract_tasks starts now. Its questions still enqueue via
    # _ft_enqueue, which is only deque/set appends from that thread.
    _t_extract0 = time.time()
    if FOLLOWTHROUGH_URL:
        threading.Thread(target=forward_to_followthrough, args=(text,), daemon=True).start()
    out = models.extract_tasks(extract_text, receipts)
    print(f"memory: EXTRACT_TIMING extract_ms={(time.time() - _t_extract0) * 1000:.0f} tasks={len(out.get('tasks', []))} done={len(out.get('done', []))}", flush=True)
    for t in out.get("tasks", []):
        # speaker = who asked, straight from extract_tasks' from_whom. Only what the model
        # heard named in the transcript; an empty from_whom stays None, never guessed.
        _create_task_from_extract(t, extract_text, None, log_label="TASK")
    for m in out.get("owed_to_me", []):
        if not quoted(m, extract_text):
            continue
        try:
            pid = payments.propose(m["who"], round(float(m["amount"]) * 100), m.get("memo") or "shared expense")
            p = payments.get(pid)
            push_card("payment", f"Request ${p['amount_cents'] / 100:.2f} from {p['counterparty']}?",
                      f"{p['memo']} — \"{m['quote']}\"", ref=pid)
        except (payments.PaymentError, KeyError, ValueError, TypeError) as e:
            print(f"memory: skipped money item {m!r}: {e}", flush=True)
    for item in out.get("done", []):
        if not quoted(item, extract_text):
            continue
        d = item["text"]
        add("event", f"Done: {d}")
        hits = search(models.embed(d), 1, "kind='task' AND status='open'")
        if hits and hits[0][0] >= DONE_MATCH:
            set_done(hits[0][1]["id"])
            print(f"memory: closed task {hits[0][1]['text']!r} ({hits[0][0]:.2f})", flush=True)
    # Extract ran clean on this batch: its durable rows can finally go. If any
    # line above threw, this never runs and recover_pending() retries the batch.
    _pending_delete(pending_ids or [])


# /api/run re-reads Follow-Through's WHOLE transcript with GPT-4o, so overlapping
# forwards (live chunk + 30 s flush + end flush) must not each pay for a full run.
# _ft_posted_seq counts transcript POSTs; a run covers every seq posted before it
# starts, and a waiter whose seq is already covered skips its own run. A duplicate
# seq from two threads posting at once only costs one extra run, never a lost one.
_ft_run_lock = threading.Lock()
_ft_posted_seq = 0
_ft_covered_seq = 0

# Sentences already posted to Follow-Through, so the live / 30 s / end-flush
# forwards stop re-posting the same words. The live path sends the WHOLE
# rolling conversation transcript every chunk, the flushes send the same words
# again: without this, one session paid for 23 full /api/run passes and the
# transcript grew with cumulative copies of the same sentence (13 copies of
# one sentence in the live state), each run re-mining the same promises with
# a slightly different quote. Only long sentences are tracked: short ones
# ("I don't know.", "Yeah.") repeat constantly in real speech, and tracking
# them would swallow a genuinely repeated short promise. The set is cleared
# when a new conversation starts, so the same words in a later conversation
# forward again.
_ft_sent_lines: set[str] = set()
_ft_sent_lock = threading.Lock()
# Split on sentence ends, but not inside "p.m." / "a.m." / "e.g." / "i.e." --
# "Tuesday at five p.m. for math class" is one sentence, and splitting it
# after "p.m." left a "for math class, please." fragment that could never
# be marked sent, so it forwarded forever.
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])(?<!p\.m\.)(?<!a\.m\.)(?<!e\.g\.)(?<!i\.e\.)\s+|\n+", re.I)


def _ft_sentence_keys(text: str) -> list[str]:
    keys: list[str] = []
    for chunk in _SENTENCE_SPLIT_RE.split(text or ""):
        s = chunk.strip()
        if not s:
            continue
        s = re.sub(r"^[A-Za-z][\w .'-]{0,24}:\s+", "", s)  # strip "Speaker: " prefix
        key = _norm(s)
        if len(key) >= 25:
            keys.append(key)
    return keys


def _ft_only_new_sentences(text: str) -> str:
    """The part of `text` Follow-Through has not been sent yet, sentences
    intact, short untracked sentences always included. Empty when nothing is
    new, which is the common case for the 30 s / end flushes after the live
    path already forwarded the same words."""
    kept: list[str] = []
    with _ft_sent_lock:
        for chunk in _SENTENCE_SPLIT_RE.split(text or ""):
            s = chunk.strip()
            if not s:
                continue
            keys = _ft_sentence_keys(s)
            if not keys or any(k not in _ft_sent_lines for k in keys):
                kept.append(s)
    return " ".join(kept)


def _ft_mark_sentences(text: str) -> None:
    with _ft_sent_lock:
        _ft_sent_lines.update(_ft_sentence_keys(text))


def _ft_unmark_sentences(text: str) -> None:
    """A forward whose POST succeeded but whose run failed is queued for
    replay: unmark its sentences so the replay is not filtered to nothing."""
    with _ft_sent_lock:
        for k in _ft_sentence_keys(text):
            _ft_sent_lines.discard(k)


def forward_to_followthrough(text: str, queue_retry: bool = True) -> bool:
    """Append conversation text to Follow-Through and let it look for commitments.
    Never blocks Scene's own extraction. Returns True when the text was posted
    (or was already covered by another run). On failure it used to be one log
    line and gone -- if Follow-Through was down, that batch never became a
    promise. Now the text is queued on disk (pending_ft.jsonl) and replayed on
    the next startup, unless this call IS the replay (queue_retry=False).
    Called from a daemon thread by both the live per-chunk path and the
    30 s / end flushes."""
    global _ft_posted_seq, _ft_covered_seq
    if not FOLLOWTHROUGH_URL:
        return True
    t0 = time.time()
    # Forward only sentences Follow-Through has not seen: the live path
    # re-sends the whole rolling transcript every chunk and the flushes
    # re-send it again, so most forwards are all-old. No new sentences means
    # no POST and no /api/run at all -- that is what makes the first delivery
    # fast and stops the run queue backing up behind duplicate runs.
    text = _ft_only_new_sentences(text)
    if not text:
        print("memory: FT_FORWARD_TIMING run_skipped=no_new_text", flush=True)
        return True
    items: list[dict] = []
    skipped = False
    try:
        with httpx.Client(timeout=60) as c:
            c.post(f"{FOLLOWTHROUGH_URL}/api/transcript", json={"text": text}).raise_for_status()
            _ft_mark_sentences(text)
            transcript_ms = (time.time() - t0) * 1000
            _ft_posted_seq += 1
            my_seq = _ft_posted_seq
            with _ft_run_lock:
                if _ft_covered_seq >= my_seq:
                    skipped = True   # a run that started after this post already covered it
                else:
                    run_at_seq = _ft_posted_seq
                    t_run0 = time.time()
                    run = c.post(f"{FOLLOWTHROUGH_URL}/api/run")
                    run.raise_for_status()
                    run_ms = (time.time() - t_run0) * 1000
                    items = run.json().get("items", [])
                    _ft_covered_seq = max(_ft_covered_seq, run_at_seq)
    except Exception as e:
        print(f"memory: Follow-Through forward failed: {e!r}", flush=True)
        _ft_unmark_sentences(text)  # so the queued replay still forwards them
        if queue_retry:
            _queue_ft_retry(text)
        return False
    if skipped:
        print(f"memory: FT_FORWARD_TIMING transcript_ms={transcript_ms:.0f} run_skipped=already_covered seq={my_seq}", flush=True)
        return True
    print(f"memory: FT_FORWARD_TIMING transcript_ms={transcript_ms:.0f} run_ms={run_ms:.0f} items={len(items)} total_ms={(time.time() - t0) * 1000:.0f}", flush=True)
    if FT_VOICE and voice.speaks():   # cards-only: approvals stay on the display, nothing is asked aloud
        _ft_enqueue(items)
    return True


# ---------- Follow-Through call and response (ElevenLabs asks, you answer yes or no) ----------
# A new promise -> Scene asks out loud with Follow-Through's own sentence ("Want me to send Nayan the deck
# by four?") -> listens for a short yes/no -> Follow-Through decides via POST /api/items/{id}/voice, where a
# clear yes approves that one item, a clear no dismisses it, anything else changes nothing. One question
# at a time. Everything here runs on the audio lane, so no locking.

FT_VOICE = os.environ.get("SCENE_FT_VOICE", "1") == "1"
FT_ANSWER_WINDOW_S = float(os.environ.get("SCENE_FT_ANSWER_WINDOW_S", "12"))
_ft_asked: set[str] = set()      # item ids already asked aloud (never ask twice)
_ft_queue: deque = deque()       # items waiting for their turn
_ft_waiting: dict | None = None  # the question in the air: {id, title, listen_from, until}


def _ft(method: str, path: str, **kw) -> dict:
    with httpx.Client(timeout=20) as c:
        r = c.request(method, f"{FOLLOWTHROUGH_URL}{path}", **kw)
        r.raise_for_status()
        return r.json()


def _ft_enqueue(items: list[dict]) -> None:
    for i in items:
        if i.get("status") == "pending" and i.get("id") and i["id"] not in _ft_asked:
            _ft_asked.add(i["id"])
            _ft_queue.append(i)
    _ft_ask_next()


def _ft_item_status(item_id: str) -> str | None:
    """Live status of one Follow-Through item, or None when it cannot be checked (FT down,
    unexpected shape). None means "unknown": callers proceed as before, never block on it."""
    try:
        state = _ft("GET", "/api/state")
    except Exception:
        return None
    for it in state.get("items", []) or []:
        if it.get("id") == item_id:
            return it.get("status")
    return None


def _ft_already_handled(w: dict, status: str) -> None:
    """The voice loop was about to act on an item the wearer already handled by tap (or that
    Follow-Through could not verify). Say why, never "skipped"."""
    title = w.get("title", "")
    if status in ("done", "drafted"):
        push_card("answer", f"Already handled: {title}", "You already approved this one. Nothing was sent twice.")
        voice.speak("That's already handled.")
    elif status == "dismissed":
        push_card("answer", f"Already dismissed: {title}", "You already dismissed this one.")
        voice.speak("That one's already dismissed.")
    elif status == "needs_review":
        push_card("answer", f"Can't approve yet: {title}",
                  "Follow-Through couldn't verify the exact quote. Check it on the phone first.")
        voice.speak("That one needs a quick check first.")
    else:  # failed, or anything else unexpected
        push_card("answer", f"That one failed: {title}", "Try again from the phone.")
        voice.speak("That one failed. Try again from the phone.")
    print(f"memory: FT already {status}: {title!r}", flush=True)


def ft_cancel(item_id: str) -> None:
    """A tap on the phone or glasses handled this promise (approve / dismiss / retry). Stop
    asking about it aloud: clear the question in the air, drop it from the queue, and never
    re-ask it in this process. Called from the /ft proxy after a successful tap. Without this,
    the voice loop kept asking and a later 'no' or 'skip' dismissed an already-approved item,
    which is exactly the 'Skipped' Nayan kept seeing."""
    global _ft_waiting, _ft_queue
    if _ft_waiting and _ft_waiting.get("id") == item_id:
        _ft_waiting = None
    _ft_queue = deque(i for i in _ft_queue if i.get("id") != item_id)
    _ft_asked.add(item_id)


def _ft_ask_next() -> None:
    global _ft_waiting
    now = time.time()
    if _conv_active:
        return   # Conversation Mode: never interrupt speech with a spoken question; the queue waits
    if _ft_waiting and now < _ft_waiting["until"]:
        return
    _ft_waiting = None
    while _ft_queue:
        item = _ft_queue.popleft()
        status = _ft_item_status(item["id"])
        if status is not None and status != "pending":
            # Handled by tap (or unverifiable) since it was queued: never ask about it aloud.
            print(f"memory: FT not asking, already {status}: {item.get('title', '')!r}", flush=True)
            continue
        try:
            q = _ft("GET", f"/api/items/{item['id']}/question")["text"]
        except Exception as e:
            print(f"memory: Follow-Through question failed: {e!r}", flush=True)
            continue
        voice.speak(q)
        talking = len(q.split()) / 2.6 + 1.5   # roughly how long the laptop is speaking: don't hear ourselves
        _ft_waiting = {"id": item["id"], "title": item.get("title", ""), "listen_from": now + talking,
                       "until": now + talking + FT_ANSWER_WINDOW_S}
        print(f"memory: FT ASK {q!r}", flush=True)
        return


def _ft_try_answer(text: str) -> bool:
    """True if this chunk belonged to the question in the air (our own voice, or the answer)."""
    global _ft_waiting
    w, now = _ft_waiting, time.time()
    if w is None:
        return False
    if now < w["listen_from"]:
        return True        # the laptop asking its own question: never let it become a "promise"
    if now > w["until"]:
        _ft_waiting = None
        return False
    if len(text.split()) <= 3 and _LATER_WORDS.search(text):
        # "skip" / "stop" / "not now" / "later" means ask me later, not "dismiss this promise
        # forever". Same meaning as the Later button on the glasses. Never posts to /voice,
        # so it can never turn into a Skipped card. Kept to short replies so a longer
        # sentence that happens to mention "later" is not swallowed.
        _ft_waiting = None
        voice.speak("Okay, later.")
        print(f"memory: FT later {text!r} (not a dismiss)", flush=True)
        _ft_ask_next()
        return True
    if len(text.split()) > MAX_ANSWER_WORDS or parse_yes_no(text) == "unclear":
        return False       # ordinary conversation, keep listening
    status = _ft_item_status(w["id"])
    if status is not None and status != "pending":
        # Already handled by tap (or unverifiable) since the question was asked: say why
        # instead of acting on a stale question, and never call it "skipped".
        _ft_waiting = None
        _ft_already_handled(w, status)
        _ft_ask_next()
        return True
    try:
        r = _ft("POST", f"/api/items/{w['id']}/voice", json={"heard": text})
    except Exception as e:
        print(f"memory: Follow-Through voice answer failed: {e!r}", flush=True)
        _ft_waiting = None
        status = _ft_item_status(w["id"])
        if status is not None and status != "pending":
            # The post failed because the item is no longer pending (e.g. a tap approved it
            # a moment ago): say what happened instead of failing silently.
            _ft_already_handled(w, status)
        return True
    _ft_waiting = None
    item = r.get("item") or {}
    if r.get("answer") == "yes":
        push_card("answer", f"Approved: {w['title']}", item.get("result") or "Done.")
        voice.speak("Done.")
        voice.chime_out()
    elif r.get("answer") == "no":
        push_card("answer", f"Skipped: {w['title']}", "Dismissed. Nothing was sent.")
        voice.speak("Okay, skipped.")
        voice.chime_out()
    print(f"memory: FT ANSWER {text!r} -> {r.get('answer')}", flush=True)
    _ft_ask_next()
    return True


def set_done(task_id: int) -> bool:
    with _db_lock:
        n = db().execute("UPDATE memories SET status='done' WHERE id=? AND kind='task'", (task_id,)).rowcount
        db().commit()
    return n > 0


# ---------- query ----------

_WHERE_IS_RE = re.compile(r"\bwhere\b[^?]{0,40}?\bmy\s+([a-z][a-z\- ]{1,40}?)(?:\?|$)", re.I)


def _where_is_object(question: str) -> str | None:
    """Object asked about in "where is my X" / "where did I leave my X", else None."""
    m = _WHERE_IS_RE.search(question or "")
    if not m:
        return None
    words = [w for w in m.group(1).lower().split() if w not in ("the", "a", "an", "my")]
    return " ".join(words[:3]) or None


def _object_matches(text: str, wanted: str) -> bool:
    """True when a stored object name is the same thing as the wanted object: the
    head noun (last word, e.g. "bottle") must match, plus another word when the
    wanted name has one ("water" in "water bottle"). A "black pouch" never matches
    "water bottle": neither word is in it."""
    have = set(re.findall(r"[a-z]+", (text or "").lower()))
    want = re.findall(r"[a-z]+", (wanted or "").lower())
    if not want or want[-1] not in have:
        return False
    # Head noun matched. A stored name of one word ("bottle") is the same thing as
    # "water bottle"; a longer stored name must share another word too ("black
    # pouch" shares nothing with "water bottle" and never matches).
    return len(want) == 1 or len(have) == 1 or any(w in have for w in want[:-1])


def ask(question: str) -> dict:
    # Per-stage timing (questions are rare, so one line per ask is already throttled).
    _t0 = time.time()
    now = time.time()
    _where = _where_is_object(question)
    if _where:
        # Exact-object gate (2026-11-14): "where is my water bottle" was answered
        # about a "black pouch" because a weak embedding hit (MIN_SIM 0.25) on a
        # different object reached the answer model, which named it. Only a
        # sighting of the SAME object, within 3 h, counts; anything else is
        # "not seen", never a different object. No model call on this path.
        seen = rows("SELECT * FROM memories WHERE kind='object' ORDER BY ts DESC LIMIT 50")
        hit = next((r for r in seen if _object_matches(r.get("text") or "", _where)
                    and now - r["ts"] <= LEAD_MAX_AGE_MIN * 60), None)
        if hit is None:
            print(f"memory: ASK_TIMING search_ms=0 answer_ms=0 hits=0 no_match=True where_is={_where!r}", flush=True)
            return {"question": question,
                    "answer": f"I have not seen your {_where} in the last 3 hours.",
                    "memory_id": None, "frame": None, "target": None,
                    "no_match": True, "checked": ["objects"], "agent_offer": False}
        place = (hit.get("place") or "").strip()
        target = {"name": hit["text"], "place": place, "frame": hit["frame"],
                  "frame_path": str(DATA_DIR / "frames" / hit["frame"]) if hit.get("frame") else None,
                  "age_min": round((now - hit["ts"]) / 60)}
        answer = f"Your {_where} is {place}." if place else f"I last saw your {_where} here."
        return {"question": question, "answer": answer,
                "memory_id": hit["id"], "frame": hit["frame"], "target": target,
                "no_match": False}
    hits = [(s, r) for s, r in search(models.embed(question), 8) if s >= MIN_SIM]
    _search_ms = (time.time() - _t0) * 1000
    # Open tasks are always relevant to "what do I need to do?"-style questions and there are few
    # of them: include them even when the wording doesn't match closely.
    seen = {r["id"] for _, r in hits}
    hits += [(0.0, r) for r in rows("SELECT * FROM memories WHERE kind='task' AND status='open' ORDER BY ts DESC LIMIT 5")
             if r["id"] not in seen]
    if not hits:  # no model call: nothing relevant is better than a confident wrong answer.
        # Never answer with the bare "I don't have a memory of that." alone: say what was
        # checked (objects + tasks + events, via the embedding search above) and offer the
        # agent path, so an action phrased as a question still has somewhere to go.
        # `checked` / `agent_offer` let the phone show that offer as a button, not just words.
        print(f"memory: ASK_TIMING search_ms={_search_ms:.0f} answer_ms=0 hits=0 no_match=True", flush=True)
        return {"question": question,
                "answer": "No match in your memories, tasks, or events. Want me to book, email, or schedule it instead?",
                "memory_id": None, "frame": None, "target": None,
                "no_match": True, "checked": ["objects", "tasks", "events"], "agent_offer": True,
                "search_ms": _search_ms, "llm_ms": 0}
    mems = [{"id": r["id"], "kind": r["kind"], "text": r["text"], "place": r["place"],
             "status": r["status"], "age_min": round((now - r["ts"]) / 60)} for _, r in hits]
    out = models.answer(question, mems)
    _llm_ms = (time.time() - _t0) * 1000 - _search_ms
    print(f"memory: ASK_TIMING search_ms={_search_ms:.0f} answer_ms={_llm_ms:.0f} hits={len(hits)}", flush=True)
    best = next((r for _, r in hits if r["id"] == out.get("memory_id")), None)
    target = None
    if best and best["kind"] == "object" and best["frame"]:
        target = {"name": best["text"], "place": best["place"] or "", "frame": best["frame"],
                  "frame_path": str(DATA_DIR / "frames" / best["frame"]),
                  "age_min": round((now - best["ts"]) / 60)}
    return {"question": question, "answer": out.get("answer", ""),
            "memory_id": best and best["id"], "frame": best and best["frame"], "target": target,
            "no_match": False, "search_ms": _search_ms, "llm_ms": _llm_ms}


def open_tasks(limit: int = 6) -> list[dict]:
    """Open tasks, each with `speaker` (and `from_whom`, same value) when the backend knows
    who asked for it, else None. Legacy rows fall back to the "(from ...)" in their text."""
    out = []
    for r in rows("SELECT id, text, ts, speaker, quote FROM memories WHERE kind='task' AND status='open' "
                  "ORDER BY ts DESC LIMIT ?", limit):
        speaker = r.get("speaker") or _speaker_from_text(r.get("text") or "")
        out.append({**r, "speaker": speaker, "from_whom": speaker})
    return out


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
