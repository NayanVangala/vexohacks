"""Every model call lives here. Google = seeing (frames). OpenAI = hearing, thinking, embedding.

Keys from env: GEMINI_API_KEY (required), OPENAI_API_KEY (optional: without it Gemini does
the hearing/thinking/embedding too). Model names overridable by env.
Plain httpx REST, no SDKs.
"""

import base64
import json
import os

import httpx

# Hydrogen = the fast Gemini 4: cheap enough to run on every frame the bridge sends.
# Exact name unconfirmed until a key is set; `python server.py` lists the gemini-4 models it can see.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-4-hydrogen")
OPENAI_CHAT_MODEL = os.environ.get("OPENAI_CHAT_MODEL", "gpt-4o-mini")
OPENAI_EMBED_MODEL = os.environ.get("OPENAI_EMBED_MODEL", "text-embedding-3-small")
OPENAI_STT_MODEL = os.environ.get("OPENAI_STT_MODEL", "whisper-1")
GEMINI_TEXT_MODEL = os.environ.get("GEMINI_TEXT_MODEL", GEMINI_MODEL)  # used only when OpenAI is absent
GEMINI_EMBED_MODEL = os.environ.get("GEMINI_EMBED_MODEL", "gemini-embedding-001")
GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta"

_http = httpx.Client(timeout=30)

VISION_PROMPT = """You are the memory of someone wearing smart glasses. This is what they see.
Return JSON: {"objects": [{"name": str, "place": str}], "activity": str,
              "receipt": {"merchant": str, "total": number} or null,
              "scene": {"type": str, "confidence": number, "text": str}}
- objects: personal belongings worth remembering the location of (keys, wallet, phone, laptop,
  bag, glasses, mug, notebook...). Skip furniture and fixed things. place = short, concrete
  ("on the kitchen counter next to the sink"). Empty list if none.
- activity: one short sentence of what the wearer is doing, or "" if nothing notable.
- receipt: only if a receipt, bill, or check is clearly readable: merchant name and final total. Else null.
- scene: the situation the wearer is in. type is one of "meeting" (people gathered at a table or in a
  conference room), "slides" (a screen or projector showing presentation slides), "whiteboard",
  "card" (a business card), "menu" (a restaurant menu), or "none". confidence is 0 to 1.
  text is the headline or main text of the slide, whiteboard, card or menu exactly as readable, else "".
  When unsure, answer "none" with low confidence."""

EXTRACT_PROMPT = """You read a transcript heard by someone's smart glasses.
Return JSON: {"tasks": [{"text": str, "from_whom": str, "due": str, "quote": str}],
              "done": [{"text": str, "quote": str}],
              "owed_to_me": [{"who": str, "amount": number, "memo": str, "quote": str}]}
Every item MUST carry "quote": the exact words from the transcript it came from, copied verbatim.
Items whose quote is not in the transcript are thrown away, so never paraphrase it.
- tasks: things the wearer was asked to do or committed to doing. text = imperative, self-contained
  ("Send the pitch deck to Sam"). from_whom/due = "" if unknown.
- done: things the wearer says they finished ("I sent the deck"); text phrased as the task.
- owed_to_me: someone clearly says they owe the wearer money or will pay them back. amount in dollars;
  use the recent receipts given as context to resolve "half" or "my share". Skip if the amount is unclear.
Empty lists if nothing. Ignore small talk."""

NOTES_PROMPT = """You turn a meeting transcript (heard by smart glasses) and the text of slides shown into notes.
Return JSON: {"summary": str, "decisions": [str], "action_items": [{"text": str, "quote": str}]}
- summary: at most 3 plain sentences.
- decisions: things that were decided, each one short.
- action_items: things someone committed to do. text = imperative, self-contained ("Send the budget to Priya").
  quote = the exact words from the transcript or a slide it came from, copied verbatim.
  Items whose quote is not in the transcript or slides are thrown away, so never paraphrase it.
Empty lists if nothing. Ignore small talk."""

ANSWER_PROMPT = """You answer questions for someone wearing smart glasses, using only their memories below.
Each memory has an id, kind, text, place, and age. Return JSON:
{"answer": str (at most 15 words, plain), "memory_id": int or null (the single most relevant memory)}
If the memories don't answer it, say so briefly and use null."""


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
    """Frame -> {"objects": [{name, place}], "activity": str, "receipt": ..., "scene": {type, confidence, text}} via Gemini 4."""
    return _gemini_json(GEMINI_MODEL, [
        {"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(jpeg).decode()}},
        {"text": VISION_PROMPT},
    ])


def gemini_models() -> list[str]:
    """Model names this key can call, for the startup check."""
    r = _http.get(f"{GEMINI_URL}/models",
                  headers={"x-goog-api-key": os.environ["GEMINI_API_KEY"]}, params={"pageSize": 1000})
    r.raise_for_status()
    return [m["name"].removeprefix("models/") for m in r.json().get("models", [])]


def transcribe(wav: bytes) -> str:
    if not _use_openai():
        out = _gemini_json(GEMINI_TEXT_MODEL, [
            {"inline_data": {"mime_type": "audio/wav", "data": base64.b64encode(wav).decode()}},
            {"text": 'Transcribe the speech verbatim. Return JSON {"text": str}; "" if there is no speech.'},
        ])
        return (out.get("text") or "").strip()
    out = _openai("/audio/transcriptions",
                  files={"file": ("chunk.wav", wav, "audio/wav")},
                  data={"model": OPENAI_STT_MODEL})
    return out.get("text", "").strip()


def extract_tasks(transcript: str, receipts: list[str] = ()) -> dict:
    return _think(EXTRACT_PROMPT, json.dumps({"recent_receipts": list(receipts), "transcript": transcript}))


def make_notes(transcript: str, slide_texts: list[str]) -> dict:
    return _think(NOTES_PROMPT, json.dumps({"transcript": transcript, "slides": slide_texts}))


def embed(text: str) -> list[float]:
    if not _use_openai():
        out = _gemini(f"models/{GEMINI_EMBED_MODEL}:embedContent", json={"content": {"parts": [{"text": text}]}})
        return out["embedding"]["values"]
    return _openai("/embeddings", json={"model": OPENAI_EMBED_MODEL, "input": text})["data"][0]["embedding"]


def answer(question: str, memories: list[dict]) -> dict:
    return _think(ANSWER_PROMPT, json.dumps({"question": question, "memories": memories}))
