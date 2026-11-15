"""Note session: after the wearer says yes to "want me to take notes?", collect what is heard and
one frame per slide, then build notes. Nothing is kept without verbatim evidence in the transcript
or on a saved slide (same rule as memory.quoted). Pure logic apart from the one model call in build().
"""

import json
import re
from datetime import datetime
from dataclasses import dataclass, field
from pathlib import Path

import models

# The session used to live only in RAM (_session): restart the backend before
# /notes/stop and the whole meeting's lines and slides were gone. It is now
# written to data/notes-session.json on every line/slide, reloaded by
# recover() on startup, and deleted only when the notes are built.
_data_dir: Path | None = None


def set_data_dir(p) -> None:
    global _data_dir
    _data_dir = Path(p)


def _session_path() -> Path:
    base = _data_dir if _data_dir is not None else Path(__file__).parent / "data"
    return base / "notes-session.json"


def _save_session() -> None:
    if _session is None:
        return
    try:
        p = _session_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"started": _session.started, "last_seen": _session.last_seen,
                                 "lines": _session.lines, "slides": _session.slides}))
    except OSError:
        pass  # a failed save must never break note-taking; the RAM copy still works


def recover() -> bool:
    """Reload a session the backend was killed with still in progress."""
    global _session
    p = _session_path()
    if not p.is_file():
        return False
    try:
        d = json.loads(p.read_text())
        _session = Session(started=float(d["started"]), last_seen=float(d["last_seen"]),
                           lines=list(d.get("lines", [])), slides=list(d.get("slides", [])))
    except (ValueError, KeyError, TypeError, OSError):
        return False
    return True

IDLE_STOP_S = 600.0       # no meeting or slides on screen for this long: the meeting is over
SAME_SLIDE = 0.8          # word overlap at or above this counts as the same slide
MIN_CONF = 0.6
MIN_QUOTE_CHARS = 8


@dataclass
class Session:
    started: float
    last_seen: float
    lines: list[str] = field(default_factory=list)
    slides: list[dict] = field(default_factory=list)   # {"frame": str, "text": str}


_session: Session | None = None


def norm(s: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9$.]+", " ", (s or "").lower()).split())


def slide_changed(prev: str, new: str) -> bool:
    """True when `new` is a different slide from `prev`. Nothing readable is never a new slide."""
    b = set(norm(new).split())
    if not b:
        return False
    a = set(norm(prev).split())
    if not a:
        return True
    return len(a & b) / len(a | b) < SAME_SLIDE


def start(now: float) -> None:
    global _session
    _session = Session(started=now, last_seen=now)
    _save_session()


def active() -> bool:
    return _session is not None


def status() -> dict:
    """For the glasses: is a note session running, since when, how much has it caught."""
    if _session is None:
        return {"active": False, "started": None, "slides": 0, "lines": 0}
    return {"active": True, "started": _session.started, "slides": len(_session.slides), "lines": len(_session.lines)}


def add_line(text: str) -> None:
    if _session is not None and text:
        _session.lines.append(text)
        _save_session()


def on_scene(scene, frame: str | None, now: float) -> bool:
    """Feed one analyzed frame to the active session. True when the meeting looks over."""
    s = _session
    if s is None or not isinstance(scene, dict):
        return False
    t, conf, text = scene.get("type"), scene.get("confidence"), scene.get("text") or ""
    if isinstance(conf, bool) or not isinstance(conf, (int, float)) or conf < MIN_CONF or not isinstance(t, str):
        return now - s.last_seen > IDLE_STOP_S
    if t in ("meeting", "slides"):
        s.last_seen = now
    if t in ("slides", "whiteboard") and isinstance(text, str) and frame:
        last = s.slides[-1]["text"] if s.slides else ""
        if slide_changed(last, text):
            s.slides.append({"frame": frame, "text": text})
    _save_session()
    return now - s.last_seen > IDLE_STOP_S


def stop() -> Session | None:
    global _session
    s, _session = _session, None
    try:
        _session_path().unlink(missing_ok=True)  # built now: the durable copy can go
    except OSError:
        pass
    return s


def _valid_start(v) -> bool:
    try:
        datetime.strptime(v, "%Y-%m-%dT%H:%M")
        return True
    except (TypeError, ValueError):
        return False


def build(session: Session, make=models.make_notes) -> dict:
    """Notes from a finished session. Action items survive only with a quote found in the transcript or a slide."""
    transcript = "\n".join(session.lines)
    texts = [s["text"] for s in session.slides]
    if not transcript and not texts:
        return {"summary": "", "decisions": [], "action_items": [], "slides": session.slides}
    out = make(transcript, texts)
    haystacks = [norm(transcript)] + [norm(t) for t in texts]
    kept = []
    for item in out.get("action_items", []):
        q = norm(item.get("quote", ""))
        if len(q) >= MIN_QUOTE_CHARS and any(q in h for h in haystacks) and item.get("text"):
            keep = {"text": item["text"], "quote": item["quote"]}
            if item.get("action") == "email" and item.get("subject") and item.get("body"):
                keep.update(action="email", to=item.get("to", ""), subject=item["subject"], body=item["body"])
            if item.get("action") == "calendar" and _valid_start(item.get("start")):
                keep.update(action="calendar", start=item["start"], duration_min=int(item.get("duration_min") or 30))
            kept.append(keep)
    return {"summary": out.get("summary", ""), "decisions": list(out.get("decisions", [])),
            "action_items": kept, "slides": session.slides}
