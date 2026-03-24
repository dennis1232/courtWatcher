"""Lazuz Telegram Bot — entry point.

Flow:
    1. /start  → pick sport
    2.         → pick city  (Tel Aviv first)
    3.         → pick clubs (multi-select with ✅ toggles)
    4.         → pick date
    5.         → pick time range
    6.         → summary + Watch all / Check now

Watchers auto-expire after 3 hours.
Power commands still work: /watch /check /find /stop /status
"""

import logging
import os
import sys

# Make src/ importable (lazuz_api package) before any bot imports
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler,
    MessageHandler, filters,
)

from bot.clubs import _clubs, _city_order, load_clubs
from bot.handlers import (
    cmd_check, cmd_find, cmd_help, cmd_start, cmd_status, cmd_stop, cmd_watch,
    on_callback, on_message,
)

load_dotenv()
logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)


def main() -> None:
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        print("Error: TELEGRAM_BOT_TOKEN not set in .env")
        sys.exit(1)

    load_clubs()

    app = Application.builder().token(token).concurrent_updates(True).build()
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
