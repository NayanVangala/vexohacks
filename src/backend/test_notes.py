"""Notes session: slide capture, auto-stop, and evidence-checked build. No network."""
import notes

SLIDE_A = "Q4 roadmap: ship the glasses app, launch payments, hire two engineers, review budget"
SLIDE_A_TYPO = "Q4 roadmap: ship the glasses app, launch payments, hire two engineers, review budgets"
SLIDE_B = "Risks and open questions about vendor contracts"


def setup_function():
    notes.stop()


def slides(text, conf=0.9, type_="slides"):
    return {"type": type_, "confidence": conf, "text": text}


def test_slide_changed():
    assert notes.slide_changed(SLIDE_A, SLIDE_A) is False
    assert notes.slide_changed(SLIDE_A, SLIDE_A_TYPO) is False      # one word changed: same slide
    assert notes.slide_changed(SLIDE_A, SLIDE_B) is True
    assert notes.slide_changed("", SLIDE_B) is True                  # first slide ever
    assert notes.slide_changed(SLIDE_A, "") is False                 # nothing readable: not a new slide


def test_static_slide_saved_once_and_new_slide_saved():
    notes.start(0)
    for i in range(5):
        notes.on_scene(slides(SLIDE_A), f"f{i}.jpg", i * 2.5)
    notes.on_scene(slides(SLIDE_A_TYPO), "typo.jpg", 20)
    notes.on_scene(slides(SLIDE_B), "b.jpg", 25)
    s = notes.stop()
    assert [x["frame"] for x in s.slides] == ["f0.jpg", "b.jpg"]


def test_on_scene_none_and_garbage_are_safe():
    notes.start(0)
    for bad in (None, {}, {"type": "slides"}, {"type": "slides", "confidence": 0.9}, "slides"):
        assert notes.on_scene(bad, "x.jpg", 1) is False
    assert notes.stop().slides == []


def test_low_confidence_slide_not_saved():
    notes.start(0)
    notes.on_scene(slides(SLIDE_A, conf=0.3), "x.jpg", 1)
    assert notes.stop().slides == []


def test_auto_stop_after_600s_without_meeting():
    notes.start(0)
    assert notes.on_scene({"type": "none", "confidence": 0.9, "text": ""}, None, 599) is False
    assert notes.on_scene({"type": "none", "confidence": 0.9, "text": ""}, None, 601) is True
    notes.start(0)
    notes.on_scene({"type": "meeting", "confidence": 0.9, "text": ""}, None, 500)   # still in the meeting
    assert notes.on_scene({"type": "none", "confidence": 0.9, "text": ""}, None, 1000) is False


def test_stop_returns_session_and_deactivates():
    assert notes.stop() is None
    notes.start(0)
    assert notes.active()
    notes.add_line("we ship friday")
    s = notes.stop()
    assert s.lines == ["we ship friday"] and not notes.active()
    notes.add_line("ignored when inactive")                          # no crash


def test_build_drops_invented_action_items():
    notes.start(0)
    notes.add_line("Sam: I will send the budget to Priya by Friday.")
    notes.on_scene(slides(SLIDE_A), "a.jpg", 1)
    s = notes.stop()

    def fake(transcript, slide_texts):
        assert "budget to Priya" in transcript and SLIDE_A in slide_texts
        return {"summary": "Roadmap review", "decisions": ["Ship Friday"],
                "action_items": [
                    {"text": "Send budget to Priya", "quote": "I will send the budget to Priya by Friday"},
                    {"text": "Hire two engineers", "quote": "hire two engineers"},          # from the slide
                    {"text": "Book the offsite", "quote": "we should book the offsite in March"},  # invented
                    {"text": "Too short", "quote": "ok"}]}

    out = notes.build(s, make=fake)
    assert [a["text"] for a in out["action_items"]] == ["Send budget to Priya", "Hire two engineers"]
    assert out["summary"] == "Roadmap review" and out["decisions"] == ["Ship Friday"]
    assert out["slides"] == [{"frame": "a.jpg", "text": SLIDE_A}]


def test_build_with_empty_session_makes_no_model_call():
    notes.start(0)
    s = notes.stop()
    out = notes.build(s, make=lambda *a: (_ for _ in ()).throw(AssertionError("no call expected")))
    assert out["action_items"] == [] and out["slides"] == []
