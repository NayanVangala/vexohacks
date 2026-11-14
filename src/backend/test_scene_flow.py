"""Scene flow: frames -> awareness -> offer card + voice -> notes session -> notes card. No network."""
import os
import tempfile
from pathlib import Path

os.environ.setdefault("SCENE_DATA_DIR", tempfile.mkdtemp())

import pytest  # noqa: E402

import awareness  # noqa: E402
import memory  # noqa: E402
import models  # noqa: E402
import notes  # noqa: E402
import voice  # noqa: E402

MEETING = {"type": "meeting", "confidence": 0.9, "text": ""}
SLIDE = {"type": "slides", "confidence": 0.9, "text": "Q4 roadmap: ship glasses app, launch payments"}


@pytest.fixture
def env(monkeypatch):
    rows, spoken = [], []
    monkeypatch.setattr(memory, "add", lambda kind, text, place=None, frame=None, status=None:
                        rows.append({"id": len(rows) + 1, "kind": kind, "text": text, "status": status}) or len(rows))
    monkeypatch.setattr(voice, "speak", lambda text: spoken.append(text))
    monkeypatch.setattr(memory, "awareness", awareness.Awareness())
    monkeypatch.setattr(memory, "DATA_DIR", Path(tempfile.mkdtemp()))
    memory._cards.clear()
    notes.stop()
    yield rows, spoken
    notes.stop()


def cards(kind):
    return [c for c in memory.cards_after(0) if c["kind"] == kind]


def three(scene):
    for _ in range(3):
        memory.handle_scene(scene, "f.jpg")


def test_three_meeting_frames_push_one_offer_card_and_speak_once(env):
    rows, spoken = env
    for _ in range(40):
        memory.handle_scene(MEETING, "f.jpg")
    assert len(cards("offer")) == 1 and len(spoken) == 1 and "take notes" in spoken[0]
    assert cards("offer")[0]["ref"] == memory.awareness.pending(__import__("time").time()).id


def test_accept_starts_notes_and_decline_does_not(env):
    three(MEETING)
    oid = cards("offer")[0]["ref"]
    assert memory.answer_offer(oid, False) == {"offer": oid, "accepted": False} and not notes.active()
    memory.awareness = awareness.Awareness()
    memory._cards.clear()
    three(MEETING)
    oid = cards("offer")[0]["ref"]
    memory.answer_offer(oid, True)
    assert notes.active()


def test_answer_offer_unknown_or_twice_raises_keyerror(env):
    with pytest.raises(KeyError):
        memory.answer_offer(999, True)
    three(MEETING)
    oid = cards("offer")[0]["ref"]
    memory.answer_offer(oid, True)
    with pytest.raises(KeyError):
        memory.answer_offer(oid, True)
    assert notes.active()                               # the second call did not restart anything


def audio(monkeypatch, text, tmp_path=Path(tempfile.mkdtemp())):
    monkeypatch.setattr(models, "transcribe", lambda wav: text)
    p = tmp_path / "a.wav"
    p.write_bytes(b"RIFF....WAVE")
    memory.process_audio(p)


def test_spoken_yes_answers_pending_offer(env, monkeypatch):
    three(MEETING)
    audio(monkeypatch, "yeah sure")
    assert notes.active()


@pytest.mark.parametrize("said", ["maybe", "yes no", "so yes the budget was approved by finance last week"])
def test_spoken_maybe_or_long_sentence_does_not_answer(env, monkeypatch, said):
    three(MEETING)
    audio(monkeypatch, said)
    assert not notes.active()


def test_stop_notes_phrase_finishes_and_pushes_notes_card(env, monkeypatch):
    rows, _ = env
    monkeypatch.setattr(models, "make_notes", lambda t, s: {
        "summary": "Roadmap review.", "decisions": ["Ship Friday"],
        "action_items": [{"text": "Send budget to Priya", "quote": "I will send the budget to Priya"},
                         {"text": "Book offsite", "quote": "we should book the offsite in March"}]})
    three(MEETING)
    memory.answer_offer(cards("offer")[0]["ref"], True)
    memory.handle_scene(SLIDE, "s1.jpg")
    audio(monkeypatch, "Sam: I will send the budget to Priya by Friday")
    audio(monkeypatch, "ok stop notes")
    assert not notes.active()
    assert [c["title"] for c in cards("task")] == ["New task"] and "Send budget to Priya" in cards("task")[0]["body"]
    assert len(cards("task")) == 1                      # the invented item was dropped
    n = cards("notes")[0]
    assert n["body"] == "Roadmap review." and memory.get_notes(n["ref"])["slides"][0]["frame"] == "s1.jpg"


def test_frame_without_scene_key_does_not_crash(env, monkeypatch, tmp_path):
    monkeypatch.setattr(models, "see", lambda jpeg: {"objects": [], "activity": ""})
    p = tmp_path / "f.jpg"
    p.write_bytes(b"\xff\xd8\xff\xe0")
    memory.process_frame(p)
    three(MEETING)
    memory.answer_offer(cards("offer")[0]["ref"], True)
    memory.process_frame(p)                             # still fine mid-session
    assert notes.active()


def test_get_notes_unknown_is_none(env):
    assert memory.get_notes(12345) is None


def test_email_action_item_pushes_email_card_not_task_card(env, monkeypatch):
    import emailer
    emailer._drafts.clear()
    monkeypatch.setenv("DEMO_EMAIL_TO", "ultra@example.com")
    monkeypatch.setattr(models, "make_notes", lambda t, s: {"summary": "Sync.", "decisions": [], "action_items": [
        {"text": "Send budget to Priya", "quote": "I will send the budget to Priya", "action": "email",
         "to": "Priya", "subject": "Budget", "body": "Hi Priya, budget on Friday."}]})
    three(MEETING)
    memory.answer_offer(cards("offer")[0]["ref"], True)
    audio(monkeypatch, "Sam: I will send the budget to Priya by Friday")
    audio(monkeypatch, "stop notes")
    assert cards("task") == []
    card = cards("email")[0]
    assert card["title"] == "Email Priya?" and "Budget" in card["body"]
    d = emailer.get(card["ref"])
    assert d["status"] == "proposed" and d["to"] == "ultra@example.com"      # nothing sent yet
