"""Claude LLM integration — parse natural language court booking intent."""

import json
import logging
import os
from datetime import date

from .clubs import _clubs, _city_clubs, _city_display, _city_order

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Client singleton
# ---------------------------------------------------------------------------

_client = None


def _get_client():
    global _client
    try:
        import anthropic
    except ImportError:
        raise RuntimeError("anthropic package not installed — run: uv add anthropic")
    if _client is None:
        api_key = os.getenv("ANTHROPIC_API_KEY", "")
        _client = anthropic.AsyncAnthropic(api_key=api_key)
    return _client


# ---------------------------------------------------------------------------
# Clubs/city context — only cities (small); clubs resolved locally via fuzzy search
# ---------------------------------------------------------------------------

_city_context_cache: str | None = None


def _build_city_context() -> str:
    """Return a compact city list for the system prompt. Cached after first call."""
    global _city_context_cache
    if _city_context_cache is not None:
        return _city_context_cache

    lines = ["Available city keys (use these exact values for city_key):"]
    for ck in _city_order:
        lines.append(f"  {ck}  ({_city_display.get(ck, ck)})")

    _city_context_cache = "\n".join(lines)
    return _city_context_cache


def invalidate_clubs_cache() -> None:
    """Call if clubs/cities are reloaded at runtime."""
    global _city_context_cache
    _city_context_cache = None


# ---------------------------------------------------------------------------
# System prompt — built with string concatenation to avoid .format() conflicts
# with the literal { } characters in the JSON schema examples.
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT_STATIC = """\
Court booking assistant for Israel. Hebrew+English. Return JSON only, no prose.

Sports: Tennis=3, Football/Soccer=6, Padel/פאדל=9, Pickleball=10 (default=3)
Action: watch(עקוב/התרע/alert) or check(בדוק/האם יש/show), default=watch
Time: בוקר=07-12, צהריים=12-16, אחה"צ=14-18, ערב=17-21, לילה=20-23
Dates: היום=today, מחר=tomorrow, day name→next YYYY-MM-DD

Examples:

User: "watch tennis at Sportek tomorrow evening"
{"intent":"booking","reply":null,"action":"watch","court_type":3,"club_query":"Sportek","city_key":"tel_aviv","date":"tomorrow","from_time":"17:00","to_time":"21:00","missing":[]}

User: "תמצא לי מגרש בתל אביב ברוקח 67 מחר"
{"intent":"booking","reply":null,"action":"watch","court_type":3,"club_query":"רוקח 67","city_key":"tel_aviv","date":"tomorrow","from_time":null,"to_time":null,"missing":["time"]}

User: "check pickleball at HaPoel tonight"
{"intent":"booking","reply":null,"action":"check","court_type":10,"club_query":"HaPoel","city_key":null,"date":"today","from_time":"20:00","to_time":"23:00","missing":[]}

User: "שלום"
{"intent":"other","reply":"היי! ספר לי איפה ומתי ואמצא לך מגרש פנוי.","action":"watch","court_type":3,"club_query":null,"city_key":null,"date":null,"from_time":null,"to_time":null,"missing":["club","date","time"]}

Schema (all keys required, null or [] when absent):
{"intent":"booking|other","reply":"null unless other","action":"watch|check","court_type":3|6|9|10,"club_query":"exact words user used for club/address, or null","city_key":"exact key from list below or null","date":"YYYY-MM-DD|today|tomorrow|null","from_time":"HH:MM|null","to_time":"HH:MM|null","missing":["club","date","time"]}

Rules:
- club_query = the club name or address the user mentioned (keep their exact words)
- For "X or Y" addresses extract the shared part (e.g. "רוקח 67 או רוקח 4" → "רוקח")
- city_key = matching key from the city list below, or null
- missing[] lists only fields the user did NOT mention

"""

# Built lazily so city list is populated before first API call
_system_prompt_cache: str | None = None


def _build_system_prompt() -> str:
    global _system_prompt_cache
    if _system_prompt_cache is not None:
        return _system_prompt_cache
    # Use concatenation — NOT .format() — to avoid KeyError on literal { } in the schema
    _system_prompt_cache = _SYSTEM_PROMPT_STATIC + _build_city_context()
    return _system_prompt_cache


def invalidate_prompt_cache() -> None:
    global _system_prompt_cache
    _system_prompt_cache = None
    invalidate_clubs_cache()


# ---------------------------------------------------------------------------
# JSON extraction — robust against markdown fences and trailing prose
# ---------------------------------------------------------------------------

def _extract_json(raw: str) -> str:
    """Pull the first complete JSON object out of a potentially noisy response."""
    # Strip markdown fences like ```json ... ```
    if "```" in raw:
        for chunk in raw.split("```"):
            chunk = chunk.strip()
            if chunk.startswith("json"):
                chunk = chunk[4:].strip()
            if chunk.startswith("{"):
                raw = chunk
                break

    # Find the outermost { ... } even if there's trailing text
    start = raw.find("{")
    if start == -1:
        return raw
    depth, end = 0, -1
    for i, ch in enumerate(raw[start:], start):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    return raw[start:end + 1] if end != -1 else raw[start:]


# ---------------------------------------------------------------------------
# Core API call with multi-turn history support
# ---------------------------------------------------------------------------

async def parse_intent_with_history(messages: list[dict]) -> dict:
    """Call Claude with a full conversation history for multi-turn support.

    `messages` is a list of {"role": "user"|"assistant", "content": str} dicts.
    Returns a parsed intent dict.
    Raises RuntimeError on configuration issues, ValueError on bad response.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY not set in .env")

    model  = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5")
    client = _get_client()

    response = await client.messages.create(
        model=model,
        max_tokens=600,
        system=_build_system_prompt(),
        messages=messages,
    )

    raw = response.content[0].text.strip()
    log.debug("Claude raw response: %r", raw)

    cleaned = _extract_json(raw)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as e:
        log.warning("Claude returned unparseable JSON. raw=%r cleaned=%r", raw, cleaned)
        raise ValueError(f"Could not parse Claude response: {cleaned!r}") from e


async def parse_intent(user_text: str) -> dict:
    """Single-message convenience wrapper around parse_intent_with_history."""
    today = date.today()
    msg   = f"Today is {today.isoformat()} ({today.strftime('%A')}). User message: {user_text}"
    return await parse_intent_with_history([{"role": "user", "content": msg}])


# ---------------------------------------------------------------------------
# Fuzzy club resolution (used when Claude returns club_query text)
# ---------------------------------------------------------------------------

def resolve_clubs(query: str | None, city_key: str | None = None) -> list[dict]:
    """Fuzzy-match a club name or city query against the loaded club list.

    If city_key is given, search is restricted to clubs in that city.
    Returns up to 8 matching club dicts (sorted by rating desc),
    each enriched with a 'city_key' field.
    """
    id_to_city: dict[int, str] = {
        c["id"]: ck
        for ck, clubs in _city_clubs.items()
        for c in clubs
    }

    # Candidate pool: restrict to city if known
    if city_key and city_key in _city_clubs:
        candidates = _city_clubs[city_key]
    else:
        candidates = list(_clubs.values())

    if not query:
        # No specific query — return all clubs in the city (sorted by rating)
        if city_key and city_key in _city_clubs:
            results = [dict(c) for c in candidates]
            for r in results:
                r["city_key"] = id_to_city.get(r["id"], "")
            return sorted(results, key=lambda c: -(c.get("rating") or 0))[:8]
        return []

    q = query.lower().strip()
    results = []
    for club in candidates:
        name_en = (club.get("name_english") or "").lower()
        name_he = (club.get("name") or "").lower()
        city_en = (club.get("city_english") or "").lower()
        city_he = (club.get("city") or "").lower()
        addr_en = (club.get("address_english") or "").lower()
        addr_he = (club.get("address") or "").lower()

        if q in name_en or q in name_he or q in city_en or q in city_he or q in addr_en or q in addr_he:
            enriched             = dict(club)
            enriched["city_key"] = id_to_city.get(club["id"], "")
            results.append(enriched)

    return sorted(results, key=lambda c: -(c.get("rating") or 0))[:8]
