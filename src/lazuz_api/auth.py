"""Authentication flow for Lazuz API.

Flow: send SMS -> verify code -> get token + refreshToken
Refresh token lasts ~13 years; access token ~16 min.
Auto-refresh: GET /users/token with Bearer <refreshToken>.
"""

import time
import json
import hmac
import hashlib
import base64
from pathlib import Path

import httpx
from . import config


def _base64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _make_appcheck_token() -> str:
    """Generate a valid x-appcheck-server JWT (HS256, 1-hour TTL)."""
    now = int(time.time())
    header_b64 = _base64url_encode(b'{"alg":"HS256","typ":"JWT"}')
    payload_b64 = _base64url_encode(
        json.dumps(
            {"app": "Lazuz", "version": config.APP_VERSION, "iat": now, "exp": now + 3600},
            separators=(",", ":"),
        ).encode()
    )
    signing_input = f"{header_b64}.{payload_b64}"
    sig = hmac.new(
        config.APPCHECK_KEY.encode(), signing_input.encode(), hashlib.sha256
    ).digest()
    return f"{signing_input}.{_base64url_encode(sig)}"


def default_headers() -> dict[str, str]:
    """Standard headers the Lazuz app sends on every request."""
    return {
        "user-agent": "Dart/3.10 (dart:io)",
        "app_version": config.APP_VERSION,
        "test": "test",
        "accept-encoding": "gzip",
        "locale": "en-GB",
        "x-appcheck-server": _make_appcheck_token(),
        "content-type": "application/json",
    }


# ---------------------------------------------------------------------------
# Token persistence
# ---------------------------------------------------------------------------

def _env_path() -> Path:
    return Path(__file__).resolve().parent.parent.parent / ".env"


def save_token_to_env(token: str, key: str = "LAZUZ_AUTH_TOKEN") -> None:
    """Persist a token to .env so it survives restarts."""
    env = _env_path()
    lines: list[str] = []
    if env.exists():
        lines = [l for l in env.read_text().splitlines(keepends=True) if not l.startswith(f"{key}=")]
    if lines and not lines[-1].endswith("\n"):
        lines.append("\n")
    lines.append(f"{key}={token}\n")
    env.write_text("".join(lines))


# ---------------------------------------------------------------------------
# JWT helpers
# ---------------------------------------------------------------------------

def _decode_jwt_payload(token: str) -> dict:
    payload_b64 = token.split(".")[1]
    padded = payload_b64 + "=" * (4 - len(payload_b64) % 4)
    return json.loads(base64.urlsafe_b64decode(padded))


def is_token_expired(token: str | None, margin_seconds: int = 60) -> bool:
    """Check if a JWT is expired (with safety margin)."""
    if not token:
        return True
    try:
        payload = _decode_jwt_payload(token)
        return time.time() > (payload.get("exp", 0) - margin_seconds)
    except Exception:
        return True


# ---------------------------------------------------------------------------
# Auth flows
# ---------------------------------------------------------------------------

async def refresh_token(refresh_token_str: str | None = None) -> str:
    """GET /users/token — refresh the access token.

    Returns the new access token string.
    """
    refresh_token_str = refresh_token_str or config.REFRESH_TOKEN
    if not refresh_token_str:
        raise ValueError("No refresh token available. Run: uv run python scripts/login.py")

    headers = default_headers()
    headers["authorization"] = f"Bearer {refresh_token_str}"

    async with httpx.AsyncClient(base_url=config.BASE_URL, timeout=30.0) as client:
        resp = await client.get("/users/token", headers=headers)
        resp.raise_for_status()

        body = resp.json()
        result = body.get("result", body)
        new_access = result.get("accessToken") or result.get("token")

        if not new_access:
            raise ValueError(f"Token refresh failed — no accessToken in response: {body}")

        save_token_to_env(new_access, "LAZUZ_AUTH_TOKEN")
        config.AUTH_TOKEN = new_access

        if new_refresh := result.get("refreshToken"):
            save_token_to_env(new_refresh, "LAZUZ_REFRESH_TOKEN")
            config.REFRESH_TOKEN = new_refresh

        return new_access


async def ensure_valid_token() -> str:
    """Return a valid access token, refreshing automatically if expired."""
    token = config.AUTH_TOKEN
    if token and not is_token_expired(token):
        return token
    return await refresh_token()
