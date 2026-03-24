"""Message formatters for availability results and pricing."""

import json
from datetime import datetime

from .constants import COURT_EMOJI, COURT_TYPE_NAMES, DAYS_LAZUZ
from .clubs import _clubs, club_label


def slots_in_range(slots: list[str], from_time: str, to_time: str) -> list[str]:
    """Filter a list of 'HH:MM:SS' slot strings to those within [from_time, to_time)."""
    from_t = datetime.strptime(from_time, "%H:%M").time()
    to_t   = datetime.strptime(to_time,   "%H:%M").time()
    return [s for s in slots if from_t <= datetime.strptime(s[:5], "%H:%M").time() < to_t]


def format_hits(hits: list[dict], club_id: int, check_date: str,
                from_time: str, to_time: str, court_type: int) -> str:
    """Format availability grouped by time slot.

    Example output:
        🎾 *Tennis available!*
        📍 Club Name (#21)  ⭐ 4.2
        📅 2026-03-25   🕐 16:00–20:00

        `16:00`  Court 1
        `17:00`  Court 1  ·  Court 2
        `18:30`  Court 2
    """
    emoji  = COURT_EMOJI.get(court_type, "🏟")
    sport  = COURT_TYPE_NAMES.get(court_type, str(court_type))
    club   = _clubs.get(club_id, {})
    name   = (club.get("name_english") or club.get("name", f"Club #{club_id}")).strip()
    rating = club.get("rating")
    rating_str = f"  ⭐ {rating}" if rating else ""

    # Group courts by time slot
    time_courts: dict[str, list[str]] = {}
    for court in hits:
        for slot in court["slots"]:
            t = slot[:5]  # HH:MM
            time_courts.setdefault(t, []).append(court["name"])

    lines = [
        f"{emoji} *{sport} available!*",
        f"📍 {name} (#{club_id}){rating_str}",
        f"📅 {check_date}   🕐 {from_time}–{to_time}",
        "",
    ]
    for t in sorted(time_courts):
        courts_str = "  ·  ".join(time_courts[t])
        lines.append(f"`{t}`  {courts_str}")

    return "\n".join(lines)


def format_prices(club_id: int) -> str | None:
    """Return a formatted pricing breakdown string, or None if no data."""
    club = _clubs.get(club_id)
    if not club:
        return None
    rent_rates = club.get("rent_rates")
    if not isinstance(rent_rates, list) or not rent_rates:
        return None

    by_type: dict[int, list] = {}
    for r in rent_rates:
        by_type.setdefault(r.get("court_type_id", 0), []).append(r)

    lines = [f"💰 *Prices — {club_label(club_id)}*\n"]
    for ct, rates in sorted(by_type.items()):
        lines.append(f"*{COURT_EMOJI.get(ct, '')} {COURT_TYPE_NAMES.get(ct, f'Type {ct}')}*")
        for r in rates:
            try:
                days_raw = json.loads(r.get("days", "[]"))
                days_str = ", ".join(DAYS_LAZUZ.get(int(d), str(d)) for d in days_raw)
            except Exception:
                days_str = str(r.get("days", ""))
            lines.append(
                f"  {r.get('fromTime', '')[:5]}–{r.get('toTime', '')[:5]}"
                f"  [{days_str}]  ₪{r.get('price', '?')}"
            )
        lines.append("")
    return "\n".join(lines)
