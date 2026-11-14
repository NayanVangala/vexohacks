"""Data shapes shared by the agent, the server and the dashboard."""
from typing import Literal, Optional
from pydantic import BaseModel, Field

Action = Literal["calendar", "email", "reply", "todo"]
# pending      verified and waiting for you to approve
# needs_review something is wrong (quote not found, no time, ...). Cannot be approved until fixed.
# done         executed for real and the executor confirmed it
# drafted      no executor configured; only a draft or link exists. Nothing was sent.
# failed       executor said no or could not be reached
# dismissed    you rejected it
Status = Literal["pending", "needs_review", "done", "drafted", "failed", "dismissed"]


class Segment(BaseModel):
    """One line of the transcript. start and end are seconds from the start of the recording, or None."""
    start: Optional[float] = None
    end: Optional[float] = None
    speaker: str = ""
    text: str


class Proof(BaseModel):
    """Where the exact quote was found in the transcript."""
    verified: bool
    first_segment: Optional[int] = None
    last_segment: Optional[int] = None
    start: Optional[float] = None
    end: Optional[float] = None


class Item(BaseModel):
    id: str                       # stable hash of the quote, so re-running does not duplicate
    action: Action
    title: str
    owner: str = ""
    counterpart: str = ""
    quote: str                    # exact words from the transcript
    proof: Proof
    due_local: str = ""           # YYYY-MM-DDTHH:MM in the configured time zone, or ""
    duration_min: int = 30
    changed_from: str = ""        # earlier deadline if it was changed later in the conversation
    to: str = ""                  # email recipient (always the demo override)
    subject: str = ""
    body: str = ""                # email body or reply text
    status: Status
    issues: list[str] = Field(default_factory=list)   # reasons for needs_review
    result: str = ""              # what really happened, in plain words
    link: str = ""                # draft link when nothing was executed
    demo: bool = False            # True when the data came from the staged demo, not the model
