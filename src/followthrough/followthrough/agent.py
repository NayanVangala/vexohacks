"""The Follow-Through agent. One entry point:

    items = run(segments)

It asks the model for commitments, checks every quote against the transcript in plain code,
and returns Items. Items are only ever "pending" (ready for you to approve) or "needs_review".
Nothing here sends or schedules anything; that happens in executor.py after you approve."""
import hashlib
from datetime import datetime
from typing import Optional
from .config import Config, get_config
from .models import Item, Segment
from .verify import find_quote, norm

TIME_FORMAT = "%Y-%m-%dT%H:%M"


def _valid_time(s: str) -> str:
    try:
        datetime.strptime(s, TIME_FORMAT)
        return s
    except (ValueError, TypeError):
        return ""


def make_item(raw: dict, segments: list[Segment], cfg: Config, demo: bool) -> Item:
    """Turn one raw model commitment into an Item, checking the quote and the required fields."""
    quote = (raw.get("quote") or "").strip()
    proof, issue = find_quote(quote, segments, raw.get("segment_index"))
    issues = [issue] if issue else []
    action = raw["action"]
    due = _valid_time(raw.get("due_local", ""))
    if action == "calendar" and not due:
        issues.append("no valid time, a calendar event needs one")
    if action == "email" and not (raw.get("body") or "").strip():
        issues.append("email has no body")
    if action == "reply" and not (raw.get("body") or "").strip():
        issues.append("reply has no text")
    return Item(
        id=hashlib.sha1((norm(quote) + action).encode()).hexdigest()[:10],
        action=action, title=raw.get("title", "").strip() or "Untitled",
        owner=raw.get("owner", ""), counterpart=raw.get("counterpart", ""),
        quote=quote, proof=proof, due_local=due,
        duration_min=raw.get("duration_min") or 30, changed_from=raw.get("changed_from", ""),
        to=cfg.email_to if action == "email" else "",   # recipient is fixed, whatever the model says
        subject=raw.get("subject", ""), body=raw.get("body", ""),
        status="needs_review" if issues else "pending", issues=issues, demo=demo,
    )


def run(segments: list[Segment], llm=None, cfg: Optional[Config] = None) -> list[Item]:
    """Find commitments in the transcript. llm is anything with .extract(segments) -> list[dict]."""
    cfg = cfg or get_config()
    if llm is None:
        from .llm import OpenAILLM
        llm = OpenAILLM(cfg)
    raws = llm.extract(segments)
    demo = getattr(llm, "name", "") == "demo"
    items = [make_item(r, segments, cfg, demo) for r in raws]
    return sorted(items, key=lambda i: (i.proof.first_segment if i.proof.first_segment is not None else 10**6))
