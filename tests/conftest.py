"""Load .env so live tests can see GEMINI_API_KEY (it is never committed)."""

from dotenv import load_dotenv

load_dotenv()
