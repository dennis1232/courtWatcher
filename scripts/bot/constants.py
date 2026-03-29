"""Shared constants for the Lazuz Telegram bot."""

COURT_TYPE_NAMES: dict[int, str] = {3: "Tennis", 6: "Football", 9: "Padel", 10: "Pickleball"}
COURT_EMOJI:      dict[int, str] = {3: "🎾",     6: "⚽",       9: "🎾",   10: "🏓"}

DEFAULT_COURT_TYPE = 3  # Tennis fallback
SELECTABLE_SPORTS = [3, 9]  # Tennis + Padel only
DAYS_LAZUZ:       dict[int, str] = {
    0: "Sun", 1: "Mon", 2: "Tue", 3: "Wed", 4: "Thu", 5: "Fri", 6: "Sat"
}

WATCHER_MAX_HOURS = 3
MIN_REQUEST_GAP   = 3.0   # seconds between API calls

TIME_PRESETS: list[tuple[str, str, str]] = [
    ("Morning  06–12", "06:00", "12:00"),
    ("Midday   12–16", "12:00", "16:00"),
    ("Evening  16–20", "16:00", "20:00"),
    ("Night    20–23", "20:00", "23:00"),
    ("Full day 06–23", "06:00", "23:00"),
]
