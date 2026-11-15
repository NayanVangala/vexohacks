"""Conversation Mode: wake commands, auto-start/silence end, live face matching,
live tasks with dedup, and the /conversation endpoints.

Models are faked (no keys, no network): embed = bag of words, identify_face =
whatever the test sets. Run: .venv/bin/python -m pytest -q test_conversation.py
"""

import os
import re
import tempfile
import time
import zlib
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
        v[zlib.crc32(w.encode()) % DIM] += n
    return v


def _reset_conv():
    memory.stop_conversation(reason="test")
    memory._conv_other_streak = 0
    memory._conv_last_chunk_at = None
    memory._conv_last_speech_at = None
    memory._seen_task_quotes.clear()
    memory._conv_transcript.clear()
    memory._ft_queue.clear()
    # These tests share one temp DB with test_memory.py when run together, and
    # test_memory's feed assertion expects only its own cards: remove exactly
    # what this file creates (Listening cards, the live-task test task, faces).
    with memory._db_lock:
        memory.db().execute("DELETE FROM cards")
        memory.db().execute("DELETE FROM memories WHERE kind='task' AND text LIKE 'Send the pitch deck to Sam%'")
        memory.db().execute("DELETE FROM memories WHERE kind='task' AND text LIKE 'Block out a time%'")
        memory.db().execute("DELETE FROM faces")
        memory.db().commit()


def test_wake_command_regex():
    for s in ["Scene, listen", "Hey Scene, take notes", "scene listen", "ok scene take notes"]:
        assert memory._conv_start_command(s), s
    for s in ["listen to some music", "take notes later", "hey scene where are my keys"]:
        assert not memory._conv_start_command(s), s


def test_conversation_start_stop_and_api(monkeypatch):
    monkeypatch.setattr(models, "embed", fake_embed)
    _reset_conv()
    c = TestClient(server.app)
    assert c.get("/conversation").json()["active"] is False
    r = c.post("/conversation/start").json()
    assert r["active"] is True and r["mode"] == "conversation" and r["started"] is True
    assert r["source"] == "api" and r["speaker"] is None
    assert c.get("/session").json()["conversation"]["active"] is True
    assert c.get("/settings").json()["conversation"]["active"] is True
    r = c.post("/conversation/stop").json()
    assert r["active"] is False and r["stopped"] is True
    assert c.get("/conversation").json()["active"] is False
    _reset_conv()


def test_auto_start_on_continuous_speech_and_silence_end(monkeypatch):
    monkeypatch.setattr(models, "embed", fake_embed)
    _reset_conv()
    memory._conversation_on_speech("we should send the deck by five pm today")
    assert memory._conv_active is False  # one chunk is not enough without a face
    memory._conversation_on_speech("and I will bring the printed copies tomorrow morning")
    assert memory._conv_active is True
    assert memory.conversation_state()["source"] == "auto"
    # 30 s of silence ends it (lazy expiry inside conversation_state).
    memory._conv_last_speech_at = time.time() - 31
    assert memory.conversation_state()["active"] is False
    _reset_conv()


def test_face_match_threshold_and_speaker(monkeypatch):
    monkeypatch.setattr(models, "embed", fake_embed)
    memory.enroll_face("Srijan", b"\xff\xd8\xff" + b"\x00" * 10)
    monkeypatch.setattr(models, "identify_face",
                        lambda frame, refs: {"name": "Srijan", "confidence": 0.9})
    assert memory.match_face(b"\xff\xd8\xff") == "Srijan"
    monkeypatch.setattr(models, "identify_face",
                        lambda frame, refs: {"name": "Srijan", "confidence": 0.5})
    assert memory.match_face(b"\xff\xd8\xff") is None  # unsure: never a guess
    monkeypatch.setattr(models, "identify_face",
                        lambda frame, refs: {"name": None, "confidence": 0.0})
    assert memory.match_face(b"\xff\xd8\xff") is None


def test_live_task_created_once_and_30s_extract_dedups(monkeypatch):
    monkeypatch.setattr(models, "embed", fake_embed)
    monkeypatch.setattr(models, "extract_tasks", lambda text, receipts=(): (
        {"tasks": [{"text": "Send the pitch deck to Sam", "from_whom": "Sam", "due": "5pm",
                    "quote": "send me the pitch deck by 5pm"}], "done": [], "owed_to_me": []}))
    _reset_conv()
    chunk = "hey can you send me the pitch deck by 5pm"
    memory._live_extract_chunk(chunk, "Srijan")
    memory._live_extract_chunk(chunk, "Srijan")  # same chunk twice: still one task
    memory._process_transcript(chunk)  # the 30 s extract must not duplicate it
    tasks = memory.open_tasks()
    assert [t["text"] for t in tasks] == ["Send the pitch deck to Sam (from Sam, due 5pm)"]
    assert tasks[0]["speaker"] == "Sam"
    _reset_conv()


def test_live_extract_full_transcript_enriches_and_drops_fragments(monkeypatch):
    """2026-11-14 log: 'Sit down.' became a task, and 'block out a time...' was
    created before 'Tuesday at 5 p.m. for math class' arrived in later chunks,
    so the task had no time. The live extract now runs on the whole conversation
    transcript, fragments are dropped, and a later extract enriches the same task
    in place instead of duplicating it."""
    monkeypatch.setattr(models, "embed", fake_embed)

    def fake_extract(text, receipts=()):
        tasks = []
        if "sit down" in text.lower():
            tasks.append({"text": "Sit down", "from_whom": "", "due": "", "quote": "Sit down."})
        if "block out a time" in text:
            if "Tuesday at 5 p.m. for math class" in text:
                tasks.append({"text": "Block out a time in your Google Calendar on Tuesday at 5 p.m. for math class",
                              "from_whom": "Srijan", "due": "Tuesday at 5 p.m.",
                              "quote": "block out a time in your Google Calendar on Tuesday at 2 p.m. "
                                       "Actually, no. Make it Tuesday at 5 p.m. for math class."})
            else:
                tasks.append({"text": "Block out a time in your Google Calendar",
                              "from_whom": "Srijan", "due": "",
                              "quote": "block out a time in your Google Calendar"})
        return {"tasks": tasks, "done": [], "owed_to_me": []}

    monkeypatch.setattr(models, "extract_tasks", fake_extract)
    _reset_conv()
    # Chunk 1: the request, no time yet. 'Sit down.' is in the same transcript.
    memory._live_extract_chunk("Sit down. block out a time in your Google Calendar", "Srijan")
    tasks = memory.open_tasks()
    assert [t["text"] for t in tasks] == ["Block out a time in your Google Calendar (from Srijan)"]
    # Chunks 2-3: the time, corrected from 2 p.m. to 5 p.m.
    full = ("Sit down. block out a time in your Google Calendar on Tuesday at 2 p.m. "
            "Actually, no. Make it Tuesday at 5 p.m. for math class.")
    memory._live_extract_chunk(full, "Srijan")
    tasks = memory.open_tasks()
    assert len(tasks) == 1  # enriched in place, not duplicated
    assert tasks[0]["text"] == ("Block out a time in your Google Calendar on Tuesday at 5 p.m. "
                                "for math class (from Srijan, due Tuesday at 5 p.m.)")
    assert tasks[0]["speaker"] == "Srijan"
    assert "Sit down" not in [t["text"] for t in tasks]
    _reset_conv()


def test_ft_voice_question_waits_during_conversation(monkeypatch):
    spoken = []
    monkeypatch.setattr(voice, "speak", spoken.append)
    monkeypatch.setattr(models, "embed", fake_embed)

    def fake_ft(method, path, **kw):
        if path.endswith("/question"):
            return {"text": "Want me to send Nayan the deck by four?"}
        return {"answer": "yes", "item": {"result": "Draft link only. Nothing was sent."}}
    monkeypatch.setattr(memory, "_ft", fake_ft)
    _reset_conv()
    memory.start_conversation(source="wake")
    memory._ft_enqueue([{"id": "c1", "status": "pending", "title": "Send Nayan the deck"}])
    assert spoken == [] and len(memory._ft_queue) == 1  # queued, never asked aloud
    memory.stop_conversation(reason="test")
    memory._ft_ask_next()
    assert spoken == ["Want me to send Nayan the deck by four?"]
    _reset_conv()
    memory._ft_waiting = None
    memory._ft_queue.clear()
