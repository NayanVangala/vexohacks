"""Holds the transcript, the items and the to-do list. Saved to data/state.json so a restart keeps them."""
import json
import os
import re
import threading
from .models import Item, Segment


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

    @property
    def clock(self) -> float:
        """End time of the last segment. New audio chunks are placed after it."""
        ends = [s.end for s in self.segments if s.end is not None]
        return max(ends) if ends else 0.0

    def append(self, segs: list[Segment]):
        with self.lock:
            self.segments += segs
            self.save()

    def replace(self, segs: list[Segment], demo: bool):
        with self.lock:
            self.segments, self.items, self.demo = list(segs), {}, demo
            self.save()

    def merge(self, new_items: list[Item]):
        """Add new items. An item we already have (same id) keeps its status, so re-running never undoes an approval."""
        with self.lock:
            for it in new_items:
                if it.id not in self.items:
                    self.items[it.id] = it
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
