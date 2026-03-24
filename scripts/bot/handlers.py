"""All Telegram command and callback handlers."""

import json
import logging
from datetime import date, datetime, timedelta

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from .constants import COURT_EMOJI, COURT_TYPE_NAMES, WATCHER_MAX_HOURS
from .clubs import _clubs, _city_clubs, club_label
from .formatters import format_hits, format_prices
from .keyboards import (
    kb_sport, kb_city,
    clubs_multiselect, clubs_single,
    kb_date_guided, kb_time_guided, kb_confirm_guided, show_confirmation_guided,
    kb_date_legacy, kb_time_legacy, kb_confirm_legacy,
)
from .watchers import _watchers, rate_limited_check, start_watcher

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# /start  /help
# ---------------------------------------------------------------------------

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    ctx.user_data.pop("flow", None)
    ctx.user_data.pop("llm_history", None)
    last_search = ctx.user_data.get("last_search")
    await update.message.reply_text(
        "👋 *Welcome to Lazuz Court Bot!*\n\nWhat sport are you looking for?",
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb_sport(last_search),
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "*Lazuz Court Bot*\n\n"
        "🚀 /start — guided flow (sport → city → clubs → date → time)\n"
        "🔎 /find <name or city> — quick club search\n"
        "📋 /status — active watchers\n"
        "🛑 /stop [club\\_id] — stop watcher(s)\n\n"
        "*Power commands:*\n"
        "`/watch <id> <from> <to> [date] [type]`\n"
        "`/check <id> <from> <to> [date] [type]`",
        parse_mode=ParseMode.MARKDOWN,
    )


# ---------------------------------------------------------------------------
# /find  /watch  /check  /stop  /status
# ---------------------------------------------------------------------------

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
        cid        = club["id"]
        name       = (club.get("name_english") or club.get("name", "")).strip()
        city       = (club.get("city_english") or club.get("city", "")).strip()
        addr       = (club.get("address_english") or club.get("address", "")).strip()
        rating     = club.get("rating")
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


def _parse_args(args: list[str]) -> dict:
    if len(args) < 3:
        raise ValueError("Need: `<club_id> <HH:MM> <HH:MM>`")
    r = {
        "club_id":    int(args[0]),
        "from_time":  args[1],
        "to_time":    args[2],
        "check_date": (date.today() + timedelta(days=1)).isoformat(),
        "court_type": 3,
        "interval":   10,
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
    ok, _ = start_watcher(update.effective_chat.id, p["club_id"], p["check_date"],
                           p["from_time"], p["to_time"], p["court_type"], p["interval"], ctx.bot)
    sport = f"{COURT_EMOJI.get(p['court_type'], '')} {COURT_TYPE_NAMES.get(p['court_type'], '')}"
    if ok:
        await update.message.reply_text(
            f"👀 *Watching {club_label(p['club_id'])}*\n"
            f"{sport}  📅 {p['check_date']}  🕐 {p['from_time']}–{p['to_time']}\n"
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
    await update.message.reply_text(f"🔍 Checking {club_label(p['club_id'])}…")
    try:
        hits = await rate_limited_check(p["club_id"], p["check_date"],
                                        p["from_time"], p["to_time"], p["court_type"])
    except Exception as e:
        await update.message.reply_text(f"Error: {e}")
        return
    if hits:
        await update.message.reply_text(
            format_hits(hits, p["club_id"], p["check_date"],
                        p["from_time"], p["to_time"], p["court_type"]),
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await update.message.reply_text(f"No slots found for {club_label(p['club_id'])}.")


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
            f"👀 *{club_label(club_id)}*\n🕐 {parts[1]}–{parts[2]}   📅 {parts[3]}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Stop", callback_data=f"stp:{key}")
            ]]),
        )


# ---------------------------------------------------------------------------
# Callback handler — drives the entire guided flow
# ---------------------------------------------------------------------------

async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    q       = update.callback_query
    await q.answer()
    data    = q.data
    chat_id = update.effective_chat.id

    # ── My Watchers ───────────────────────────────────────────────────────────
    if data == "my_watchers":
        active = {k: t for k, t in _watchers.get(chat_id, {}).items() if not t.done()}
        if not active:
            await q.edit_message_text(
                "No active watchers.\n\nWhat sport are you looking for?",
                reply_markup=kb_sport(),
            )
            return
        await q.edit_message_text(f"👀 *{len(active)} active watcher(s):*", parse_mode=ParseMode.MARKDOWN)
        for key in active:
            parts   = key.split("_")
            club_id = int(parts[0])
            await q.message.reply_text(
                f"📍 *{club_label(club_id)}*\n🕐 {parts[1]}–{parts[2]}   📅 {parts[3]}",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("❌ Stop",        callback_data=f"stp:{key}"),
                    InlineKeyboardButton("🔄 New search",  callback_data="restart"),
                ]]),
            )
        return

    # ── Restart ───────────────────────────────────────────────────────────────
    if data == "restart":
        ctx.user_data.pop("flow", None)
        await q.edit_message_text("What sport are you looking for?", reply_markup=kb_sport(ctx.user_data.get("last_search")))
        return

    # ── Repeat last search ────────────────────────────────────────────────────
    if data == "rl":
        last = ctx.user_data.get("last_search")
        if not last:
            await q.edit_message_text(
                "No previous search found. What sport are you looking for?",
                reply_markup=kb_sport(),
            )
            return
        court_type = last["court_type"]
        selected   = last["selected_clubs"]
        check_date = last["check_date"]
        from_time  = last["from_time"]
        to_time    = last["to_time"]
        sport      = f"{COURT_EMOJI[court_type]} {COURT_TYPE_NAMES[court_type]}"
        started    = sum(
            1 for cid in selected
            if start_watcher(chat_id, cid, check_date, from_time, to_time, court_type, 10, ctx.bot)[0]
        )
        skipped = len(selected) - started
        msg = (
            f"👀 *{started} watcher{'s' if started != 1 else ''} restarted!*\n\n"
            f"{sport}\n📅 {check_date}   🕐 {from_time}–{to_time}\n"
            f"⏰ Auto-expires in {WATCHER_MAX_HOURS}h."
        )
        if skipped:
            msg += f"\n\n_{skipped} already watched — skipped._"
        await q.edit_message_text(msg, parse_mode=ParseMode.MARKDOWN)
        return

    # ── Back navigation ───────────────────────────────────────────────────────
    if data.startswith("bk:"):
        parts = data.split(":", 2)
        step  = parts[1]
        flow  = ctx.user_data.get("flow", {})

        if step == "sport":
            ctx.user_data.pop("flow", None)
            await q.edit_message_text(
                "What sport are you looking for?",
                reply_markup=kb_sport(ctx.user_data.get("last_search")),
            )
        elif step == "city":
            court_type = flow.get("court_type", 3)
            sport = f"{COURT_EMOJI[court_type]} {COURT_TYPE_NAMES[court_type]}"
            await q.edit_message_text(
                f"{sport} — choose a city or area:",
                reply_markup=kb_city(court_type),
            )
        elif step == "clubs":
            court_type = flow.get("court_type", 3)
            city_key   = flow.get("city_key", "")
            selected   = flow.get("selected_clubs", [])
            text, kb   = clubs_multiselect(court_type, city_key, selected)
            if not kb:
                await q.edit_message_text(
                    "Couldn't reload clubs. Try starting over.",
                    reply_markup=kb_sport(ctx.user_data.get("last_search")),
                )
                return
            await q.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
        elif step == "date":
            court_type = flow.get("court_type", 3)
            n          = len(flow.get("selected_clubs", []))
            sport      = f"{COURT_EMOJI[court_type]} {COURT_TYPE_NAMES[court_type]}"
            await q.edit_message_text(
                f"{sport} · *{n} club{'s' if n > 1 else ''} selected*\n\nChoose a date:",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb_date_guided(),
            )
        elif step == "time":
            court_type = flow.get("court_type", 3)
            check_date = flow.get("check_date", "")
            n          = len(flow.get("selected_clubs", []))
            sport      = f"{COURT_EMOJI[court_type]} {COURT_TYPE_NAMES[court_type]}"
            await q.edit_message_text(
                f"{sport} · *{n} club{'s' if n > 1 else ''}* · {check_date}\n\nChoose a time range:",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb_time_guided(),
            )
        return

    # ── Step 1: sport ─────────────────────────────────────────────────────────
    if data.startswith("fs:"):
        court_type = int(data.split(":")[1])
        sport = f"{COURT_EMOJI[court_type]} {COURT_TYPE_NAMES[court_type]}"
        await q.edit_message_text(f"{sport} — choose a city or area:", reply_markup=kb_city(court_type))

    elif data.startswith("fx:"):
        court_type = int(data.split(":")[1])
        sport = f"{COURT_EMOJI[court_type]} {COURT_TYPE_NAMES[court_type]}"
        await q.edit_message_text(f"{sport} — choose a city or area:", reply_markup=kb_city(court_type, expanded=True))

    # ── Step 2: city → init flow + multi-select club picker ──────────────────
    elif data.startswith("fc:"):
        _, ct_s, city_key = data.split(":", 2)
        court_type = int(ct_s)
        ctx.user_data["flow"] = {"court_type": court_type, "city_key": city_key, "selected_clubs": []}
        text, kb = clubs_multiselect(court_type, city_key, [])
        if not kb:
            await q.edit_message_text("No clubs found for that city. Try another.")
            return
        await q.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

    # ── Step 3a: toggle a club ────────────────────────────────────────────────
    elif data.startswith("fbt:"):
        club_id  = int(data.split(":")[1])
        flow     = ctx.user_data.get("flow", {})
        selected = flow.get("selected_clubs", [])
        if club_id in selected:
            selected.remove(club_id)
        else:
            selected.append(club_id)
        flow["selected_clubs"] = selected
        ctx.user_data["flow"]  = flow
        text, kb = clubs_multiselect(flow["court_type"], flow["city_key"], selected)
        await q.edit_message_text(text, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

    # ── Step 3b: done selecting clubs → date picker ───────────────────────────
    elif data == "fbd":
        flow = ctx.user_data.get("flow", {})
        if not flow.get("selected_clubs"):
            await q.answer("Please select at least one club first.", show_alert=True)
            return
        court_type = flow["court_type"]
        n          = len(flow["selected_clubs"])
        sport      = f"{COURT_EMOJI[court_type]} {COURT_TYPE_NAMES[court_type]}"
        await q.edit_message_text(
            f"{sport} · *{n} club{'s' if n > 1 else ''} selected*\n\nChoose a date:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_date_guided(),
        )

    elif data == "noop":
        await q.answer("Select at least one club first.", show_alert=True)

    # ── Step 4: date ──────────────────────────────────────────────────────────
    elif data.startswith("gd:"):
        check_date = data[3:]
        flow = ctx.user_data.get("flow", {})
        flow["check_date"] = check_date
        ctx.user_data["flow"] = flow
        court_type = flow["court_type"]
        n = len(flow.get("selected_clubs", []))
        sport = f"{COURT_EMOJI[court_type]} {COURT_TYPE_NAMES[court_type]}"
        await q.edit_message_text(
            f"{sport} · *{n} club{'s' if n > 1 else ''}* · {check_date}\n\nChoose a time range:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_time_guided(),
        )

    elif data == "gdx":
        flow = ctx.user_data.get("flow", {})
        flow["awaiting"] = "date"
        ctx.user_data["flow"] = flow
        await q.edit_message_text("Type the date you want (`YYYY-MM-DD`):", parse_mode=ParseMode.MARKDOWN)

    # ── Step 5: time ──────────────────────────────────────────────────────────
    elif data.startswith("gtp:"):
        parts = data.split(":")
        from_time, to_time = f"{parts[1]}:{parts[2]}", f"{parts[3]}:{parts[4]}"
        flow = ctx.user_data.get("flow", {})
        flow["from_time"] = from_time
        flow["to_time"]   = to_time
        ctx.user_data["flow"] = flow
        await show_confirmation_guided(q, flow)

    elif data == "gtx":
        flow = ctx.user_data.get("flow", {})
        flow["awaiting"] = "time_range"
        ctx.user_data["flow"] = flow
        check_date = flow.get("check_date", "")
        n = len(flow.get("selected_clubs", []))
        await q.edit_message_text(
            f"*{n} club{'s' if n > 1 else ''} selected* · {check_date}\n\n"
            f"Type your time range:\n`HH:MM - HH:MM`  e.g. `17:00 - 20:00`",
            parse_mode=ParseMode.MARKDOWN,
        )

    # ── Step 6a: Watch all ────────────────────────────────────────────────────
    elif data == "gw":
        flow       = ctx.user_data.pop("flow", {})
        court_type = flow["court_type"]
        selected   = flow.get("selected_clubs", [])
        check_date = flow["check_date"]
        from_time  = flow["from_time"]
        to_time    = flow["to_time"]
        sport      = f"{COURT_EMOJI[court_type]} {COURT_TYPE_NAMES[court_type]}"
        started    = sum(
            1 for cid in selected
            if start_watcher(chat_id, cid, check_date, from_time, to_time, court_type, 10, ctx.bot)[0]
        )
        skipped = len(selected) - started

        ctx.user_data["last_search"] = {
            "court_type": court_type, "selected_clubs": selected,
            "check_date": check_date, "from_time": from_time, "to_time": to_time,
        }
        ctx.user_data.pop("llm_history", None)

        base_msg = (
            f"👀 *{started} watcher{'s' if started != 1 else ''} started!*\n\n"
            f"{sport}\n📅 {check_date}   🕐 {from_time}–{to_time}\n"
            f"⏰ Auto-expires in {WATCHER_MAX_HOURS}h."
        )
        if skipped:
            base_msg += f"\n\n_{skipped} club{'s' if skipped > 1 else ''} already watched — skipped._"

        await q.edit_message_text(base_msg + "\n\n🔍 Checking current slots…", parse_mode=ParseMode.MARKDOWN)

        check_lines = []
        for cid in selected[:3]:
            try:
                hits = await rate_limited_check(cid, check_date, from_time, to_time, court_type)
                lbl  = club_label(cid)
                if hits:
                    total   = sum(len(h["slots"]) for h in hits)
                    preview = ", ".join(s[:5] for s in hits[0]["slots"][:3])
                    if len(hits[0]["slots"]) > 3:
                        preview += "…"
                    check_lines.append(f"📍 *{lbl}*: {total} slot{'s' if total != 1 else ''} — {preview}")
                else:
                    check_lines.append(f"📍 *{lbl}*: no slots yet")
            except Exception:
                check_lines.append(f"📍 *{club_label(cid)}*: couldn't check")

        final_msg = base_msg
        if check_lines:
            final_msg += "\n\n*Current availability:*\n" + "\n".join(check_lines)
            if len(selected) > 3:
                extra = len(selected) - 3
                final_msg += f"\n_…and {extra} more club{'s' if extra > 1 else ''} being watched_"
        await q.edit_message_text(final_msg, parse_mode=ParseMode.MARKDOWN)

    # ── Step 6b: Check now ────────────────────────────────────────────────────
    elif data == "gk":
        flow       = ctx.user_data.pop("flow", {})
        court_type = flow["court_type"]
        selected   = flow.get("selected_clubs", [])
        check_date = flow["check_date"]
        from_time  = flow["from_time"]
        to_time    = flow["to_time"]

        ctx.user_data["last_search"] = {
            "court_type": court_type, "selected_clubs": selected,
            "check_date": check_date, "from_time": from_time, "to_time": to_time,
        }
        ctx.user_data.pop("llm_history", None)

        await q.edit_message_text(f"🔍 Checking {len(selected)} club{'s' if len(selected) > 1 else ''}…")
        any_hits = False
        for cid in selected:
            try:
                hits = await rate_limited_check(cid, check_date, from_time, to_time, court_type)
            except Exception as e:
                await q.message.reply_text(f"Error checking {club_label(cid)}: {e}")
                continue
            if hits:
                any_hits = True
                await q.message.reply_text(
                    format_hits(hits, cid, check_date, from_time, to_time, court_type),
                    parse_mode=ParseMode.MARKDOWN,
                )
            else:
                sport = COURT_TYPE_NAMES.get(court_type, "")
                await q.message.reply_text(
                    f"No {sport} slots for *{club_label(cid)}*\n📅 {check_date}   🕐 {from_time}–{to_time}",
                    parse_mode=ParseMode.MARKDOWN,
                )
        if not any_hits and selected:
            await q.message.reply_text(
                "No slots found at any club. Want to watch for openings?",
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(f"👀 Watch all ({WATCHER_MAX_HOURS}h)", callback_data="gw"),
                ]]),
            )

    # ── Legacy: single-club chosen (from /find → act: path) ──────────────────
    elif data.startswith("fb:"):
        parts      = data.split(":")
        court_type = int(parts[1])
        club_id    = int(parts[3])
        club       = _clubs.get(club_id, {})
        addr       = (club.get("address_english") or club.get("address") or "").strip()
        extra      = f"\n📍 {addr}" if addr else ""
        sport      = f"{COURT_EMOJI[court_type]} {COURT_TYPE_NAMES[court_type]}"
        await q.edit_message_text(
            f"{sport} · *{club_label(club_id)}*{extra}\n\nChoose a date:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_date_legacy(court_type, club_id),
        )

    elif data.startswith("fd:"):
        parts      = data.split(":")
        court_type = int(parts[1])
        club_id    = int(parts[2])
        check_date = parts[3]
        sport      = f"{COURT_EMOJI[court_type]} {COURT_TYPE_NAMES[court_type]}"
        await q.edit_message_text(
            f"{sport} · *{club_label(club_id)}* · {check_date}\n\nChoose a time range:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_time_legacy(court_type, club_id, check_date),
        )

    elif data.startswith("fdx:"):
        _, ct_s, club_id_s = data.split(":")
        ctx.user_data["flow"] = {
            "awaiting": "date", "court_type": int(ct_s), "club_id": int(club_id_s),
        }
        await q.edit_message_text("Type the date you want (`YYYY-MM-DD`):", parse_mode=ParseMode.MARKDOWN)

    elif data.startswith("ftp:"):
        parts      = data.split(":")
        court_type = int(parts[1])
        club_id    = int(parts[2])
        check_date = parts[3]
        from_time  = f"{parts[4]}:{parts[5]}"
        to_time    = f"{parts[6]}:{parts[7]}"
        await _show_confirmation_legacy(q, court_type, club_id, check_date, from_time, to_time)

    elif data.startswith("ftx:"):
        parts      = data.split(":")
        court_type = int(parts[1])
        club_id    = int(parts[2])
        check_date = parts[3]
        ctx.user_data["flow"] = {
            "awaiting": "time_range", "court_type": court_type,
            "club_id": club_id, "check_date": check_date,
        }
        await q.edit_message_text(
            f"*{club_label(club_id)}* · {check_date}\n\nType your time range:\n"
            f"`HH:MM - HH:MM`  e.g. `17:00 - 20:00`",
            parse_mode=ParseMode.MARKDOWN,
        )

    # ── Legacy Watch / Check ──────────────────────────────────────────────────
    elif data.startswith("fw:"):
        court_type, club_id, check_date, from_time, to_time = _unpack_confirm(data[3:])
        ok, _ = start_watcher(chat_id, club_id, check_date, from_time, to_time, court_type, 10, ctx.bot)
        sport = f"{COURT_EMOJI[court_type]} {COURT_TYPE_NAMES[court_type]}"
        if ok:
            await q.edit_message_text(
                f"👀 *Watcher started!*\n\n{sport}\n📍 {club_label(club_id)}\n"
                f"📅 {check_date}   🕐 {from_time}–{to_time}\n\n"
                f"I'll alert you the moment a slot opens.\n⏰ Auto-expires in {WATCHER_MAX_HOURS}h.",
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            await q.edit_message_text(f"Already watching {club_label(club_id)} for that slot.")

    elif data.startswith("fk:"):
        court_type, club_id, check_date, from_time, to_time = _unpack_confirm(data[3:])
        await q.edit_message_text(f"🔍 Checking {club_label(club_id)}…")
        try:
            hits = await rate_limited_check(club_id, check_date, from_time, to_time, court_type)
        except Exception as e:
            await q.message.reply_text(f"Error: {e}")
            return
        if hits:
            await q.message.reply_text(
                format_hits(hits, club_id, check_date, from_time, to_time, court_type),
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            sport = COURT_TYPE_NAMES.get(court_type, "")
            await q.message.reply_text(
                f"No {sport} slots for *{club_label(club_id)}*\n"
                f"📅 {check_date}   🕐 {from_time}–{to_time}\n\nWant to watch for openings?",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton(
                        f"👀 Watch ({WATCHER_MAX_HOURS}h)",
                        callback_data=f"fw:{court_type}:{club_id}:{check_date}:{from_time}:{to_time}",
                    ),
                ]]),
            )

    # ── Stop watcher ──────────────────────────────────────────────────────────
    elif data.startswith("stp:"):
        key = data[4:]
        w   = _watchers.get(chat_id, {})
        if key in w:
            w[key].cancel()
            del w[key]
            await q.edit_message_reply_markup(reply_markup=None)
            await q.message.reply_text("✅ Watcher stopped.")
        else:
            await q.answer("Watcher already stopped.", show_alert=True)

    # ── /find actions ─────────────────────────────────────────────────────────
    elif data.startswith("act:"):
        _, club_id_s, action = data.split(":")
        club_id = int(club_id_s)
        if action == "p":
            text = format_prices(club_id)
            if text:
                await q.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)
            else:
                await q.message.reply_text(
                    f"*{club_label(club_id)}*\nNo pricing data available.",
                    parse_mode=ParseMode.MARKDOWN,
                )
        else:
            await q.edit_message_reply_markup(reply_markup=None)
            await q.message.reply_text(
                f"{'👀 Watch' if action == 'w' else '🔍 Check'} *{club_label(club_id)}*\n\nChoose sport:",
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
            f"{COURT_EMOJI[court_type]} {COURT_TYPE_NAMES[court_type]} · *{club_label(club_id)}*\n\nChoose time range:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_time_legacy(court_type, club_id, check_date),
        )


# ---------------------------------------------------------------------------
# Free-text handler — custom date / time input
# ---------------------------------------------------------------------------

async def on_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    flow = ctx.user_data.get("flow")
    if not flow:
        await handle_llm_message(update, ctx)
        return

    text = update.message.text.strip()

    # ── Custom date input ─────────────────────────────────────────────────────
    if flow.get("awaiting") == "date":
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

        if flow.get("selected_clubs"):
            n = len(flow["selected_clubs"])
            await update.message.reply_text(
                f"{sport} · *{n} club{'s' if n > 1 else ''} selected* · {check_date}\n\nChoose a time range:",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb_time_guided(),
            )
        else:
            await update.message.reply_text(
                f"{sport} · *{club_label(flow['club_id'])}* · {check_date}\n\nChoose a time range:",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb_time_legacy(flow["court_type"], flow["club_id"], check_date),
            )
        return

    # ── Custom time range input ───────────────────────────────────────────────
    if flow.get("awaiting") == "time_range":
        cleaned = text.replace(" ", "").replace("–", "-").replace("—", "-")
        if "-" not in cleaned:
            await update.message.reply_text("Format: `HH:MM - HH:MM`  e.g. `17:00 - 20:00`",
                                             parse_mode=ParseMode.MARKDOWN)
            return
        parts = cleaned.split("-", 1)
        try:
            from_time = datetime.strptime(parts[0], "%H:%M").strftime("%H:%M")
            to_time   = datetime.strptime(parts[1], "%H:%M").strftime("%H:%M")
        except ValueError:
            await update.message.reply_text("Invalid time. Use `HH:MM - HH:MM`:",
                                             parse_mode=ParseMode.MARKDOWN)
            return

        court_type = flow["court_type"]
        check_date = flow.get("check_date", (date.today() + timedelta(days=1)).isoformat())

        # Multi-club guided flow
        if flow.get("selected_clubs"):
            flow["from_time"] = from_time
            flow["to_time"]   = to_time
            ctx.user_data["flow"] = flow
            selected_ids = flow["selected_clubs"]
            sport = f"{COURT_EMOJI[court_type]} {COURT_TYPE_NAMES[court_type]}"
            clubs_lines = []
            for cid in selected_ids:
                club   = _clubs.get(cid, {})
                name   = (club.get("name_english") or club.get("name", f"Club #{cid}")).strip()
                rating = club.get("rating")
                suffix = f"  ⭐ {rating}" if rating else ""
                clubs_lines.append(f"📍 {name} (#{cid}){suffix}")
            n = len(selected_ids)
            await update.message.reply_text(
                f"*Summary*\n\n{sport}\n"
                + "\n".join(clubs_lines)
                + f"\n📅 {check_date}\n🕐 {from_time} – {to_time}\n\n"
                  f"{'1 club' if n == 1 else f'{n} clubs'} · What would you like to do?",
                parse_mode=ParseMode.MARKDOWN,
                reply_markup=kb_confirm_guided(),
            )
            return

        # Legacy single-club flow
        club_id    = flow["club_id"]
        ctx.user_data.pop("flow", None)
        sport      = f"{COURT_EMOJI[court_type]} {COURT_TYPE_NAMES[court_type]}"
        label      = club_label(club_id)
        club       = _clubs.get(club_id, {})
        city       = (club.get("city_english") or club.get("city") or "").strip()
        rating     = club.get("rating")
        rating_str = f"  ⭐ {rating}" if rating else ""
        await update.message.reply_text(
            f"*Summary*\n\n{sport}\n📍 {label}{rating_str}\n🏙 {city}\n"
            f"📅 {check_date}\n🕐 {from_time} – {to_time}\n\nWhat would you like to do?",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_confirm_legacy(court_type, club_id, check_date, from_time, to_time),
        )


# ---------------------------------------------------------------------------
# LLM free-text handler
# ---------------------------------------------------------------------------

async def handle_llm_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Parse a free-text message with Claude (multi-turn) and jump into the guided flow."""
    from datetime import date as _date, timedelta as _td
    from .llm import parse_intent_with_history, resolve_clubs

    text     = update.message.text.strip()
    thinking = await update.message.reply_text("🤔 Let me figure that out…")

    # ── Build conversation history (improvement #4: multi-turn) ──────────────
    today    = _date.today()
    user_msg = f"Today is {today.isoformat()} ({today.strftime('%A')}). User message: {text}"
    history: list[dict] = ctx.user_data.get("llm_history", [])
    history.append({"role": "user", "content": user_msg})

    try:
        intent = await parse_intent_with_history(history[-6:])  # last 3 exchanges
    except Exception as e:
        log.warning("LLM parse failed: %s", e)
        await thinking.edit_text(
            "I couldn't understand that. Use /start for the guided flow or /help for commands.",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🚀 Start", callback_data="restart"),
            ]]),
        )
        return

    # Save assistant turn to history
    history.append({"role": "assistant", "content": json.dumps(intent)})
    ctx.user_data["llm_history"] = history[-6:]

    # ── Improvement #1: non-booking intent ────────────────────────────────────
    if intent.get("intent") == "other":
        reply = intent.get("reply") or "Use /start to search for courts or /help for all commands."
        await thinking.edit_text(
            reply,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("🚀 Start", callback_data="restart"),
            ]]),
        )
        return

    court_type   = int(intent.get("court_type") or 3)
    club_query   = intent.get("club_query")
    check_date   = intent.get("date")
    from_time    = intent.get("from_time")
    to_time      = intent.get("to_time")
    city_key_llm = (intent.get("city_key") or "").strip()

    # Normalise date aliases
    if check_date == "today":
        check_date = _date.today().isoformat()
    elif check_date == "tomorrow":
        check_date = (_date.today() + _td(days=1)).isoformat()

    sport = f"{COURT_EMOJI.get(court_type, '🏟')} {COURT_TYPE_NAMES.get(court_type, '')}"

    # ── Club resolution ───────────────────────────────────────────────────────
    # If Claude gave a city_key and no specific club query → show club picker for that city
    city_only = city_key_llm and city_key_llm in _city_clubs and not club_query

    if city_only:
        clubs    = []
        city_key = city_key_llm
    else:
        # Fuzzy search, optionally scoped to the city Claude identified
        clubs    = resolve_clubs(club_query, city_key=city_key_llm or None)
        city_key = clubs[0]["city_key"] if clubs else city_key_llm

    sel_ids = [c["id"] for c in clubs]

    # Pre-fill flow with everything extracted so far
    flow: dict = {"court_type": court_type, "city_key": city_key, "selected_clubs": sel_ids}
    if check_date:
        flow["check_date"] = check_date
    if from_time and to_time:
        flow["from_time"] = from_time
        flow["to_time"]   = to_time
    ctx.user_data["flow"] = flow

    # ── All info present → summary + action buttons ───────────────────────────
    if clubs and check_date and from_time and to_time:
        clubs_lines = []
        for c in clubs[:5]:
            name   = (c.get("name_english") or c.get("name", "")).strip()
            rating = c.get("rating")
            clubs_lines.append(f"📍 {name}" + (f" ⭐{rating}" if rating else ""))
        n       = len(sel_ids)
        summary = (
            f"*Got it!*\n\n{sport}\n"
            + "\n".join(clubs_lines)
            + f"\n📅 {check_date}   🕐 {from_time}–{to_time}\n\n"
              f"{'1 club' if n == 1 else f'{n} clubs'} · What would you like to do?"
        )
        await thinking.edit_text(summary, parse_mode=ParseMode.MARKDOWN, reply_markup=kb_confirm_guided())
        return

    # ── City identified, no specific club → club multi-select ─────────────────
    if city_only:
        text_msg, kb = clubs_multiselect(court_type, city_key, [])
        if kb:
            await thinking.edit_text(text_msg, parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
            return

    # ── Partial info → drop at the first missing step ─────────────────────────
    if not clubs:
        await thinking.edit_text(
            f"{sport} — I couldn't find *{club_query or 'that club'}*. Choose a city:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_city(court_type),
        )
    elif not check_date:
        n = len(sel_ids)
        await thinking.edit_text(
            f"{sport} · *{n} club{'s' if n > 1 else ''} selected*\n\nChoose a date:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_date_guided(),
        )
    elif not from_time or not to_time:
        n = len(sel_ids)
        await thinking.edit_text(
            f"{sport} · *{n} club{'s' if n > 1 else ''}* · {check_date}\n\nChoose a time range:",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=kb_time_guided(),
        )
    else:
        ctx.user_data.pop("flow", None)
        await thinking.edit_text(
            "What sport are you looking for?",
            reply_markup=kb_sport(ctx.user_data.get("last_search")),
        )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _unpack_confirm(s: str) -> tuple:
    """Unpack '{ct}:{club_id}:{date}:{HH}:{MM}:{HH}:{MM}'"""
    parts = s.split(":")
    return (
        int(parts[0]),          # court_type
        int(parts[1]),          # club_id
        parts[2],               # check_date
        f"{parts[3]}:{parts[4]}",  # from_time
        f"{parts[5]}:{parts[6]}",  # to_time
    )


async def _show_confirmation_legacy(q, court_type: int, club_id: int,
                                    check_date: str, from_time: str, to_time: str) -> None:
    sport      = f"{COURT_EMOJI[court_type]} {COURT_TYPE_NAMES[court_type]}"
    label      = club_label(club_id)
    club       = _clubs.get(club_id, {})
    city       = (club.get("city_english") or club.get("city") or "").strip()
    rating     = club.get("rating")
    rating_str = f"  ⭐ {rating}" if rating else ""
    text = (
        f"*Summary*\n\n{sport}\n📍 {label}{rating_str}\n🏙 {city}\n"
        f"📅 {check_date}\n🕐 {from_time} – {to_time}\n\nWhat would you like to do?"
    )
    await q.edit_message_text(
        text, parse_mode=ParseMode.MARKDOWN,
        reply_markup=kb_confirm_legacy(court_type, club_id, check_date, from_time, to_time),
    )
