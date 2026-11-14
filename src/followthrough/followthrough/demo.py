"""A staged conversation and a fake model, for testing without an API key.
Everything produced from here is marked demo=True so the dashboard can label it."""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from .config import Config
from .models import Segment

DEMO_SEGMENTS = [
    Segment(start=0, end=4, speaker="Nayan", text="Did you catch the game last night?"),
    Segment(start=4, end=8, speaker="Srijan", text="Barely, I fell asleep in the third quarter."),
    Segment(start=8, end=14, speaker="Nayan", text="Ha. Anyway, can you send me the deck by three today?"),
    Segment(start=14, end=18, speaker="Srijan", text="Yeah, I'll send you the deck by three."),
    Segment(start=18, end=23, speaker="Nayan", text="Also, let's sync on the demo tomorrow at ten."),
    Segment(start=23, end=27, speaker="Srijan", text="Works for me, tomorrow at ten for thirty minutes."),
    Segment(start=27, end=31, speaker="Nayan", text="Actually make the deck deadline four, I need to review something first."),
    Segment(start=31, end=35, speaker="Srijan", text="No problem, four o'clock then."),
    Segment(start=35, end=40, speaker="Nayan", text="Oh and this coffee is terrible, by the way."),
    Segment(start=40, end=46, speaker="Srijan", text="I'll also call the venue about parking and tell you what they say."),
]


class FakeLLM:
    """Returns canned output for DEMO_SEGMENTS. The last item has a made up quote on purpose,
    to prove the quote check catches a model that invents things."""
    name = "demo"

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def extract(self, segments: list[Segment]) -> list[dict]:
        now = datetime.now(ZoneInfo(self.cfg.timezone))
        today4 = now.replace(hour=16, minute=0, second=0, microsecond=0)
        tmr10 = (now + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
        f = "%Y-%m-%dT%H:%M"
        return [
            {"action": "email", "title": "Send Nayan the deck", "summary": "Email Nayan the deck before the demo.", "owner": "Srijan", "counterpart": "Nayan",
             "quote": "I'll send you the deck by three", "segment_index": 3, "due_local": today4.strftime(f),
             "duration_min": 30, "changed_from": "3:00 PM",
             "subject": "The deck",
             "body": "Hi Nayan, the deck is on its way. You asked for it by four, and I will get it to you before then."},
            {"action": "calendar", "title": "Demo sync with Nayan", "summary": "Thirty minute sync with Nayan to run through the demo.", "owner": "Srijan", "counterpart": "Nayan",
             "quote": "tomorrow at ten for thirty minutes", "segment_index": 5, "due_local": tmr10.strftime(f),
             "duration_min": 30, "changed_from": "", "subject": "", "body": ""},
            {"action": "todo", "title": "Call the venue about parking", "summary": "Ask the venue where the team can park.", "owner": "Srijan", "counterpart": "Nayan",
             "quote": "I'll also call the venue about parking", "segment_index": 9, "due_local": "",
             "duration_min": 30, "changed_from": "", "subject": "", "body": ""},
            {"action": "reply", "title": "Buy Nayan coffee next time", "summary": "Reply to Nayan about covering the next coffee.", "owner": "Srijan", "counterpart": "Nayan",
             "quote": "I promise to buy the coffee next time", "segment_index": 8, "due_local": "",
             "duration_min": 30, "changed_from": "", "subject": "",
             "body": "Next coffee is on me."},
        ]
