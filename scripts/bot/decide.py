"""Understanding layer: free text (Hebrew/English) → a decision, via the Gemini Decisions API.

The model only *chooses* among fixed options (action, date, time window, length, sport, city,
clubs). Code turns the chosen options into a search with lookup tables and clock arithmetic —
there is no text parsing here. Missing information becomes one of the fixed questions
(`where` / `day` / `time`), which the bot shows as buttons.

    d = await decide("אני רוצה מגרש כלשהו בתל אביב בין השעות 18-21 למשך שעה")
    d.action, d.ask, d.date, d.frm, d.to, d.duration, d.sport, [c["id"] for c in d.clubs]

Two calls at most: call 1 always (≈450 input tokens + options); call 2 only when the message
names a specific club/street, asking one yes/no per club in the chosen city.
Config: GEMINI_API_KEY (required), GEMINI_DECISION_MODEL (default gemini-decision-1). Plain HTTPS via httpx
(the google-genai SDK adds ~0.9 s of import time per CLI run for one POST).
"""

import asyncio
import json
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from types import SimpleNamespace

import httpx

from .clubs import _CLUBS_PATH, _clubs, _norm_city, load_clubs
from .constants import TZ

ACTIONS = ("search", "watch", "stop_watch", "list_watches", "chit_chat")
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
HE_WEEKDAYS = ("שני", "שלישי", "רביעי", "חמישי", "שישי", "שבת", "ראשון")
PERIODS = {"morning": ("07:00", "12:00"), "noon": ("12:00", "16:00"), "afternoon": ("14:00", "18:00"),
           "evening": ("17:00", "21:00"), "night": ("20:00", "23:00")}
REGION_LABEL = {"tel_aviv_area": "גוש דן", "sharon": "השרון", "rishon_area": "השפלה", "jerusalem_area": "אזור ירושלים",
                "center": "מרכז", "north": "צפון", "south": "דרום"}
AREAS_KEYS = {"tel_aviv_area", "sharon", "rishon_area", "jerusalem_area"}  # regions the bot's city buttons know
CLUB_YES, CLUB_TOP = 0.85, 0.6  # club yes/no thresholds: all ≥0.85, else the single best if ≥0.6


@dataclass
class Decision:
    action: str
    ask: str | None = None          # fixed question to ask: where | day | time | which_watch
    clubs: list[dict] = field(default_factory=list)    # clubs to search (named clubs, or the whole city/area)
    named: bool = False              # True when the user named specific clubs/streets
    city: str | None = None          # chosen city or area option (Hebrew)
    city_key: str | None = None      # bot.clubs key for the button flow
    date: str | None = None          # first (or only) date
    dates: list[str] = field(default_factory=list)     # every date to search ("next week" = Sun–Sat)
    frm: str | None = None
    to: str | None = None
    duration: int = 60
    sport: str = "tennis"
    retired: list[dict] = field(default_factory=list)  # named clubs Lazuz no longer lists
    stop_target: str | None = None   # watch id or "all"
    confidence: dict = field(default_factory=dict)


def _clean(s: str | None) -> str:
    return " ".join((s or "").split())


def _retired() -> list[dict]:
    return json.loads(_CLUBS_PATH.read_text()).get("retired", []) if _CLUBS_PATH.exists() else []


def _cities(clubs: list[dict]) -> dict[str, str]:
    """City option (Hebrew, as in clubs.json) → English names, for the given clubs' cities."""
    out: dict[str, set] = {}
    for c in clubs:
        if he := _clean(c.get("city")):
            out.setdefault(he, set()).add(_clean(c.get("city_english")))
    return {he: ", ".join(sorted(e for e in en if e)) for he, en in sorted(out.items())}


def _regions() -> dict[str, str]:
    """Region options, each described by the cities it contains (generated from clubs.json)."""
    fixed = {"tel_aviv_area": "Tel Aviv / Gush Dan", "sharon": "Sharon", "rishon_area": "Rishon LeZion / Shfela",
             "jerusalem_area": "Jerusalem area", "center": "Other central Israel", "north": "Haifa and the north",
             "south": "The south"}
    by_region: dict[str, set] = {}
    for c in _all_clubs():
        if name := _clean(c.get("city_english")) or _clean(c.get("city")):
            by_region.setdefault(region_of(c), set()).add(name.title())
    return {"none": "No place, or not a real location ('near work', 'near home')",
            **{k: f"{v} ({', '.join(sorted(by_region.get(k, [])))})" for k, v in fixed.items()}}


def region_of(club: dict) -> str:
    """Region key for a club: one of the bot's four areas, else north/south/center by latitude."""
    for name in (_clean(club.get("city")), _clean(club.get("city_english"))):
        if name and (k := _norm_city(name)) in ("tel_aviv_area", "sharon", "rishon_area", "jerusalem_area"):
            return k
    lat = club.get("loc_lat")
    return "center" if lat is None else "north" if lat >= 32.45 else "south" if lat < 31.83 else "center"


def in_place(club: dict, place: str) -> bool:
    """Is the club in this city / area / region option? ("תל אביב" also covers "תל אביב יפו")."""
    city = _clean(club.get("city"))
    if place in REGION_LABEL.values():
        return region_of(club) == next(k for k, v in REGION_LABEL.items() if v == place)
    return city == place or city.startswith(place + " ")


def _all_clubs() -> list[dict]:
    return list(_clubs.values()) + _retired()


def questions(today: date, watches: dict[str, str] | None = None) -> dict:
    """Call-1 questions. Day options are the next 14 dates, labelled for the model."""
    days = {"none": "No day or date mentioned."}
    for d in range(14):
        day = today + timedelta(days=d)
        tag = " - today" if d == 0 else " - tomorrow (מחר)" if d == 1 else " - day after tomorrow (מחרתיים)" if d == 2 else ""
        days[day.isoformat()] = f"{WEEKDAYS[day.weekday()]} ({HE_WEEKDAYS[day.weekday()]}){tag}"
    days["this_week"] = "any day this week, today until Saturday (השבוע, during the week) - no single day named"
    days["next_week"] = "any day next week, Sunday to Saturday (שבוע הבא, next week) - no single day named"
    q = {
        "action": {"type": "choice",
            "instructions": "A user wrote this message to a tennis/padel court-finding bot. What should the bot do?",
            "criteria": {
                "search": "Find free courts now (find / check / is there / book a court, or just names a place and time). The default for a court request.",
                "watch": "ONLY when the user explicitly asks to keep checking later / follow / track / notify or ping them when something frees up (תעקוב, מעקב, תודיע לי כשמתפנה).",
                "stop_watch": "Stop or cancel one or all running watches.",
                "list_watches": "Show the status of running watches.",
                "chit_chat": "Greeting, thanks, never mind - no action."}},
        "day": {"type": "choice", "criteria": days,
            "instructions": f"Today is {WEEKDAYS[today.weekday()]} {today}. Which date does the user mean? A weekday name means its next "
                            "occurrence (today if it is today). 'now'/'עכשיו'/'tonight'/'הערב' mean today. 'this week'/'next week' "
                            "without a single day = this_week/next_week. No day or date mentioned = none."},
        "window": {"type": "choice", "instructions": "How does the user express the time window?",
            "criteria": {"exact": "a single start time: 'at 19', 'ב-18', '7:30'",
                         "from": "open-ended from a time: 'after 19', 'from 18', 'אחרי 17', 'החל מ-18'",
                         "until": "only an end: 'until 12', 'before 10', 'עד 12'",
                         "range": "between two times: '18-21', 'בין 18 ל-20', 'between 6 and 9pm'",
                         "period": "a part-of-day word only: morning/evening/night/after work/בבוקר/בערב/הערב",
                         "none": "no time at all (or 'now')"}},
        "period": {"type": "choice", "instructions": "Which part of the day does the user mention, if any?",
            "criteria": {"morning": "בוקר/morning", "noon": "צהריים/noon", "afternoon": "אחה\"צ/afternoon",
                         "evening": "ערב/evening/after work/אחרי העבודה/tonight/הערב", "night": "לילה/night", "none": "no part-of-day word"}},
        "start": {"type": "choice",
            "instructions": "Start time the user gives (24h). 'at 19'/'ב-19'/'after 19'/'from 19' = 19:00; '6-9 בערב' = 18:00; '6pm' = 18:00. "
                            "No explicit clock time = none.",
            "criteria": {"none": "No explicit start time.", **{f"{h:02d}:{m:02d}": "" for h in range(6, 24) for m in (0, 30)}}},
        "end": {"type": "choice",
            "instructions": "End time the user gives (24h). '18-21' = 21:00; '6-9 בערב' = 21:00; 'until 12'/'עד 12' = 12:00. "
                            "Only a start time, or no explicit clock time = none.",
            "criteria": {"none": "No explicit end time.", **{f"{h:02d}:{m:02d}": "" for h in range(7, 24) for m in (0, 30)}, "23:59": "end of day"}},
        "duration": {"type": "choice", "instructions": "How long does the user want to play? (שעה=60, שעה וחצי=90, שעתיים=120, חצי שעה=30). Not mentioned = 60.",
            "criteria": {"30": "half an hour", "60": "one hour or not mentioned", "90": "an hour and a half", "120": "two hours", "150": "two and a half hours"}},
        "sport": {"type": "choice", "instructions": "Which sport? Not mentioned = tennis.",
            "criteria": {"tennis": "tennis / טניס or not mentioned", "padel": "padel / פאדל / פדל",
                         "pickleball": "pickleball / פיקלבול", "football": "football / soccer / כדורגל"}},
        # Few options on purpose: one 100-option city question made p90 latency ~5 s; 8 regions keep it <0.5 s.
        "region": {"type": "choice",
            "instructions": "In which region is the place the user names (city, area, club or street)? Gush Dan = Tel Aviv area.",
            "criteria": _regions()},
        "names_club": {"type": "noul",
            "instructions": "Does the message name a specific club, venue, street or neighbourhood (more specific than a city or area)? "
                            "Generic words like 'court'/'מגרש' do not count."},
    }
    if watches:
        q["stop_target"] = {"type": "choice", "instructions": "If the user wants to stop a watch, which one?",
                            "criteria": {"all": "all watches, or not specified", **watches}}
    return q


def place_questions(region: str, named: bool) -> dict:
    """Call-2 questions: the city within the chosen region (+ one yes/no per club there if a club was named)."""
    pool = [c for c in _all_clubs() if region_of(c) == region]
    q = {"city": {"type": "choice",
                  "instructions": "Which city does the user name? If they name the whole area or no specific city, answer all.",
                  "criteria": {"all": f"the whole {REGION_LABEL[region]} area, or no specific city", **_cities(pool)}}}
    return q | (_club_questions(pool) if named else {})


def _club_questions(clubs: list[dict]) -> dict:
    return {f"club_{c['id']}": {"type": "noul", "instructions":
            "Does the user name this specific club, its street or its neighbourhood (not merely its city)? "
            f"Club: {_clean(c.get('name'))} / {_clean(c.get('name_english'))}; "
            f"address: {_clean(c.get('address'))} / {_clean(c.get('address_english'))}; city: {_clean(c.get('city'))}."}
            for c in clubs}


def _plus(hm: str, minutes: int) -> str:
    h, m = map(int, hm.split(":"))
    t = h * 60 + m + minutes
    return "23:59" if t >= 24 * 60 else f"{t // 60:02d}:{t % 60:02d}"


def window(kind: str, period: str, start: str, end: str, duration: int) -> tuple[str | None, str | None]:
    """Chosen window options → (from, to). Lookup + arithmetic only."""
    if kind == "period" and period in PERIODS:
        return PERIODS[period]
    if kind == "exact" and start != "none":
        return start, _plus(start, duration)
    if kind == "from" and start != "none":
        return start, "23:00"  # "after 19" is open-ended, even if the model also tags a period
    if kind == "until" and end != "none":
        return PERIODS[period][0] if period in PERIODS else "06:00", end
    if kind == "range" and start != "none" and end != "none":
        return start, end
    return None, None


# Beta endpoint. When Google promotes it, set GEMINI_DECISION_API_VERSION=v1 in .env — no code change.
API_URL = "https://generativelanguage.googleapis.com/{version}/decisions"
TIMEOUT = 30.0  # seconds per call; callers fall back to buttons/flags. Typical ≈0.3–2 s; the beta has slow spells (10–20 s seen)
_http: httpx.AsyncClient | None = None


async def _ask(text: str, qs: dict) -> SimpleNamespace:
    """One Decisions API call → namespace with .answers[question_id].{choice|noul, confidence}.

    Measured: duplicate ("hedged") requests did not shorten the slow tail — when the beta service is
    slow, every copy is — so this is one request with a 429/503 retry. Keep choice questions small:
    a 100-option question pushed p90 to ~5 s; ≤40 options keeps a call ≈0.3 s typical.
    """
    global _http
    key = os.getenv("GEMINI_API_KEY")
    if not key:
        raise RuntimeError("GEMINI_API_KEY is not set — add it to .env (template: .env.example)")
    if _http is None or _http.is_closed:
        _http = httpx.AsyncClient(headers={"x-goog-api-key": key}, timeout=TIMEOUT)  # kept alive across calls
    url = API_URL.format(version=os.getenv("GEMINI_DECISION_API_VERSION") or "v1beta")
    body = {"model": os.getenv("GEMINI_DECISION_MODEL") or "gemini-decision-1", "input": text, "questions": qs}
    for attempt in range(3):
        r = await _http.post(url, json=body)
        if r.status_code not in (429, 503) or attempt == 2:
            break
        await asyncio.sleep(2 ** attempt)
    if r.status_code != 200:
        raise RuntimeError(f"Decisions API {r.status_code}: {r.text[:200]}")
    return SimpleNamespace(answers={k: SimpleNamespace(**v) for k, v in r.json().get("answers", {}).items()})


async def decide(text: str, now: datetime | None = None, watches: dict[str, str] | None = None) -> Decision:
    """`watches`: running watch id → label, so "stop it" can pick one."""
    if not _clubs:
        load_clubs()
    now = now or datetime.now(TZ)
    today = now.date()
    a = (await _ask(text, questions(today, watches))).answers  # call 1: small questions only
    d = Decision(action=a["action"].choice, confidence={k: round(v.confidence, 2) for k, v in a.items()})
    if d.action in ("stop_watch", "list_watches", "chit_chat"):
        if d.action == "stop_watch" and watches:
            d.stop_target = a["stop_target"].choice
        return d

    d.sport, d.duration = a["sport"].choice, int(a["duration"].choice)
    region = a["region"].choice
    if region in REGION_LABEL:
        # Call 2: the city within the region, and which named clubs (yes/no per club there)
        named = a["names_club"].noul > 0.5
        b = (await _ask(text, place_questions(region, named))).answers
        city = b["city"].choice
        d.city = REGION_LABEL[region] if city == "all" else city
        d.city_key = region if city == "all" and region in AREAS_KEYS else _norm_city(city) if city != "all" else None
        if named:
            scored = sorted(((v.noul, int(k[5:])) for k, v in b.items() if k.startswith("club_")), reverse=True)
            ids = [cid for p, cid in scored if p >= CLUB_YES] or [cid for p, cid in scored[:1] if p >= CLUB_TOP]
            if not ids:  # named a club the region guess missed (כפר המכביה is in Ramat Gan): ask across all clubs
                everywhere = (await _ask(text, _club_questions(_all_clubs()))).answers
                scored = sorted(((v.noul, int(k[5:])) for k, v in everywhere.items()), reverse=True)
                ids = [cid for p, cid in scored if p >= CLUB_YES] or [cid for p, cid in scored[:1] if p >= CLUB_TOP]
                city = "all" if ids else city
            if city != "all":  # a named street exists in many cities: keep the ones in the named city
                ids = [i for i in ids if in_place((_clubs.get(i) or next(r for r in _retired() if r["id"] == i)), city)] or ids
            d.named = bool(ids)
            d.clubs = [_clubs[i] for i in ids if i in _clubs]
            d.retired = [c for c in _retired() if c["id"] in ids]
            if d.named and not any(in_place(c, d.city) for c in d.clubs + d.retired):
                d.city = _clean((d.clubs + d.retired)[0].get("city")) or d.city  # region guess was wrong
                d.city_key = _norm_city(d.city)
        if not d.named:
            d.clubs = sorted((c for c in _clubs.values() if in_place(c, d.city)), key=lambda c: -(c.get("rating") or 0))

    day = a["day"].choice
    d.frm, d.to = window(a["window"].choice, a["period"].choice, a["start"].choice, a["end"].choice, d.duration)
    if not d.clubs and not d.retired:
        d.ask = "where"
    elif day in ("none", "this_week", "next_week") and d.frm is None:
        d.ask = "day"
    elif d.frm is None and (a["window"].choice != "none" or day != today.isoformat()):
        d.ask = "time"
    if d.frm is None and d.ask is None:  # "now" / "today" with no time: the rest of today
        d.frm, d.to = "06:00", "23:00"
    if day in ("this_week", "next_week"):  # Israeli week: Sunday–Saturday
        to_sunday = (6 - today.weekday()) % 7 or 7
        span = range(0, to_sunday) if day == "this_week" else range(to_sunday, to_sunday + 7)
        d.dates = [(today + timedelta(days=i)).isoformat() for i in span]
        d.date = d.dates[0]
    elif day != "none":
        d.date = day
    elif d.frm:  # a time but no day: today if that window is still reachable, else tomorrow
        last = datetime.combine(today, datetime.strptime(d.to, "%H:%M").time(), TZ) - timedelta(minutes=d.duration)
        d.date = (today if last > now else today + timedelta(days=1)).isoformat()
    d.dates = d.dates or ([d.date] if d.date else [])
    return d
