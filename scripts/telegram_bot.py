"""Lazuz Telegram Bot — guided flow edition.

Flow:
    1. /start  → pick sport
    2.         → pick city  (Tel Aviv first)
    3.         → pick club
    4.         → pick date
    5.         → pick time range
    6.         → confirmation + Watch / Check now

Watchers auto-expire after 3 hours.
Power commands still work: /watch /check /find /stop /status
"""

import asyncio
import json
import os
import sys
import logging
import random
import time
from datetime import date, timedelta, datetime
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler,
    MessageHandler, ContextTypes, filters,
)
from telegram.constants import ParseMode

from lazuz_api.client import LazuzClient

load_dotenv()

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

COURT_TYPE_NAMES = {3: "Tennis",      6: "Football",  10: "Pickleball"}
COURT_EMOJI      = {3: "🎾",          6: "⚽",         10: "🏓"}
DAYS_LAZUZ       = {0: "Sun", 1: "Mon", 2: "Tue", 3: "Wed", 4: "Thu", 5: "Fri", 6: "Sat"}

WATCHER_MAX_HOURS = 3

TIME_PRESETS = [
    ("Morning  06–12", "06:00", "12:00"),
    ("Midday   12–16", "12:00", "16:00"),
    ("Evening  16–20", "16:00", "20:00"),
    ("Night    20–23", "20:00", "23:00"),
    ("Full day 06–23", "06:00", "23:00"),
]


# ---------------------------------------------------------------------------
# Club data — loaded once at startup
# ---------------------------------------------------------------------------

_CLUBS_PATH = Path(__file__).resolve().parent.parent / "data" / "clubs.json"
_clubs: dict[int, dict] = {}
_city_clubs: dict[str, list[dict]] = {}   # normalised city → clubs
_city_display: dict[str, str] = {}         # normalised city → display name
_city_order: list[str] = []                # normalised cities in display order


def _norm_city(raw: str) -> str:
    c = raw.strip().lower()
    if "tel aviv" in c or "תל אביב" in c or "jaffa" in c:
        return "tel_aviv"
    return c.replace(" ", "_")[:20]


def _load_clubs() -> None:
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

    # Tel Aviv first, then alphabetical
    other = sorted(k for k in _city_clubs if k != "tel_aviv")
    _city_order.clear()
    if "tel_aviv" in _city_clubs:
        _city_order.append("tel_aviv")
    _city_order.extend(other)

    log.info("Loaded %d clubs across %d cities", len(_clubs), len(_city_order))


def _club_name(club_id: int) -> str:
    c = _clubs.get(club_id)
    return (c.get("name_english") or c.get("name", f"Club #{club_id}")).strip() if c else f"Club #{club_id}"


def _club_label(club_id: int) -> str:
    return f"{_club_name(club_id)} (#{club_id})"

# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

MIN_REQUEST_GAP = 3.0
_api_lock = asyncio.Lock()
_last_request_at: float = 0.0


async def _rate_limited_check(club_id, check_date, from_time, to_time, court_type) -> list[dict]:
    global _last_request_at
    async with _api_lock:
        gap = time.monotonic() - _last_request_at
        if gap < MIN_REQUEST_GAP:
            await asyncio.sleep(MIN_REQUEST_GAP - gap)
        try:
            return await _check_once(club_id, check_date, from_time, to_time, court_type)
        finally:
            _last_request_at = time.monotonic()

# ---------------------------------------------------------------------------
# Core availability logic
# ---------------------------------------------------------------------------

def _slots_in_range(slots: list[str], from_time: str, to_time: str) -> list[str]:
    from_t = datetime.strptime(from_time, "%H:%M").time()
    to_t   = datetime.strptime(to_time,   "%H:%M").time()
    return [s for s in slots if from_t <= datetime.strptime(s[:5], "%H:%M").time() < to_t]


async def _check_once(club_id, check_date, from_time, to_time, court_type) -> list[dict]:
    # Use external_club_id from local JSON to avoid 2 extra club-settings API calls per check
    club = _clubs.get(club_id, {})
    external_club_id = club.get("external_club_id") or None
    async with LazuzClient() as client:
        data = await client.get_available_slots(
            club_id=club_id, date=check_date, court_type=court_type,
            external_club_id=external_club_id,
        )

    # Sort courts by courtId so numbering is stable, then assign "Court N" labels.
    # If the API already provides a meaningful name (not a raw number), use it directly.
    all_courts = sorted(data.get("courts", []), key=lambda c: c.get("courtId", 0))
    hits = []
    for i, court in enumerate(all_courts, start=1):
        raw_name = court.get("name", "")
        if raw_name and not str(raw_name).strip().isdigit():
            display_name = str(raw_name).strip()
        else:
            display_name = f"Court {i}"
        matching = _slots_in_range(court.get("availbleTimeSlot", []), from_time, to_time)
        if matching:
            hits.append({"name": display_name, "slots": matching})
    return hits


def _format_hits(hits, club_id, check_date, from_time, to_time, court_type) -> str:
    emoji = COURT_EMOJI.get(court_type, "🏟")
    sport = COURT_TYPE_NAMES.get(court_type, str(court_type))
    lines = [
        f"{emoji} *{sport} available!*",
        f"📍 {_club_label(club_id)}",
        f"📅 {check_date}   🕐 {from_time}–{to_time}",
        "",
    ]
    for court in hits:
        slots_str = "  ".join(s[:5] for s in court["slots"])
        lines.append(f"*{court['name']}*\n{slots_str}")
    return "\n".join(lines)

# ---------------------------------------------------------------------------
# Watcher state & loop
# ---------------------------------------------------------------------------

_watchers: dict[int, dict[str, asyncio.Task]] = {}


async def _watcher_loop(chat_id, club_id, check_date, from_time, to_time, court_type, interval, bot, expire_at: float):
    last_fp: frozenset = frozenset()
    label = _club_label(club_id)
    await asyncio.sleep(random.uniform(0, min(interval * 60 * 0.3, 20)))

    while True:
        # Auto-expire after WATCHER_MAX_HOURS
        if time.time() >= expire_at:
            sport = COURT_TYPE_NAMES.get(court_type, "")
            await bot.send_message(
                chat_id=chat_id,
                text=f"⏰ *Watcher expired* after {WATCHER_MAX_HOURS}h\n"
                     f"📍 {label}   {sport}   🕐 {from_time}–{to_time}\n\n"
                     f"No slots were found. Use /start to set up a new one.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

        try:
            hits = await _rate_limited_check(club_id, check_date, from_time, to_time, court_type)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("Watcher error club %s: %s", club_id, e)
            await asyncio.sleep(interval * 60)
            continue

        if hits:
            fp = frozenset((c["name"], tuple(c["slots"])) for c in hits)
            if fp != last_fp:
                key = f"{club_id}_{from_time}_{to_time}_{check_date}"
                msg = _format_hits(hits, club_id, check_date, from_time, to_time, court_type)
                remaining = max(0, int((expire_at - time.time()) / 60))
                msg += f"\n\n_Checking every {interval}m · expires in {remaining}m_"
                kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton("❌ Stop watching", callback_data=f"stp:{key}")
                ]])
                await bot.send_message(chat_id=chat_id, text=msg, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
                last_fp = fp
        else:
            if last_fp:
                sport = COURT_TYPE_NAMES.get(court_type, "")
                await bot.send_message(
                    chat_id=chat_id,
                    text=f"⚠️ Slots gone — {label} {sport} {from_time}–{to_time}\nStill watching...",
                )
            last_fp = frozenset()

        await asyncio.sleep(interval * 60 * random.uniform(0.8, 1.2))


def _start_watcher(chat_id, club_id, check_date, from_time, to_time, court_type, interval, bot) -> tuple[bool, str]:
    key = f"{club_id}_{from_time}_{to_time}_{check_date}"
    existing = _watchers.get(chat_id, {}).get(key)
    if existing and not existing.done():
        return False, key
    expire_at = time.time() + WATCHER_MAX_HOURS * 3600
    task = asyncio.create_task(_watcher_loop(
        chat_id, club_id, check_date, from_time, to_time, court_type, interval, bot, expire_at
    ))
    _watchers.setdefault(chat_id, {})[key] = task
    return True, key

# ---------------------------------------------------------------------------
# Guided flow — keyboard builders
# ---------------------------------------------------------------------------

def _kb_sport() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{COURT_EMOJI[ct]} {COURT_TYPE_NAMES[ct]}", callback_data=f"fs:{ct}")
         for ct in COURT_TYPE_NAMES],
        [InlineKeyboardButton("📋 My Watchers", callback_data="my_watchers")],
    ])


def _kb_city(court_type: int, expanded: bool = False) -> InlineKeyboardMarkup:
    cities  = _city_order if expanded else _city_order[:10]
    rows    = []
    row     = []
    for key in cities:
        display = _city_display.get(key, key)
        row.append(InlineKeyboardButton(display, callback_data=f"fc:{court_type}:{key}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if not expanded and len(_city_order) > 10:
        rows.append([InlineKeyboardButton(
            f"➕ Show all cities ({len(_city_order)})",
            callback_data=f"fx:{court_type}",
        )])
    return InlineKeyboardMarkup(rows)


def _clubs_card(court_type: int, city_key: str) -> tuple[str, InlineKeyboardMarkup] | tuple[None, None]:
    """Return (message_text, keyboard) for the club selection step."""
    clubs = _city_clubs.get(city_key, [])
    if not clubs:
        return None, None

    sorted_clubs = sorted(clubs, key=lambda c: -(c.get("rating") or 0))
    city_disp    = _city_display.get(city_key, city_key)
    sport        = f"{COURT_EMOJI[court_type]} {COURT_TYPE_NAMES[court_type]}"
    header       = f"{sport} · *{city_disp}*\n\nChoose a club:"

    rows = []
    for club in sorted_clubs:
        cid  = club["id"]
        name = (club.get("name_english") or club.get("name", "")).strip()
        rows.append([InlineKeyboardButton(name[:60], callback_data=f"fb:{court_type}:{city_key[:12]}:{cid}")])

    return header, InlineKeyboardMarkup(rows)


def _kb_date(court_type: int, club_id: int) -> InlineKeyboardMarkup:
    today    = date.today()
    tomorrow = today + timedelta(days=1)
    day2     = today + timedelta(days=2)
    day3     = today + timedelta(days=3)

    def btn(label, d):
        return InlineKeyboardButton(label, callback_data=f"fd:{court_type}:{club_id}:{d.isoformat()}")

    return InlineKeyboardMarkup([
        [btn(f"Today ({today.strftime('%a %d/%m')})",       today),
         btn(f"Tomorrow ({tomorrow.strftime('%a %d/%m')})", tomorrow)],
        [btn(f"{day2.strftime('%A %d/%m')}",                day2),
         btn(f"{day3.strftime('%A %d/%m')}",                day3)],
        [InlineKeyboardButton("📅 Other date…", callback_data=f"fdx:{court_type}:{club_id}")],
    ])


def _kb_time(court_type: int, club_id: int, check_date: str) -> InlineKeyboardMarkup:
    rows = []
    for label, frm, to in TIME_PRESETS:
        cb = f"ftp:{court_type}:{club_id}:{check_date}:{frm}:{to}"
        rows.append([InlineKeyboardButton(label, callback_data=cb)])
    rows.append([InlineKeyboardButton("✏️ Custom time…", callback_data=f"ftx:{court_type}:{club_id}:{check_date}")])
    return InlineKeyboardMarkup(rows)


def _kb_confirm(court_type: int, club_id: int, check_date: str, from_time: str, to_time: str) -> InlineKeyboardMarkup:
    base = f"{court_type}:{club_id}:{check_date}:{from_time}:{to_time}"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"👀 Watch ({WATCHER_MAX_HOURS}h)", callback_data=f"fw:{base}"),
            InlineKeyboardButton("🔍 Check now",                     callback_data=f"fk:{base}"),
        ],
        [InlineKeyboardButton("🔄 Start over", callback_data="restart")],
    ])

# ---------------------------------------------------------------------------
# /start — entry point
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    ctx.user_data.pop("flow", None)
    await update.message.reply_text(
        "👋 *Welcome to Lazuz Court Bot!*\n\nWhat sport are you looking for?",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_kb_sport(),
    )

# ---------------------------------------------------------------------------
# Callback handler — drives the entire guided flow
# ---------------------------------------------------------------------------

async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()
    data  = q.data
    chat_id = update.effective_chat.id

    # ── My Watchers ──────────────────────────────────────────────────────────
    if data == "my_watchers":
        active = {k: t for k, t in _watchers.get(chat_id, {}).items() if not t.done()}
        if not active:
            await q.edit_message_text(
                "No active watchers.\n\nWhat sport are you looking for?",
                reply_markup=_kb_sport(),
            )
            return
        await q.edit_message_text(
            f"👀 *{len(active)} active watcher(s):*",
            parse_mode=ParseMode.MARKDOWN,
        )
        for key in active:
            parts   = key.split("_")
            club_id = int(parts[0])
            sport_key = key  # reuse full key for stop button
            await q.message.reply_text(
                f"📍 *{_club_label(club_id)}*\n🕐 {parts[1]}–{parts[2]}   📅 {parts[3]}",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("❌ Stop", callback_data=f"stp:{sport_key}"),
                    InlineKeyboardButton("🔄 New search", callback_data="restart"),
                ]]),
            )
        return

    # ── Restart ──────────────────────────────────────────────────────────────
    if data == "restart":
        ctx.user_data.pop("flow", None)
        await q.edit_message_text(
            "What sport are you looking for?",
            reply_markup=_kb_sport(),
        )
        return

    # ── Step 1: sport chosen ─────────────────────────────────────────────────
    if data.startswith("fs:"):
        court_type = int(data.split(":")[1])
        sport = f"{COURT_EMOJI[court_type]} {COURT_TYPE_NAMES[court_type]}"
        await q.edit_message_text(
            f"{sport} — choose a city or area:",
            reply_markup=_kb_city(court_type),
        )

    # ── Expand city list ──────────────────────────────────────────────────────
    elif data.startswith("fx:"):
        court_type = int(data.split(":")[1])
        sport = f"{COURT_EMOJI[court_type]} {COURT_TYPE_NAMES[court_type]}"
        await q.edit_message_text(
            f"{sport} — choose a city or area:",
            reply_markup=_kb_city(court_type, expanded=True),
        )

    # ── Step 2: city chosen ──────────────────────────────────────────────────
    elif data.startswith("fc:"):
        _, ct_s, city_key = data.split(":", 2)
        court_type = int(ct_s)
        text, kb = _clubs_card(court_type, city_key)
        if not kb:
            await q.edit_message_text("No clubs found for that city. Try another.")
            return
        await q.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

    # ── Step 3: club chosen ──────────────────────────────────────────────────
    elif data.startswith("fb:"):
        parts = data.split(":")
        court_type = int(parts[1])
        club_id    = int(parts[3])
        label      = _club_label(club_id)
        sport      = f"{COURT_EMOJI[court_type]} {COURT_TYPE_NAMES[court_type]}"

        # Show club info if available
        club  = _clubs.get(club_id, {})
        addr  = (club.get("address_english") or club.get("address") or "").strip()
        extra = f"\n📍 {addr}" if addr else ""

        await q.edit_message_text(
            f"{sport} · *{label}*{extra}\n\nChoose a date:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_kb_date(court_type, club_id),
        )

    # ── Step 4a: date preset chosen ─────────────────────────────────────────
    elif data.startswith("fd:"):
        parts      = data.split(":")
        court_type = int(parts[1])
        club_id    = int(parts[2])
        check_date = ":".join(parts[3:])   # YYYY-MM-DD (no colons, safe)
        check_date = parts[3]
        sport      = f"{COURT_EMOJI[court_type]} {COURT_TYPE_NAMES[court_type]}"
        await q.edit_message_text(
            f"{sport} · *{_club_label(club_id)}* · {check_date}\n\nChoose a time range:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_kb_time(court_type, club_id, check_date),
        )

    # ── Step 4b: "other date" — ask user to type ─────────────────────────────
    elif data.startswith("fdx:"):
        _, ct_s, club_id_s = data.split(":")
        ctx.user_data["flow"] = {
            "awaiting": "date",
            "court_type": int(ct_s),
            "club_id": int(club_id_s),
        }
        await q.edit_message_text("Type the date you want (`YYYY-MM-DD`):", parse_mode=ParseMode.MARKDOWN)

    # ── Step 5a: time preset chosen → show confirmation ──────────────────────
    elif data.startswith("ftp:"):
        # ftp:{ct}:{club_id}:{date}:{from}:{to}
        parts      = data.split(":")
        court_type = int(parts[1])
        club_id    = int(parts[2])
        check_date = parts[3]
        from_time  = parts[4] + ":" + parts[5]
        to_time    = parts[6] + ":" + parts[7]
        await _show_confirmation(q, court_type, club_id, check_date, from_time, to_time)

    # ── Step 5b: "custom time" — ask user to type ────────────────────────────
    elif data.startswith("ftx:"):
        parts = data.split(":")
        court_type = int(parts[1])
        club_id    = int(parts[2])
        check_date = parts[3]
        ctx.user_data["flow"] = {
            "awaiting":   "time_range",
            "court_type": court_type,
            "club_id":    club_id,
            "check_date": check_date,
        }
        await q.edit_message_text(
            f"*{_club_label(club_id)}* · {check_date}\n\nType your time range:\n`HH:MM - HH:MM`  e.g. `17:00 - 20:00`",
            parse_mode=ParseMode.MARKDOWN,
        )

    # ── Step 6a: confirm → Watch ─────────────────────────────────────────────
    elif data.startswith("fw:"):
        court_type, club_id, check_date, from_time, to_time = _unpack_confirm(data[3:])
        ok, _ = _start_watcher(chat_id, club_id, check_date, from_time, to_time, court_type, 10, ctx.bot)
        sport = f"{COURT_EMOJI[court_type]} {COURT_TYPE_NAMES[court_type]}"
        if ok:
            await q.edit_message_text(
                f"👀 *Watcher started!*\n\n"
                f"{sport}\n"
                f"📍 {_club_label(club_id)}\n"
                f"📅 {check_date}   🕐 {from_time}–{to_time}\n\n"
                f"I'll alert you the moment a slot opens.\n"
                f"⏰ Auto-expires in {WATCHER_MAX_HOURS}h.",
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            await q.edit_message_text(f"Already watching {_club_label(club_id)} for that slot.")

    # ── Step 6b: confirm → Check now ─────────────────────────────────────────
    elif data.startswith("fk:"):
        court_type, club_id, check_date, from_time, to_time = _unpack_confirm(data[3:])
        await q.edit_message_text(f"🔍 Checking {_club_label(club_id)}…")
        try:
            hits = await _rate_limited_check(club_id, check_date, from_time, to_time, court_type)
        except Exception as e:
            await q.message.reply_text(f"Error: {e}")
            return
        if hits:
            await q.message.reply_text(
                _format_hits(hits, club_id, check_date, from_time, to_time, court_type),
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            sport = COURT_TYPE_NAMES.get(court_type, "")
            await q.message.reply_text(
                f"No {sport} slots for *{_club_label(club_id)}*\n"
                f"📅 {check_date}   🕐 {from_time}–{to_time}\n\n"
                f"Want to watch for openings?",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(f"👀 Watch ({WATCHER_MAX_HOURS}h)",
                                         callback_data=f"fw:{court_type}:{club_id}:{check_date}:{from_time}:{to_time}"),
                ]]),
            )

    # ── Stop watcher (from alert button) ─────────────────────────────────────
    elif data.startswith("stp:"):
        key = data[4:]
        w = _watchers.get(chat_id, {})
        if key in w:
            w[key].cancel()
            del w[key]
            await q.edit_message_reply_markup(reply_markup=None)
            await q.message.reply_text("✅ Watcher stopped.")
        else:
            await q.answer("Watcher already stopped.", show_alert=True)

    # ── Legacy /find flow actions ─────────────────────────────────────────────
    elif data.startswith("act:"):
        _, club_id_s, action = data.split(":")
        club_id = int(club_id_s)
        if action == "p":
            await _show_prices_msg(q, club_id)
        else:
            await q.edit_message_reply_markup(reply_markup=None)
            await q.message.reply_text(
                f"{'👀 Watch' if action == 'w' else '🔍 Check'} *{_club_label(club_id)}*\n\nChoose sport:",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(f"{COURT_EMOJI[ct]} {COURT_TYPE_NAMES[ct]}",
                                         callback_data=f"spt:{club_id}:{action}:{ct}")
                    for ct in COURT_TYPE_NAMES
                ]]),
            )

    elif data.startswith("spt:"):
        _, club_id_s, action, ct_s = data.split(":")
        club_id, court_type = int(club_id_s), int(ct_s)
        check_date = (date.today() + timedelta(days=1)).isoformat()
        await q.edit_message_reply_markup(reply_markup=None)
        await q.message.reply_text(
            f"{COURT_EMOJI[court_type]} {COURT_TYPE_NAMES[court_type]} · *{_club_label(club_id)}*\n\nChoose time range:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_kb_time(court_type, club_id, check_date),
        )


def _unpack_confirm(s: str):
    """Unpack '{ct}:{club_id}:{date}:{HH}:{MM}:{HH}:{MM}'"""
    parts = s.split(":")
    court_type = int(parts[0])
    club_id    = int(parts[1])
    check_date = parts[2]
    from_time  = f"{parts[3]}:{parts[4]}"
    to_time    = f"{parts[5]}:{parts[6]}"
    return court_type, club_id, check_date, from_time, to_time


async def _show_confirmation(q, court_type, club_id, check_date, from_time, to_time):
    sport  = f"{COURT_EMOJI[court_type]} {COURT_TYPE_NAMES[court_type]}"
    label  = _club_label(club_id)
    club   = _clubs.get(club_id, {})
    city   = (club.get("city_english") or club.get("city") or "").strip()
    rating = club.get("rating")
    rating_str = f"  ⭐ {rating}" if rating else ""

    text = (
        f"*Summary*\n\n"
        f"{sport}\n"
        f"📍 {label}{rating_str}\n"
        f"🏙 {city}\n"
        f"📅 {check_date}\n"
        f"🕐 {from_time} – {to_time}\n\n"
        f"What would you like to do?"
    )
    await q.edit_message_text(
        text,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=_kb_confirm(court_type, club_id, check_date, from_time, to_time),
    )


async def _show_prices_msg(q, club_id: int) -> None:
    club = _clubs.get(club_id)
    if not club:
        await q.message.reply_text(f"Club #{club_id} not found.")
        return
    rent_rates = club.get("rent_rates")
    if not isinstance(rent_rates, list) or not rent_rates:
        await q.message.reply_text(f"*{_club_label(club_id)}*\nNo pricing data available.", parse_mode=ParseMode.MARKDOWN)
        return
    by_type: dict[int, list] = {}
    for r in rent_rates:
        by_type.setdefault(r.get("court_type_id", 0), []).append(r)
    lines = [f"💰 *Prices — {_club_label(club_id)}*\n"]
    for ct, rates in sorted(by_type.items()):
        lines.append(f"*{COURT_EMOJI.get(ct,'')} {COURT_TYPE_NAMES.get(ct, f'Type {ct}')}*")
        for r in rates:
            try:
                days_raw = json.loads(r.get("days", "[]"))
                days_str = ", ".join(DAYS_LAZUZ.get(int(d), str(d)) for d in days_raw)
            except Exception:
                days_str = str(r.get("days", ""))
            lines.append(f"  {r.get('fromTime','')[:5]}–{r.get('toTime','')[:5]}  [{days_str}]  ₪{r.get('price','?')}")
        lines.append("")
    await q.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)

# ---------------------------------------------------------------------------
# Free-text handler — captures custom date / time input
# ---------------------------------------------------------------------------

async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    flow = ctx.user_data.get("flow")
    if not flow:
        await update.message.reply_text(
            "Use /start to begin or /help for all commands.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🚀 Start", callback_data="restart")
            ]]),
        )
        return

    text = update.message.text.strip()

    if flow["awaiting"] == "date":
        if text.lower() == "today":
            check_date = date.today().isoformat()
        elif text.lower() == "tomorrow":
            check_date = (date.today() + timedelta(days=1)).isoformat()
        else:
            try:
                datetime.strptime(text, "%Y-%m-%d")
                check_date = text
            except ValueError:
                await update.message.reply_text("Invalid date. Use `YYYY-MM-DD`:", parse_mode=ParseMode.MARKDOWN)
                return
        flow["check_date"] = check_date
        flow["awaiting"]   = "time_range"
        ctx.user_data["flow"] = flow
        sport = f"{COURT_EMOJI[flow['court_type']]} {COURT_TYPE_NAMES[flow['court_type']]}"
        await update.message.reply_text(
            f"{sport} · *{_club_label(flow['club_id'])}* · {check_date}\n\nChoose a time range:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_kb_time(flow["court_type"], flow["club_id"], check_date),
        )
        return

    if flow["awaiting"] == "time_range":
        cleaned = text.replace(" ", "").replace("–", "-").replace("—", "-")
        if "-" not in cleaned:
            await update.message.reply_text("Format: `HH:MM - HH:MM`  e.g. `17:00 - 20:00`", parse_mode=ParseMode.MARKDOWN)
            return
        parts = cleaned.split("-", 1)
        try:
            from_time = datetime.strptime(parts[0], "%H:%M").strftime("%H:%M")
            to_time   = datetime.strptime(parts[1], "%H:%M").strftime("%H:%M")
        except ValueError:
            await update.message.reply_text("Invalid time. Use `HH:MM - HH:MM`:", parse_mode=ParseMode.MARKDOWN)
            return

        court_type = flow["court_type"]
        club_id    = flow["club_id"]
        check_date = flow.get("check_date", (date.today() + timedelta(days=1)).isoformat())
        ctx.user_data.pop("flow", None)

        sport  = f"{COURT_EMOJI[court_type]} {COURT_TYPE_NAMES[court_type]}"
        label  = _club_label(club_id)
        club   = _clubs.get(club_id, {})
        city   = (club.get("city_english") or club.get("city") or "").strip()
        rating = club.get("rating")
        rating_str = f"  ⭐ {rating}" if rating else ""

        await update.message.reply_text(
            f"*Summary*\n\n"
            f"{sport}\n"
            f"📍 {label}{rating_str}\n"
            f"🏙 {city}\n"
            f"📅 {check_date}\n"
            f"🕐 {from_time} – {to_time}\n\n"
            f"What would you like to do?",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=_kb_confirm(court_type, club_id, check_date, from_time, to_time),
        )

# ---------------------------------------------------------------------------
# Power commands
# ---------------------------------------------------------------------------

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "*Lazuz Court Bot*\n\n"
        "🚀 /start — guided flow (sport → city → club → date → time)\n"
        "🔎 /find <name or city> — quick club search\n"
        "📋 /status — active watchers\n"
        "🛑 /stop [club\\_id] — stop watcher(s)\n\n"
        "*Power commands:*\n"
        "`/watch <id> <from> <to> [date] [type]`\n"
        "`/check <id> <from> <to> [date] [type]`",
        parse_mode=ParseMode.MARKDOWN,
    )


async def cmd_find(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query = " ".join(ctx.args or []).strip()
    if not query:
        await update.message.reply_text("Usage: `/find <name or city>`", parse_mode=ParseMode.MARKDOWN)
        return
    q = query.lower()
    results = [
        c for c in _clubs.values()
        if q in (c.get("name_english") or "").lower()
        or q in (c.get("name") or "").lower()
        or q in (c.get("city_english") or "").lower()
        or q in (c.get("city") or "").lower()
    ]
    if not results:
        await update.message.reply_text(f"No clubs found for *{query}*.", parse_mode=ParseMode.MARKDOWN)
        return
    for club in sorted(results, key=lambda c: c.get("id", 0))[:8]:
        cid    = club["id"]
        name   = (club.get("name_english") or club.get("name", "")).strip()
        city   = (club.get("city_english") or club.get("city", "")).strip()
        addr   = (club.get("address_english") or club.get("address", "")).strip()
        rating = club.get("rating")
        rating_str = f"  ⭐ {rating}" if rating else ""
        await update.message.reply_text(
            f"📍 *{name}*{rating_str}\n{city} — {addr}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("👀 Watch",  callback_data=f"act:{cid}:w"),
                InlineKeyboardButton("🔍 Check",  callback_data=f"act:{cid}:k"),
                InlineKeyboardButton("💰 Prices", callback_data=f"act:{cid}:p"),
            ]]),
        )


def _parse_args(args):
    if len(args) < 3:
        raise ValueError("Need: `<club_id> <HH:MM> <HH:MM>`")
    r = {
        "club_id": int(args[0]), "from_time": args[1], "to_time": args[2],
        "check_date": (date.today() + timedelta(days=1)).isoformat(),
        "court_type": 3, "interval": 10,
    }
    for a in args[3:]:
        if len(a) == 10 and a[4] == "-" and a[7] == "-":
            r["check_date"] = a
        elif a.isdigit() and int(a) in COURT_TYPE_NAMES:
            r["court_type"] = int(a)
        elif a.isdigit():
            r["interval"] = int(a)
    return r


async def cmd_watch(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        p = _parse_args(ctx.args or [])
    except ValueError as e:
        await update.message.reply_text(str(e), parse_mode=ParseMode.MARKDOWN)
        return
    ok, _ = _start_watcher(update.effective_chat.id, p["club_id"], p["check_date"],
                            p["from_time"], p["to_time"], p["court_type"], p["interval"], ctx.bot)
    sport = f"{COURT_EMOJI.get(p['court_type'],'')} {COURT_TYPE_NAMES.get(p['court_type'],'')}"
    if ok:
        await update.message.reply_text(
            f"👀 *Watching {_club_label(p['club_id'])}*\n{sport}  📅 {p['check_date']}  🕐 {p['from_time']}–{p['to_time']}\n"
            f"⏰ Auto-expires in {WATCHER_MAX_HOURS}h",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await update.message.reply_text("Already watching that slot.")


async def cmd_check(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        p = _parse_args(ctx.args or [])
    except ValueError as e:
        await update.message.reply_text(str(e), parse_mode=ParseMode.MARKDOWN)
        return
    await update.message.reply_text(f"🔍 Checking {_club_label(p['club_id'])}…")
    try:
        hits = await _rate_limited_check(p["club_id"], p["check_date"], p["from_time"], p["to_time"], p["court_type"])
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")
        return
    if hits:
        await update.message.reply_text(
            _format_hits(hits, p["club_id"], p["check_date"], p["from_time"], p["to_time"], p["court_type"]),
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await update.message.reply_text(f"No slots found for {_club_label(p['club_id'])}.")


async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id  = update.effective_chat.id
    watchers = _watchers.get(chat_id, {})
    if not watchers:
        await update.message.reply_text("No active watchers.")
        return
    filt = int(ctx.args[0]) if ctx.args and ctx.args[0].isdigit() else None
    keys = [k for k in list(watchers) if filt is None or k.startswith(f"{filt}_")]
    if not keys:
        await update.message.reply_text(f"No watcher for club {filt}.")
        return
    for k in keys:
        watchers[k].cancel()
        del watchers[k]
    await update.message.reply_text(f"Stopped {len(keys)} watcher(s).")


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    active  = {k: t for k, t in _watchers.get(chat_id, {}).items() if not t.done()}
    if not active:
        await update.message.reply_text("No active watchers.")
        return
    for key in active:
        parts   = key.split("_")
        club_id = int(parts[0])
        await update.message.reply_text(
            f"👀 *{_club_label(club_id)}*\n🕐 {parts[1]}–{parts[2]}   📅 {parts[3]}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Stop", callback_data=f"stp:{key}")
            ]]),
        )

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        print("Error: TELEGRAM_BOT_TOKEN not set in .env")
        sys.exit(1)

    _load_clubs()

    app = Application.builder().token(token).build()
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("help",   cmd_help))
    app.add_handler(CommandHandler("find",   cmd_find))
    app.add_handler(CommandHandler("watch",  cmd_watch))
    app.add_handler(CommandHandler("check",  cmd_check))
    app.add_handler(CommandHandler("stop",   cmd_stop))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    log.info("Bot ready — %d clubs, %d cities", len(_clubs), len(_city_order))
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
