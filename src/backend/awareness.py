"""Awareness: turns what the glasses see into at most one polite offer ("want me to take notes?").

Pure logic, no network, time is passed in so it is easy to test. memory.py owns the wiring.
Thresholds are first guesses; tune them live (env vars below).
"""

import itertools
import os
import re
from dataclasses import dataclass

NEED = int(os.environ.get("SCENE_NEED_FRAMES", "3"))             # same scene on this many frames in a row
MIN_CONF = float(os.environ.get("SCENE_MIN_CONF", "0.6"))
SHOWN_COOLDOWN_S = float(os.environ.get("SCENE_SHOWN_COOLDOWN_S", "600"))
DECLINED_COOLDOWN_S = float(os.environ.get("SCENE_DECLINED_COOLDOWN_S", "1800"))
EXPIRE_S = float(os.environ.get("SCENE_OFFER_EXPIRE_S", "60"))
LOG_COOLDOWN_S = 600.0

OFFER_TYPES = {"meeting", "slides"}
LOG_TYPES = {"whiteboard": "a whiteboard", "card": "a business card", "menu": "a menu"}

YES = ["yes", "yeah", "yep", "yup", "sure", "ok", "okay", "please do", "go ahead", "do it",
       "sounds good", "absolutely", "definitely"]
NO = ["no", "nope", "nah", "don't", "dont", "do not", "cancel", "skip", "dismiss", "never mind",
      "nevermind", "not now"]


def _has(words: list[str], text: str) -> bool:
    return any(re.search(r"(?<![a-z'])" + re.escape(w) + r"(?![a-z'])", text) for w in words)


def parse_yes_no(heard: str) -> str:
    """Only a clear yes or a clear no counts. Both, or neither, is "unclear", so a mumble answers nothing."""
    text = re.sub(r"[^a-z' ]", " ", (heard or "").lower())
    yes, no = _has(YES, text), _has(NO, text)
    if yes == no:
        return "unclear"
    return "yes" if yes else "no"


@dataclass
class Offer:
    id: int
    scene_type: str
    text: str
    created: float


def _offer_text() -> str:
    first = os.environ.get("WEARER_NAME", "").split()[:1]
    hey = f"Hey {first[0]}, " if first else ""
    return f"{hey}looks like you're sitting in on a meeting. Want me to take notes?"


class Awareness:
    def __init__(self):
        self._ids = itertools.count(1)
        self._type: str | None = None
        self._streak = 0
        self._busy = False
        self._pending: Offer | None = None
        self._quiet_until = 0.0
        self._logged: dict[str, float] = {}

    def set_busy(self, busy: bool) -> None:
        self._busy = busy

    def pending(self, now: float) -> Offer | None:
        if self._pending and now - self._pending.created > EXPIRE_S:
            self._pending = None
        return self._pending

    @staticmethod
    def _clean(scene) -> tuple[str, float] | None:
        """(type, confidence) from a model scene, or None when it is missing or malformed."""
        if not isinstance(scene, dict):
            return None
        t, c = scene.get("type"), scene.get("confidence")
        if not isinstance(t, str) or isinstance(c, bool) or not isinstance(c, (int, float)):
            return None
        return t, float(c)

    def feed(self, scene, now: float) -> Offer | None:
        """One analyzed frame. Returns an Offer the first time a meeting/slides scene is steady, else None."""
        clean = self._clean(scene)
        if clean is None:
            return None
        t, conf = clean
        if conf < MIN_CONF:
            return None
        if t != self._type:
            self._type, self._streak = t, 0
        self._streak += 1
        if (t not in OFFER_TYPES or self._streak < NEED or self._busy
                or self.pending(now) or now < self._quiet_until):
            return None
        self._pending = Offer(next(self._ids), t, _offer_text(), now)
        self._quiet_until = now + SHOWN_COOLDOWN_S
        return self._pending

    def _answer(self, offer_id: int, now: float) -> Offer:
        p = self.pending(now)
        if p is None or p.id != offer_id:
            raise KeyError(offer_id)
        self._pending = None
        return p

    def accept(self, offer_id: int, now: float) -> Offer:
        return self._answer(offer_id, now)

    def decline(self, offer_id: int, now: float) -> Offer:
        offer = self._answer(offer_id, now)
        self._quiet_until = max(self._quiet_until, now + DECLINED_COOLDOWN_S)
        return offer

    def loggable(self, scene, now: float) -> str | None:
        """Whiteboards, cards and menus are remembered, not offered on. At most once per 10 minutes each."""
        clean = self._clean(scene)
        if clean is None or clean[1] < MIN_CONF or clean[0] not in LOG_TYPES:
            return None
        t = clean[0]
        if now - self._logged.get(t, -LOG_COOLDOWN_S) < LOG_COOLDOWN_S:
            return None
        self._logged[t] = now
        return f"Looking at {LOG_TYPES[t]}"
