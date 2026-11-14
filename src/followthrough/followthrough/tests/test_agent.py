"""Tests with a fake model and a mocked OpenAI client. No API key, no network."""
import json
import httpx
import pytest
from fastapi.testclient import TestClient
from followthrough import agent, server
from followthrough.config import get_config
from followthrough.demo import DEMO_SEGMENTS, FakeLLM
from followthrough.executor import execute
from followthrough.llm import OpenAILLM, SCHEMA
from followthrough.models import Segment
from followthrough.store import Store, parse_text
from followthrough.transcribe import transcribe
from followthrough.verify import find_quote


# ---------- quote check ----------
def test_exact_quote_is_verified():
    p, issue = find_quote("I'll send you the deck by three", DEMO_SEGMENTS, hint=3)
    assert p.verified and issue is None and p.first_segment == 3 and p.start == 14

def test_ignores_case_and_punctuation():
    p, _ = find_quote("YEAH I'LL SEND YOU THE DECK BY THREE!", DEMO_SEGMENTS)
    assert p.verified

def test_paraphrase_is_rejected():
    p, issue = find_quote("I promise to buy the coffee next time", DEMO_SEGMENTS, hint=8)
    assert not p.verified and "not found" in issue

def test_too_short_quote_is_rejected():
    p, issue = find_quote("yeah", DEMO_SEGMENTS)
    assert not p.verified and "too short" in issue

def test_quote_split_across_two_segments():
    segs = [Segment(text="I'll send the deck"), Segment(text="to Sam by five")]
    p, _ = find_quote("send the deck to Sam by five", segs)
    assert p.verified and (p.first_segment, p.last_segment) == (0, 1)

def test_speaker_prefix_in_quote_is_tolerated():
    p, _ = find_quote("Srijan: I'll also call the venue about parking", DEMO_SEGMENTS)
    assert p.verified

def test_wrong_hint_still_finds_quote():
    p, _ = find_quote("I'll send you the deck by three", DEMO_SEGMENTS, hint=9)
    assert p.verified and p.first_segment == 3


# ---------- agent ----------
def test_agent_on_staged_conversation():
    items = agent.run(DEMO_SEGMENTS, FakeLLM(get_config()), get_config())
    by = {i.action: i for i in items}
    assert [i.status for i in items] == ["pending", "pending", "pending", "needs_review"]
    assert by["email"].changed_from == "3:00 PM" and by["email"].due_local.endswith("T16:00")
    assert by["reply"].status == "needs_review" and not by["reply"].proof.verified
    assert all(i.demo for i in items)

def test_email_recipient_is_forced():
    class Evil:
        name = "x"
        def extract(self, segs):
            return [{"action": "email", "title": "t", "owner": "", "counterpart": "", "quote": "I'll send you the deck by three",
                     "segment_index": 3, "due_local": "", "duration_min": 30, "changed_from": "",
                     "subject": "s", "body": "b", "to": "attacker@example.com"}]
    it = agent.run(DEMO_SEGMENTS, Evil(), get_config())[0]
    assert it.to == get_config().email_to

def test_calendar_without_time_needs_review():
    class L:
        name = "x"
        def extract(self, segs):
            return [{"action": "calendar", "title": "Sync", "owner": "", "counterpart": "", "quote": "tomorrow at ten for thirty minutes",
                     "segment_index": 5, "due_local": "tomorrow", "duration_min": 30, "changed_from": "", "subject": "", "body": ""}]
    it = agent.run(DEMO_SEGMENTS, L(), get_config())[0]
    assert it.status == "needs_review" and any("time" in s for s in it.issues)

def test_item_ids_are_stable_so_rerun_does_not_duplicate():
    a = agent.run(DEMO_SEGMENTS, FakeLLM(get_config()), get_config())
    b = agent.run(DEMO_SEGMENTS, FakeLLM(get_config()), get_config())
    assert [i.id for i in a] == [i.id for i in b]


# ---------- OpenAI wrapper with a mocked client ----------
class FakeClient:
    def __init__(self, content=None, refusal=None):
        self.calls = []
        self.content, self.refusal = content, refusal
        outer = self
        class Comp:
            def create(self, **kw):
                outer.calls.append(kw)
                msg = type("M", (), {"content": outer.content, "refusal": outer.refusal})()
                return type("R", (), {"choices": [type("C", (), {"message": msg})()]})()
        self.chat = type("Chat", (), {"completions": Comp()})()

def test_openai_llm_sends_schema_and_parses(monkeypatch):
    monkeypatch.setenv("OPENAI_MODEL", "my-model")
    cfg = get_config()
    c = FakeClient(content=json.dumps({"commitments": []}))
    assert OpenAILLM(cfg, client=c).extract(DEMO_SEGMENTS) == []
    kw = c.calls[0]
    assert kw["model"] == "my-model"
    assert kw["response_format"]["json_schema"]["strict"] is True
    assert "[3] (00:14-00:18) Srijan: Yeah" in kw["messages"][1]["content"]

def test_openai_llm_refusal_raises():
    with pytest.raises(RuntimeError):
        OpenAILLM(get_config(), client=FakeClient(refusal="no")).extract(DEMO_SEGMENTS)

def test_schema_is_strict_compatible():
    item = SCHEMA["properties"]["commitments"]["items"]
    assert set(item["required"]) == set(item["properties"]) and item["additionalProperties"] is False


# ---------- transcription with a mocked client ----------
def test_transcribe_offsets_segments():
    seg = lambda s, e, t: {"start": s, "end": e, "text": t}
    resp = {"segments": [seg(0, 2.5, " hello there"), seg(2.5, 5, "  "), seg(5, 7, "bye")]}
    client = type("C", (), {"audio": type("A", (), {"transcriptions": type("T", (), {
        "create": staticmethod(lambda **kw: resp)})()})()})()
    out = transcribe(b"x", "a.wav", 100.0, get_config(), client=client)
    assert [(s.start, s.end, s.text) for s in out] == [(100.0, 102.5, "hello there"), (105.0, 107.0, "bye")]


# ---------- executor honesty ----------
def _item(action="email"):
    items = agent.run(DEMO_SEGMENTS, FakeLLM(get_config()), get_config())
    return next(i for i in items if i.action == action)

def _cfg(monkeypatch, url="https://exec.test/x"):
    monkeypatch.setenv("EXECUTOR_URL", url)
    monkeypatch.setenv("EXECUTOR_TOKEN", "secret")
    return get_config()

def test_no_executor_means_drafted_never_done(monkeypatch):
    monkeypatch.delenv("EXECUTOR_URL", raising=False)
    out = execute(_item("email"), get_config(), [])
    assert out.status == "drafted" and "mail.google.com" in out.link and "Nothing was sent" in out.result
    cal = execute(_item("calendar"), get_config(), [])
    assert cal.status == "drafted" and "calendar.google.com" in cal.link

def test_executor_success(monkeypatch):
    seen = {}
    def handler(req):
        seen.update(json.loads(req.content))
        return httpx.Response(200, json={"ok": True, "did": "Sent email"})
    out = execute(_item("email"), _cfg(monkeypatch), [], httpx.Client(transport=httpx.MockTransport(handler)))
    assert out.status == "done" and out.result == "Sent email"
    assert seen["token"] == "secret" and seen["to"] == get_config().email_to

def test_executor_calendar_sends_offset_times(monkeypatch):
    seen = {}
    def handler(req):
        seen.update(json.loads(req.content)); return httpx.Response(200, json={"ok": True, "did": "Event created"})
    execute(_item("calendar"), _cfg(monkeypatch), [], httpx.Client(transport=httpx.MockTransport(handler)))
    assert seen["start"].endswith(("-08:00", "-07:00")) and seen["end"] > seen["start"]

def test_executor_refusal_and_crash_are_failed_not_done(monkeypatch):
    no = lambda req: httpx.Response(200, json={"ok": False, "error": "bad token"})
    out = execute(_item("email"), _cfg(monkeypatch), [], httpx.Client(transport=httpx.MockTransport(no)))
    assert out.status == "failed" and out.result == "bad token"
    def boom(req): raise httpx.ConnectError("down")
    out = execute(_item("email"), _cfg(monkeypatch), [], httpx.Client(transport=httpx.MockTransport(boom)))
    assert out.status == "failed"

def test_todo_and_reply(monkeypatch):
    todos = []
    t = execute(_item("todo"), get_config(), todos)
    assert t.status == "done" and todos == ["Call the venue about parking"]
    r = agent.make_item({"action": "reply", "title": "r", "quote": "Works for me, tomorrow at ten", "segment_index": 5,
                         "body": "ok"}, DEMO_SEGMENTS, get_config(), False)
    assert execute(r, get_config(), []).status == "drafted"


# ---------- full HTTP flow ----------
@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("EXECUTOR_URL", raising=False)
    monkeypatch.setattr(server, "store", Store(str(tmp_path)))
    return TestClient(server.app)

def test_flow_demo_run_approve(client):
    assert client.post("/api/demo/load").status_code == 200
    items = {i["action"]: i for i in client.post("/api/run").json()["items"]}
    assert items["reply"]["status"] == "needs_review"
    # a needs_review item can never be approved
    assert client.post(f"/api/items/{items['reply']['id']}/approve").status_code == 409
    # approve a verified item: no executor, so honest "drafted"
    r = client.post(f"/api/items/{items['email']['id']}/approve").json()
    assert r["status"] == "drafted" and r["link"]
    # approving twice is refused
    assert client.post(f"/api/items/{items['email']['id']}/approve").status_code == 409
    # todo really completes
    assert client.post(f"/api/items/{items['todo']['id']}/approve").json()["status"] == "done"
    assert client.get("/api/state").json()["todos"] == ["Call the venue about parking"]

def test_rerun_keeps_statuses_and_does_not_duplicate(client):
    client.post("/api/demo/load")
    a = client.post("/api/run").json()["items"]
    client.post(f"/api/items/{a[0]['id']}/dismiss")
    b = client.post("/api/run").json()["items"]
    assert len(a) == len(b) == 4 and b[0]["status"] == "dismissed"

def test_edit_fixes_missing_time_but_not_a_bad_quote(client):
    client.post("/api/demo/load")
    items = client.post("/api/run").json()["items"]
    reply = next(i for i in items if i["action"] == "reply")
    r = client.patch(f"/api/items/{reply['id']}", json={"body": "new text"}).json()
    assert r["status"] == "needs_review"          # editing never verifies a made up quote

def test_dismiss(client):
    client.post("/api/demo/load")
    it = client.post("/api/run").json()["items"][0]
    assert client.post(f"/api/items/{it['id']}/dismiss").json()["status"] == "dismissed"
    assert client.post(f"/api/items/{it['id']}/approve").status_code == 409

def test_real_transcript_without_key_is_refused_not_faked(client):
    client.post("/api/transcript", json={"text": "Sam: Can you send the report by five?\nMe: Sure, I will send it by five."})
    r = client.post("/api/run")
    assert r.status_code == 503 and "OPENAI_API_KEY" in r.json()["detail"]
    assert client.post("/api/transcribe", files={"audio": ("a.wav", b"x")}).status_code == 503

def test_real_transcript_with_mocked_model(client, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    out = {"commitments": [{"action": "todo", "title": "Send the report", "owner": "Me", "counterpart": "Sam",
                            "quote": "Sure, I will send it by five", "segment_index": 1, "due_local": "2026-11-14T17:00",
                            "duration_min": 30, "changed_from": "", "subject": "", "body": ""}]}
    monkeypatch.setattr("openai.OpenAI", lambda **kw: FakeClient(content=json.dumps(out)))
    client.post("/api/transcript", json={"text": "Sam: Can you send the report by five?\nMe: Sure, I will send it by five."})
    items = client.post("/api/run").json()["items"]
    assert len(items) == 1 and items[0]["status"] == "pending" and items[0]["demo"] is False

def test_state_survives_restart(client, tmp_path):
    client.post("/api/demo/load"); client.post("/api/run")
    again = Store(str(tmp_path))
    assert len(again.segments) == 10 and len(again.items) == 4

def test_parse_text():
    segs = parse_text("Sam: hello there\nno speaker line\n\n")
    assert segs[0].speaker == "Sam" and segs[0].text == "hello there" and segs[1].speaker == ""


def test_retry_only_for_failed(client, monkeypatch):
    client.post("/api/demo/load")
    items = {i["action"]: i for i in client.post("/api/run").json()["items"]}
    eid = items["email"]["id"]
    assert client.post(f"/api/items/{eid}/retry").status_code == 409          # pending cannot be retried
    monkeypatch.setenv("EXECUTOR_URL", "http://127.0.0.1:9/none")              # unreachable executor
    assert client.post(f"/api/items/{eid}/approve").json()["status"] == "failed"
    assert client.post(f"/api/items/{eid}/retry").json()["status"] == "pending"


# ---------- calendar titles and descriptions ----------
def test_calendar_event_has_specific_title_and_short_description(monkeypatch):
    seen = {}
    def handler(req):
        seen.update(json.loads(req.content)); return httpx.Response(200, json={"ok": True, "did": "ok"})
    execute(_item("calendar"), _cfg(monkeypatch), [], httpx.Client(transport=httpx.MockTransport(handler)))
    assert seen["title"] == "Demo sync with Nayan"
    assert seen["details"].startswith("Thirty minute sync with Nayan")
    assert 'Heard: "tomorrow at ten for thirty minutes"' in seen["details"]

@pytest.mark.parametrize("bad", ["", "Untitled", "Meeting", "  event. "])
def test_blank_or_generic_title_is_never_untitled(bad):
    raw = {"action": "calendar", "title": bad, "counterpart": "Nayan", "quote": "x", "summary": ""}
    assert agent.good_title(raw) == "Meeting with Nayan"
    raw["counterpart"] = ""
    raw["quote"] = "let's sync on the demo script tomorrow at ten"
    assert agent.good_title(raw).lower().startswith("let's sync on the demo")

def test_empty_summary_is_filled_in():
    assert agent.good_summary({"counterpart": "Nayan"}, "Demo sync with Nayan") == "Demo sync with Nayan (Nayan)."


# ---------- voice approval ----------
from followthrough import voice

@pytest.mark.parametrize("heard,want", [
    ("yes", "yes"), ("Yeah, go ahead.", "yes"), ("yep sure", "yes"), ("No.", "no"), ("nope, skip it", "no"),
    ("don't do it", "unclear"), ("yes no", "unclear"), ("", "unclear"), ("hmm maybe", "unclear"),
    ("I know", "unclear"), ("nothing", "unclear"),
])
def test_parse_answer_is_cautious(heard, want):
    assert voice.parse_answer(heard) == want

def test_question_mentions_what_will_happen(monkeypatch):
    monkeypatch.setenv("WEARER_NAME", "Srijan Saha")
    q = voice.question_for(_item("calendar"), get_config())
    assert q.startswith("Hey Srijan,") and "calendar" in q and "Say yes or no" not in q
    assert "email" in voice.question_for(_item("email"), get_config())

def test_speak_calls_elevenlabs_and_caches(monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k"); voice._cache.clear()
    calls = []
    def handler(req):
        calls.append(req); return httpx.Response(200, content=b"MP3DATA")
    c = httpx.Client(transport=httpx.MockTransport(handler))
    assert voice.speak("hi", get_config(), c) == b"MP3DATA"
    assert voice.speak("hi", get_config(), c) == b"MP3DATA" and len(calls) == 1
    req = calls[0]
    assert req.headers["xi-api-key"] == "k" and "/v1/text-to-speech/" in str(req.url)
    assert json.loads(req.content)["text"] == "hi" and json.loads(req.content)["model_id"]

def test_speak_without_key_raises(monkeypatch):
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    with pytest.raises(RuntimeError):
        voice.speak("hi", get_config())

def test_voice_route_yes_approves_only_on_clear_yes(client):
    client.post("/api/demo/load"); client.post("/api/run")
    items = client.get("/api/state").json()["items"]
    todo = next(i for i in items if i["action"] == "todo")
    r = client.post(f"/api/items/{todo['id']}/voice", json={"heard": "hmm maybe later"}).json()
    assert r["answer"] == "unclear" and r["item"]["status"] == "pending"
    r = client.post(f"/api/items/{todo['id']}/voice", json={"heard": "yes please"}).json()
    assert r["answer"] == "yes" and r["item"]["status"] == "done"
    assert client.post(f"/api/items/{todo['id']}/voice", json={"heard": "yes"}).status_code == 409

def test_voice_route_no_dismisses(client):
    client.post("/api/demo/load"); client.post("/api/run")
    cal = next(i for i in client.get("/api/state").json()["items"] if i["action"] == "calendar")
    r = client.post(f"/api/items/{cal['id']}/voice", json={"heard": "no"}).json()
    assert r["answer"] == "no" and r["item"]["status"] == "dismissed"

def test_cannot_voice_approve_unverified_item(client):
    client.post("/api/demo/load"); client.post("/api/run")
    bad = next(i for i in client.get("/api/state").json()["items"] if i["status"] == "needs_review")
    assert client.post(f"/api/items/{bad['id']}/voice", json={"heard": "yes"}).status_code == 409

def test_speech_route_without_key_is_503_with_question_available(client, monkeypatch):
    monkeypatch.delenv("ELEVENLABS_API_KEY", raising=False)
    client.post("/api/demo/load"); client.post("/api/run")
    it = client.get("/api/state").json()["items"][0]
    assert client.get(f"/api/items/{it['id']}/speech").status_code == 503
    assert "Nayan" in client.get(f"/api/items/{it['id']}/question").json()["text"]

def test_speech_route_returns_audio(client, monkeypatch):
    monkeypatch.setenv("ELEVENLABS_API_KEY", "k"); voice._cache.clear()
    monkeypatch.setattr(voice, "speak", lambda text, cfg, client=None: b"AUDIO")
    client.post("/api/demo/load"); client.post("/api/run")
    it = client.get("/api/state").json()["items"][0]
    r = client.get(f"/api/items/{it['id']}/speech")
    assert r.status_code == 200 and r.content == b"AUDIO" and r.headers["content-type"] == "audio/mpeg"
