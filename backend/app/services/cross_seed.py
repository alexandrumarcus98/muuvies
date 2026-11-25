import httpx
from typing import Optional, Dict, Any, Tuple
from backend.app.config import settings
from backend.app.services.http_clients import get_search_client, get_telemetry_client


class CrossSeedClient:
    @property
    def base_url(self) -> str:
        return (settings.CROSS_SEED_URL or "").rstrip("/")

    @base_url.setter
    def base_url(self, value: str) -> None:
        settings.CROSS_SEED_URL = value

    @property
    def api_key(self) -> str:
        return settings.CROSS_SEED_API_KEY

    @api_key.setter
    def api_key(self, value: str) -> None:
        settings.CROSS_SEED_API_KEY = value

    def _get_headers(self) -> Dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.api_key:
            headers["X-Api-Key"] = self.api_key
        return headers

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    async def check_health(self) -> Tuple[bool, Optional[str], Optional[int]]:
        try:
            client = get_telemetry_client()
            res = await client.get(
                self._url("/api/ping"),
                headers=self._get_headers(),
                timeout=1.5,
            )
            if res.status_code in (200, 405):
                return True, "Active", 200
            # Fallback to root
            res2 = await client.get(
                self._url("/"),
                headers=self._get_headers(),
                timeout=1.5,
            )
            if res2.status_code in (200, 401, 404, 405):
                return True, "Running", res2.status_code
            return False, None, res.status_code
        except Exception:
            return False, None, None

    async def trigger_search(self, info_hash: Optional[str] = None, path: Optional[str] = None) -> bool:
        """Triggers cross-seed search webhook for newly completed release."""
        if not info_hash and not path:
            return False
        payload = {}
        if info_hash:
            payload["infoHash"] = info_hash
        if path:
            payload["path"] = path

        try:
            client = get_search_client()
            res = await client.post(
                self._url("/api/webhook"),
                headers=self._get_headers(),
                data=payload,
                timeout=4.0,
            )
            return res.status_code in (200, 204)
        except Exception:
            return False


cross_seed_client = CrossSeedClient()

