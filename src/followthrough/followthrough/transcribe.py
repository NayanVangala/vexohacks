"""Audio to transcript segments with start and end times, using OpenAI.
whisper-1 with verbose_json returns segment timestamps, which is what the quote proof needs."""
import io
from .config import Config
from .models import Segment


def transcribe(audio: bytes, filename: str, offset: float, cfg: Config, client=None) -> list[Segment]:
    """offset is where this audio starts on the running clock, so chunks line up one after another."""
    if client is None:
        from openai import OpenAI
        client = OpenAI(api_key=cfg.openai_api_key)
    f = io.BytesIO(audio)
    f.name = filename or "audio.wav"
    r = client.audio.transcriptions.create(model=cfg.transcribe_model, file=f,
                                           response_format="verbose_json", timestamp_granularities=["segment"])
    segs = getattr(r, "segments", None) or (r.get("segments") if isinstance(r, dict) else None) or []
    get = lambda s, k: s.get(k) if isinstance(s, dict) else getattr(s, k)
    out = [Segment(start=offset + float(get(s, "start")), end=offset + float(get(s, "end")), text=get(s, "text").strip())
           for s in segs if get(s, "text").strip()]
    if not out and getattr(r, "text", ""):          # no segments came back, keep the text without times
        out = [Segment(text=r.text.strip())]
    return out
