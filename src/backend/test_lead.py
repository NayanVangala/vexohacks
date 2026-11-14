"""Lead me there: offer after a found object, yes starts it, steps are spoken, arrival or stop ends it. No network."""
import os
import tempfile
from pathlib import Path

os.environ.setdefault("SCENE_DATA_DIR", tempfile.mkdtemp())

import pytest  # noqa: E402

import lead  # noqa: E402
import memory  # noqa: E402
import models  # noqa: E402
import voice  # noqa: E402

BOTTLE = {"name": "water bottle", "place": "on the white table next to the laptop", "frame": "t.jpg",
          "frame_path": None, "age_min": 5}


@pytest.fixture
def env(monkeypatch, tmp_path):
    spoken = []
    monkeypatch.setattr(voice, "speak", lambda t: spoken.append(t))
    monkeypatch.setattr(models, "guide", lambda *a, **k: {"arrived": False, "instruction": "Turn left toward the window."})
    lead.stop()
    f = tmp_path / "now.jpg"
    f.write_bytes(b"jpg")
    yield spoken, f
    lead.stop()


def start(now=1000.0):
    lead.offer(BOTTLE, "On the table. Want me to lead you there?", now)
    return now + 30        # past the voice's own talking time, inside the answer window


def test_own_voice_and_chatter_never_answer_the_offer(env):
    lead.offer(BOTTLE, "On the table. Want me to lead you there?", 1000.0)
    assert lead.try_answer("yes", 1001.0) == "ignore"           # Scene's own sentence still playing
    assert lead.try_answer("so anyway the meeting went long today", 1008.0) is None
    assert not lead.active()


def test_yes_starts_no_declines_and_late_answers_expire(env):
    assert lead.try_answer("yeah please", start()) == "start" and lead.active()
    lead.stop()
    assert lead.try_answer("no thanks", start()) == "declined" and lead.mode() is None
    lead.offer(BOTTLE, "x", 1000.0)
    assert lead.try_answer("yes", 5000.0) is None and lead.mode() is None


def test_step_speaks_once_per_new_instruction_then_arrives(env, monkeypatch):
    spoken, frame = env
    t = start()
    lead.try_answer("yes", t)
    said = []
    assert lead.due(t) and not lead.due(t)                       # one look in flight at a time
    lead.step(frame, lambda text, arrived: said.append((text, arrived)), t)
    assert said == [("Turn left toward the window.", False)]
    assert not lead.due(t + 0.5)                                 # paced: not again right away
    lead.due(t + 20)                                             # same instruction again: stay quiet
    lead.step(frame, lambda text, arrived: said.append((text, arrived)), t + 20)
    assert len(said) == 1
    monkeypatch.setattr(models, "guide", lambda *a, **k: {"arrived": True, "instruction": "There it is, on your left."})
    lead.due(t + 40)
    lead.step(frame, lambda text, arrived: said.append((text, arrived)), t + 40)
    assert said[-1] == ("There it is, on your left.", True) and not lead.active()


def test_stop_ends_it_and_gives_up_after_three_minutes(env):
    spoken, frame = env
    t = start()
    lead.try_answer("yes", t)
    assert lead.wants_stop("okay stop") and lead.wants_stop("never mind")
    said = []
    lead.due(t + lead.MAX_S + 1)
    lead.step(frame, lambda text, arrived: said.append(text), t + lead.MAX_S + 1)
    assert "can't find it" in said[0] and not lead.active()


def test_memory_offers_only_for_recent_sightings_with_a_photo(env):
    assert memory._can_lead({"target": BOTTLE})
    assert not memory._can_lead({"target": {**BOTTLE, "age_min": 600}})
    assert not memory._can_lead({"target": {**BOTTLE, "frame": None}})
    assert not memory._can_lead({"target": None})


def test_voice_flow_offer_then_yes_then_stop(env, monkeypatch):
    spoken, _ = env
    monkeypatch.setattr(memory, "push_card", lambda *a, **k: {})
    memory._lead_audio("hey scene")            # no offer yet: not ours
    lead.offer(BOTTLE, "x", 1.0)               # long ago: window closed
    assert memory._lead_audio("yes") is False or not lead.active()
    lead.stop()
    lead.offer(BOTTLE, "x", __import__("time").time() - 20)
    assert memory._lead_audio("yes") and lead.active() and "tell you which way" in spoken[-1]
    assert memory._lead_audio("stop") and not lead.active()
