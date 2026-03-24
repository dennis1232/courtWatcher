"""Watcher state, rate-limited API calls, and background polling loop."""

import asyncio
import logging
import random
import time

from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode

from .constants import MIN_REQUEST_GAP, WATCHER_MAX_HOURS, COURT_TYPE_NAMES
from .clubs import _clubs, club_label
from .formatters import slots_in_range, format_hits

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

_api_lock       = asyncio.Lock()
_last_request_at: float = 0.0


async def rate_limited_check(club_id: int, check_date: str,
                              from_time: str, to_time: str, court_type: int) -> list[dict]:
    global _last_request_at
    async with _api_lock:
        gap = time.monotonic() - _last_request_at
        if gap < MIN_REQUEST_GAP:
            await asyncio.sleep(MIN_REQUEST_GAP - gap)
        try:
            return await _check_once(club_id, check_date, from_time, to_time, court_type)
        finally:
            _last_request_at = time.monotonic()


async def _check_once(club_id: int, check_date: str,
                      from_time: str, to_time: str, court_type: int) -> list[dict]:
    from lazuz_api.client import LazuzClient  # imported here to keep sys.path setup in main

    club             = _clubs.get(club_id, {})
    external_club_id = club.get("external_club_id") or None
    async with LazuzClient() as client:
        data = await client.get_available_slots(
            club_id=club_id, date=check_date, court_type=court_type,
            external_club_id=external_club_id,
        )

    all_courts = sorted(data.get("courts", []), key=lambda c: c.get("courtId", 0))
    hits = []
    for i, court in enumerate(all_courts, start=1):
        raw_name = court.get("name", "")
        display_name = str(raw_name).strip() if raw_name and not str(raw_name).strip().isdigit() else f"Court {i}"
        matching = slots_in_range(court.get("availbleTimeSlot", []), from_time, to_time)
        if matching:
            hits.append({"name": display_name, "slots": matching})
    return hits


# ---------------------------------------------------------------------------
# Watcher state & loop
# ---------------------------------------------------------------------------

_watchers: dict[int, dict[str, asyncio.Task]] = {}


async def _watcher_loop(chat_id: int, club_id: int, check_date: str,
                        from_time: str, to_time: str, court_type: int,
                        interval: int, bot, expire_at: float) -> None:
    last_fp: frozenset = frozenset()
    label = club_label(club_id)
    await asyncio.sleep(random.uniform(0, min(interval * 60 * 0.3, 20)))

    while True:
        if time.time() >= expire_at:
            sport = COURT_TYPE_NAMES.get(court_type, "")
            await bot.send_message(
                chat_id=chat_id,
                text=(
                    f"⏰ *Watcher expired* after {WATCHER_MAX_HOURS}h\n"
                    f"📍 {label}   {sport}   🕐 {from_time}–{to_time}\n\n"
                    f"No slots were found. Use /start to set up a new one."
                ),
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        try:
            hits = await rate_limited_check(club_id, check_date, from_time, to_time, court_type)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("Watcher error club %s: %s", club_id, e)
            await asyncio.sleep(interval * 60)
            continue

        if hits:
            fp = frozenset((c["name"], tuple(c["slots"])) for c in hits)
            if fp != last_fp:
                key      = f"{club_id}_{from_time}_{to_time}_{check_date}"
                msg      = format_hits(hits, club_id, check_date, from_time, to_time, court_type)
                remaining = max(0, int((expire_at - time.time()) / 60))
                msg += f"\n\n_Checking every {interval}m · expires in {remaining}m_"
                kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton("❌ Stop watching", callback_data=f"stp:{key}")
                ]])
                await bot.send_message(chat_id=chat_id, text=msg,
                                       parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
                last_fp = fp
        else:
            if last_fp:
                sport = COURT_TYPE_NAMES.get(court_type, "")
                await bot.send_message(
                    chat_id=chat_id,
                    text=f"⚠️ Slots gone — {label} {sport} {from_time}–{to_time}\nStill watching…",
                )
            last_fp = frozenset()

        await asyncio.sleep(interval * 60 * random.uniform(0.8, 1.2))


def start_watcher(chat_id: int, club_id: int, check_date: str,
                  from_time: str, to_time: str, court_type: int,
                  interval: int, bot) -> tuple[bool, str]:
    """Start a background watcher. Returns (started, key)."""
    key      = f"{club_id}_{from_time}_{to_time}_{check_date}"
    existing = _watchers.get(chat_id, {}).get(key)
    if existing and not existing.done():
        return False, key
    expire_at = time.time() + WATCHER_MAX_HOURS * 3600
    task = asyncio.create_task(_watcher_loop(
        chat_id, club_id, check_date, from_time, to_time, court_type, interval, bot, expire_at
    ))
    _watchers.setdefault(chat_id, {})[key] = task
    return True, key
