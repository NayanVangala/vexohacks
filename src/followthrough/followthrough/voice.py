"""Voice approval. Three small jobs, none of which approve anything by themselves:

  question_for(item)  -> the sentence that is spoken ("Want me to put this on your calendar?")
  speak(text, cfg)    -> mp3 bytes from ElevenLabs
  parse_answer(text)  -> "yes", "no" or "unclear" from what the person said

Approving still goes through the same server route as the Approve button, one item at a time.
When the answer is not clearly yes, nothing runs."""
import re
from datetime import datetime
import httpx
from .config import Config
from .models import Item

_cache: dict[tuple, bytes] = {}   # the same question is not paid for twice


def _when(due_local: str) -> str:
    try:
        d = datetime.strptime(due_local, "%Y-%m-%dT%H:%M")
    except ValueError:
        return ""
    return d.strftime("%A at %-I:%M %p").replace(":00 ", " ")


def question_for(item: Item, cfg: Config, n: int = 0, total: int = 1) -> str:
    """Casual, like a friend. n is this item's place in the queue (0 first), so back to back questions
    flow on from each other instead of each one starting with a greeting. No instructions to say yes or no."""
    first = cfg.wearer.split()[0] if cfg.wearer and cfg.wearer != "the wearer" else ""
    who = item.counterpart or "them"
    when = _when(item.due_local)
    if n == 0:
        hey = f"Hey {first}," if first else "Hey,"
        lead = f"{hey} got a few for you. First up:" if total > 1 else hey
    elif n == total - 1:
        lead = "Last one, promise."
    else:
        lead = ["Oh, and by the way,", "Okay, next up.", "Also,", "Aaand another one."][(n - 1) % 4]
    if item.action == "calendar":
        said = f"{item.title} came up."
        ask = [f"Do I have the green light to block off {when + ' ' if when else 'time '}on your calendar?",
               f"Want me to lock {when or 'that in'} on your calendar?"][n % 2]
    else:
        said = f"you said you'd {item.title[:1].lower() + item.title[1:]}."
        ask = {
            "email": [f"Green light to shoot {who} an email about it?", f"Want me to fire off an email to {who}?"][n % 2],
            "reply": [f"Want me to draft {who} a reply, or are we ghosting?", f"Should I whip up a reply to {who}?"][n % 2],
            "todo": ["Should I toss it on your to-do list before it falls out of your head?", "Want me to put it on your to-do list?"][n % 2],
        }[item.action]
    return f"{lead} {said[:1].upper() + said[1:] if lead.endswith('.') else said} {ask}"


def speak(text: str, cfg: Config, client: httpx.Client | None = None) -> bytes:
    """Text to speech with ElevenLabs. Raises RuntimeError with a plain message when it cannot."""
    if not cfg.eleven_key:
        raise RuntimeError("ELEVENLABS_API_KEY is not set")
    key = (text, cfg.eleven_voice, cfg.eleven_model)
    if key in _cache:
        return _cache[key]
    own = client is None
    client = client or httpx.Client(timeout=30)
    try:
        r = client.post(f"https://api.elevenlabs.io/v1/text-to-speech/{cfg.eleven_voice}",
                        params={"output_format": "mp3_44100_128"},
                        headers={"xi-api-key": cfg.eleven_key, "Content-Type": "application/json"},
                        json={"text": text, "model_id": cfg.eleven_model})
    finally:
        if own:
            client.close()
    if r.status_code != 200:
        raise RuntimeError(f"ElevenLabs said {r.status_code}: {r.text[:200]}")
    _cache[key] = r.content
    return r.content


YES = ["yes", "yeah", "yep", "yup", "sure", "ok", "okay", "approve", "approved", "confirm", "please do",
       "go ahead", "do it", "sounds good", "absolutely", "definitely"]
NO = ["no", "nope", "nah", "don't", "dont", "do not", "cancel", "skip", "dismiss", "stop", "never mind",
      "nevermind", "negative", "not now"]


def _has(words: list[str], text: str) -> bool:
    return any(re.search(r"(?<![a-z'])" + re.escape(w) + r"(?![a-z'])", text) for w in words)


def parse_answer(heard: str) -> str:
    """Only a clear yes returns "yes". Both, or neither, is "unclear", so a mumble never approves."""
    text = re.sub(r"[^a-z' ]", " ", (heard or "").lower())
    yes, no = _has(YES, text), _has(NO, text)
    if yes and no:
        return "unclear"
    return "yes" if yes else "no" if no else "unclear"
