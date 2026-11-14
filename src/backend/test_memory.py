"""One check for memory: a frame and a transcript become searchable rows, and the endpoints serve them.

Models are faked (no keys, no network): embed = bag of words, so search behaves predictably.
Run: .venv/bin/python -m pytest -q
"""

import math
import os
import re
import zlib
import tempfile
from collections import Counter
from pathlib import Path

os.environ.setdefault("SCENE_DATA_DIR", tempfile.mkdtemp())

from fastapi.testclient import TestClient  # noqa: E402

import memory  # noqa: E402
import models  # noqa: E402
import voice  # noqa: E402
import server  # noqa: E402

DIM = 256


def fake_embed(text: str) -> list[float]:
    v = [0.0] * DIM
    for w, n in Counter(re.findall(r"[a-z]+", text.lower())).items():
        v[zlib.crc32(w.encode()) % DIM] += n   # not hash(): it's salted per run, so collisions were random
    return v


def fake_answer(question, mems):
    return {"answer": f"{mems[0]['text']} {mems[0]['place'] or ''}".strip(), "memory_id": mems[0]["id"]}


def test_frame_and_transcript_become_memories(monkeypatch):
    monkeypatch.setattr(models, "embed", fake_embed)
    monkeypatch.setattr(models, "answer", fake_answer)
    monkeypatch.setattr(models, "see", lambda jpeg: {
        "objects": [{"name": "Keys", "place": "on the kitchen counter"}], "activity": "making coffee"})
    monkeypatch.setattr(models, "extract_tasks", lambda text, receipts=(): (
        {"tasks": [{"text": "Send the pitch deck to Sam", "from_whom": "Sam", "due": "5pm",
                    "quote": "send me the pitch deck by 5pm"},
                   {"text": "Buy Sam a car", "quote": "you promised me a car"}],  # never said: dropped
         "done": []}
        if "send me" in text else
        {"tasks": [], "done": [{"text": "sent the deck to Sam", "quote": "I sent the deck to Sam"}]}))
    monkeypatch.setattr(memory, "MIN_SIM", 0.1)
    monkeypatch.setattr(server, "PUBLIC_BASE_URL", "https://tunnel.example")

    frame = Path(tempfile.mkdtemp()) / "123.jpg"
    frame.write_bytes(b"\xff\xd8\xff")
    memory.process_frame(frame)
    memory.process_frame(frame)  # same object, same place: refreshed, not duplicated

    memory._transcript[:] = ["hey can you send me the pitch deck by 5pm"]
    memory.flush_transcript()

    c = TestClient(server.app)
    items = c.get("/items").json()
    assert [(i["name"], i["place"]) for i in items] == [("keys", "on the kitchen counter")]
    assert items[0]["image_url"] == "https://tunnel.example/frames/123.jpg"

    tasks = c.get("/tasks").json()
    assert [t["text"] for t in tasks] == ["Send the pitch deck to Sam (from Sam, due 5pm)"]
    assert [(f["kind"], f["ref"]) for f in c.get("/feed").json()] == [("task", tasks[0]["id"])]

    a = c.post("/ask", data={"text": "where are my keys"}).json()
    assert "kitchen counter" in a["answer"] and a["frame"] == "123.jpg"

    # Saying it's done closes the task via semantic match.
    memory._transcript[:] = ["ok I sent the deck to Sam"]
    memory.flush_transcript()
    assert c.get("/tasks").json() == []

    assert c.post("/ask", data={"text": "zebra migration"}).json()["answer"] == "I don't have a memory of that."
    t = c.get("/today").json()
    assert t["memories"] >= 3 and len(t["hourly"]) == 24 and sum(t["hourly"]) == t["memories"]
    assert c.post("/tasks/999999/done").status_code == 404
    assert c.post("/ask", data={"text": "  "}).status_code == 422


def test_wake_word_answers_aloud(monkeypatch):
    spoken = []
    monkeypatch.setattr(voice, "speak", spoken.append)
    monkeypatch.setattr(voice, "_cooldown_until", 0.0)
    monkeypatch.setattr(memory, "ask", lambda q: {"question": q, "answer": "On the kitchen counter.",
                                                  "memory_id": 1, "frame": "123.jpg"})
    said = iter(["Hey Scene,", "where did I leave my keys?", "Thank you."])
    monkeypatch.setattr(models, "transcribe", lambda wav: next(said))
    before = len(memory._transcript)
    last_card = max([c["id"] for c in memory.cards_after(0)] or [0])

    wav = Path(tempfile.mkdtemp()) / "a.wav"
    wav.write_bytes(b"RIFF")
    for _ in range(3):
        memory.process_audio(wav)

    assert spoken == ["On the kitchen counter."]
    [card] = memory.cards_after(last_card)
    assert (card["kind"], card["title"]) == ("answer", "where did I leave my keys?")
    assert len(memory._transcript) == before  # question and Whisper's "Thank you." kept out of tasks


def test_wake_regex_variants():
    for s in ["hey scene what's on my list", "Hey, Seen. what did Sam ask", "ok sean where's my wallet"]:
        assert voice.WAKE.search(s), s
    assert not voice.WAKE.search("I have seen hey there")


def test_provider_switch_reembeds_old_rows(monkeypatch):
    # A row embedded with a 3-dim model, searched with a 256-dim one: it must be re-embedded, not skipped.
    monkeypatch.setattr(models, "embed", fake_embed)
    with memory._db_lock:
        rid = memory.db().execute(
            "INSERT INTO memories (kind, text, place, ts, embedding) VALUES ('object','umbrella','by the door',?,?)",
            (memory.time.time(), "[1, 2, 3]")).lastrowid
        memory.db().commit()
    hits = memory.search(fake_embed("umbrella by the door"), 50)
    assert any(r["id"] == rid for _, r in hits)
    assert len(memory.json.loads(memory.rows("SELECT embedding FROM memories WHERE id=?", rid)[0]["embedding"])) == DIM


def test_silence_and_phantom_phrases_are_dropped():
    import struct
    quiet = b"RIFF" + b"\0" * 40 + struct.pack("<1000h", *([15, -12] * 500))
    loud = b"RIFF" + b"\0" * 40 + struct.pack("<1000h", *([900, -850] * 500))
    assert voice.is_silent(quiet) and not voice.is_silent(loud)
    assert voice.is_noise("Share this video with your friends on social media.")
    assert voice.is_noise("Thanks for watching!")
    assert not voice.is_noise("Can you send me the deck by four?")
