import os
from dotenv import load_dotenv

load_dotenv()

# `or default`: an empty line copied from .env.example means "use the default", not ""
BASE_URL = os.getenv("LAZUZ_BASE_URL") or "https://server.lazuz.co.il"
AUTH_TOKEN = os.getenv("LAZUZ_AUTH_TOKEN", "")
REFRESH_TOKEN = os.getenv("LAZUZ_REFRESH_TOKEN", "")
APP_VERSION = os.getenv("LAZUZ_APP_VERSION") or "5.2.3"

# HS256 key for x-appcheck-server JWT. Extracted from APK .env.prod.
# The base64 string is used as-is (not decoded) as the HMAC signing key.
APPCHECK_KEY = (
    os.getenv("LAZUZ_APPCHECK_KEY")
    or "Y3FvQXZWWlpBUUJoaVRRSk9NV2ZZWWZzZ3ZSdUNia2lPQ0FHS3F2ZlBsVlN6UUlBbXNnOFBaTWs="
)
