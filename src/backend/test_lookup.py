"""Lookups: grounded model text in, clean place and menu data out. The model call is always faked."""
import lookup

PLACES = """Here are the nearest:
```json
[{"name": "McDonald's", "address": "609 Market St, San Francisco", "distance_mi": "0.34 miles", "open_now": true},
 {"name": "McDonald's", "address": "441 Sutter St, San Francisco", "distance": "~0.1 mi", "open_now": "yes"},
 {"name": "McDonald's", "address": "1100 Fillmore St", "distance_mi": 1.44, "open_now": false},
 {"name": "McDonald's", "address": "Far Away Rd", "distance_mi": 9, "open_now": true}]
```
Hope that helps."""

MENU = """[{"name": "Big Mac", "price": "$5.99", "calories": 590, "protein_g": 25},
 {"name": "Fries (Small)", "price": 3, "calories": "230", "protein_g": 3},
 {"name": "Mystery item", "price": null},
 {"name": "big mac", "price": "$6.49"},
 {"name": "Free sticker", "price": 0}]"""


def setup_function():
    lookup.clear_cache()


def test_parse_json_variants():
    assert lookup.parse_json('[{"a": 1}]') == [{"a": 1}]
    assert lookup.parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert lookup.parse_json('Sure! [1, 2] there you go') == [1, 2]
    assert lookup.parse_json("I could not find anything.") is None
    assert lookup.parse_json("") is None


def test_find_nearby_normalizes_and_sorts():
    got = lookup.find_nearby("McDonald's", (37.78, -122.41), ask=lambda *a, **k: PLACES)
    assert len(got) == 3
    assert [p["address"] for p in got] == ["441 Sutter St, San Francisco", "609 Market St, San Francisco", "1100 Fillmore St"]
    assert got[0]["distance_mi"] == 0.1 and got[0]["open_now"] is True
    assert got[2]["open_now"] is False and got[2]["distance_mi"] == 1.44


def test_find_nearby_passes_latlng_or_text():
    seen = []
    lookup.find_nearby("McDonald's", (1.5, 2.5), ask=lambda prompt, tool, latlng=None: seen.append((prompt, tool, latlng)) or "[]")
    lookup.find_nearby("Chipotle", "downtown Austin", ask=lambda prompt, tool, latlng=None: seen.append((prompt, tool, latlng)) or "[]")
    assert seen[0][1] == "google_maps" and seen[0][2] == (1.5, 2.5)
    assert seen[1][2] is None and "downtown Austin" in seen[1][0]


def test_find_nearby_no_json_returns_empty():
    assert lookup.find_nearby("x", None, ask=lambda *a, **k: "Sorry, nothing found.") == []


def test_get_menu_drops_unpriced_and_dedupes_and_converts_cents():
    place = {"name": "McDonald's", "address": "441 Sutter St"}
    menu = lookup.get_menu(place, ask=lambda *a, **k: MENU)
    assert [(m["id"], m["price_cents"]) for m in menu] == [("big-mac", 599), ("fries-small", 300)]
    assert menu[0]["calories"] == 590 and menu[1]["calories"] == 230 and menu[1]["protein_g"] == 3


def test_price_strings_become_cents():
    assert lookup.to_cents("$5.99") == 599 and lookup.to_cents(6) == 600 and lookup.to_cents("about $1.5") == 150
    assert lookup.to_cents(None) is None and lookup.to_cents("free") is None and lookup.to_cents(0) is None


def test_cache_hits_once_per_day():
    calls = []
    ask = lambda *a, **k: calls.append(1) or MENU  # noqa: E731
    place = {"name": "McDonald's", "address": "441 Sutter St"}
    lookup.get_menu(place, ask=ask)
    lookup.get_menu(place, ask=ask)
    assert len(calls) == 1
    lookup.get_menu({"name": "McDonald's", "address": "609 Market St"}, ask=ask)
    assert len(calls) == 2
