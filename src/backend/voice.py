"""Wake word + spoken answers.

"hey scene, where are my keys" -> question -> memory.ask -> spoken on the laptop.
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

# Whisper hears "scene" a few ways; these are the guesses. Override with SCENE_WAKE_REGEX.
WAKE = re.compile(os.environ.get(
    "SCENE_WAKE_REGEX", r"\b(?:hey|hi|ok|okay)[\s,.!]+(?:scene|scenes|seen|sean|shean|sene|scen)\b"), re.I)
WINDOW_S = float(os.environ.get("SCENE_WAKE_WINDOW_S", "6"))      # keep listening this long for the question
MIN_QUESTION_CHARS = int(os.environ.get("SCENE_WAKE_MIN_CHARS", "12"))
COOLDOWN_S = float(os.environ.get("SCENE_WAKE_COOLDOWN_S", "12"))  # glasses mic hears the laptop answer
ELEVEN_VOICE = os.environ.get("ELEVENLABS_VOICE_ID", "21m00Tcm4TlvDq8ikWAM")
ELEVEN_MODEL = os.environ.get("ELEVENLABS_MODEL", "eleven_turbo_v2_5")

# Whisper invents these on silence. ponytail: fixed list; widen it from real logs if more show up.
HALLUCINATIONS = {"thank you", "thanks for watching", "you", "bye", "thank you for watching", "okay"}
# Phrases Whisper invents on near-silence (YouTube outros). Seen live: "Share this video with your
# friends on social media" became a task. Any chunk containing one of these is dropped.
PHANTOM_PHRASES = ("share this video", "like and subscribe", "subscribe to", "see you in the next video",
                   "thanks for watching", "thank you for watching", "please subscribe", "don't forget to subscribe")

# Below this RMS (16-bit samples) a chunk is room tone, not speech: don't transcribe it at all.
# Measured on the iPhone mic: room 12-25, speech in the hundreds. Override with SCENE_SILENCE_RMS.
SILENCE_RMS = float(os.environ.get("SCENE_SILENCE_RMS", "45"))

_pending: str | None = None
_until = 0.0
_cooldown_until = 0.0


def is_noise(text: str) -> bool:
    t = re.sub(r"[^a-z' ]", "", text.lower()).strip()
    return t.replace("'", "") in HALLUCINATIONS or any(p in t for p in PHANTOM_PHRASES)


def is_silent(wav: bytes) -> bool:
    """RMS of a 16-bit PCM WAV (header skipped). Silent chunks never reach Whisper."""
    import array
    import math
    a = array.array("h", wav[44:len(wav) - (len(wav) - 44) % 2])
    return bool(a) and math.sqrt(sum(x * x for x in a) / len(a)) < SILENCE_RMS   # no samples: can't tell, keep it


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


# ---------- how Scene talks to you: "cards", "voice", or "both" (chosen on the glasses) ----------
OUTPUT_MODES = ("cards", "voice", "both")


def _settings_path():
    from pathlib import Path
    return Path(os.environ.get("SCENE_DATA_DIR", Path(__file__).parent / "data")) / "settings.json"


def output_mode(default: str | None = "both") -> str | None:
    """The saved choice, or `default` if the wearer hasn't picked one yet."""
    import json
    try:
        m = json.loads(_settings_path().read_text()).get("output")
        return m if m in OUTPUT_MODES else default
    except (OSError, ValueError):
        return default


def set_output_mode(mode: str) -> None:
    import json
    if mode not in OUTPUT_MODES:
        raise ValueError(f"output must be one of {OUTPUT_MODES}")
    p = _settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"output": mode}))


def speaks() -> bool:
    return output_mode() in ("voice", "both")


# Where speech plays. "glasses" (default): queued for the phone, which plays it and iOS routes it to
# the glasses' speakers over Bluetooth. "laptop": the laptop's speakers. "both": either way.
SPEAK_ON = os.environ.get("SCENE_SPEAK_ON", "glasses")

from collections import deque as _deque  # noqa: E402
import itertools as _itertools           # noqa: E402

_utterances: _deque = _deque(maxlen=30)
_utterance_ids = _itertools.count(1)
_audio_cache: dict[int, tuple[bytes, str]] = {}


def speak(text: str) -> None:
    """Fire and forget: never blocks the audio lane. Silent when the wearer chose cards only."""
    if not speaks():
        return
    if SPEAK_ON in ("glasses", "both"):
        _utterances.append({"id": next(_utterance_ids), "text": text, "ts": time.time()})
    if SPEAK_ON in ("laptop", "both"):
        _play_on_laptop(text)


def utterances_after(n: int) -> list[dict]:
    """What the phone hasn't played yet (it polls with the last id it saw)."""
    return [u for u in list(_utterances) if u["id"] > n]


def audio_for(utterance_id: int) -> tuple[bytes, str] | None:
    """ElevenLabs MP3 for one queued line (macOS `say` AIFF without a key). Cached: the phone may retry."""
    if utterance_id in _audio_cache:
        return _audio_cache[utterance_id]
    u = next((u for u in list(_utterances) if u["id"] == utterance_id), None)
    if u is None:
        return None
    out = synthesize(u["text"])
    _audio_cache[utterance_id] = out
    for old in [k for k in _audio_cache if k < utterance_id - 30]:
        _audio_cache.pop(old, None)
    return out


def synthesize(text: str) -> tuple[bytes, str]:
    key = os.environ.get("ELEVENLABS_API_KEY", "")
    if key and key != "REPLACE_ME":
        try:
            r = httpx.post(f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE}",
                           headers={"xi-api-key": key}, timeout=15,
                           json={"text": text, "model_id": ELEVEN_MODEL})
            r.raise_for_status()
            return r.content, "audio/mpeg"
        except Exception as e:
            print(f"voice: ElevenLabs failed ({e!r}), falling back to say", flush=True)
    with tempfile.NamedTemporaryFile(suffix=".aiff", delete=False) as f:
        path = f.name
    subprocess.run(["say", "-o", path, text], check=True, timeout=20)
    with open(path, "rb") as f:
        data = f.read()
    os.unlink(path)
    return data, "audio/aiff"


def _play_on_laptop(text: str) -> None:
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
