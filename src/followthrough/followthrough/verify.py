"""Quote check. The model proposes a quote; this code decides whether it really appears in the transcript.
The model never gets to mark its own work as verified."""
import re
from typing import Optional
from .models import Proof, Segment

MIN_WORDS = 3  # a one word quote like "yes" would match almost anything


def norm(s: str) -> str:
    """Lowercase, straighten quotes, drop punctuation, collapse spaces. Used on both sides of the match."""
    s = s.lower().replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = re.sub(r"[^a-z0-9' ]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def find_quote(quote: str, segments: list[Segment], hint: Optional[int] = None) -> tuple[Proof, Optional[str]]:
    """Return (proof, issue). The quote must appear word for word inside one segment,
    or across up to 3 neighbouring segments (people get split mid sentence by the transcriber)."""
    q = norm(quote)
    if len(q.split()) < MIN_WORDS:
        return Proof(verified=False), "quote is too short to count as proof"
    cleaned = norm(re.sub(r"^\s*[A-Za-z][\w ]{0,20}:\s*", "", quote))  # model sometimes prefixes "Nayan:"
    candidates = [q] if cleaned == q else [q, cleaned]
    normed = [norm(s.text) for s in segments]
    order = list(range(len(segments)))
    if hint is not None and 0 <= hint < len(segments):
        order.remove(hint)
        order.insert(0, hint)  # look where the model said it was first
    # Try the tightest match first: every single segment, then pairs, then triples.
    # (Looping start-first would report a wider span than the quote really covers.)
    for size in (1, 2, 3):
        for i in order:
            j = i + size - 1
            if j >= len(segments):
                continue
            joined = " ".join(normed[i:j + 1])
            if any(c and c in joined for c in candidates):
                return Proof(verified=True, first_segment=i, last_segment=j,
                             start=segments[i].start, end=segments[j].end), None
    return Proof(verified=False), "quote not found in the transcript"
