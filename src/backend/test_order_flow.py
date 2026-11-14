"""Wiring: spoken dining requests start the agent, replies resume it, order routes and the order page."""
import os
import tempfile

os.environ.setdefault("SCENE_DATA_DIR", tempfile.mkdtemp())

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import agent as agent_mod  # noqa: E402
import memory  # noqa: E402
import models  # noqa: E402
import orders  # noqa: E402
import payments  # noqa: E402
import server  # noqa: E402
import voice  # noqa: E402

c = TestClient(server.app)


class FakeAgent:
    def __init__(self, status="idle"):
        self.status, self.started, self.replies = status, [], []

    def state(self):
        return {"status": self.status, "question": "q?" if self.status == "waiting" else None, "session": 1}

    def start(self, text):
        self.started.append(text)

    def reply(self, text):
        if self.status != "waiting":
            raise KeyError("no")
        self.replies.append(text)


def audio(monkeypatch, text, tmp=tempfile.mkdtemp()):
    from pathlib import Path
    monkeypatch.setattr(models, "transcribe", lambda wav: text)
    p = Path(tmp) / "a.wav"
    p.write_bytes(b"RIFF....WAVE")
    memory.process_audio(p)


@pytest.fixture(autouse=True)
def quiet(monkeypatch):
    monkeypatch.setattr(voice, "speak", lambda t: None)
    monkeypatch.setattr(memory, "ask", lambda q: pytest.fail("dining question must not go to memory.ask"))
    voice._cooldown_until = 0.0


def test_dining_question_starts_agent_not_memory_ask(monkeypatch):
    fake = FakeAgent()
    monkeypatch.setattr(memory, "agent", fake)
    audio(monkeypatch, "hey scene can you place an order at mcdonalds")
    assert fake.started and "order at mcdonalds" in fake.started[0]


def test_reply_routes_to_waiting_agent_only(monkeypatch):
    fake = FakeAgent("waiting")
    monkeypatch.setattr(memory, "agent", fake)
    audio(monkeypatch, "yeah that one is fine")
    assert fake.replies == ["yeah that one is fine"]
    idle = FakeAgent("idle")
    monkeypatch.setattr(memory, "agent", idle)
    audio(monkeypatch, "we should talk about the budget next week")
    assert idle.replies == [] and idle.started == []


def test_location_expires_and_env_fallback(monkeypatch):
    monkeypatch.delenv("SCENE_LOCATION", raising=False)
    memory._loc = None
    assert memory.get_location() is None
    monkeypatch.setenv("SCENE_LOCATION", "37.7,-122.4")
    assert memory.get_location() == (37.7, -122.4)
    memory.set_location(40.0, -74.0)
    assert memory.get_location() == (40.0, -74.0)
    memory._loc = (40.0, -74.0, memory._loc[2] - 1801)
    assert memory.get_location() == (37.7, -122.4)          # fix expired: back to the env value
    memory._loc = None


def test_location_and_agent_routes(monkeypatch):
    memory._loc = None
    assert c.post("/location", json={"lat": 1.5, "lng": 2.5}).status_code == 200
    assert memory.get_location() == (1.5, 2.5)
    assert c.post("/location", json={"lat": 999, "lng": 2}).status_code == 422
    monkeypatch.setattr(memory, "agent", FakeAgent("idle"))
    assert c.get("/agent").json()["status"] == "idle"
    assert c.post("/agent/reply", json={"text": "yes"}).status_code == 409
    monkeypatch.setattr(memory, "agent", FakeAgent("waiting"))
    assert c.post("/agent/reply", json={"text": "yes"}).status_code == 200
    memory._loc = None


def test_order_routes_409_and_404(monkeypatch):
    def boom(i):
        raise payments.PaymentError("order is paid")
    monkeypatch.setattr(orders, "approve", boom)
    assert c.post("/orders/1/approve").status_code == 409
    monkeypatch.setattr(orders, "reject", boom)
    assert c.post("/orders/1/reject").status_code == 409
    monkeypatch.setattr(orders, "refresh", lambda i: {"id": i})
    monkeypatch.setattr(orders, "status_view", lambda i: {"id": i, "stage": "ready", "items": [], "total_cents": 1})
    assert c.get("/orders/5").json()["stage"] == "ready"
    monkeypatch.setattr(orders, "refresh", lambda i: (_ for _ in ()).throw(payments.PaymentError("no such order")))
    assert c.get("/orders/9").status_code == 404


def test_order_page_served():
    r = c.get("/order/3")
    assert r.status_code == 200 and "Demo kitchen" in r.text and "text/html" in r.headers["content-type"]


def test_dev_say_and_location_require_flag(monkeypatch):
    monkeypatch.delenv("SCENE_DEV", raising=False)
    assert c.post("/dev/say", json={"text": "order food"}).status_code == 404
    assert c.post("/dev/location", json={"lat": 1, "lng": 2}).status_code == 404
    monkeypatch.setenv("SCENE_DEV", "1")
    fake = FakeAgent()
    monkeypatch.setattr(memory, "agent", fake)
    assert c.post("/dev/say", json={"text": "can you place an order at mcdonalds"}).status_code == 200
    assert fake.started
    assert c.post("/dev/location", json={"lat": 3.0, "lng": 4.0}).status_code == 200 and memory.get_location() == (3.0, 4.0)
    memory._loc = None


@pytest.mark.parametrize("said,starts", [
    ("hey scene email Priya about the budget", True), ("can you send an email to nayan", True),
    ("put this on my calendar for friday at 3", True), ("schedule a meeting tomorrow", True),
    ("search up the best go pro cameras online at the moment", True), ("look up flights to tokyo", True),
    ("remind me to call mom", True), ("can you place an order at mcdonalds", True),
    ("where are my keys", False), ("what did sam ask me to do", False), ("does anyone owe me money", False)])
def test_task_requests_start_the_agent_and_memory_questions_do_not(said, starts):
    fake = FakeAgent()
    import memory as m
    old, m.agent = m.agent, fake
    try:
        assert m.dining_turn(said, wake=True) is starts
    finally:
        m.agent = old
    assert bool(fake.started) is starts
