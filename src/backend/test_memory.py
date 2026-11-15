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

    no_match = c.post("/ask", data={"text": "zebra migration"}).json()
    assert no_match["answer"] == ("No match in your memories, tasks, or events. "
                                  "Want me to book, email, or schedule it instead?")
    assert no_match["no_match"] is True and no_match["checked"] == ["objects", "tasks", "events"]
    assert no_match["agent_offer"] is True and no_match["target"] is None
    t = c.get("/today").json()
    assert t["memories"] >= 3 and len(t["hourly"]) == 24 and sum(t["hourly"]) == t["memories"]
    assert c.post("/tasks/999999/done").status_code == 404
    assert c.post("/ask", data={"text": "  "}).status_code == 422


def test_wake_word_answers_aloud(monkeypatch):
    spoken = []
    monkeypatch.setattr(voice, "speak", spoken.append)
    chimes = []
    monkeypatch.setattr(voice, "chime", lambda: chimes.append(1))
    monkeypatch.setattr(voice, "_cooldown_until", 0.0)
    monkeypatch.setattr(memory, "ask", lambda q: {"question": q, "answer": "On the kitchen counter.",
                                                  "memory_id": 1, "frame": "123.jpg"})
    said = iter(["Hey Scene,", "where did I leave my keys?", "Thank you."])
    monkeypatch.setattr(models, "transcribe", lambda wav, names=(), context="", items=(): next(said))
    before = len(memory._transcript)
    last_card = max([c["id"] for c in memory.cards_after(0)] or [0])

    wav = Path(tempfile.mkdtemp()) / "a.wav"
    wav.write_bytes(b"RIFF")
    for _ in range(3):
        memory.process_audio(wav)

    assert spoken == ["On the kitchen counter."]
    assert chimes == [1]          # pinged once, when "Hey Scene" was heard
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
    quiet = b"RIFF" + b"\0" * 40 + struct.pack("<1000h", *([3, -2] * 500))
    loud = b"RIFF" + b"\0" * 40 + struct.pack("<1000h", *([20, -18] * 500))   # quiet speech at a table
    assert voice.is_silent(quiet) and not voice.is_silent(loud)
    assert voice.is_noise("Share this video with your friends on social media.")
    assert voice.is_noise("Thanks for watching!")
    assert not voice.is_noise("Can you send me the deck by four?")


def test_followthrough_call_and_response(monkeypatch):
    spoken, calls = [], []
    monkeypatch.setattr(voice, "speak", spoken.append)

    def fake_ft(method, path, **kw):
        calls.append((method, path, kw.get("json")))
        if path.endswith("/question"):
            return {"text": "Want me to send Nayan the deck by four?"}
        return {"answer": "yes", "item": {"result": "Draft link only. Nothing was sent."}}
    monkeypatch.setattr(memory, "_ft", fake_ft)
    monkeypatch.setattr(memory, "_ft_waiting", None)
    memory._ft_queue.clear()

    memory._ft_enqueue([{"id": "a1", "status": "pending", "title": "Send Nayan the deck"},
                        {"id": "b2", "status": "needs_review", "title": "Buy coffee"}])   # unverified: never asked
    assert spoken == ["Want me to send Nayan the deck by four?"]

    assert memory._ft_try_answer("Want me to send Nayan the deck") is True   # our own voice: swallowed
    assert not any(p.endswith("/voice") for _, p, _ in calls)

    memory._ft_waiting["listen_from"] = 0          # the laptop has finished talking
    assert memory._ft_try_answer("let's talk about the venue tomorrow") is False   # not an answer
    assert memory._ft_try_answer("yeah go ahead") is True
    assert ("POST", "/api/items/a1/voice", {"heard": "yeah go ahead"}) in calls
    assert spoken[-1] == "Done." and memory._ft_waiting is None

    memory._ft_enqueue([{"id": "a1", "status": "pending", "title": "Send Nayan the deck"}])   # asked once only
    assert len([s for s in spoken if s.startswith("Want me")]) == 1


def test_ft_skip_is_later_not_a_dismiss(monkeypatch):
    """'skip' to a Follow-Through question means ask later, never dismiss: no /voice call,
    no Skipped card, and the voice loop moves on. (The bug: 'skip' parsed as a hard no, so an
    already-approved promise ended up 'Skipped'.)"""
    spoken, calls = [], []
    monkeypatch.setattr(voice, "speak", spoken.append)
    monkeypatch.setattr(memory, "_ft_waiting", None)
    memory._ft_queue.clear()

    def fake_ft(method, path, **kw):
        calls.append((method, path, kw.get("json")))
        if path.endswith("/question"):
            return {"text": "Want me to send Nayan the deck by four?"}
        if path == "/api/state":
            return {"items": [{"id": "c3", "status": "pending", "title": "Send Nayan the deck"}]}
        return {"answer": "yes", "item": {"result": "Draft link only. Nothing was sent."}}
    monkeypatch.setattr(memory, "_ft", fake_ft)

    memory._ft_enqueue([{"id": "c3", "status": "pending", "title": "Send Nayan the deck"}])
    assert spoken == ["Want me to send Nayan the deck by four?"]

    memory._ft_waiting["listen_from"] = 0          # the laptop has finished talking
    assert memory._ft_try_answer("skip") is True
    assert not any(p.endswith("/voice") for _, p, _ in calls)
    assert spoken[-1] == "Okay, later."
    assert memory._ft_waiting is None


def test_ft_tap_cancels_voice_question_and_already_handled_says_why(monkeypatch):
    spoken = []
    monkeypatch.setattr(voice, "speak", spoken.append)

    # A tap on the phone (via the /ft proxy) stops the voice loop asking about that item.
    monkeypatch.setattr(memory, "_ft_waiting", {"id": "c3", "title": "Send Nayan the deck",
                                                "listen_from": 0, "until": 1e18})
    memory._ft_queue.clear()
    memory._ft_queue.append({"id": "c3", "status": "pending", "title": "Send Nayan the deck"})
    memory.ft_cancel("c3")
    assert memory._ft_waiting is None and len(memory._ft_queue) == 0
    assert "c3" in memory._ft_asked

    # A stale question about an already-approved item says why instead of acting on it.
    def fake_ft(method, path, **kw):
        if path == "/api/state":
            return {"items": [{"id": "c3", "status": "done", "title": "Send Nayan the deck"}]}
        raise AssertionError(f"should not call {method} {path}")
    monkeypatch.setattr(memory, "_ft", fake_ft)
    last_card = max([c["id"] for c in memory.cards_after(0)] or [0])
    monkeypatch.setattr(memory, "_ft_waiting", {"id": "c3", "title": "Send Nayan the deck",
                                                "listen_from": 0, "until": 1e18})
    assert memory._ft_try_answer("yeah go ahead") is True
    assert spoken[-1] == "That's already handled."
    assert any(c["title"].startswith("Already handled") for c in memory.cards_after(last_card))
    assert memory._ft_waiting is None


def test_whisper_room_noise_is_dropped():
    for junk in ["Thank you very much.", "Thank you. Bye. Bye.", "😋", "谢谢观看 欢迎订阅我的频道", "시청 해주셔서 감사합니다.", "Thanks."]:
        assert voice.is_noise(junk), junk
    for real in ["Hey scene, where did I leave my headphones?", "Where are my headphones?", "Yep, send it by four."]:
        assert not voice.is_noise(real), real


def test_question_to_scene_without_wake_word():
    q = memory._QUESTION_TO_SCENE
    for s in ["Where are my headphones?", "where did I leave my keys?", "What did Sam ask me to do?", "Did I pay the venue?"]:
        assert q.search(s), s
    for s in ["Can you send me the deck by four?", "Where are we going for lunch?", "I left my keys there."]:
        assert not q.search(s), s


# ---- direct orders: "order a Big Mac from McDonald's at 8pm from the Newark location" ----

def test_parse_order_big_mac_newark_8pm():
    p = memory._parse_order("hey scene, order a Big Mac from McDonald's at 8pm from the Newark location")
    assert p is not None
    assert p["restaurant"] == "McDonald's" and p["items"] == [{"name": "Big Mac", "qty": 1}]
    assert p["city"] == "Newark" and p["time_label"] == "8:00 PM" and p["start"].endswith("T20:00")


def test_parse_order_quantities_and_no_time():
    p = memory._parse_order("order two Big Macs and fries from McDonald's")
    assert p is not None
    assert p["items"] == [{"name": "Big Macs", "qty": 2}, {"name": "fries", "qty": 1}]
    assert p["restaurant"] == "McDonald's" and p["city"] is None and p["time_label"] is None
    # ...and the plural still matches the menu's singular "Big Mac" (see test_agent).
    assert memory.agent_mod._match("Big Macs", [{"name": "Big Mac"}])["name"] == "Big Mac"


def test_parse_order_incomplete_returns_none():
    assert memory._parse_order("order a Big Mac") is None          # no restaurant
    assert memory._parse_order("order from McDonald's") is None    # no item
    assert memory._parse_order("what did the boss say about lunch?") is None
    assert memory._parse_order("order 2 big macs") is None        # "2" is a quantity, not a place


def _order_rig(monkeypatch):
    """Two Newark McDonald's (0.8 and 2.1 mi) plus a nearer Fremont one: the city
    filter must leave the Newark pair, and the pick must be the 0.8 mi one."""
    spoken = []
    monkeypatch.setattr(voice, "speak", spoken.append)
    monkeypatch.setattr(voice, "chime_out", lambda: None)
    monkeypatch.setattr(memory, "get_location", lambda: (37.53, -121.99))
    places = [{"name": "McDonald's", "address": "35200 Newark Blvd, Newark, CA", "distance_mi": 0.8, "open_now": True},
              {"name": "McDonald's", "address": "3900 Newark Blvd, Newark, CA", "distance_mi": 2.1, "open_now": True},
              {"name": "McDonald's", "address": "1 Fremont St, Fremont, CA", "distance_mi": 0.3, "open_now": True}]
    monkeypatch.setattr(memory.lookup, "find_nearby", lambda q, w: list(places))
    menu = [{"id": "big-mac", "name": "Big Mac", "price_cents": 899, "calories": 580, "protein_g": 25}]
    monkeypatch.setattr(memory.lookup, "get_menu", lambda place, wanted="": menu)
    import orders
    monkeypatch.setattr(orders, "propose",
                        lambda place, lines: {"id": 7, "total_cents": 899, "place_name": place["name"],
                                              "address": place["address"], "items": lines})
    return spoken


def test_order_direct_picks_closest_newark_location(monkeypatch):
    spoken = _order_rig(monkeypatch)
    last_card = max([c["id"] for c in memory.cards_after(0)] or [0])
    assert memory._order_direct("hey scene, order a Big Mac from McDonald's at 8pm from the Newark location",
                                wake=True) is True
    cards = [c for c in memory.cards_after(last_card) if c["kind"] == "order"]
    assert len(cards) == 1
    assert "35200 Newark Blvd" in cards[0]["body"] and "closest to you (0.8 mi)" in cards[0]["body"]
    assert "for 8:00 PM" in cards[0]["body"] and cards[0]["ref"] == 7
    assert "Approve it on your glasses" in spoken[-1]


def test_order_direct_falls_through_without_location_or_menu_match(monkeypatch):
    spoken = _order_rig(monkeypatch)
    # No GPS and no city in the request: no reference point to pick the closest from.
    monkeypatch.setattr(memory, "get_location", lambda: None)
    assert memory._order_direct("hey scene, order a Big Mac from McDonald's at 8pm", wake=True) is False
    # The item is not on the menu: the agent asks instead of inventing it.
    monkeypatch.setattr(memory, "get_location", lambda: (37.53, -121.99))
    monkeypatch.setattr(memory.lookup, "get_menu", lambda place, wanted="": [])
    assert memory._order_direct("hey scene, order a Big Mac from McDonald's at 8pm from the Newark location",
                                wake=True) is False
    assert spoken == []  # nothing was spoken or drafted on either fall-through


# ---- display focus: 'tell me more about this' ----

def _focus_item(**over):
    item = {"id": "ft-1", "kind": "promise", "title": "Send Srijan the deck by four",
            "quote": "send Srijan the deck by four", "speaker": "Srijan", "status": "pending",
            "action": "email", "due_local": "4:00 PM", "body": "Draft an email to Srijan.",
            "context": ["Srijan: send me the deck by four"]}
    item.update(over)
    return item


def test_focus_set_get_expires():
    memory.clear_focus()
    assert memory.get_focus() is None
    f = memory.set_focus(_focus_item())
    assert f["id"] == "ft-1" and memory.get_focus()["title"].startswith("Send Srijan")
    memory._focus["ts"] -= 301  # 5 min passed: expired, never a stale 'this'
    assert memory.get_focus() is None
    memory.clear_focus()


def test_tell_me_more_about_focused_item(monkeypatch):
    spoken = []
    monkeypatch.setattr(voice, "speak", spoken.append)
    monkeypatch.setattr(voice, "chime_out", lambda: None)
    memory.clear_focus()
    memory.set_focus(_focus_item())
    last_card = max([c["id"] for c in memory.cards_after(0)] or [0])
    assert memory.dining_turn("hey scene, can you tell me more about this?", wake=True) is True
    assert len(spoken[0].split()) <= 30  # spoken line is short by design
    assert "Srijan" in spoken[0] and "pending" in spoken[0]
    [card] = memory.cards_after(last_card)
    assert "send Srijan the deck by four" in card["body"]  # exact quote on the card
    assert "Draft an email" in card["body"] and "Approving it drafts the email" in card["body"]
    memory.clear_focus()


def test_tell_me_more_without_focus_says_so(monkeypatch):
    spoken = []
    monkeypatch.setattr(voice, "speak", spoken.append)
    monkeypatch.setattr(voice, "chime_out", lambda: None)
    memory.clear_focus()
    assert memory.dining_turn("hey scene, tell me more about this", wake=True) is True
    assert spoken[0] == "I am not sure which one you mean. Open one on the display first."


def test_tell_me_more_needs_this_and_wake(monkeypatch):
    spoken = []
    monkeypatch.setattr(voice, "speak", spoken.append)
    monkeypatch.setattr(voice, "chime_out", lambda: None)
    memory.clear_focus()
    memory.set_focus(_focus_item())
    # 'this restaurant' names something else: not the focused item, fall through.
    assert memory._tell_me_more_direct("tell me more about this restaurant") is False
    # No wake word: a conversation mentioning it is not a command.
    assert memory.dining_turn("tell me more about this", wake=False) is False
    assert spoken == []
    memory.clear_focus()


def test_focus_endpoints():
    c = TestClient(server.app)
    memory.clear_focus()
    assert c.get("/focus").json() == {"focus": None}
    r = c.post("/focus", json=_focus_item())
    assert r.status_code == 200 and r.json()["focus"]["id"] == "ft-1"
    assert c.get("/focus").json()["focus"]["title"].startswith("Send Srijan")
    assert c.post("/focus", json={"title": "no id"}).status_code == 422
    assert c.post("/focus/clear").json() == {"focus": None}
    assert c.get("/focus").json() == {"focus": None}
