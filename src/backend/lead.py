"""Lead me there: after Scene says where a thing is, it offers to walk you to it.

"Where's my water bottle?" -> "On the white table next to the laptop. Want me to lead you there?" -> "Yes" ->
every few seconds Scene looks through the glasses, compares what you see with the photo of where the thing was,
and says one short step ("Turn left toward the window.") until the thing is in view. Say "stop" any time.

State machine only: memory.py feeds it audio and frames and says what it speaks. Vision is Gemini (models.guide).
"""
import re
import threading
import time
from pathlib import Path

import models
from awareness import parse_yes_no

OFFER_WINDOW_S = 15.0      # how long a "want me to lead you there?" stays answerable
EVERY_S = 3.5              # minimum gap between looks
MAX_S = 180.0              # give up after three minutes; nobody wants a forgotten guide talking all day
MAX_ANSWER_WORDS = 6
STOP = re.compile(r"\b(stop|cancel|never ?mind|found it|got it|i see it|that'?s (enough|ok|okay)|i'?m (good|here))\b", re.I)

_lock = threading.Lock()
_state: dict | None = None      # {"mode": "offer"|"active", target, ...}


def _talk_s(text: str) -> float:
    return len(text.split()) / 2.6 + 1.5     # roughly how long the voice takes, so we never answer ourselves


def offer(target: dict, spoken: str, now: float | None = None) -> None:
    """target: {name, place, frame (file name or None), frame_path (Path or None)}. `spoken` is what Scene just said."""
    global _state
    now = time.time() if now is None else now
    with _lock:
        _state = {"mode": "offer", "target": target, "listen_from": now + _talk_s(spoken),
                  "until": now + _talk_s(spoken) + OFFER_WINDOW_S}


def mode() -> str | None:
    return _state["mode"] if _state else None


def active() -> bool:
    return mode() == "active"


def stop() -> None:
    global _state
    with _lock:
        _state = None


def try_answer(text: str, now: float | None = None, from_tap: bool = False) -> str | None:
    """Handle one transcribed chunk while an offer is open. Returns what to do:
    "start" (yes), "declined" (no), "ignore" (our own voice, or ordinary talk: keep listening), None (no offer).
    from_tap=True is a deliberate human tap (glasses pinch / phone button): it is an answer, never
    Scene's own voice, so the listen_from gate does not apply. The voice path never passes it."""
    global _state
    now = time.time() if now is None else now
    with _lock:
        s = _state
        if not s or s["mode"] != "offer":
            return None
        if now < s["listen_from"] and not from_tap:
            return "ignore"                    # Scene's own question reaching the mic: never an answer
        if now > s["until"]:
            _state = None
            return None
        if len(text.split()) > MAX_ANSWER_WORDS:
            return None
        answer = parse_yes_no(text)
        if answer == "unclear":
            return None
        if answer == "no":
            _state = None
            return "declined"
        _state = {"mode": "active", "target": s["target"], "started": now, "next_at": now, "last": "", "steps": 0,
                  "busy": False}
        return "start"


def wants_stop(text: str) -> bool:
    return active() and bool(STOP.search(text))


def due(now: float | None = None) -> bool:
    """True when the next look should be taken; marks it in flight so frames don't pile up behind a slow model."""
    now = time.time() if now is None else now
    with _lock:
        s = _state
        if not s or s["mode"] != "active" or s["busy"] or now < s["next_at"]:
            return False
        s["busy"] = True
        return True


def step(path: Path, say, now: float | None = None) -> None:
    """One look. `say(text, arrived)` speaks or shows the instruction. Ends the lead on arrival or timeout."""
    global _state
    now = time.time() if now is None else now
    with _lock:
        s = _state
        if not s or s["mode"] != "active":
            return
        target, last, steps, started = s["target"], s["last"], s["steps"], s["started"]
    if now - started > MAX_S:
        stop()
        say("I can't find it from here. Ask me again if you want to try once more.", True)
        return
    try:
        target_jpeg = Path(target["frame_path"]).read_bytes() if target.get("frame_path") else None
    except OSError:
        target_jpeg = None
    try:
        out = models.guide(path.read_bytes(), target, target_jpeg, last)
    except Exception as e:  # a bad model call just skips this look
        print(f"lead: guide failed: {e!r}", flush=True)
        out = {}
    finally:
        with _lock:
            if _state is s:
                s["busy"] = False
    text = str(out.get("instruction") or "").strip()
    with _lock:
        if _state is not s:                      # stopped while the model was thinking
            return
        if out.get("arrived"):
            _state = None
        elif text and text.lower() != last.lower():
            s["last"], s["steps"] = text, steps + 1
            s["next_at"] = now + max(EVERY_S, _talk_s(text))
        else:
            s["next_at"] = now + EVERY_S
            text = ""
    if text:
        say(text, bool(out.get("arrived")))


def get_target() -> dict | None:
    return _state["target"] if _state else None
