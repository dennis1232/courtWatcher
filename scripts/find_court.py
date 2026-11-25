"""Find free courts from a free-text request (Hebrew/English) — the one command agents use.

Usage:
    bin/find-court "אני רוצה מגרש כלשהו בתל אביב בין השעות 18-21 למשך שעה"
    bin/find-court "פאדל מחר בערב ברעננה" --json
    bin/find-court "רוקח 67 מחר ב-18" --dry-run           # decision + club list, no Lazuz calls
    bin/find-court "טניס בתל אביב מחר" --from 18:00 --to 21:00   # flags override what was understood
    nohup bin/find-court "<request>" --watch > watch.log 2>&1 &   # poll until a court frees up

The request is understood by the Gemini Decisions API (bot/decide.py): it only chooses among
fixed options. Exit codes: 0 done · 2 a fixed question must be asked (printed with options) ·
3 the user asked to watch and nothing is free now (the exact --watch command is printed).
A slot qualifies when the booking fits inside the window: from <= start and start + duration <= to.
"""

import argparse
import asyncio
import fcntl
import functools
import glob
import hashlib
import json
import os
import random
import shlex
import signal
import sys
import tempfile
from datetime import date, datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import httpx

from bot.clubs import _city_clubs, _city_display, _norm_city, club_name, load_clubs
from bot.constants import SPORT_CODES, TZ
from bot.decide import REGION_LABEL, decide
from lazuz_api.auth import ensure_valid_token
from lazuz_api.client import LazuzClient


def parse_date(raw: str) -> str:
    today = datetime.now(TZ).date()
    raw = raw.strip().lower()
    if raw in ("today", "tomorrow"):
        return (today + timedelta(days=raw == "tomorrow")).isoformat()
    return date.fromisoformat(raw).isoformat()


def offers(club: dict, sport: int) -> bool:
    """Exact per-sport club list from fetch_clubs.py; falls back to the price list for older data."""
    if "court_types" in club:
        return sport in club["court_types"]
    rates = club.get("rent_rates")
    return not rates or any(r.get("court_type_id") == sport for r in rates)



def fits(slot: str, start: str, end: str, duration: int) -> bool:
    t = datetime.strptime(slot[:5], "%H:%M")
    return datetime.strptime(start, "%H:%M") <= t and t + timedelta(minutes=duration) <= datetime.strptime(end, "%H:%M")


async def check(client: LazuzClient, sem: asyncio.Semaphore, club: dict, args, day: str | None = None) -> dict:
    day = day or args.date
    result = {"club_id": club["id"], "club": club_name(club["id"]), "date": day,
              "phone": club.get("phone"), "address": (club.get("address_english") or club.get("address") or "").strip(),
              "slots": {}}
    for attempt in range(3):
        try:
            async with sem:
                data = await asyncio.wait_for(client.get_available_slots(
                    club_id=club["id"], date=day, duration=args.duration, court_type=args.sport,
                    external_club_id=club.get("external_club_id") or None,
                    resolve_external=False,  # clubs.json already has external_club_id
                ), args.timeout)
            break
        except (asyncio.TimeoutError, httpx.TimeoutException):  # some clubs hang; don't wait on them
            result["error"] = f"timed out after {args.timeout:g}s"
            return result
        except httpx.TransportError:  # external-API clubs drop connections under concurrency
            if attempt == 2:
                result["error"] = "connection dropped (3 tries)"
                return result
            await asyncio.sleep(0.5 * (attempt + 1))
        except httpx.HTTPStatusError as e:  # 429/403/5xx: tell the watch loop to back off
            result["error"] = f"HTTP {e.response.status_code}"
            if e.response.status_code in (403, 429, 503):
                ra = e.response.headers.get("retry-after", "")
                result["retry_after"] = float(ra) if ra.isdigit() else 0.0
            return result
        except Exception as e:  # one bad club must not sink the search
            result["error"] = repr(e)
            return result
    for i, court in enumerate(sorted(data.get("courts", []), key=lambda c: c.get("courtId", 0)), 1):
        name = str(court.get("name") or "").strip()
        name = name if name and not name.isdigit() else f"Court {i}"
        for slot in court.get("availbleTimeSlot", []):
            if fits(slot, args.from_time, args.to_time, args.duration):
                result["slots"].setdefault(slot[:5], []).append(name)
    return result


def show(r: dict, with_date: bool = False) -> None:
    print("\n" + " — ".join(x for x in (f"#{r['club_id']} {r['club']}", r["address"], r["phone"]) if x))
    when = f"{datetime.strptime(r['date'], '%Y-%m-%d'):%a %d.%m} " if with_date else ""
    for t in sorted(r["slots"]):
        print(f"  {when}{t}  {', '.join(r['slots'][t])}", flush=True)


def window_closed(day: str, end: str, duration: int) -> bool:
    """True once no booking of `duration` can still start inside the window."""
    last_start = datetime.strptime(f"{day} {end}", "%Y-%m-%d %H:%M").replace(tzinfo=TZ) - timedelta(minutes=duration)
    return datetime.now(TZ) > last_start


WATCH_MINUTES = (10, 15)  # each wait is drawn uniformly from this range
QUIET_HOURS = (23, 7)     # Israel time: no checks from 23:00 until 07:00 (+0-15 min)


def quiet_until(t: datetime, quiet=QUIET_HOURS) -> datetime | None:
    """End of the quiet period `t` falls in, or None when `t` is daytime."""
    start, stop = quiet
    if t.hour >= start or t.hour < stop:
        end = t.replace(hour=stop, minute=0, second=0, microsecond=0)
        return end + timedelta(days=1) if t.hour >= start else end
    return None


async def watch(cycle, closed, minutes=WATCH_MINUTES, sleep=asyncio.sleep,
                log=functools.partial(print, flush=True), clock=lambda: datetime.now(TZ)) -> list[dict] | None:
    """Re-run `cycle()` until it returns a club with courts; None once the window closes.

    Polite by design — one user's pace, never a burst: each wait is a fresh uniform draw from
    `minutes`. A cycle that is throttled (403/429/503) or mostly failing doubles the multiplier
    (up to 4x, honouring Retry-After) until a clean cycle resets it. A check that would land in
    QUIET_HOURS is pushed to the morning instead.
    """
    backoff, n = 1, 0
    while not closed():
        n += 1
        results = await cycle()
        if hits := [r for r in results if r["slots"]]:
            return hits
        throttled = [r["retry_after"] for r in results if "retry_after" in r]
        failed = sum("error" in r for r in results)
        backoff = min(backoff * 2, 4) if throttled or failed * 2 > len(results) else 1
        wait = max([random.uniform(*minutes) * 60 * backoff, *throttled])
        note = f", backing off x{backoff} ({failed} failed)" if backoff > 1 else ""
        now = clock()
        if morning := quiet_until(now + timedelta(seconds=wait)):
            wait = (morning - now).total_seconds() + random.uniform(0, 900)
            note += ", quiet hours"
        log(f"{now:%H:%M} · check {n}: nothing free at {len(results) - failed}/{len(results)} clubs"
            f"{note} · next at {(now + timedelta(seconds=wait)):%a %H:%M}")
        await sleep(wait)
    return None


async def notify(text: str) -> None:
    """Telegram push when TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID are set (.env); otherwise just the terminal bell."""
    token, chat = os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID")
    print("\a", end="", flush=True)
    if token and chat:
        try:
            async with httpx.AsyncClient(timeout=10) as http:
                await http.post(f"https://api.telegram.org/bot{token}/sendMessage", json={"chat_id": chat, "text": text})
        except httpx.HTTPError as e:
            print(f"Telegram notify failed: {e!r}", file=sys.stderr)


FIXED_QUESTIONS = {  # the only questions ever asked; the bot shows the options as buttons
    "where": ("איפה לחפש?", ["תל אביב", "גוש דן", "השרון", "השפלה", "אזור ירושלים", "עיר/מועדון אחר"]),
    "day": ("לאיזה יום?", ["היום", "מחר", "מחרתיים", "יום אחר"]),
    "time": ("באיזו שעה?", ["בוקר 07–12", "צהריים 12–16", "אחה״צ 14–18", "ערב 17–21", "לילה 20–23", "שעה מדויקת"]),
}
LOCK_GLOB = os.path.join(tempfile.gettempdir(), "courtwatcher-*.lock")


def running_watches() -> dict[str, dict]:
    """Watch id → {pid, label} for every watch process holding its lock file."""
    out = {}
    for path in glob.glob(LOCK_GLOB):
        try:
            with open(path) as f:
                try:
                    fcntl.flock(f, fcntl.LOCK_SH | fcntl.LOCK_NB)  # acquirable = nobody holds it = stale
                    continue
                except BlockingIOError:
                    info = json.loads(f.read() or "{}")
            if info.get("pid"):
                out[os.path.basename(path)[13:-5]] = info
        except (OSError, ValueError):
            continue
    return out


async def main() -> None:
    p = argparse.ArgumentParser(description="Find free Lazuz courts")
    p.add_argument("request", nargs="*", help="Free text, e.g. 'פאדל מחר בערב ברעננה'")
    p.add_argument("--where", help="Area, city, club name or address (overrides the text)")
    p.add_argument("--date", type=parse_date, help="today | tomorrow | YYYY-MM-DD")
    p.add_argument("--from", dest="from_time", metavar="HH:MM")
    p.add_argument("--to", dest="to_time", metavar="HH:MM")
    p.add_argument("--duration", type=int, help="Minutes (default: 60)")
    p.add_argument("--sport", choices=list(SPORT_CODES))
    p.add_argument("--limit", type=int, default=30, help="Max clubs to check (default: 30)")
    p.add_argument("--concurrency", type=int, default=8)
    p.add_argument("--timeout", type=float, default=8, help="Seconds per club before giving up (default: 8)")
    p.add_argument("--json", action="store_true", help="Machine-readable output")
    p.add_argument("--dry-run", action="store_true", help="Only show which clubs would be checked")
    p.add_argument("--watch", action="store_true",
                   help="Re-check every 10-15 min (random) until a court frees up or the window passes")
    args = p.parse_args()

    load_clubs()
    text = " ".join(args.request + ([args.where] if args.where else [])).strip()
    if not text:
        p.error("give the request as free text, e.g. bin/find-court \"טניס בתל אביב מחר בערב\"")
    watches = running_watches()
    try:
        d = await decide(text, watches={k: v.get("label", k) for k, v in watches.items()} or None)
    except RuntimeError as e:
        sys.exit(str(e))
    except httpx.TimeoutException:
        sys.exit("The Decisions API (beta) didn't answer in time — retry, or pass --where/--date/--from/--to explicitly.")

    if d.action == "list_watches" and not args.watch:
        for k, v in watches.items():
            print(f"{k}  pid {v['pid']}  {v.get('label', '')}")
        print(f"{len(watches)} watch(es) running")
        return
    if d.action == "stop_watch" and not args.watch:
        targets = watches if d.stop_target in (None, "all") else {k: v for k, v in watches.items() if k == d.stop_target}
        for k, v in targets.items():
            os.kill(v["pid"], signal.SIGTERM)
            print(f"stopped {k}  {v.get('label', '')}")
        print(f"{len(targets)} watch(es) stopped" if targets else "No watches running.")
        return
    if d.action == "chit_chat" and not args.watch:
        print("Nothing to search — name a place, a day and a time.")
        return

    # explicit flags override what was understood, and answer the matching fixed question
    for attr, flag in (("date", args.date), ("frm", args.from_time), ("to", args.to_time),
                       ("duration", args.duration), ("sport", args.sport)):
        if flag:
            setattr(d, attr, flag)
    if (d.ask == "time" and args.from_time) or (d.ask == "day" and (args.date or args.from_time)):
        d.ask = None
    if d.ask:
        question, options = FIXED_QUESTIONS[d.ask]
        print(f"ASK {d.ask}: {question}\noptions: " + " | ".join(options))
        sys.exit(2)
    d.date = d.date or datetime.now(TZ).date().isoformat()
    dates = [args.date] if args.date else [x for x in (d.dates or [d.date])
                                           if not window_closed(x, d.to or "23:00", d.duration)] or [d.date]
    multi = len(dates) > 1
    d.frm, d.to = d.frm or "06:00", d.to or "23:00"
    args.date, args.from_time, args.to_time, args.duration = d.date, d.frm, d.to, d.duration
    args.sport = SPORT_CODES[d.sport]

    for c in d.retired:  # a named club Lazuz dropped: say so instead of silently skipping it
        print(f"Note: #{c['id']} {(c.get('name') or '').strip()} ({(c.get('address') or '').strip()}) is no longer "
              f"bookable on Lazuz (dropped {c.get('retired_on')}) — phone {c.get('phone') or 'n/a'}", file=sys.stderr)
    label = " / ".join(club_name(c["id"]) for c in d.clubs) if d.named else (d.city or "")
    if d.named and d.city:
        label += f" · {d.city}"
    clubs = [c for c in d.clubs if offers(c, args.sport)][:args.limit]
    # A city search that comes up empty widens to its area (רמת גן → גוש דן) inside the same run
    area = _norm_city(d.city) if d.city and d.city not in REGION_LABEL.values() and not d.named else None
    ids = {c["id"] for c in clubs}
    nearby = [c for c in sorted(_city_clubs.get(area, []), key=lambda c: -(c.get("rating") or 0))
              if offers(c, args.sport) and c["id"] not in ids][:args.limit]
    nearby_label = _city_display.get(area, area)
    if not clubs and not nearby:
        sys.exit(f"No {d.sport} clubs on Lazuz for {label or 'that place'}. Try a nearby city or an area (גוש דן, השרון, אזור ירושלים).")
    request = {k: v for k, v in vars(d).items() if k not in ("clubs", "retired")} | {"clubs": [c["id"] for c in clubs]}

    if args.dry_run:
        for c in clubs:
            print(f"#{c['id']}  {club_name(c['id'])}")
        print(f"{len(clubs)} clubs (+{len(nearby)} nearby in {nearby_label} if empty) · {json.dumps(request, ensure_ascii=False)}")
        return

    try:
        token = await ensure_valid_token()  # refresh once up front, not per concurrent task
    except ValueError as e:
        sys.exit(str(e))
    sem = asyncio.Semaphore(args.concurrency)
    when = f"{dates[0]}…{dates[-1]} ({len(dates)} days)" if multi else args.date
    head = f"{label} · {d.sport} · {when} · {args.from_time}-{args.to_time} · {args.duration}min"

    if args.watch:
        batch = clubs or nearby
        # one watcher per identical search: a second copy would only double the load
        key = hashlib.sha1(f"{[c['id'] for c in batch]}{dates}{args.from_time}{args.to_time}{args.duration}{args.sport}".encode())
        lock = open(os.path.join(tempfile.gettempdir(), f"courtwatcher-{key.hexdigest()[:12]}.lock"), "w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            sys.exit(f"Already watching this exact search ({head}).")
        lock.write(json.dumps({"pid": os.getpid(), "label": head}, ensure_ascii=False))  # for list/stop
        lock.flush()
        sem = asyncio.Semaphore(min(args.concurrency, 3))  # a person's pace, not a burst
        print(f"{head} · watching {len(batch)} clubs every {WATCH_MINUTES[0]}-{WATCH_MINUTES[1]} min until "
              f"{dates[-1]} {args.to_time} (Ctrl-C to stop)", flush=True)
        async with LazuzClient(token=token) as client:  # one session for the whole watch; token auto-refreshes
            hits = await watch(lambda: asyncio.gather(*(check(client, sem, c, args, x) for c in batch for x in dates
                                                        if not window_closed(x, args.to_time, args.duration))),
                               lambda: window_closed(dates[-1], args.to_time, args.duration))
        if not hits:
            sys.exit(f"Window passed ({dates[-1]} {args.to_time}) — no court freed up.")
        print(f"\nCourt available! {head}")
        for r in hits:
            show(r, multi)
        await notify(f"Court available! {head}\n" + "\n".join(
            f"{r['club']} {r['date']}: {', '.join(sorted(r['slots']))}" + (f" ({r['phone']})" if r["phone"] else "") for r in hits))
        return

    async def search(batch: list[dict], title: str) -> list[dict]:
        if not args.json:
            print(f"{title} · checking {len(batch)} clubs in parallel", flush=True)
        out = []
        # stream: each club prints the moment it answers (most take ~30 ms; Israel Tennis Center ~2 s)
        for done in asyncio.as_completed([check(client, sem, c, args, x) for c in batch for x in dates]):
            out.append(r := await done)
            if r["slots"] and not args.json and not multi:
                show(r)  # single day: stream each club as it answers
        if multi and not args.json:  # several days: one block per club, days in order
            for cid in dict.fromkeys(r["club_id"] for r in sorted(out, key=lambda r: r["date"]) if r["slots"]):
                days = sorted((r for r in out if r["club_id"] == cid and r["slots"]), key=lambda r: r["date"])
                show(days[0], True)
                for r in days[1:]:
                    when = f"{datetime.strptime(r['date'], '%Y-%m-%d'):%a %d.%m}"
                    for t in sorted(r["slots"]):
                        print(f"  {when} {t}  {', '.join(r['slots'][t])}")
        if not args.json:
            hit_clubs = {r["club_id"] for r in out if r["slots"]}
            per_day = f" on {sum(1 for r in out if r['slots'])} club-days" if multi else ""
            print(f"\n{len(hit_clubs)}/{len(batch)} clubs have courts{per_day}", flush=True)
        return out

    results, nearby_results = [], []
    async with LazuzClient(token=token) as client:
        if clubs:
            results = await search(clubs, head)
        if nearby and not any(r["slots"] for r in results):
            if not args.json:
                print(f"\nNothing free in {label} — widening to {nearby_label}\n")
            nearby_results = await search(nearby, f"{nearby_label} (nearby) · {d.sport} · {when} · "
                                                  f"{args.from_time}-{args.to_time} · {args.duration}min")

    if args.json:
        print(json.dumps({"request": request, "results": results, "nearby": nearby_label if nearby_results else None,
                          "nearby_results": nearby_results}, ensure_ascii=False, indent=2))
        return
    if errors := [r for r in results + nearby_results if "error" in r]:
        print(f"{len(errors)} checks failed: " + ", ".join(f"#{r['club_id']} {r['date']} ({r['error']})" for r in errors), file=sys.stderr)
    if d.action == "watch" and not any(r["slots"] for r in results + nearby_results):
        print(f"\nWATCH requested and nothing is free now. Start it with:\n"
              f"nohup bin/find-court {shlex.quote(text)} --watch > watch.log 2>&1 &")
        sys.exit(3)

if __name__ == "__main__":
    asyncio.run(main())
