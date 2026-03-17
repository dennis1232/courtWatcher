"""Lazuz API client.

Async HTTP client that replicates the Lazuz app's API calls.
All endpoints verified against MITM-captured traffic.
"""

import httpx
from . import config
from .auth import default_headers, is_token_expired, ensure_valid_token


class LazuzClient:
    """Async client for the Lazuz tennis court booking API.

    Usage:
        async with LazuzClient() as client:
            slots = await client.get_available_slots(club_id=139, date="2026-03-17")
    """

    def __init__(self, token: str | None = None, base_url: str | None = None, auto_refresh: bool = True):
        self.base_url = base_url or config.BASE_URL
        self.token = token or config.AUTH_TOKEN
        self.auto_refresh = auto_refresh
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self.auto_refresh and is_token_expired(self.token):
            self.token = await ensure_valid_token()
            if self._client and not self._client.is_closed:
                await self._client.aclose()
            self._client = None

        if self._client is None or self._client.is_closed:
            headers = default_headers()
            headers["authorization"] = f"Bearer {self.token}" if self.token else ""
            self._client = httpx.AsyncClient(
                base_url=self.base_url, headers=headers, timeout=30.0
            )
        return self._client

    async def _force_refresh(self) -> None:
        """Force a token refresh and recreate the HTTP client."""
        from .auth import refresh_token
        self.token = await refresh_token()
        if self._client and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    async def _get(self, path: str, params: dict | None = None) -> dict:
        """Make a GET request, auto-retrying once on 401."""
        client = await self._get_client()
        resp = await client.get(path, params=params)

        if resp.status_code == 401 and self.auto_refresh:
            await self._force_refresh()
            client = await self._get_client()
            resp = await client.get(path, params=params)

        resp.raise_for_status()
        return resp.json()

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        await self.close()

    # --- Clubs ---

    async def get_club_list(
        self,
        date: str | None = None,
        court_type: int | None = None,
        category: int | None = None,
        duration: int | None = None,
        lat: float | None = None,
        lng: float | None = None,
    ) -> dict:
        """List clubs. All params are optional — no params returns all 168 clubs."""
        params = {}
        if date is not None:
            params["date"] = date
        if court_type is not None:
            params["court_type"] = court_type
        if category is not None:
            params["category"] = category
        if duration is not None:
            params["duration"] = duration
        if lat is not None:
            params["lat"] = lat
        if lng is not None:
            params["lng"] = lng
        return await self._get("/client-app/club-list/", params)

    async def get_club_settings(self, club_id: int, external_api_id: int | None = None) -> dict:
        """Club config (slot duration, booking limit, cancellation time)."""
        params: dict = {"club_id": club_id}
        if external_api_id is not None:
            params["external_api_id"] = external_api_id
        return await self._get("/client-app/club-settings/", params)

    async def _resolve_external_club_id(self, club_id: int) -> int | None:
        """Check if a club uses an external booking system.

        First tries without external_api_id. If club_id matches, retries with
        external_api_id=1 to see if an external mapping exists.
        Returns the external club_id if found, None otherwise.
        """
        settings = await self.get_club_settings(club_id)
        results = settings.get("results", {})
        if not isinstance(results, dict):
            return None
        settings_club_id = results.get("club_id")
        if settings_club_id != club_id:
            return settings_club_id

        # No mapping yet — try with external_api_id=1
        try:
            ext_settings = await self.get_club_settings(club_id, external_api_id=1)
            ext_results = ext_settings.get("results", {})
            if isinstance(ext_results, dict):
                ext_club_id = ext_results.get("club_id")
                if ext_club_id is not None and ext_club_id != club_id:
                    return ext_club_id
        except httpx.HTTPStatusError:
            pass  # 401 means this club doesn't use external API

        return None

    async def get_rent_rates(self, club_id: int, date: str) -> dict:
        """Price rates by time of day for a club.

        Returns: {"results": [{"court_type_id", "days", "fromTime", "toTime", "price"}, ...]}
        """
        return await self._get("/client-app/rent-rate/", {"club_id": club_id, "date": date})

    # --- Availability ---

    async def get_available_slots(
        self,
        club_id: int,
        date: str,
        duration: int = 60,
        court_type: int = 3,
        external_club_id: int | None = None,
    ) -> dict:
        """Available time slots per court for a club on a date.

        Automatically resolves external_club_id via club-settings if needed.
        Some clubs (e.g. 55) use an external booking system — the server returns
        a different club_id in settings which must be passed as external_club_id.

        Returns: {"courts": [{"courtId", "name", "prices", "availbleTimeSlot"}, ...]}
        """
        if external_club_id is None:
            external_club_id = await self._resolve_external_club_id(club_id)

        params: dict = {
            "club_id": club_id, "date": date, "duration": duration,
            "court_type": court_type, "from_time": "",
        }
        if external_club_id is not None:
            params["external_club_id"] = external_club_id
        return await self._get("/client-app/club/availble-slots/", params)

