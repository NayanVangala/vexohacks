"""Wake word + spoken answers.

"hey scene, where is my wallet" -> question -> memory.ask -> spoken on the laptop.
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
COOLDOWN_S = float(os.environ.get("SCENE_WAKE_COOLDOWN_S", "4"))   # answers play in the glasses now, the mic barely hears them  # glasses mic hears the laptop answer
# Alternatives on the account: Sarah (EXAVITQu4vr4xnSDxMaL, reassuring), River (SAz9YHcvj6GT2YYXdXww, relaxed,
# neutral), Eric (cjVigY5qzO86Huf0OWal, smooth).
ELEVEN_VOICE = os.environ.get("ELEVENLABS_VOICE_ID", "cgSgspJ2msm6clMCkdW9")   # Jessica: bright, warm
ELEVEN_MODEL = os.environ.get("ELEVENLABS_MODEL", "eleven_turbo_v2_5")    # turbo, not multilingual_v2: same Jessica voice, roughly half the synthesis time on short answers (multilingual_v2 was 2-4 s of the ~10 s answer lag). Override with ELEVENLABS_MODEL.
# Conversational delivery: lower stability = less monotone, some style = expression. Not a copy of any product's voice.
ELEVEN_SETTINGS = {"stability": 0.4, "similarity_boost": 0.8, "style": 0.3, "use_speaker_boost": True}

# The transcriber invents these on silence (Gemini now, Whisper before the 2026-11-14 switch;
# same filter gates both). Seen live in Follow-Through state.json (2026-11-14): whole
# transcripts of "Thank you so much for watching!" + Korean/Chinese subtitles, and "I don't know
# where my keys are / Where are my keys?" loops, all from room noise with nobody speaking.
# Fixed list; widen it from real logs if more show up. "okay"/"ok" are NOT here: a bare "okay" is
# also a real yes to a waiting question (awareness.parse_yes_no accepts it).
HALLUCINATIONS = {"thank you", "thanks for watching", "you", "bye", "thank you for watching"}
# Phrases Whisper invents on near-silence (YouTube outros). Seen live: "Share this video with your
# friends on social media" became a task. Any chunk containing one of these is dropped.
PHANTOM_PHRASES = ("share this video", "like and subscribe", "subscribe to", "see you in the next video",
                   "see you next time", "thanks for watching", "thank you for watching", "thank you so much for watching",
                   "thanks so much for watching", "please subscribe", "don't forget to subscribe",
                   "subscribe to my channel", "smash that like", "check out my channel",
                   "[music]", "[applause]", "[laughter]", "[musik]",
                   "subtitles by", "transcribed by", "captioned by", "auto-generated")
# A chunk made of ONLY these words is a YouTube-outro hallucination, never a real sentence.
OUTRO_WORDS = {"thank", "thanks", "you", "very", "much", "so", "for", "watching", "bye"}

# Below this RMS (16-bit samples) a chunk is room tone, not speech: don't transcribe it at all.
# Measured on the glasses mic at a table: true silence 0-5, speech 14-25 (peaks ~1000). Only drop true
# silence; the transcriber's inventions on quiet audio are caught by PHANTOM_PHRASES. Override with SCENE_SILENCE_RMS.
SILENCE_RMS = float(os.environ.get("SCENE_SILENCE_RMS", "6"))
# A no-wake-word question ("did I leave my wallet?") needs CLEAR speech, not just above-silence
# noise: its loudest 100 ms must clear this. Quiet mumbles never become questions. Override with
# SCENE_CLEAR_SPEECH_RMS.
CLEAR_SPEECH_RMS = float(os.environ.get("SCENE_CLEAR_SPEECH_RMS", "10"))
# 100 ms windows at 16 kHz: a short word inside 2.5 s of room tone still shows up in one window.
_WINDOW_SAMPLES = 1600

_pending: str | None = None
_until = 0.0
_cooldown_until = 0.0
_speaking_until = 0.0   # Scene's own voice is on the air until then: the mic hears it, we must not
_chime_at = 0.0         # last chime ping: a tone captured by the mic is not a transcript line
_last_spoken = ""       # exact text of Scene's last spoken answer (ElevenLabs / say)
_last_spoken_at = 0.0   # when it was queued: our own answer echoing back late still counts
_last_display_at = 0.0  # last /glasses/ or /settings hit (server.py): the display web app is open
_last_text = ""         # last accepted transcript, for exact-duplicate suppression
_last_text_at = 0.0
_laptop_proc = None   # the laptop's afplay/say process, if one is playing right now
_interrupt_seq = 0    # increments on every barge-in: the phone polls it to stop its own player


def is_noise(text: str) -> bool:
    t = re.sub(r"[^a-z' ]", "", text.lower()).strip()
    if not re.search(r"[a-z]{2}", t):          # emoji, CJK subtitles, punctuation: not English speech
        return True
    words = t.replace("'", "")
    # Whisper's favourite inventions on room noise: a chunk of nothing but outro words.
    if words.split() and set(words.split()) <= OUTRO_WORDS:
        return True
    return words in HALLUCINATIONS or any(p in t for p in PHANTOM_PHRASES)


def is_non_english(text: str) -> bool:
    """True when the transcript is mostly not English: CJK / Hangul / Cyrillic runs,
    which is what the transcriber invents as fake subtitles on room noise (seen live:
    Korean/Chinese subtitles on a silent chunk). A real English chunk with one foreign
    name in it stays well under the bar."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return False
    def _is_cjk(c: str) -> bool:
        o = ord(c)
        return (0x4E00 <= o <= 0x9FFF or 0x3040 <= o <= 0x30FF or 0xAC00 <= o <= 0xD7A3
                or 0x0400 <= o <= 0x04FF)
    return sum(1 for c in letters if _is_cjk(c)) / len(letters) > 0.2


def is_hallucination(text: str) -> bool:
    """One call for the whole validation layer: noise/outro, loops, or non-English.
    Callers log the specific reason separately; this is the single gate."""
    return is_noise(text) or is_repetitive(text) or is_non_english(text)


def is_near_duplicate(text: str, now: float | None = None) -> bool:
    """Same words as the previous chunk in a different order / with one word changed
    (the transcriber stuck on one hallucination, rewording it slightly chunk after
    chunk). Word-overlap >= 80% within 30 s, 3+ words. A real repeated question WITH
    a wake word is exempt: saying "hey scene, where is my wallet?" twice is real.
    Trade-off, on purpose: a genuinely different second task that shares 80% of
    its words with the previous chunk and has no wake word is also dropped -- say
    it again with "hey scene" and it goes through. Dropping a hallucination is
    worth more than that one repeat."""
    global _last_text, _last_text_at
    now = time.time() if now is None else now
    if WAKE.search(text):
        return False
    words = set(re.sub(r"[^a-z' ]", "", text.lower()).split())
    last = set(re.sub(r"[^a-z' ]", "", _last_text).split())
    if len(words) < 3 or not last:
        return False
    overlap = len(words & last) / max(len(words), len(last))
    return overlap >= 0.8 and now - _last_text_at <= 30


def is_repetitive(text: str) -> bool:
    """The transcriber loops one sentence on noise ("I don't know. I don't know. ..."). True when one
    clause of 2+ words repeats 3+ times in a single chunk, or any 3-5 word phrase repeats
    3+ times with no punctuation between the repeats: real speech in 2.5 s never does
    either, and the looped keys hallucinations in Follow-Through state.json are exactly
    this shape (a 3x "where are my keys" buried in a longer ramble). Two repeats pass."""
    from collections import Counter
    clauses = []
    for c in re.split(r"[.!?;,\n]+", text):
        c = re.sub(r"[^a-z ]", "", c.lower()).strip()
        if len(c.split()) >= 2:
            clauses.append(c)
    if len(clauses) >= 3 and Counter(clauses).most_common(1)[0][1] >= 3:
        return True
    # Same loop with no punctuation at all ("where are my keys where are my keys
    # where are my keys"): one clause, so the check above never fires. Catch it at
    # word level: any contiguous 3-5 word phrase occurring 3+ times in one chunk.
    # Real speech in 2.5 s never repeats a 3-word phrase three times.
    words = re.sub(r"[^a-z ]", "", text.lower()).split()
    for n in (3, 4, 5):
        if len(words) < 3 * n:
            continue
        phrases = [" ".join(words[i:i + n]) for i in range(len(words) - n + 1)]
        if phrases and Counter(phrases).most_common(1)[0][1] >= 3:
            return True
    return False


def is_duplicate(text: str, now: float | None = None) -> bool:
    """Exact repeat of the chunk just before it (the transcriber stuck on one hallucination for
    chunk after chunk). Short yes/no answers are exempt: "yes" twice can be two real answers."""
    global _last_text, _last_text_at
    now = time.time() if now is None else now
    t = re.sub(r"[^a-z' ]", "", text.lower()).strip()
    if len(t.split()) < 3:
        return False
    if t == _last_text and now - _last_text_at <= 30:
        _last_text_at = now
        return True
    _last_text, _last_text_at = t, now
    return False


# The Whisper prompt used to contain this exact example, and Whisper now invents it on noise
# (verbatim in Follow-Through state.json). A chunk that IS only this, with no wake word, is never
# treated as a question: the wearer really asking says "hey scene" first.
PRIMING_EXAMPLE = re.compile(r"^where are my keys[?.!]*$", re.I)


def is_priming_echo(text: str) -> bool:
    return bool(PRIMING_EXAMPLE.match(text.strip()))


def peak_window_rms(wav: bytes) -> float:
    """Loudest 100 ms window's RMS (16-bit PCM WAV, header skipped). 0 when there are no samples."""
    import array
    import math
    a = array.array("h", wav[44:len(wav) - (len(wav) - 44) % 2])
    if not a:
        return 0.0
    best = 0.0
    for i in range(0, len(a), _WINDOW_SAMPLES):
        seg = a[i:i + _WINDOW_SAMPLES]
        best = max(best, math.sqrt(sum(x * x for x in seg) / len(seg)))
    return best


def is_silent(wav: bytes) -> bool:
    """No 100 ms window clears SILENCE_RMS: room tone, not speech. Whole-chunk RMS used to dilute
    a short quiet word in 2.5 s of silence below the threshold and drop real speech; the loudest
    window is what matters. All-zero samples (the HFP mic failure) count as silent.
    No samples at all: can't tell, keep it."""
    import array
    a = array.array("h", wav[44:len(wav) - (len(wav) - 44) % 2])
    if not a:
        return False
    return peak_window_rms(wav) < SILENCE_RMS


def heard(text: str, now: float | None = None) -> str | None:
    """Feed one chunk's transcript. Returns a full question once one is complete, else None."""
    global _pending, _until, _cooldown_until
    now = time.time() if now is None else now
    m = WAKE.search(text)
    if m:
        # A fresh wake word cancels the post-answer cooldown: the old code swallowed it while the
        # chime still pinged, promising an answer that never came.
        _cooldown_until = 0.0
        _pending, _until = text[m.end():].strip(" ,.!"), now + WINDOW_S
    elif now < _cooldown_until:
        return None
    elif _pending is not None and now <= _until + 2.5:
        # One chunk of grace: a question whose words started inside the window
        # but whose 2.5 s chunk ended after it still joins the wake word.
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


def own_voice(now: float | None = None) -> bool:
    """True while Scene's own spoken answer is still on the air (see speak): the mic hears it."""
    now = time.time() if now is None else now
    return now < _speaking_until


def is_own_answer(text: str, now: float | None = None) -> bool:
    """True when this chunk IS Scene's last spoken answer, heard back through the
    glasses mic -- even after _speaking_until expired (phone playback can lag the
    estimate by seconds, and the chime-out queue delays it further). Compares word
    overlap >= 80% with the last thing Scene said, within 60 s of it being queued.
    A real person repeating Scene's words back is indistinguishable from the echo,
    and dropping that one chunk is the safe side: the next chunk still lands."""
    now = time.time() if now is None else now
    if not _last_spoken or now - _last_spoken_at > 60:
        return False
    words = set(re.sub(r"[^a-z' ]", "", text.lower()).split())
    spoken = set(re.sub(r"[^a-z' ]", "", _last_spoken.lower()).split())
    if len(words) < 3 or not spoken:
        return False
    return len(words & spoken) / max(len(words), len(spoken)) >= 0.8


def just_chimed(now: float | None = None, window_s: float = 1.5) -> bool:
    """True just after the wake chime pinged: a tone the mic caught is not a transcript line."""
    now = time.time() if now is None else now
    return 0 <= now - _chime_at < window_s


def note_local_chime(now: float | None = None) -> None:
    """The phone played the fast local chime on speech onset (POST /chime/local).
    That tone reaches the glasses mic in the next chunk, exactly like the
    backend chime, so the echo filter (just_chimed) must know about it --
    otherwise Gemini transcribes the ping and it becomes a phantom line.
    Also lets chime() skip queuing a second ping for the same wake word."""
    global _chime_at
    _chime_at = time.time() if now is None else now


def note_display_seen(now: float | None = None) -> None:
    """The display web app was just hit (server.py: a /glasses/ or /settings request)."""
    global _last_display_at
    _last_display_at = time.time() if now is None else now


def display_open(within_s: float = 30.0, now: float | None = None) -> bool:
    """The display web app was open recently. Heuristic, not proof: it polls /settings while
    open, and the phone bridge does not GET /settings. While it is open the camera is off,
    but the glasses mic keeps listening (Display Audio Mode), so voice still works."""
    now = time.time() if now is None else now
    return 0 <= now - _last_display_at < within_s


def display_seen_ago_s(now: float | None = None) -> float | None:
    """Seconds since the display web app was last seen, or None if it never was."""
    if not _last_display_at:
        return None
    now = time.time() if now is None else now
    return round(now - _last_display_at, 1)


# ---------- how Scene talks to you: "cards", "voice", or "both" (chosen on the glasses) ----------
OUTPUT_MODES = ("cards", "voice", "both")


def _settings_path():
    from pathlib import Path
    return Path(os.environ.get("SCENE_DATA_DIR", Path(__file__).parent / "data")) / "settings.json"


SPEAK_TARGETS = ("glasses", "laptop", "both")


def _read_settings() -> dict:
    import json
    try:
        return json.loads(_settings_path().read_text())
    except (OSError, ValueError):
        return {}


def _write_settings(**changes) -> dict:
    import json
    cur = _read_settings()
    cur.update(changes)
    p = _settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cur))
    return cur


def output_mode(default: str | None = "both") -> str | None:
    """The saved choice, or `default` if the wearer hasn't picked one yet."""
    m = _read_settings().get("output")
    return m if m in OUTPUT_MODES else default


def set_output_mode(mode: str) -> None:
    if mode not in OUTPUT_MODES:
        raise ValueError(f"output must be one of {OUTPUT_MODES}")
    _write_settings(output=mode)


def speak_on() -> str:
    """Where speech plays; Settings on the glasses overrides SCENE_SPEAK_ON."""
    t = _read_settings().get("speak_on") or SPEAK_ON
    return t if t in SPEAK_TARGETS else "glasses"


def set_speak_on(target: str) -> None:
    if target not in SPEAK_TARGETS:
        raise ValueError(f"speak_on must be one of {SPEAK_TARGETS}")
    _write_settings(speak_on=target)


def paused() -> bool:
    """Privacy pause: while on, Scene ignores the camera and the mic."""
    return bool(_read_settings().get("paused"))


def set_paused(value: bool) -> None:
    _write_settings(paused=bool(value))


def speaks() -> bool:
    return output_mode() in ("voice", "both")


# Where speech plays. "glasses" (default): queued for the phone, which plays it and iOS routes it to
# the glasses' speakers over Bluetooth. "laptop": the laptop's speakers. "both": either way.
SPEAK_ON = os.environ.get("SCENE_SPEAK_ON", "glasses")

from collections import deque as _deque  # noqa: E402
import itertools as _itertools           # noqa: E402

_utterances: _deque = _deque(maxlen=30)
# Ids start from the clock, so a server restart never reuses an id the phone already played.
_utterance_ids = _itertools.count(int(time.time() * 1000))
_audio_cache: dict[int, tuple[bytes, str]] = {}


def chime() -> None:
    """The 'I heard you' ping, played the moment the wake word is recognized (like Meta AI's).
    The phone plays its own chime for a {kind: chime} line (nothing to download); the laptop plays Tink.
    Skipped when a chime already pinged within 4 s -- the fast local chime
    on speech onset (note_local_chime) or a chime-out just before it: one
    wake word, one ping, never two. 4 s covers the chunk + transcription lag
    between the local ping and this one, and matches the wake cooldown."""
    global _chime_at
    if not speaks():
        return
    if just_chimed(time.time(), window_s=4.0):
        return
    _chime_at = time.time()
    target = speak_on()
    if target in ("glasses", "both"):
        _utterances.append({"id": next(_utterance_ids), "text": "", "kind": "chime", "ts": time.time()})
    if target in ("laptop", "both"):
        subprocess.Popen(["afplay", "/System/Library/Sounds/Tink.aiff"])


def chime_out() -> None:
    """The 'I'm done' ping, the mirror of chime(): a short descending two-note
    chime played right after a real answer finishes. The phone queues it behind
    the spoken answer (same /speech queue, in order), so it sounds the moment
    the answer ends; it also fires after a text-only answer card, when speak()
    queued nothing because the wearer chose cards. Never called for dropped or
    hallucinated chunks: those never become an answer, so they never reach here.
    Unlike chime(), it does NOT gate on speaks(): a cards-mode answer still gets
    its done ping on the glasses. The laptop plays Pop (not Tink) after the
    answer's estimated air time, only when speech actually played there."""
    global _chime_at
    if paused():
        return
    now = time.time()
    # The tone itself reaches the mic: mark when it will be on the air so the
    # chime-echo filter can drop it. Speech still playing: it sounds at the end
    # of the answer; cards mode: it sounds as soon as the phone polls (~0.5 s).
    _chime_at = _speaking_until if _speaking_until > now else now
    if speak_on() in ("glasses", "both"):
        _utterances.append({"id": next(_utterance_ids), "text": "", "kind": "chime-out", "ts": now})
    if speaks() and speak_on() in ("laptop", "both"):
        import threading
        delay = max(0.0, _speaking_until - now)
        threading.Timer(delay, lambda: subprocess.Popen(
            ["afplay", "/System/Library/Sounds/Pop.aiff"])).start()


def speaking(now: float | None = None) -> bool:
    """True while Jessica is still talking (or about to): her estimated air time
    hasn't run out, or the laptop player is still alive."""
    now = time.time() if now is None else now
    if now < _speaking_until:
        return True
    p = _laptop_proc
    return p is not None and p.poll() is None


def interrupt_seq() -> int:
    """How many barge-ins have happened this process: the phone polls this and
    stops its own player when it changes."""
    return _interrupt_seq


def interrupt() -> bool:
    """Barge-in: stop Jessica mid-sentence, on both speakers. Clears the queued
    speech lines (and their cached audio, so a stale MP3 is never served),
    kills the laptop player, and resets the air-time / echo bookkeeping so the
    new words are heard as the wearer's, not as our own voice. The new wake
    chime is queued AFTER this by the caller, so it still pings. Returns True
    when anything was actually playing or queued."""
    global _speaking_until, _last_spoken, _last_spoken_at, _cooldown_until, _interrupt_seq, _laptop_proc
    had = speaking()
    _utterances.clear()
    _audio_cache.clear()
    _speaking_until = 0.0
    _last_spoken, _last_spoken_at = "", 0.0
    _cooldown_until = 0.0   # the new question must not be swallowed by the post-answer cooldown
    proc = _laptop_proc
    _laptop_proc = None
    if proc is not None and proc.poll() is None:
        try:
            proc.terminate()
            proc.wait(timeout=0.5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
    if had:
        _interrupt_seq += 1
        print("voice: interrupted, Jessica stopped mid-sentence", flush=True)
    return had


def speak(text: str) -> None:
    """Fire and forget: never blocks the audio lane. Silent when the wearer chose cards only."""
    global _speaking_until, _last_spoken, _last_spoken_at
    if not speaks():
        return
    # Remember exactly what Scene said, so a late echo of it through the glasses
    # mic is recognised as our own voice even after _speaking_until expires.
    _last_spoken, _last_spoken_at = text, time.time()
    # How long our own voice will be on the air: words at ~2.6/s, plus synthesis lead, plus the
    # phone's ~1 s poll before it plays. Chunks heard in this window are our echo, not the wearer.
    _speaking_until = time.time() + len(text.split()) / 2.6 + 1.5 + 1.0
    target = speak_on()
    if target in ("glasses", "both"):
        u = {"id": next(_utterance_ids), "text": text, "ts": time.time()}
        _utterances.append(u)
        # Make the audio now, so it's ready the moment the phone asks (~1.5 s saved).
        import threading
        threading.Thread(target=audio_for, args=(u["id"],), daemon=True).start()
    if target in ("laptop", "both"):
        # _play_on_laptop does a blocking ElevenLabs POST (~0.6-2 s, timeout 15 s):
        # called on the audio lane, every spoken answer stalled the next 2.5 s chunk
        # behind it. Play in the background instead; _speaking_until is already set
        # above, so the mic still ignores our own voice while it plays.
        import threading
        threading.Thread(target=_play_on_laptop, args=(text,), daemon=True).start()


def utterances_after(n: int) -> list[dict]:
    """What the phone hasn't played yet (it polls with the last id it saw)."""
    return [u for u in list(_utterances) if u["id"] > n]


_synth_locks: dict[int, "object"] = {}


def audio_for(utterance_id: int) -> tuple[bytes, str] | None:
    """ElevenLabs MP3 for one queued line (macOS `say` AIFF without a key). Made once: the background
    pre-synthesis and the phone's request share one lock per line, so it's never generated twice."""
    import threading
    lock = _synth_locks.setdefault(utterance_id, threading.Lock())
    with lock:
        return _audio_for_locked(utterance_id)


def _audio_for_locked(utterance_id: int) -> tuple[bytes, str] | None:
    if utterance_id in _audio_cache:
        return _audio_cache[utterance_id]
    u = next((u for u in list(_utterances) if u["id"] == utterance_id), None)
    if u is None:
        return None
    out = synthesize(u["text"])
    _audio_cache[utterance_id] = out
    for old in [k for k in list(_synth_locks) if k < utterance_id - 30]:
        _synth_locks.pop(old, None)
    for old in [k for k in _audio_cache if k < utterance_id - 30]:
        _audio_cache.pop(old, None)
    return out


def synthesize(text: str) -> tuple[bytes, str]:
    key = os.environ.get("ELEVENLABS_API_KEY", "")
    if key and key != "REPLACE_ME":
        try:
            # optimize_streaming_latency=3: ElevenLabs starts generating sooner; with the
            # non-streaming endpoint it still cuts time-to-first-byte on short answers.
            _t0 = time.time()
            r = httpx.post(f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE}",
                           headers={"xi-api-key": key}, timeout=15,
                           json={"text": text, "model_id": ELEVEN_MODEL, "voice_settings": ELEVEN_SETTINGS,
                                 "optimize_streaming_latency": 3})
            r.raise_for_status()
            print(f"voice: TTS_MS={(time.time() - _t0) * 1000:.0f} model={ELEVEN_MODEL} chars={len(text)}", flush=True)
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
    """Laptop playback, on its own thread (see speak). Tracks the player process
    in _laptop_proc so interrupt() can kill it, and checks the interrupt
    generation after synthesis: if the wearer barged in while ElevenLabs was
    still generating, the old answer must not start playing after she was told
    to stop."""
    global _laptop_proc
    gen = _interrupt_seq
    key = os.environ.get("ELEVENLABS_API_KEY", "")
    if key and key != "REPLACE_ME":
        try:
            r = httpx.post(f"https://api.elevenlabs.io/v1/text-to-speech/{ELEVEN_VOICE}",
                           headers={"xi-api-key": key}, timeout=15,
                           json={"text": text, "model_id": ELEVEN_MODEL, "voice_settings": ELEVEN_SETTINGS,
                                 "optimize_streaming_latency": 3})
            r.raise_for_status()
            f = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
            f.write(r.content)
            f.close()
            if _interrupt_seq != gen:
                return   # barged in during synthesis: stay stopped
            proc = subprocess.Popen(["afplay", f.name])
            _laptop_proc = proc
            proc.wait()
            if _laptop_proc is proc:
                _laptop_proc = None
            return
        except Exception as e:
            print(f"voice: ElevenLabs failed ({e!r}), falling back to say", flush=True)
    if _interrupt_seq != gen:
        return   # barged in while the ElevenLabs call was failing: stay stopped
    proc = subprocess.Popen(["say", text])
    _laptop_proc = proc
    proc.wait()
    if _laptop_proc is proc:
        _laptop_proc = None
