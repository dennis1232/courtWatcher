"""Poll Lazuz for available court slots in a specific time range.

Usage:
    uv run python scripts/watch_availability.py --club-id 139 --from 17:00 --to 20:00
    uv run python scripts/watch_availability.py --club-id 139 --from 08:00 --to 12:00 --date 2026-03-20
    uv run python scripts/watch_availability.py --club-id 139 --from 17:00 --to 20:00 --interval 3 --court-type 10
"""

import asyncio
import argparse
import os
import sys
from datetime import date, timedelta, datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import httpx
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from lazuz_api.client import LazuzClient

load_dotenv()
console = Console()

COURT_TYPE_NAMES = {3: "Tennis", 6: "Football", 10: "Pickleball"}

_TG_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
_TG_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")


async def _notify(title: str, message: str) -> None:
    """Send a Telegram message to the configured chat."""
    if not _TG_TOKEN or not _TG_CHAT_ID:
        console.print("[yellow]Tip: set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID in .env to get phone notifications[/]")
        return
    text = f"*{title}*\n{message}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"https://api.telegram.org/bot{_TG_TOKEN}/sendMessage",
                json={"chat_id": _TG_CHAT_ID, "text": text, "parse_mode": "Markdown"},
            )
    except Exception as e:
        console.print(f"[yellow]Telegram notify failed:[/] {e}")


def _slots_in_range(slots: list[str], from_time: str, to_time: str) -> list[str]:
    """Filter slot strings (e.g. '17:00:00') to those within [from_time, to_time)."""
    from_t = datetime.strptime(from_time, "%H:%M").time()
    to_t = datetime.strptime(to_time, "%H:%M").time()
    result = []
    for s in slots:
        slot_t = datetime.strptime(s[:5], "%H:%M").time()
        if from_t <= slot_t < to_t:
            result.append(s)
    return result


async def check_once(club_id: int, check_date: str, from_time: str, to_time: str, court_type: int) -> list[dict]:
    """Return list of courts that have slots in the requested time range."""
    async with LazuzClient() as client:
        data = await client.get_available_slots(
            club_id=club_id, date=check_date, court_type=court_type
        )

    courts = data.get("courts", [])
    hits = []
    for court in courts:
        matching = _slots_in_range(court.get("availbleTimeSlot", []), from_time, to_time)
        if matching:
            hits.append({"name": court.get("name", court.get("courtId", "?")), "slots": matching})
    return hits


def _print_hits(hits: list[dict], club_id: int, check_date: str, from_time: str, to_time: str, court_type: int) -> None:
    sport = COURT_TYPE_NAMES.get(court_type, str(court_type))
    table = Table(title=f"[green]AVAILABLE[/] — Club {club_id} ({sport}) — {check_date} — {from_time}–{to_time}")
    table.add_column("Court", style="cyan")
    table.add_column("Available Slots", style="green")
    table.add_column("Count", justify="right")
    for court in hits:
        table.add_row(
            str(court["name"]),
            "  ".join(s[:5] for s in court["slots"]),
            str(len(court["slots"])),
        )
    console.print(table)


async def watch(club_id: int, check_date: str, from_time: str, to_time: str, court_type: int, interval: int) -> None:
    sport = COURT_TYPE_NAMES.get(court_type, str(court_type))
    console.print(
        f"[bold]Watching[/] club [cyan]{club_id}[/] ({sport}) on [cyan]{check_date}[/] "
        f"for slots between [cyan]{from_time}[/]–[cyan]{to_time}[/] "
        f"— checking every [cyan]{interval}[/] minute(s). Press Ctrl+C to stop.\n"
    )

    last_hits: set[tuple] = set()

    while True:
        now = datetime.now().strftime("%H:%M:%S")
        try:
            hits = await check_once(club_id, check_date, from_time, to_time, court_type)
        except Exception as e:
            console.print(f"[dim]{now}[/] [red]Error:[/] {e}")
            await asyncio.sleep(interval * 60)
            continue

        if hits:
            # Build a hashable fingerprint of current hits to avoid spamming repeated notifications
            current = {(c["name"], tuple(c["slots"])) for c in hits}
            if current != last_hits:
                _print_hits(hits, club_id, check_date, from_time, to_time, court_type)
                await _notify(
                    f"Lazuz — {sport} available!",
                    f"Club {club_id} on {check_date}: {', '.join(s[:5] for c in hits for s in c['slots'][:3])}",
                )
                last_hits = current
            else:
                console.print(f"[dim]{now}[/] Still available ({sum(len(c['slots']) for c in hits)} slots) — next check in {interval}m")
        else:
            if last_hits:
                console.print(f"[dim]{now}[/] [yellow]Slots gone.[/] Continuing to watch...")
            else:
                console.print(f"[dim]{now}[/] No slots in {from_time}–{to_time}. Next check in {interval}m")
            last_hits = set()

        await asyncio.sleep(interval * 60)


def main():
    parser = argparse.ArgumentParser(description="Watch Lazuz for available court slots")
    parser.add_argument("--club-id", type=int, required=True, help="Club ID to watch")
    parser.add_argument("--from", dest="from_time", type=str, required=True, metavar="HH:MM", help="Start of time range (e.g. 17:00)")
    parser.add_argument("--to", dest="to_time", type=str, required=True, metavar="HH:MM", help="End of time range (e.g. 20:00)")
    parser.add_argument("--date", type=str, default=None, help="Date (YYYY-MM-DD), defaults to tomorrow")
    parser.add_argument("--court-type", type=int, default=3, help="Court type (3=tennis, 6=football, 10=pickleball)")
    parser.add_argument("--interval", type=int, default=5, help="Polling interval in minutes (default: 5)")
    args = parser.parse_args()

    check_date = args.date or (date.today() + timedelta(days=1)).isoformat()

    try:
        asyncio.run(watch(args.club_id, check_date, args.from_time, args.to_time, args.court_type, args.interval))
    except KeyboardInterrupt:
        console.print("\n[dim]Stopped.[/]")


if __name__ == "__main__":
    main()
