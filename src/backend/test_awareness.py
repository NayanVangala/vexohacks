"""Awareness: scene sightings in, at most one polite offer out. No network, time is passed in."""
import pytest

from awareness import Awareness, parse_yes_no

MEETING = {"type": "meeting", "confidence": 0.9, "text": ""}
SLIDES = {"type": "slides", "confidence": 0.9, "text": "Q4 roadmap"}
NONE = {"type": "none", "confidence": 0.9, "text": ""}


def feed_n(a, scene, n, t0=0.0):
    out = None
    for i in range(n):
        out = a.feed(scene, t0 + i * 2.5) or out
    return out


def test_three_in_a_row_offers_once(monkeypatch):
    monkeypatch.setenv("WEARER_NAME", "Srijan Saha")
    a = Awareness()
    assert a.feed(MEETING, 0) is None and a.feed(MEETING, 2.5) is None
    offer = a.feed(MEETING, 5)
    assert offer and "take notes" in offer.text and offer.text.startswith("Hey Srijan, ")
    # a long meeting keeps returning the same scene: still one offer
    assert feed_n(a, MEETING, 50, t0=10) is None


def test_no_greeting_without_wearer_name(monkeypatch):
    monkeypatch.delenv("WEARER_NAME", raising=False)
    offer = feed_n(Awareness(), MEETING, 3)
    assert offer and not offer.text.startswith("Hey")


def test_different_scene_resets_count():
    a = Awareness()
    a.feed(MEETING, 0); a.feed(MEETING, 2.5)
    assert a.feed(NONE, 5) is None
    assert a.feed(MEETING, 7.5) is None and a.feed(MEETING, 10) is None
    assert a.feed(MEETING, 12.5) is not None


def test_low_confidence_ignored():
    a = Awareness()
    low = {"type": "meeting", "confidence": 0.4, "text": ""}
    assert feed_n(a, low, 10) is None


@pytest.mark.parametrize("bad", [None, {}, {"type": "meeting"}, {"type": "meeting", "confidence": "high"}])
def test_missing_or_malformed_scene_is_ignored(bad):
    a = Awareness()
    assert feed_n(a, bad, 10) is None
    assert feed_n(a, MEETING, 3, t0=100) is not None  # and it did not break state


def test_cooldown_after_shown_and_after_decline():
    a = Awareness()
    o = feed_n(a, MEETING, 3)
    a.decline(o.id, 10)
    assert feed_n(a, MEETING, 3, t0=20) is None           # inside 1800 s decline cooldown
    assert feed_n(a, MEETING, 3, t0=1900) is not None     # after it


def test_shown_cooldown_when_ignored():
    a = Awareness()
    o = feed_n(a, MEETING, 3)
    assert a.pending(100) is None                         # expired after 60 s, never answered
    assert feed_n(a, MEETING, 3, t0=200) is None          # still inside 600 s
    assert feed_n(a, MEETING, 3, t0=700) is not None
    assert o is not None


def test_no_offer_while_busy_or_pending():
    a = Awareness()
    a.set_busy(True)
    assert feed_n(a, MEETING, 5) is None
    a.set_busy(False)
    assert feed_n(a, MEETING, 3) is not None
    assert feed_n(a, SLIDES, 5, t0=30) is None            # first offer still pending


def test_pending_expires_after_60s():
    a = Awareness()
    o = feed_n(a, MEETING, 3)
    assert a.pending(30).id == o.id
    assert a.pending(70) is None


def test_accept_unknown_or_answered_raises_keyerror():
    a = Awareness()
    o = feed_n(a, MEETING, 3)
    with pytest.raises(KeyError):
        a.accept(999, 10)
    assert a.accept(o.id, 10).id == o.id
    with pytest.raises(KeyError):
        a.accept(o.id, 11)                                # already answered
    with pytest.raises(KeyError):
        a.decline(o.id, 11)


def test_accept_after_expiry_raises():
    a = Awareness()
    o = feed_n(a, MEETING, 3)
    with pytest.raises(KeyError):
        a.accept(o.id, 500)


def test_loggable_once_per_ten_minutes():
    a = Awareness()
    menu = {"type": "menu", "confidence": 0.9, "text": ""}
    assert a.loggable(menu, 0) == "Looking at a menu"
    assert a.loggable(menu, 100) is None
    assert a.loggable(menu, 700) == "Looking at a menu"
    assert a.loggable(MEETING, 800) is None               # meetings offer, they are not logged
    assert a.loggable(None, 900) is None


@pytest.mark.parametrize("heard,want", [
    ("yeah sure", "yes"), ("nope", "no"), ("yes no", "unclear"), ("maybe", "unclear"), ("", "unclear"),
    ("Yes, go ahead!", "yes"), ("not now", "no"),
    ("approve", "yes"), ("approved", "yes"), ("Can you approve that task?", "yes"), ("confirm", "yes")])
def test_parse_yes_no(heard, want):
    assert parse_yes_no(heard) == want
