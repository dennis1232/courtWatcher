"""Inline keyboard builders for every step of the guided flow."""

from datetime import date, datetime, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .constants import COURT_EMOJI, COURT_TYPE_NAMES, TIME_PRESETS, WATCHER_MAX_HOURS
from .clubs import _clubs, _city_clubs, _city_display, _city_order


# ---------------------------------------------------------------------------
# Sport / city pickers
# ---------------------------------------------------------------------------

def kb_sport(last_search: dict | None = None) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"{COURT_EMOJI[ct]} {COURT_TYPE_NAMES[ct]}", callback_data=f"fs:{ct}")
         for ct in COURT_TYPE_NAMES],
        [InlineKeyboardButton("📋 My Watchers", callback_data="my_watchers")],
    ]
    if last_search:
        ct    = last_search.get("court_type", 3)
        sport = f"{COURT_EMOJI.get(ct, '')} {COURT_TYPE_NAMES.get(ct, '')}"
        n     = len(last_search.get("selected_clubs", []))
        label = (
            f"🔁 {sport} · {n} club{'s' if n != 1 else ''} · "
            f"{last_search.get('from_time', '')}–{last_search.get('to_time', '')}"
        )
        rows.insert(0, [InlineKeyboardButton(label, callback_data="rl")])
    return InlineKeyboardMarkup(rows)


def kb_city(court_type: int, expanded: bool = False) -> InlineKeyboardMarkup:
    cities = _city_order if expanded else _city_order[:10]
    rows, row = [], []
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
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data=f"bk:sport:{court_type}")])
    return InlineKeyboardMarkup(rows)


# ---------------------------------------------------------------------------
# Club picker — multi-select (guided flow)
# ---------------------------------------------------------------------------

def clubs_multiselect(court_type: int, city_key: str,
                      selected_ids: list[int]) -> tuple[str, InlineKeyboardMarkup] | tuple[None, None]:
    clubs = _city_clubs.get(city_key, [])
    if not clubs:
        return None, None

    selected_set = set(selected_ids)
    sorted_clubs = sorted(clubs, key=lambda c: -(c.get("rating") or 0))
    city_disp    = _city_display.get(city_key, city_key)
    sport        = f"{COURT_EMOJI[court_type]} {COURT_TYPE_NAMES[court_type]}"
    n            = len(selected_ids)
    header = (
        f"{sport} · *{city_disp}*\n\n"
        f"Tap clubs to select, then tap *Done*"
        + (f" ({n} selected)" if n else "") + ":"
    )

    rows = []
    for club in sorted_clubs:
        cid   = club["id"]
        name  = (club.get("name_english") or club.get("name", "")).strip()
        label = ("✅ " if cid in selected_set else "⬜ ") + name[:55]
        rows.append([InlineKeyboardButton(label, callback_data=f"fbt:{cid}")])

    if n > 0:
        rows.append([InlineKeyboardButton(f"✅ Done ({n} selected)", callback_data="fbd")])
    else:
        rows.append([InlineKeyboardButton("— select at least one club —", callback_data="noop")])
    rows.append([
        InlineKeyboardButton("⬅️ Back", callback_data="bk:city"),
        InlineKeyboardButton("🔄 Start over", callback_data="restart"),
    ])

    return header, InlineKeyboardMarkup(rows)


# ---------------------------------------------------------------------------
# Club picker — single (legacy, used by /find → act: → spt: path)
# ---------------------------------------------------------------------------

def clubs_single(court_type: int, city_key: str) -> tuple[str, InlineKeyboardMarkup] | tuple[None, None]:
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


# ---------------------------------------------------------------------------
# Date / time pickers — guided multi-club flow (gd: / gtp: callbacks)
# ---------------------------------------------------------------------------

def kb_date_guided() -> InlineKeyboardMarkup:
    today    = date.today()
    tomorrow = today + timedelta(days=1)
    day2     = today + timedelta(days=2)
    day3     = today + timedelta(days=3)

    def btn(label, d):
        return InlineKeyboardButton(label, callback_data=f"gd:{d.isoformat()}")

    return InlineKeyboardMarkup([
        [btn(f"Today ({today.strftime('%a %d/%m')})",       today),
         btn(f"Tomorrow ({tomorrow.strftime('%a %d/%m')})", tomorrow)],
        [btn(f"{day2.strftime('%A %d/%m')}",                day2),
         btn(f"{day3.strftime('%A %d/%m')}",                day3)],
        [InlineKeyboardButton("📅 Other date…", callback_data="gdx")],
        [InlineKeyboardButton("⬅️ Back", callback_data="bk:clubs")],
    ])


def kb_time_guided() -> InlineKeyboardMarkup:
    hour = datetime.now().hour
    if hour < 12:
        presets = [
            ("🌅 07:00–10:00", "07:00", "10:00"),
            ("☀️ 10:00–13:00", "10:00", "13:00"),
            ("🌤 13:00–17:00", "13:00", "17:00"),
            ("🌆 17:00–21:00", "17:00", "21:00"),
        ]
    else:
        presets = [
            ("🌆 17:00–19:00", "17:00", "19:00"),
            ("🌇 19:00–21:00", "19:00", "21:00"),
            ("🌆 17:00–21:00", "17:00", "21:00"),
            ("📆 Full day 06–23", "06:00", "23:00"),
        ]
    rows = []
    for label, frm, to in presets:
        fh, fm = frm.split(":")
        th, tm = to.split(":")
        rows.append([InlineKeyboardButton(label, callback_data=f"gtp:{fh}:{fm}:{th}:{tm}")])
    rows.append([InlineKeyboardButton("✏️ Custom time…", callback_data="gtx")])
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="bk:date")])
    return InlineKeyboardMarkup(rows)


def kb_confirm_guided() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"👀 Watch ({WATCHER_MAX_HOURS}h)", callback_data="gw"),
            InlineKeyboardButton("🔍 Check now",                     callback_data="gk"),
        ],
        [
            InlineKeyboardButton("⬅️ Back", callback_data="bk:time"),
            InlineKeyboardButton("🔄 Start over", callback_data="restart"),
        ],
    ])


# ---------------------------------------------------------------------------
# Date / time pickers — legacy single-club flow (fd: / ftp: callbacks)
# ---------------------------------------------------------------------------

def kb_date_legacy(court_type: int, club_id: int) -> InlineKeyboardMarkup:
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


def kb_time_legacy(court_type: int, club_id: int, check_date: str) -> InlineKeyboardMarkup:
    rows = []
    for label, frm, to in TIME_PRESETS:
        cb = f"ftp:{court_type}:{club_id}:{check_date}:{frm}:{to}"
        rows.append([InlineKeyboardButton(label, callback_data=cb)])
    rows.append([InlineKeyboardButton("✏️ Custom time…", callback_data=f"ftx:{court_type}:{club_id}:{check_date}")])
    return InlineKeyboardMarkup(rows)


def kb_confirm_legacy(court_type: int, club_id: int, check_date: str,
                      from_time: str, to_time: str) -> InlineKeyboardMarkup:
    base = f"{court_type}:{club_id}:{check_date}:{from_time}:{to_time}"
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(f"👀 Watch ({WATCHER_MAX_HOURS}h)", callback_data=f"fw:{base}"),
            InlineKeyboardButton("🔍 Check now",                     callback_data=f"fk:{base}"),
        ],
        [InlineKeyboardButton("🔄 Start over", callback_data="restart")],
    ])


# ---------------------------------------------------------------------------
# Summary screen
# ---------------------------------------------------------------------------

async def show_confirmation_guided(q, flow: dict) -> None:
    """Edit the current message to show the search summary + action buttons."""
    court_type   = flow["court_type"]
    selected_ids = flow["selected_clubs"]
    check_date   = flow["check_date"]
    from_time    = flow["from_time"]
    to_time      = flow["to_time"]
    sport        = f"{COURT_EMOJI[court_type]} {COURT_TYPE_NAMES[court_type]}"

    clubs_lines = []
    for cid in selected_ids:
        club   = _clubs.get(cid, {})
        name   = (club.get("name_english") or club.get("name", f"Club #{cid}")).strip()
        rating = club.get("rating")
        suffix = f"  ⭐ {rating}" if rating else ""
        clubs_lines.append(f"📍 {name} (#{cid}){suffix}")

    n    = len(selected_ids)
    text = (
        f"*Summary*\n\n"
        f"{sport}\n"
        + "\n".join(clubs_lines)
        + f"\n📅 {check_date}\n"
          f"🕐 {from_time} – {to_time}\n\n"
          f"{'1 club' if n == 1 else f'{n} clubs'} · What would you like to do?"
    )
    from telegram.constants import ParseMode
    await q.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_confirm_guided())
