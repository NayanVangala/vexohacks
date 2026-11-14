"""Note session: after the wearer says yes to "want me to take notes?", collect what is heard and
one frame per slide, then build notes. Nothing is kept without verbatim evidence in the transcript
or on a saved slide (same rule as memory.quoted). Pure logic apart from the one model call in build().
"""

import re
from dataclasses import dataclass, field

import models

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


def active() -> bool:
    return _session is not None


def add_line(text: str) -> None:
    if _session is not None and text:
        _session.lines.append(text)


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
    return now - s.last_seen > IDLE_STOP_S


def stop() -> Session | None:
    global _session
    s, _session = _session, None
    return s


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
            kept.append({"text": item["text"], "quote": item["quote"]})
    return {"summary": out.get("summary", ""), "decisions": list(out.get("decisions", [])),
            "action_items": kept, "slides": session.slides}
