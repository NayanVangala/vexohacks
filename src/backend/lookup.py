"""Live place and menu lookups for the ordering agent. Two grounded Gemini calls (Maps for places, Search
for menus), turned into clean data in code. Nothing is preloaded; results are cached per day so a repeat is instant.
Prices come from the web and may differ at the store.
"""

import json
import re
import time

import models

_cache: dict[tuple, list] = {}


def clear_cache() -> None:
    _cache.clear()


def _day() -> str:
    return time.strftime("%Y-%m-%d")


def parse_json(text: str):
    """The first JSON array or object in model text (fenced, or inside prose). None when there is none."""
    if not text:
        return None
    m = re.search(r"```(?:json)?\s*(.*?)```", text, re.S)
    for chunk in ([m.group(1)] if m else []) + [text]:
        dec = json.JSONDecoder()
        for i, ch in enumerate(chunk):
            if ch in "[{":
                try:
                    return dec.raw_decode(chunk[i:])[0]
                except ValueError:
                    continue
    return None


def _num(v) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"\d+(?:\.\d+)?", str(v or ""))
    return float(m.group()) if m else None


def to_cents(v) -> int | None:
    """Dollars (number or text like "$5.99") to integer cents. None for missing, free or zero."""
    n = _num(v)
    return round(n * 100) if n and n > 0 else None


def _open(v) -> bool | None:
    if isinstance(v, bool):
        return v
    s = str(v or "").strip().lower()
    return True if s in ("yes", "true", "open") else False if s in ("no", "false", "closed") else None


def find_nearby(query: str, where, ask=models.grounded) -> list[dict]:
    """Up to 3 nearest places: {name, address, distance_mi, open_now}. `where` is (lat, lng), free text, or None."""
    latlng = where if isinstance(where, tuple) else None
    key = ("places", query.lower(), tuple(round(x, 3) for x in latlng) if latlng else str(where), _day())
    if key in _cache:
        return _cache[key]
    near = (f"latitude {latlng[0]}, longitude {latlng[1]}" if latlng else f"the location {where}" if where else "the user")
    prompt = (f"Find the 3 nearest places matching \"{query}\" to {near}. Reply with ONLY a JSON array, no prose, of "
              '{"name": str, "address": str (full street address), "distance_mi": number (approximate straight-line miles from '
              'that point), "open_now": true|false|null}.')
    raw = parse_json(ask(prompt, "google_maps", latlng))
    out = []
    for p in raw if isinstance(raw, list) else []:
        if isinstance(p, dict) and p.get("name") and p.get("address"):
            out.append({"name": str(p["name"]), "address": str(p["address"]),
                        "distance_mi": _num(p.get("distance_mi", p.get("distance"))), "open_now": _open(p.get("open_now"))})
    out.sort(key=lambda p: (p["distance_mi"] is None, p["distance_mi"] or 0))
    out = out[:3]
    if out:
        _cache[key] = out
    return out


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def get_menu(place: dict, wanted: str = "", ask=models.grounded) -> list[dict]:
    """Menu items with prices for this specific place: {id, name, price_cents, calories, protein_g}."""
    key = ("menu", place["name"].lower(), place["address"].lower(), wanted.lower().strip(), _day())
    if key in _cache:
        return _cache[key]
    prompt = (f"List the menu items and current prices at the {place['name']} at {place['address']} (use its delivery or "
              'pickup listing prices if that is what you can find). Reply with ONLY a JSON array, no prose, of '
              '{"name": str, "price": number in dollars, "calories": number|null, "protein_g": number|null}. '
              "Cover the main menu, not just breakfast or drinks: burgers, chicken sandwiches, nuggets, fries and other sides, "
              "drinks and desserts, at least 15 items, with each size as its own item (e.g. \"Fries (Small)\")."
              + (f" Make sure to include: {wanted}." if wanted.strip() else ""))
    raw = parse_json(ask(prompt, "google_search", None))
    seen, out = set(), []
    for it in raw if isinstance(raw, list) else []:
        if not isinstance(it, dict) or not it.get("name"):
            continue
        cents, iid = to_cents(it.get("price")), _slug(str(it["name"]))
        if cents is None or not iid or iid in seen:
            continue
        seen.add(iid)
        cal, pro = _num(it.get("calories")), _num(it.get("protein_g"))
        out.append({"id": iid, "name": str(it["name"]), "price_cents": cents,
                    "calories": int(cal) if cal is not None else None, "protein_g": int(pro) if pro is not None else None})
    if out:
        _cache[key] = out
    return out
