"""Every model call lives here. Google (Gemini) = seeing (frames) AND hearing (audio transcription).
OpenAI = thinking, embedding (gpt-4o-mini answers/tasks/notes, agent reasoning, embeddings).

Keys from env: GEMINI_API_KEY (required), OPENAI_API_KEY (optional: without it Gemini does
the thinking/embedding too). Model names overridable by env.
Plain httpx REST, no SDKs.
"""

import base64
import json
import os
import time

import httpx

# Hydrogen = the fast Gemini 4: cheap enough to run on every frame the bridge sends.
# Exact name unconfirmed until a key is set; `python server.py` lists the gemini-4 models it can see.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-4-hydrogen")
# Audio STT uses the same model: hydrogen is multimodal and already accepts audio/wav
# inline data (the old no-OpenAI fallback path did exactly this). Override with
# GEMINI_AUDIO_MODEL if a faster Gemini audio model is ever needed.
GEMINI_AUDIO_MODEL = os.environ.get("GEMINI_AUDIO_MODEL", GEMINI_MODEL)
OPENAI_CHAT_MODEL = os.environ.get("OPENAI_CHAT_MODEL", "gpt-4o-mini")
OPENAI_EMBED_MODEL = os.environ.get("OPENAI_EMBED_MODEL", "text-embedding-3-small")
OPENAI_STT_MODEL = os.environ.get("OPENAI_STT_MODEL", "whisper-1")
# Grounded (Maps/Search) calls: hydrogen takes ~40 s for these, flash-lite ~3 s but is less grounded. Speed wins
# live; set GEMINI_GROUNDED_MODEL=gemini-4-hydrogen when accuracy matters more than the wait.
GEMINI_GROUNDED_MODEL = os.environ.get("GEMINI_GROUNDED_MODEL", "gemini-3.1-flash-lite")
# Web search needs real grounding (sources); 3.5-flash-lite does it in ~2 s, 3.1-flash-lite returned none.
GEMINI_SEARCH_MODEL = os.environ.get("GEMINI_SEARCH_MODEL", "gemini-3.5-flash-lite")
GEMINI_TEXT_MODEL = os.environ.get("GEMINI_TEXT_MODEL", GEMINI_MODEL)  # used only when OpenAI is absent
GEMINI_EMBED_MODEL = os.environ.get("GEMINI_EMBED_MODEL", "gemini-embedding-001")
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta"

_http = httpx.Client(timeout=30)

VISION_PROMPT = """You are the memory of someone wearing smart glasses. This is what they see.
Return JSON: {"objects": [{"name": str, "place": str}], "activity": str,
              "receipt": {"merchant": str, "total": number} or null,
              "scene": {"type": str, "confidence": number, "text": str},
              "face": {"present": bool, "clear": bool}}
- objects: personal belongings worth remembering the location of (keys, wallet, phone, laptop,
  bag, glasses, mug, notebook, water bottle, bottle, pouch, charger, headphones...).
  Name the object as it actually is: a water bottle is "water bottle", never "bag" or "pouch".
  Skip furniture and fixed things. place = short, concrete
  ("on the kitchen counter next to the sink"). Empty list if none.
- activity: one short sentence of what the wearer is doing, or "" if nothing notable.
- receipt: only if a receipt, bill, or check is clearly readable: merchant name and final total. Else null.
- scene: the situation the wearer is in. type is one of "meeting" (people gathered at a table or in a
  conference room), "slides" (a screen or projector showing presentation slides), "whiteboard",
  "card" (a business card), "menu" (a restaurant menu), or "none". confidence is 0 to 1.
  text is the headline or main text of the slide, whiteboard, card or menu exactly as readable, else "".
  When unsure, answer "none" with low confidence.
- face: whether a person's face is visible in this frame. present = true when any face is
  visible at all; clear = true only when one face is front-facing, in focus, and large
  enough to recognise someone from (not a blur, profile, partial face, or distant crowd).
  The wearer's own face is never in frame, so any face here is someone else's."""

EXTRACT_PROMPT = """You read the FULL transcript of a whole conversation heard by someone's smart glasses.
A request, its day/time, and its correction may be sentences apart; they belong to the same task.
Return JSON: {"tasks": [{"text": str, "from_whom": str, "due": str, "quote": str}],
              "done": [{"text": str, "quote": str}],
              "owed_to_me": [{"who": str, "amount": number, "memo": str, "quote": str}]}
Every item MUST carry "quote": the exact words from the transcript it came from, copied verbatim.
Items whose quote is not in the transcript are thrown away, so never paraphrase it.
Quote one contiguous passage, in order, as said (it may span several sentences); never splice
words from far-apart parts of the transcript into one quote.
- tasks: things the wearer was asked to do or committed to doing. text = imperative, self-contained
  ("Send the pitch deck to Sam"). from_whom/due = "" if unknown.
  Resolve relative times from now_local (given with the transcript) and put due as
  YYYY-MM-DDTHH:MM: "tomorrow at 10pm" said on Sat 2026-11-14 is 2026-11-15T22:00,
  "Tuesday at 5pm" is the next Tuesday at 17:00, "by five" today is today at 17:00.
  If a later line corrects the time ("Tuesday at 2, no, make it 5"), use the LATEST
  one. If no time was said at all, due is "" -- never guess a date.
  Keep the day/time in the task text too, not only in due: a task text without its
  day/time is wrong when a day/time was said.
  A fragment is not a task: "sit down", or any short fragment with no object, is
  never a task, and neither is a bare "send a reminder" with no recipient, content,
  or time. Only list a task when the transcript clearly asks the wearer to do
  something specific, with a verb and an object.
- done: things the wearer says they finished ("I sent the deck"); text phrased as the task.
- owed_to_me: someone clearly says they owe the wearer money or will pay them back. amount in dollars;
  use the recent receipts given as context to resolve "half" or "my share". Skip if the amount is unclear.
Empty lists if nothing. Ignore small talk."""

NOTES_PROMPT = """You turn a meeting transcript (heard by smart glasses) and the text of slides shown into notes.
Return JSON: {"summary": str, "decisions": [str],
              "action_items": [{"text": str, "quote": str, "action": str, "to": str, "subject": str, "body": str,
                                "start": str, "duration_min": number}]}
- summary: at most 3 plain sentences.
- decisions: things that were decided, each one short.
- action_items: things someone committed to do. text = imperative, self-contained ("Send the budget to Priya").
  quote = the exact words from the transcript or a slide it came from, copied verbatim.
  Items whose quote is not in the transcript or slides are thrown away, so never paraphrase it.
  Keep any day/time that was said in the action item text as well as in start, and if a time
  is corrected later in the transcript, use only the corrected time. A fragment with no
  object ("sit down") is never an action item.
  action = "email" when the item is sending something (a file, an update, a reply) to a named person, else "todo".
  For email: to = that person's name, subject = short, body = 2 to 4 plain sentences from the wearer to them.
  action = "calendar" when a meeting or time block was agreed with a specific day and time. Then start is
  YYYY-MM-DDTHH:MM in the wearer's local time (resolve "Friday at five" from the current date given), duration_min
  defaults to 30, and text is a short title ("Demo sync with Nayan"). A vague time ("sometime", "soon") is a todo.
  For todo: to, subject, body and start are empty strings.
Empty lists if nothing. Ignore small talk."""

ANSWER_PROMPT = """You answer questions for someone wearing smart glasses, using only their memories below.
Each memory has an id, kind, text, place, and age. Return JSON:
{"answer": str (at most 15 words, plain), "memory_id": int or null (the single most relevant memory)}
If the memories don't answer it, say so briefly and use null.
Never substitute a different object: if the question asks where a water bottle is and the
memories only mention a pouch, a bag, or anything that is not a water bottle, do NOT name
that other object. Say you have not seen the thing asked about and use null."""


GUIDE_PROMPT = """You walk someone wearing camera glasses to a thing they are looking for.
Image 1 is what they see right now. Image 2 (if present) is a photo taken earlier where the thing was last seen.
Target: {name}, last seen {place}. Your last instruction was: {last!r}.
Return JSON {{"arrived": bool, "instruction": str}}.
- If the target is visible in image 1: arrived true, and the instruction says where it is in view
  ("There it is, on the white table to your left.").
- Otherwise arrived false and ONE short next step (at most 12 words) using the person's own left/right/ahead and
  landmarks you can actually see in image 1 that match image 2 or the remembered place ("Turn left toward the window.").
- If you cannot tell where they are, say "Turn slowly so I can see more." Never guess.
- Never tell them to run, climb, cross a road, or go down stairs. Do not repeat your last instruction word for word."""


def guide(now_jpeg: bytes, target: dict, target_jpeg: bytes | None, last: str = "") -> dict:
    """The next step toward `target` ({name, place}), from what the glasses see now and the photo of the spot."""
    img = lambda b: {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(b).decode()}}  # noqa: E731
    parts = [img(now_jpeg)] + ([img(target_jpeg)] if target_jpeg else [])
    parts.append({"text": GUIDE_PROMPT.format(name=target.get("name", "it"), place=target.get("place") or "unknown", last=last)})
    return _gemini_json(GEMINI_MODEL, parts)


def _use_openai() -> bool:
    return bool(os.environ.get("OPENAI_API_KEY"))


def _gemini(path: str, **kw) -> dict:
    r = _http.post(f"{GEMINI_URL}/{path}", headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]}, **kw)
    r.raise_for_status()
    return r.json()


def _gemini_json(model: str, parts: list[dict]) -> dict:
    out = _gemini(f"models/{model}:generateContent", json={
        "contents": [{"parts": parts}], "generationConfig": {"responseMimeType": "application/json"}})
    return json.loads(out["candidates"][0]["content"]["parts"][0]["text"])


def _think(system: str, user: str) -> dict:
    if _use_openai():
        return _openai_json(system, user)
    return _gemini_json(GEMINI_TEXT_MODEL, [{"text": system + "\n\n" + user}])


def _openai(path: str, **kw) -> dict:
    r = _http.post(f"https://api.openai.com/v1{path}",
                   headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"}, **kw)
    r.raise_for_status()
    return r.json()


def _openai_json(system: str, user: str) -> dict:
    out = _openai("/chat/completions", json={
        "model": OPENAI_CHAT_MODEL,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    })
    return json.loads(out["choices"][0]["message"]["content"])


def see(jpeg: bytes) -> dict:
    """Frame -> {"objects": [{name, place}], "activity": str, "receipt": ..., "scene": {type, confidence, text}, "face": {present, clear}} via Gemini 4.
    The face field is presence only (is a clear face visible?), never a name: naming is
    match_face() / identify_face() in memory.py, against enrolled reference photos."""
    return _gemini_json(GEMINI_MODEL, [
        {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(jpeg).decode()}},
        {"text": VISION_PROMPT},
    ])


FACE_MATCH_PROMPT = """You are identifying the person in a photo taken by smart glasses.
The first image is the current frame. The following images are reference photos, one per enrolled person, labelled by name.
Return JSON: {"name": str or null, "confidence": number}
- name is the enrolled person who is clearly the same person as someone visible in the current frame, or null when no enrolled person is clearly recognisable.
- confidence is 0 to 1: how sure you are that this is the same person (face shape, hair, glasses, clothing may help, but the face must match).
- Only name someone when you are confident it is the same person. A stranger, a blurry face, a face turned away, or a partial face is null, never a guess.
- If several enrolled people are visible, name the one whose face is clearest and most central."""


def identify_face(frame_jpeg: bytes, references: list[tuple[str, bytes]]) -> dict:
    """Match one frame against enrolled reference photos via Gemini Hydrogen vision.
    references = [(name, jpeg bytes), ...]. Returns {"name": str | None, "confidence": float}.
    No API call (and no guess) when there are no references. Raises on API error so the
    caller can log it and fall back to Unknown; never returns a name it did not get."""
    if not references:
        return {"name": None, "confidence": 0.0}
    img = lambda b: {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(b).decode()}}  # noqa: E731
    parts: list[dict] = [img(frame_jpeg)]
    for name, jpeg in references[:8]:
        parts.append(img(jpeg))
        parts.append({"text": f"Reference photo for {name}."})
    parts.append({"text": FACE_MATCH_PROMPT})
    out = _gemini_json(GEMINI_MODEL, parts)
    name = out.get("name") or None
    try:
        conf = float(out.get("confidence") or 0.0)
    except (TypeError, ValueError):
        conf = 0.0
    if not isinstance(name, str) or not name.strip():
        return {"name": None, "confidence": 0.0}
    return {"name": name.strip(), "confidence": max(0.0, min(1.0, conf))}


def gemini_models() -> list[str]:
    """Model names this key can call, for the startup check."""
    r = _http.get(f"{GEMINI_URL}/models",
                  headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]}, params={"pageSize": 1000})
    r.raise_for_status()
    return [m["name"].removeprefix("models/") for m in r.json().get("models", [])]


# Audio STT model split (2026-11-14): Gemini does audio AND video; OpenAI does everything
# else Gemini is not doing (gpt-4o-mini answers/tasks/notes, agent reasoning, embeddings).
# Gemini is primary for transcription; Whisper is only a logged fallback when Gemini errors.
# The prompt must NOT contain an example question: the old keys example leaked verbatim
# into hallucinations on room noise (Follow-Through state.json, 2026-11-14: the example
# question as a whole segment, nobody speaking). Same rule for Gemini as for Whisper.
# Hallucination fix (2026-11-14): deterministic generation (temperature 0), explicit
# "return empty for silence / noise / music", and real context priming -- the names of
# people Nayan actually knows (contacts + enrolled faces) and the last lines of this
# session's transcript -- so the model hears "Srijan" instead of inventing a similar
# word, and does not re-invent words it already transcribed. Names/context come from
# the caller (memory.py); they are hints for spelling, never words to insert.
_transcribe_logged = False
_last_transcribe_source = "none"  # "gemini" or "whisper": which STT produced the last chunk


# Common objects the wearer asks about. Gemini audio had no object hints at all:
# it only primed person names, so a common noun like "water bottle" could be
# misheard as a similar-sounding invented phrase ("black pouch") with nothing in
# the prompt to anchor it. These are spelling hints only, never words to insert:
# if the audio has no speech, the answer is still {"text": ""}.
COMMON_ITEMS = ("water bottle", "bottle", "keys", "wallet", "phone", "laptop",
                "bag", "backpack", "pouch", "glasses", "mug", "cup", "notebook",
                "charger", "headphones", "earbuds", "jacket", "hat", "watch",
                "sunglasses", "umbrella", "lunchbox")


def _transcribe_prompt(names=(), context: str = "", items=()) -> str:
    prompt = (
        'Transcribe the speech verbatim, in English only. '
        'Return JSON {"text": str}. '
        'If there is no speech, or only silence, room noise, music, or sounds that '
        'are not speech, return {"text": ""}. '
        'Do NOT add words nobody said, do NOT invent subtitles, captions, or outro '
        'phrases such as "thank you for watching". '
        'Spell the wake word exactly "Hey Scene" when you hear it.'
    )
    names = [str(n).strip() for n in (names or []) if str(n).strip()]
    if names:
        prompt += ' People you may hear named: ' + ', '.join(names[:12]) + '.'
    items = [str(i).strip() for i in (items or []) if str(i).strip()]
    if items:
        prompt += (' Common objects you may hear asked about: ' + ', '.join(items[:24]) + '. '
                   'These are spelling hints only: transcribe the object actually said, '
                   'never substitute one object for another.')
    if context:
        prompt += ' Recent conversation (do not repeat it, use it only for context): ' + context[:400]
    return prompt


def transcribe_source() -> str:
    """Which STT produced the last transcribed chunk: "gemini", "whisper", or "none"."""
    return _last_transcribe_source


def _gemini_transcribe(wav: bytes, names=(), context: str = "", items=()) -> str:
    """One Gemini audio call -> plain transcript text. Raises on API error / empty
    candidates so the caller can fall back to Whisper; never returns a made-up line.
    Parses flexibly: the model may return JSON {"text": ...} or plain text.
    temperature 0 = deterministic: the same audio gives the same transcript, and
    the model is far less likely to invent fluent filler on noise."""
    out = _gemini(f"models/{GEMINI_AUDIO_MODEL}:generateContent", json={
        "contents": [{"parts": [
            {"inline_data": {"mime_type": "audio/wav", "data": base64.b64encode(wav).decode()}},
            {"text": _transcribe_prompt(names, context, items)}, 
        ]}], "generationConfig": {"responseMimeType": "application/json", "temperature": 0.0, "topP": 1.0}})
    cands = out.get("candidates") or []
    if not cands:
        raise ValueError("Gemini audio returned no candidates (filtered or empty)")
    raw = "".join(p.get("text", "") for p in cands[0].get("content", {}).get("parts", [])).strip()
    if not raw:
        return ""
    # Strip a ```json fence if the model wrapped the JSON in one, then parse; if it is
    # not JSON at all, treat the whole reply as the transcript (better than dropping speech).
    if raw.startswith("```"):
        raw = raw.strip("`").strip()
        if raw.lower().startswith("json"):
            raw = raw[4:].strip()
    try:
        return (json.loads(raw).get("text") or "").strip()
    except (ValueError, AttributeError):
        return raw.strip()


def _whisper_transcribe(wav: bytes) -> str:
    """Fallback only: OpenAI Whisper-1, English only, temperature 0, same no-example prompt."""
    out = _openai("/audio/transcriptions",
                  files={"file": ("chunk.wav", wav, "audio/wav")} ,
                  data={"model": OPENAI_STT_MODEL, "language": os.environ.get("SCENE_LANGUAGE", "en"),
                        "temperature": "0", "prompt": "Hey Scene. English conversation, short questions and answers."})
    return out.get("text", "").strip()


def transcribe(wav: bytes, names=(), context: str = "", items=()) -> str:
    """Speech-to-text for one 16 kHz mono WAV chunk. Gemini first (GEMINI_AUDIO_MODEL,
    default gemini-4-hydrogen, same model as vision); Whisper only if Gemini errors and
    an OpenAI key is set, logged so we can see whether Gemini audio is actually working.
    Same interface as before: plain transcript text, "" when there is no speech. The
    hallucination filter in voice.py / memory.py gates the result either way.
    names/context/items are priming hints for the Gemini prompt (real contact +
    enrolled-face names, recent transcript, common objects like "water bottle");
    transcribe_source() reports which engine produced this
    chunk so the log can prove it."""
    global _transcribe_logged, _last_transcribe_source
    _t0 = time.time()
    try:
        text = _gemini_transcribe(wav, names=names, context=context, items=items or COMMON_ITEMS)
        _stt_ms = (time.time() - _t0) * 1000
        if _stt_ms > 2000:   # slow STT is the usual suspect when answers feel late; log it, don't spam fast chunks
            print(f"models: slow transcribe_ms={_stt_ms:.0f} source=gemini", flush=True)
    except Exception as e:
        if _use_openai():
            print(f"models: Gemini audio failed ({e!r}), falling back to Whisper", flush=True)
            _last_transcribe_source = "whisper"
            return _whisper_transcribe(wav)
        print(f"models: Gemini audio failed ({e!r}), no Whisper fallback (no OpenAI key)", flush=True)
        _last_transcribe_source = "none"
        return ""
    _last_transcribe_source = "gemini"
    if not _transcribe_logged:
        _transcribe_logged = True
        print(f"models: audio STT is Gemini {GEMINI_AUDIO_MODEL} (Whisper is fallback only)", flush=True)
    return text.strip()


def extract_tasks(transcript: str, receipts: list[str] = ()) -> dict:
    # now_local anchors relative times: without it the model had no date to resolve
    # "tomorrow at 10pm" / "Tuesday at 5pm" from, so due times were vague or wrong.
    # Same pattern as make_notes below.
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo(os.environ.get("TIMEZONE", "America/Los_Angeles")))
    return _think(EXTRACT_PROMPT, json.dumps({"now_local": now.strftime("%A %Y-%m-%d %H:%M"),
                                              "recent_receipts": list(receipts), "transcript": transcript}))


def grounded(prompt: str, tool: str, latlng: tuple[float, float] | None = None) -> str:
    """One Gemini call with a grounding tool ("google_maps" or "google_search"). Returns the answer text."""
    body = {"contents": [{"parts": [{"text": prompt}]}], "tools": [{tool: {}}]}
    if latlng:
        body["toolConfig"] = {"retrievalConfig": {"latLng": {"latitude": latlng[0], "longitude": latlng[1]}}}
    out = _gemini(f"models/{GEMINI_GROUNDED_MODEL}:generateContent", json=body, timeout=90)
    return "".join(p.get("text", "") for p in out["candidates"][0]["content"]["parts"])


def search_web(query: str) -> dict:
    """Google-grounded answer to a web question: {"answer": str, "sources": [{"title", "url"}]} (up to 4 sources)."""
    out = _gemini(f"models/{GEMINI_SEARCH_MODEL}:generateContent", timeout=60, json={
        "contents": [{"parts": [{"text": f"Search the web and answer: {query}\nAt most 3 short plain sentences, specific and current: "
                                         "name concrete products, prices or facts. No markdown."}]}],
        "tools": [{"google_search": {}}]})
    c = out["candidates"][0]
    answer = "".join(p.get("text", "") for p in c["content"]["parts"]).strip()
    seen, sources = set(), []
    for ch in c.get("groundingMetadata", {}).get("groundingChunks", []):
        w = ch.get("web") or {}
        if w.get("title") and w["title"] not in seen:
            seen.add(w["title"])
            sources.append({"title": w["title"], "url": w.get("uri", "")})
    return {"answer": answer, "sources": sources[:4]}


def agent_step(system: str, history: list[dict], tools: list[dict]) -> dict:
    """One turn of the dining agent: Gemini function calling. Returns {text, call: {name, args} | None, parts}.
    `parts` is the model's raw turn; the caller replays it so thought signatures survive."""
    out = _gemini(f"models/{GEMINI_MODEL}:generateContent", timeout=60, json={
        "systemInstruction": {"parts": [{"text": system}]}, "contents": history,
        "tools": [{"functionDeclarations": tools}], "toolConfig": {"functionCallingConfig": {"mode": "AUTO"}}})
    parts = out["candidates"][0]["content"].get("parts", [])
    fc = next((p["functionCall"] for p in parts if "functionCall" in p), None)
    text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip() or None
    return {"text": None if fc else text, "call": {"name": fc["name"], "args": fc.get("args", {})} if fc else None, "parts": parts}


def make_notes(transcript: str, slide_texts: list[str]) -> dict:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    now = datetime.now(ZoneInfo(os.environ.get("TIMEZONE", "America/Los_Angeles")))
    return _think(NOTES_PROMPT, json.dumps({"now_local": now.strftime("%A %Y-%m-%d %H:%M"),
                                            "transcript": transcript, "slides": slide_texts}))


def embed(text: str) -> list[float]:
    if not _use_openai():
        out = _gemini(f"models/{GEMINI_EMBED_MODEL}:embedContent", json={"content": {"parts": [{"text": text}]}})
        return out["embedding"]["values"]
    return _openai("/embeddings", json={"model": OPENAI_EMBED_MODEL, "input": text})["data"][0]["embedding"]


def answer(question: str, memories: list[dict]) -> dict:
    return _think(ANSWER_PROMPT, json.dumps({"question": question, "memories": memories}))
