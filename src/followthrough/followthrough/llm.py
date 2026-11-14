"""The model call. Only this file knows about OpenAI. Swap it out and nothing else changes."""
import json
from datetime import datetime
from zoneinfo import ZoneInfo
from .config import Config
from .models import Segment

# Strict structured output: every field is required, so "not applicable" is an empty string.
SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["commitments"],
    "properties": {"commitments": {"type": "array", "items": {
        "type": "object",
        "additionalProperties": False,
        "required": ["action", "title", "summary", "owner", "counterpart", "quote", "segment_index", "due_local",
                     "duration_min", "changed_from", "subject", "body"],
        "properties": {
            "action": {"type": "string", "enum": ["calendar", "email", "reply", "todo"]},
            "title": {"type": "string"},
            "summary": {"type": "string"},
            "owner": {"type": "string"},
            "counterpart": {"type": "string"},
            "quote": {"type": "string"},
            "segment_index": {"type": "integer"},
            "due_local": {"type": "string"},
            "duration_min": {"type": "integer"},
            "changed_from": {"type": "string"},
            "subject": {"type": "string"},
            "body": {"type": "string"},
        }}}},
}


def fmt_time(t):
    if t is None:
        return "--:--"
    return f"{int(t) // 60:02d}:{int(t) % 60:02d}"


def numbered_transcript(segments: list[Segment]) -> str:
    """One line per segment: [3] (00:14-00:18) Speaker: text"""
    return "\n".join(f"[{i}] ({fmt_time(s.start)}-{fmt_time(s.end)}) {s.speaker + ': ' if s.speaker else ''}{s.text}"
                     for i, s in enumerate(segments))


def build_prompt(cfg: Config) -> str:
    now = datetime.now(ZoneInfo(cfg.timezone))
    return f"""You are Follow-Through, an assistant for {cfg.wearer}. You read a conversation transcript and find real commitments.
Current local date and time: {now.strftime('%A %Y-%m-%d %H:%M')} ({cfg.timezone}).

A commitment is something someone agreed to DO (send, write, call, book, finish, review, buy) or a meeting with a time.
Ignore small talk, opinions, jokes, and facts. If unsure, leave it out. Never invent commitments.

Rules:
- quote: copy the exact words from ONE transcript line, word for word. Do not paraphrase or combine lines. segment_index is that line's number.
- If a deadline changes later, use the LATEST one in due_local and put the old one in changed_from.
- due_local is YYYY-MM-DDTHH:MM in {cfg.timezone}. Resolve words like "tomorrow" from the current date above. If a day is given with no hour, use 17:00. If no time at all, use an empty string.
- action: "calendar" for meetings or time blocks, "email" when something must be sent to a person, "reply" for a short message back to someone, "todo" for anything else.
- email and reply: write subject (email only, else empty) and body, 2 to 4 plain sentences from {cfg.wearer}. For calendar and todo leave subject and body empty.
- title: 3 to 7 words that say exactly what this is for, with the person or topic. Meeting: "Demo sync with Nayan". Email: "Send Nayan the deck". To-do: "Call the venue about parking". Never use generic titles like "Meeting", "Event", "Follow up" or "Untitled".
- summary: one plain sentence under 20 words saying what this is for and who it involves. No quotes.
- owner is who must do it. counterpart is who it is for.
- duration_min: 30 unless the conversation says otherwise."""


class OpenAILLM:
    """Calls the OpenAI chat API with a JSON schema. Reads OPENAI_API_KEY through the config."""
    name = "openai"

    def __init__(self, cfg: Config, client=None):
        self.cfg = cfg
        if client is None:
            from openai import OpenAI
            client = OpenAI(api_key=cfg.openai_api_key)
        self.client = client

    def extract(self, segments: list[Segment]) -> list[dict]:
        resp = self.client.chat.completions.create(
            model=self.cfg.model,
            messages=[{"role": "system", "content": build_prompt(self.cfg)},
                      {"role": "user", "content": "Transcript:\n" + numbered_transcript(segments)}],
            response_format={"type": "json_schema",
                             "json_schema": {"name": "commitments", "strict": True, "schema": SCHEMA}},
        )
        msg = resp.choices[0].message
        if getattr(msg, "refusal", None):
            raise RuntimeError("model refused: " + msg.refusal)
        return json.loads(msg.content)["commitments"]
