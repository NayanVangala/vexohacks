"""Wake word + spoken answers.

"hey vexo, where are my keys" -> question -> memory.ask -> spoken on the laptop.
Rides on transcripts memory already makes, so it costs a regex per chunk, no second speech model.
The answer plays from the LAPTOP: no HFP duplex fight on the phone, audience can't tell.
ElevenLabs when ELEVENLABS_API_KEY is set, macOS `say` otherwise (a real path, not a stub).
"""

import os
import re
import subprocess
import tempfile
import time

import httpx

# Whisper doesn't know "Vexo" and guesses; these are the guesses. Override with VEXO_WAKE_REGEX.
WAKE = re.compile(os.environ.get(
    "VEXO_WAKE_REGEX", r"\b(?:hey|hi|ok|okay)[\s,.!]+(?:vexo|vexa|vexxo|vex oh|vecso|vesso|bexo|lexo|texo)\b"), re.I)
WINDOW_S = float(os.environ.get("VEXO_WAKE_WINDOW_S", "6"))      # keep listening this long for the question
MIN_QUESTION_CHARS = int(os.environ.get("VEXO_WAKE_MIN_CHARS", "12"))
COOLDOWN_S = float(os.environ.get("VEXO_WAKE_COOLDOWN_S", "12"))  # glasses mic hears the laptop answer
ELEVEN_VOICE = os.environ.get("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")
ELEVEN_MODEL = os.environ.get("ELEVENLABS_MODEL", "eleven_turbo_v2_5")

# Whisper invents these on silence. ponytail: fixed list; widen it from real logs if more show up.
HALLUCINATIONS = {"thank you", "thanks for watching", "you", "bye", "thank you for watching", "okay"}

_pending: str | None = None
_until = 0.0
_cooldown_until = 0.0


def is_noise(text: str) -> bool:
    return re.sub(r"[^a-z ]", "", text.lower()).strip() in HALLUCINATIONS


def heard(text: str, now: float | None = None) -> str | None:
    """Feed one chunk's transcript. Returns a full question once one is complete, else None."""
    global _pending, _until, _cooldown_until
    now = time.time() if now is None else now
    if now < _cooldown_until:
        return None
    m = WAKE.search(text)
    if m:
        _pending, _until = text[m.end():].strip(" ,.!"), now + WINDOW_S
    elif _pending is not None and now <= _until:
        _pending = f"{_pending} {text}".strip()
    else:
        _pending = None
        return None
    if len(_pending) >= MIN_QUESTION_CHARS:
        q, _pending, _cooldown_until = _pending, None, now + COOLDOWN_S
        return q
    return None


def listening() -> bool:
    """True while a wake word was heard and the question is still arriving."""
    return _pending is not None


def speak(text: str) -> None:
    """Fire and forget: never blocks the audio lane."""
    key = os.environ.get("ELEVENLABS_API_KEY", "")
    if key and key != "REPLACE_ME":
        try:
            r = httpx.post(f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE}",
                           headers={"xi-api-key": key}, timeout=15,
                           json={"text": text, "model_id": ELEVEN_MODEL})
            r.raise_for_status()
            f = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
            f.write(r.content)
            f.close()
            subprocess.Popen(["afplay", f.name])
            return
        except Exception as e:
            print(f"voice: ElevenLabs failed ({e!r}), falling back to say", flush=True)
    subprocess.Popen(["say", text])
