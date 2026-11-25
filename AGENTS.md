# courtWatcher

Finds free tennis/padel courts on Lazuz (Israeli court booking app) and watches for openings via a Telegram bot.

## Court requests

Any request to find a court — free text, Hebrew or English — goes through the `find-court` skill (`.agents/skills/find-court/SKILL.md`): one command, `bin/find-court "<request>"`.

## Setup

- `uv sync` (uv only, never pip). Python 3.12.
- `.env` (copy `.env.example`): `LAZUZ_REFRESH_TOKEN` is required for any Lazuz call (the access token auto-refreshes and is written back to `.env`); `GEMINI_API_KEY` is required to understand free text; `TELEGRAM_BOT_TOKEN` only for the bot.

## Map

- `src/lazuz_api/` — async Lazuz HTTP client (`client.py`) and token/app-check auth (`auth.py`).
- `scripts/bot/decide.py` — the only free-text understanding: Gemini Decisions API (`gemini-decision-1`, beta, plain HTTPS) choosing among fixed options; code maps choices to a search with lookups and clock arithmetic, no text parsing. Cases: `tests/decision_cases.py`.
- `scripts/find_court.py` — parallel multi-club search and `--watch` loop.
- `scripts/telegram_bot.py` + `scripts/bot/` — Telegram bot: button flow, free text via `bot/decide.py` (missing info → the existing city/date/time buttons), background watchers (`bot/watchers.py`).
- `scripts/check_availability.py`, `scripts/watch_availability.py` — single-club CLI check / poll.
- `scripts/fetch_clubs.py` — refreshes `data/clubs.json` (catalogue, per-sport `court_types`, prices; ~2 s). Run it when a club seems missing — Lazuz adds/removes clubs often.
- Deploy: `Dockerfile` runs the bot.

## Check before done

```bash
ruff check . && uv run pytest -q   # offline tests + live decision cases (live part needs GEMINI_API_KEY)
bin/find-court "מגרש בתל אביב מחר בערב" --dry-run
```

Gotchas:
- The API spells it `availbleTimeSlot` / `availble-slots` — don't "fix" it.
- Decisions API is beta (`v1beta`; `gemini-decision-1` is not in the model list). Keep every choice question ≤40 options — one 100-option question pushed p90 to ~5 s. Its slow tail (up to ~7 s) is server-side: duplicate/hedged requests were measured and did not help. Callers time out and fall back to buttons/flags. When Google ships GA, set `GEMINI_DECISION_API_VERSION=v1`.
- Most clubs answer in ~30 ms; Israel Tennis Center clubs (external booking system) take ~2 s, which bounds area searches that include them.
- `--watch` polls at one person's pace (every 10–15 min, uniform random, quiet 23:00–07:00 via `QUIET_HOURS`, ≤3 requests in flight, backs off on 403/429/503 and honours Retry-After). The API uses the user's own Lazuz account: pace is the protection — keep it human, with the app's real headers.
- Keep requests gentle (default concurrency 8). External-API clubs (Israel Tennis Center) drop connections under load → retried; some clubs hang → skipped after `--timeout` (8s) and listed as failed.
- `data/clubs.json` holds `external_club_id` and `court_types`; `find_court.py` uses them to skip lookups and clubs without the sport.
- Time filtering is client-side on purpose: the API's `from_time` gives identical results at the same speed. `duration` must go to the server (it knows which consecutive slots are free).
