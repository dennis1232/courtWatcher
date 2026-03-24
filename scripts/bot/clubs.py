"""Club data — loaded once at startup from data/clubs.json."""

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

_CLUBS_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "clubs.json"

# Public data stores (read-only after load_clubs())
_clubs:        dict[int, dict]       = {}
_city_clubs:   dict[str, list[dict]] = {}   # normalised city key → clubs
_city_display: dict[str, str]        = {}   # normalised city key → display name
_city_order:   list[str]             = []   # cities in display order (Tel Aviv first)


def _norm_city(raw: str) -> str:
    c = raw.strip().lower()
    if "tel aviv" in c or "תל אביב" in c or "jaffa" in c:
        return "tel_aviv"
    return c.replace(" ", "_")[:20]


def load_clubs() -> None:
    if not _CLUBS_PATH.exists():
        log.warning("data/clubs.json not found — run fetch_clubs.py first")
        return

    data = json.loads(_CLUBS_PATH.read_text())
    for club in data.get("clubs", []):
        _clubs[club["id"]] = club

        raw_city = (club.get("city_english") or club.get("city") or "").strip()
        if not raw_city:
            continue
        key = _norm_city(raw_city)
        _city_clubs.setdefault(key, []).append(club)

        if key not in _city_display:
            _city_display[key] = "Tel Aviv" if key == "tel_aviv" else raw_city.strip().title()

    other = sorted(k for k in _city_clubs if k != "tel_aviv")
    _city_order.clear()
    if "tel_aviv" in _city_clubs:
        _city_order.append("tel_aviv")
    _city_order.extend(other)

    log.info("Loaded %d clubs across %d cities", len(_clubs), len(_city_order))


def club_name(club_id: int) -> str:
    c = _clubs.get(club_id)
    return (c.get("name_english") or c.get("name", f"Club #{club_id}")).strip() if c else f"Club #{club_id}"


def club_label(club_id: int) -> str:
    return f"{club_name(club_id)} (#{club_id})"
