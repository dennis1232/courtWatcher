"""Fetch all Lazuz clubs with their pricing rules and save to data/clubs.json.

Usage:
    uv run python scripts/fetch_clubs.py
    uv run python scripts/fetch_clubs.py --output data/clubs.json
    uv run python scripts/fetch_clubs.py --concurrency 5
"""

import asyncio
import argparse
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn

from lazuz_api.client import LazuzClient

console = Console()
DEFAULT_OUTPUT = Path(__file__).resolve().parent.parent / "data" / "clubs.json"


async def fetch_club_detail(client: LazuzClient, club: dict, check_date: str, sem: asyncio.Semaphore) -> dict:
    """Fetch rent rates for a single club and merge into its record."""
    club_id = club["id"]
    result = dict(club)
    async with sem:
        try:
            rent_data = await client.get_rent_rates(club_id, check_date)
            result["rent_rates"] = rent_data.get("results", [])
        except Exception as e:
            result["rent_rates"] = []
            result["rent_rates_error"] = str(e)
    return result


async def main(output: Path, concurrency: int) -> None:
    # Use a Monday as the reference date so we get weekday pricing rules
    # (rent_rate rules are day-of-week based, not date-specific)
    today = date.today()
    days_until_monday = (7 - today.weekday()) % 7 or 7
    check_date = (today + timedelta(days=days_until_monday)).isoformat()

    console.print(f"Using [cyan]{check_date}[/] (next Monday) as reference date for pricing rules\n")

    async with LazuzClient() as client:
        console.print("Fetching club list...")
        data = await client.get_club_list()
        clubs = data.get("clubs", [])
        console.print(f"Found [green]{len(clubs)}[/] clubs. Fetching pricing rules...\n")

        sem = asyncio.Semaphore(concurrency)
        tasks = [fetch_club_detail(client, club, check_date, sem) for club in clubs]

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total}"),
            TimeElapsedColumn(),
            console=console,
        ) as progress:
            task_id = progress.add_task("Fetching prices", total=len(tasks))
            results = []
            for coro in asyncio.as_completed(tasks):
                club_result = await coro
                results.append(club_result)
                progress.advance(task_id)

    # Sort by club id for stable output
    results.sort(key=lambda c: c.get("id", 0))

    output_data = {
        "updated_at": date.today().isoformat(),
        "reference_date": check_date,
        "total": len(results),
        "clubs": results,
    }

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(output_data, ensure_ascii=False, indent=2))

    errors = sum(1 for c in results if "rent_rates_error" in c)
    console.print(f"\n[green]Saved {len(results)} clubs to [bold]{output}[/bold][/]")
    if errors:
        console.print(f"[yellow]{errors} clubs had pricing errors (marked in JSON)[/]")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Fetch all Lazuz clubs with pricing rules")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT, help="Output JSON path")
    parser.add_argument("--concurrency", type=int, default=10, help="Parallel requests (default: 10)")
    args = parser.parse_args()
    asyncio.run(main(args.output, args.concurrency))
