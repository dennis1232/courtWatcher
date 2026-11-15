"""Decision layer (bot/decide.py).

Offline tests pin the code around the Decisions API (window arithmetic, places, routing on fake
answers). The live test runs every case in decision_cases.py against the real API and needs
GEMINI_API_KEY; it is skipped without one.
"""

import asyncio
import os
import types as pytypes
from datetime import date, datetime

import pytest

import bot.decide as dec
from bot.clubs import _clubs, load_clubs
from bot.constants import TZ
from decision_cases import CASES

NOW = datetime(2026, 11, 16, 10, 0, tzinfo=TZ)
load_clubs()


# ---------------------------------------------------------------- offline

def test_window_arithmetic():
    assert dec.window("period", "evening", "none", "none", 60) == ("17:00", "21:00")
    assert dec.window("exact", "none", "18:00", "none", 90) == ("18:00", "19:30")
    assert dec.window("from", "none", "19:00", "none", 60) == ("19:00", "23:00")
    assert dec.window("from", "evening", "18:00", "none", 60) == ("18:00", "23:00")
    assert dec.window("until", "morning", "none", "10:00", 60) == ("07:00", "10:00")
    assert dec.window("range", "none", "18:00", "21:00", 60) == ("18:00", "21:00")
    assert dec.window("none", "none", "none", "none", 60) == (None, None)


def test_places():
    ta = [c for c in _clubs.values() if dec.in_place(c, "תל אביב")]
    assert ta and all((c.get("city") or "").strip().startswith("תל אביב") for c in ta)
    assert len([c for c in _clubs.values() if dec.in_place(c, "גוש דן")]) > len(ta)
    assert all(dec.region_of(c) == "tel_aviv_area" for c in ta)


def test_questions_are_fixed_options():
    q = dec.questions(date(2026, 11, 16))
    assert len(q["day"]["criteria"]) == 17 and {"2026-11-17", "this_week", "next_week"} <= set(q["day"]["criteria"])
    assert set(q["region"]["criteria"]) == {"none", *dec.REGION_LABEL}  # few options keeps call 1 fast
    assert all(len(v.get("criteria", {})) <= 40 for v in q.values()), "a large choice question makes latency spike"
    assert "stop_target" not in q and "stop_target" in dec.questions(date(2026, 11, 16), {"w1": "Rokach 18-20"})
    c2 = dec.place_questions("tel_aviv_area", named=True)
    assert "תל אביב" in c2["city"]["criteria"] and any(k.startswith("club_") for k in c2)


def test_every_club_has_a_region():
    assert {dec.region_of(c) for c in _clubs.values()} <= set(dec.REGION_LABEL)


def _fake(answers_by_call):
    """Patch the API call: each call returns the next dict of {question: choice or noul probability}."""
    calls = iter(answers_by_call)

    async def fake_ask(text, qs):
        a = {}
        for k, v in next(calls).items():
            a[k] = pytypes.SimpleNamespace(noul=v, confidence=v) if isinstance(v, float) else \
                pytypes.SimpleNamespace(choice=v, confidence=1.0)
        return pytypes.SimpleNamespace(answers=a)
    return fake_ask


BASE = dict(action="search", day="2026-11-17", window="period", period="evening", start="none", end="none",
            duration="60", sport="tennis", region="tel_aviv_area", names_club=0.1)
TA = {"city": "תל אביב"}


def test_routing_city_search(monkeypatch):
    monkeypatch.setattr(dec, "_ask", _fake([BASE, TA]))
    d = asyncio.run(dec.decide("x", now=NOW))
    assert (d.action, d.ask, d.date, d.frm, d.to, d.named) == ("search", None, "2026-11-17", "17:00", "21:00", False)
    assert d.clubs and all(dec.in_place(c, "תל אביב") for c in d.clubs)


def test_routing_named_clubs_and_threshold(monkeypatch):
    rokach = next(i for i, c in _clubs.items() if "רוקח 67" in (c.get("name") or ""))
    other = next(i for i, c in _clubs.items() if i != rokach and dec.in_place(c, "תל אביב"))
    monkeypatch.setattr(dec, "_ask", _fake([BASE | {"names_club": 0.97}, TA | {f"club_{rokach}": 0.95, f"club_{other}": 0.7}]))
    d = asyncio.run(dec.decide("x", now=NOW))
    assert d.named and [c["id"] for c in d.clubs] == [rokach]


def test_routing_fixed_questions(monkeypatch):
    for over, ask in [({"region": "none"}, "where"), ({"day": "none", "window": "none", "period": "none"}, "day"),
                      ({"window": "none", "period": "none"}, "time")]:
        monkeypatch.setattr(dec, "_ask", _fake([BASE | over, TA]))
        assert asyncio.run(dec.decide("x", now=NOW)).ask == ask, over


def test_now_means_rest_of_today(monkeypatch):
    monkeypatch.setattr(dec, "_ask", _fake([BASE | {"day": "2026-11-16", "window": "none", "period": "none"}, TA]))
    d = asyncio.run(dec.decide("x", now=NOW))
    assert (d.ask, d.date, d.frm, d.to) == (None, "2026-11-16", "06:00", "23:00")


def test_next_week_is_sunday_to_saturday(monkeypatch):
    monkeypatch.setattr(dec, "_ask", _fake([BASE | {"day": "next_week", "window": "exact", "period": "none", "start": "19:00"}, TA]))
    d = asyncio.run(dec.decide("x", now=NOW))  # NOW is Monday 2026-11-16
    assert d.dates == [f"2026-11-{x}" for x in range(22, 29)] and (d.frm, d.to, d.ask) == ("19:00", "20:00", None)


# ---------------------------------------------------------------- live

def _outcome(d):
    """Decision → (outcome, values) in the vocabulary of decision_cases.py."""
    if d.action in ("stop_watch", "list_watches"):
        return d.action, {}
    if d.action == "chit_chat":
        return None, {}
    if d.ask:
        return f"ask:{d.ask}", {}
    names = [(c.get(k) or "").strip() for c in d.clubs + d.retired for k in ("name", "name_english", "address")] if d.named else []
    return ("start_watch" if d.action == "watch" else "search_courts"), {
        "places": names + ([d.city] if d.city else []), "date": d.date, "dates": d.dates, "from": d.frm, "to": d.to,
        "duration_min": d.duration, "sport": d.sport}


def _matches(case, outcome, values):
    msg, accepted, exp = case
    if outcome not in accepted:
        return False
    if outcome not in ("search_courts", "start_watch"):
        return True
    for k, v in exp.items():
        if k == "places":
            got = " | ".join(values["places"]).lower()
            if not all(any(a.lower() in got for a in alt.split("|")) for alt in v):
                return False
        elif str(values.get(k)) != str(v):
            return False
    return True


@pytest.mark.skipif(not os.getenv("GEMINI_API_KEY"), reason="live Decisions API test needs GEMINI_API_KEY")
def test_live_decisions():
    async def run():
        sem = asyncio.Semaphore(5)  # gentle on the beta API; all-at-once timed out during its slow spells

        async def one(c):
            async with sem:
                return await dec.decide(c[0], now=NOW)
        return await asyncio.gather(*(one(c) for c in CASES))
    misses = [(c[0], _outcome(d)) for c, d in zip(CASES, asyncio.run(run())) if not _matches(c, *_outcome(d))]
    assert len(misses) <= 1, misses  # beta API: tolerate one miss across the set, never more
