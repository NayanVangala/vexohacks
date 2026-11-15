"""Holds the transcript, the items and the to-do list. Saved to data/state.json so a restart keeps them."""
import json
import os
import re
import threading
from .models import Item, Segment
from .verify import norm


def _same_commitment(a: Item, b: Item) -> bool:
    """True when two items are the same promise mined twice. An item's id is
    a hash of its quote, so the model returning a slightly different quote
    slice on the next /api/run (longer, shorter, different start) mints a
    different id for the same commitment -- that is how one "book a
    reservation" became 4 items and one "hang out at 10 p.m." became 5.
    Only exact duplicates merge: either the very same quote (whatever action
    the model picked for it -- the same ask mined once as an email and once
    as a todo is still one commitment), or, for the same action, one quote
    containing the other (a truncation), or the same title + person + time
    with >= 70% of the shorter quote's content words in common. Two
    genuinely different items that happen to share words (different title,
    person or time) never merge."""
    qa, qb = norm(a.quote or ""), norm(b.quote or "")
    if qa and qa == qb:
        return True
    # One quote containing the other is the same commitment whatever action
    # the model picked (a calendar draft and a todo of the same ask are one
    # ask). Strip a leading "hey scene" first: it is on one truncation of
    # the same ramble and not on the other.
    _greet = re.compile(r"^(hey|hi|ok|okay)[\s,.]+scene[\s,.]+")
    ga, gb = _greet.sub("", qa), _greet.sub("", qb)
    if len(ga) >= 20 and len(gb) >= 20 and (ga in gb or gb in ga):
        return True
    if a.action != b.action:
        return False
    if (a.due_local and a.due_local == b.due_local
            and norm(a.title or "") and norm(a.title or "") == norm(b.title or "")
            and norm(a.counterpart or "") == norm(b.counterpart or "")):
        # Content words only: greetings and fillers ("hey scene", "um") make
        # a short quote look different from the long ramble it came from.
        # "Hey Scene! Can you book a reservation?" and the same request buried
        # in "I don't know..." are the same commitment when title, person
        # and time all match and >= 70% of the shorter quote's content words
        # appear in the longer one.
        _stop = {"hey", "scene", "hi", "please", "um", "uh", "yeah", "ok", "okay"}
        wa = {w for w in qa.split() if w not in _stop}
        wb = {w for w in qb.split() if w not in _stop}
        if wa and wb and len(wa & wb) / min(len(wa), len(wb)) >= 0.70:
            return True
    return False


# When duplicates already exist, keep the most final one: a done/drafted item
# is handled, a dismissed one was rejected -- either beats a pending copy of
# the same promise. Earliest transcript position wins ties.
_STATUS_KEEP = {"done": 0, "drafted": 1, "dismissed": 2, "pending": 3, "needs_review": 4}


def parse_text(text: str) -> list[Segment]:
    """Plain text to segments. A line like 'Nayan: hello' becomes speaker Nayan, text hello. No times."""
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^([A-Za-z][\w .'-]{0,24}):\s+(.*)$", line)
        out.append(Segment(speaker=m.group(1), text=m.group(2)) if m else Segment(text=line))
    return out


class Store:
    def __init__(self, data_dir: str = "data"):
        self.path = os.path.join(data_dir, "state.json")
        self.lock = threading.RLock()
        self.segments: list[Segment] = []
        self.items: dict[str, Item] = {}
        self.todos: list[str] = []
        self.demo = False          # True while the staged demo transcript is loaded
        self._load()
        # Clean up duplicates minted before the merge dedup existed (the live
        # state had e.g. 5 copies of one "hang out at 10 p.m." promise).
        dropped = self._dedupe_items()
        if dropped:
            print(f"followthrough: deduped {dropped} duplicate items on load", flush=True)
            self.save()

    @property
    def clock(self) -> float:
        """End time of the last segment. New audio chunks are placed after it."""
        ends = [s.end for s in self.segments if s.end is not None]
        return max(ends) if ends else 0.0

    def append(self, segs: list[Segment]):
        """Append new transcript segments, dropping re-posts. The backend used
        to forward the whole rolling transcript on every chunk, so the same
        sentence arrived as 13 growing cumulative copies; a run then mined
        the same promise out of every copy. Exact long lines already present
        are skipped, and when one segment contains another (>= 80 chars) only
        the longest version is kept. Short lines repeat constantly in real
        speech, so they are never deduped here."""
        with self.lock:
            for s in segs:
                t = norm(s.text or "")
                if not t:
                    continue
                if len(t) >= 40 and any(norm(x.text or "") == t for x in self.segments):
                    continue
                replaced = False
                if len(t) >= 40:
                    for x in self.segments:
                        xt = norm(x.text or "")
                        if len(xt) >= 40 and xt != t and (xt in t or t in xt):
                            if len(t) > len(xt):
                                self.segments[self.segments.index(x)] = s
                            replaced = True
                            break
                if not replaced:
                    self.segments.append(s)
            self.save()

    def _dedupe_items(self) -> int:
        """Collapse near-duplicate items already in the store into one per
        commitment, keeping the most final status. Returns how many dropped."""
        items = sorted(self.items.values(),
                       key=lambda i: (i.proof.first_segment if i.proof.first_segment is not None else 10 ** 6, i.id))
        keep: list[Item] = []
        dropped = 0
        for it in items:
            dup = next((k for k in keep if _same_commitment(it, k)), None)
            if dup is None:
                keep.append(it)
                continue
            dropped += 1
            # Same status: keep the actionable version (email/calendar/reply)
            # over a plain todo of the same ask.
            def _keep_key(i: Item) -> tuple[int, int]:
                return (_STATUS_KEEP[i.status], 0 if i.action != "todo" else 1)
            if _keep_key(it) < _keep_key(dup):
                keep[keep.index(dup)] = it
        if dropped:
            self.items = {i.id: i for i in keep}
        return dropped

    def replace(self, segs: list[Segment], demo: bool):
        with self.lock:
            self.segments, self.items, self.demo = list(segs), {}, demo
            self.save()

    def merge(self, new_items: list[Item]):
        """Add new items. An item we already have (same id) keeps its status,
        so re-running never undoes an approval. A new item that is the same
        commitment under a different quote slice (see _same_commitment) is
        dropped instead of becoming a second row."""
        with self.lock:
            for it in new_items:
                if it.id in self.items:
                    continue
                if any(_same_commitment(it, old) for old in self.items.values()):
                    continue
                self.items[it.id] = it
            self._dedupe_items()
            self.save()

    def ordered(self) -> list[Item]:
        key = lambda i: i.proof.first_segment if i.proof.first_segment is not None else 10**6
        return sorted(self.items.values(), key=key)

    def save(self):
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        with open(self.path, "w") as f:
            json.dump({"segments": [s.model_dump() for s in self.segments],
                       "items": [i.model_dump() for i in self.items.values()],
                       "todos": self.todos, "demo": self.demo}, f)

    def _load(self):
        if not os.path.exists(self.path):
            return
        try:
            d = json.load(open(self.path))
            self.segments = [Segment(**s) for s in d["segments"]]
            self.items = {i["id"]: Item(**i) for i in d["items"]}
            self.todos, self.demo = d.get("todos", []), d.get("demo", False)
        except Exception:
            pass  # a damaged file just means we start empty
