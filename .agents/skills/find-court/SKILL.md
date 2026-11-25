---
name: find-court
description: Find free tennis, padel, pickleball or football courts in Israel (Lazuz) from a free-text request in Hebrew or English — "מגרש", "court", a place, a day, a time — and start, list or stop watches for cancellations. Use for any court availability question.
---

# Find a court

Run from the repo root, passing the user's words verbatim:

```bash
bin/find-court "<user's request>"
```

The Gemini Decisions API understands the request (search, watch, list/stop watches, or chit-chat) and picks place, day, time window, length and sport from fixed options. One run checks every matching club in parallel; when a city has nothing free it also checks the surrounding area.

## 1. Read the outcome

| Exit | Meaning | Do |
|---|---|---|
| 0 | Results (or watch list / stop done) | Answer (step 2). |
| 2 | `ASK <id>: <question>` + `options:` | Ask the user exactly that question with those options; re-run with their answer appended to the original request. |
| 3 | User asked to watch and nothing is free now | Run the printed `nohup … --watch` line (step 3). |

The first output line is the reading: `place · sport · date · from-to · minutes`. When it is wrong, re-run with flags — they override what was understood: `--date today|tomorrow|YYYY-MM-DD`, `--from HH:MM`, `--to HH:MM`, `--duration MIN`, `--sport tennis|padel|pickleball|football`, `--where "<place>"`. `--dry-run` shows the reading and club list without calling Lazuz.

## 2. Answer

Answer from that one run. Done when the user has, per club with courts: name, free start times, phone — a compact table in the user's language. When nothing is free, say so and ask whether to try another day, time or length, or to watch for a cancellation; run that follow-up only once the user picks. Booking happens in the Lazuz app; this tool only finds availability.

## 3. Watch for a cancellation

Start the watch as a detached process — watches routinely outlast an agent's 2-hour background-task cap:

```bash
nohup bin/find-court "<user's request>" --watch > watch.log 2>&1 &
```

It re-checks every 10–15 min (random each time, backs off when throttled, pauses 23:00–07:00 Israel time), prints one status line per check, and exits 0 with the free courts the moment one appears — plus a Telegram push when `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are in `.env`. Exit 1 means the window passed with nothing freed. An identical second watch refuses to start. "What's being watched?" / "stop watching" in free text go through the same command. Tell the user the watch is running and how to follow it (`tail -f watch.log`).

## Errors

- `GEMINI_API_KEY is not set` / `No refresh token available` → the user must add that key to `.env` (template: `.env.example`).
- `N clubs failed` on stderr → mention those clubs as unchecked; the rest of the answer stands.
- `Note:` lines on stderr name a club the user asked for that Lazuz dropped (with its phone). Always relay them.
- A request it misunderstands: add the phrase and the expected outcome to `tests/decision_cases.py`, adjust the options/instructions in `scripts/bot/decide.py`, keep `uv run pytest -q` green (the live case set needs `GEMINI_API_KEY`).
