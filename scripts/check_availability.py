"""Check tennis court availability on Lazuz.

Usage:
    uv run python scripts/check_availability.py --clubs
    uv run python scripts/check_availability.py --club-id 139
    uv run python scripts/check_availability.py --club-id 139 --date 2026-03-17
    uv run python scripts/check_availability.py --club-id 139 --court-type 10
"""

import asyncio
import argparse
import sys
import os
from datetime import date, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from rich.console import Console
from rich.table import Table

from lazuz_api.client import LazuzClient

console = Console()

COURT_TYPE_NAMES = {3: "Tennis", 6: "Football", 10: "Pickleball"}


async def list_clubs(check_date: str | None, court_type: int | None):
    """List clubs. Filters are optional — omitting them returns all clubs."""
    async with LazuzClient() as client:
        data = await client.get_club_list(date=check_date, court_type=court_type)

    clubs = data.get("clubs", [])
    if not clubs:
        console.print("[yellow]No clubs found.[/]")
        return

    sport = COURT_TYPE_NAMES.get(court_type, str(court_type)) if court_type else "All Sports"
    date_label = check_date or "any date"
    table = Table(title=f"Lazuz {sport} Clubs — {date_label}")
    table.add_column("ID", style="cyan", justify="right")
    table.add_column("Name", style="green")
    table.add_column("City")
    table.add_column("Address")

    for club in clubs:
        table.add_row(
            str(club.get("id", "")),
            club.get("name", "").strip(),
            club.get("city", "").strip(),
            club.get("address", "").strip(),
        )

    console.print(table)
    console.print(f"\n[dim]{len(clubs)} clubs found. Use --club-id <ID> to check availability.[/]")


def _build_price_lookup(rent_rates: list[dict], check_date: str) -> dict[str, int]:
    """Build a time->price lookup from rent-rate rules.

    Returns: {"06:00": 55, "11:00": 28, "16:00": 70, ...}
    """
    import json as _json
    from datetime import datetime

    day_of_week = datetime.strptime(check_date, "%Y-%m-%d").weekday()
    # Lazuz uses 0=Sunday convention (JS-style), Python uses 0=Monday
    # Convert: Python Monday=0 -> Lazuz 1, ..., Python Sunday=6 -> Lazuz 0
    lazuz_dow = str((day_of_week + 1) % 7)

    prices: dict[str, int] = {}
    for rate in rent_rates:
        try:
            days = _json.loads(rate.get("days", "[]"))
        except (TypeError, _json.JSONDecodeError):
            continue
        if lazuz_dow not in days:
            continue

        from_time = rate.get("fromTime", "00:00:00")
        to_time = rate.get("toTime", "24:00:00")
        price = rate.get("price", 0)

        # Generate all half-hour slots in this range
        from_h, from_m = int(from_time[:2]), int(from_time[3:5])
        to_h, to_m = int(to_time[:2]), int(to_time[3:5])
        t = from_h * 60 + from_m
        end = to_h * 60 + to_m
        while t < end:
            key = f"{t // 60:02d}:{t % 60:02d}"
            prices[key] = price
            t += 30

    return prices


async def check_availability(club_id: int, check_date: str, court_type: int):
    """Show available time slots for a specific club."""
    async with LazuzClient() as client:
        data = await client.get_available_slots(
            club_id=club_id, date=check_date, court_type=court_type
        )
        # Fetch rent rates for price info (used when slots don't include prices)
        rent_data = await client.get_rent_rates(club_id, check_date)

    courts = data.get("courts", [])
    if not courts:
        console.print(f"[yellow]No courts available for club {club_id} on {check_date}.[/]")
        return

    rent_rates = rent_data.get("results", [])
    price_lookup = _build_price_lookup(rent_rates, check_date) if rent_rates else {}

    sport = COURT_TYPE_NAMES.get(court_type, str(court_type))
    table = Table(title=f"Club {club_id} — {sport} — {check_date}")
    table.add_column("Court", style="cyan", justify="right")
    table.add_column("Available Times", style="green")
    table.add_column("Slots", justify="right")
    table.add_column("Price", justify="right")

    total_slots = 0
    for court in courts:
        slots = court.get("availbleTimeSlot", [])
        if not slots:
            continue
        court_name = str(court.get("name", court.get("courtId", "?")))
        total_slots += len(slots)

        # Get price: from court data (external clubs) or rent-rate lookup
        prices = court.get("prices", [])
        if prices:
            # External clubs include per-slot prices
            unique = sorted(set(prices))
            price_str = f"₪{unique[0]}" if len(unique) == 1 else f"₪{unique[0]}-{unique[-1]}"
        elif price_lookup:
            # Look up price for each slot from rent-rate
            slot_prices_set = set()
            for s in slots:
                time_key = s[:5]
                if time_key in price_lookup:
                    slot_prices_set.add(price_lookup[time_key])
            if slot_prices_set:
                unique = sorted(slot_prices_set)
                price_str = f"₪{unique[0]}" if len(unique) == 1 else f"₪{unique[0]}-{unique[-1]}"
            else:
                price_str = "—"
        else:
            price_str = "—"

        table.add_row(
            court_name,
            "  ".join(s[:5] for s in slots),
            str(len(slots)),
            price_str,
        )

    console.print(table)
    console.print(f"\n[dim]{total_slots} total slots across {len([c for c in courts if c.get('availbleTimeSlot')])} courts.[/]")


def main():
    parser = argparse.ArgumentParser(description="Lazuz Court Availability Checker")
    parser.add_argument("--club-id", type=int, help="Club ID to check availability")
    parser.add_argument("--clubs", action="store_true", help="List all clubs")
    parser.add_argument("--date", type=str, help="Date (YYYY-MM-DD), defaults to tomorrow")
    parser.add_argument("--court-type", type=int, help="Court type (3=tennis, 6=football, 10=pickleball)")
    args = parser.parse_args()

    if args.club_id:
        check_date = args.date or (date.today() + timedelta(days=1)).isoformat()
        asyncio.run(check_availability(args.club_id, check_date, args.court_type or 3))
    elif args.clubs:
        asyncio.run(list_clubs(args.date, args.court_type))
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
