import asyncio
import logging
import re
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from backend.app.config import settings
from backend.app.services.adult_preview import canonical_preview_indexer, supports_poster_fallback
from backend.app.services.http_clients import get_search_client, get_telemetry_client
from backend.app.services.qbittorrent import format_bytes
from backend.app.services.search_scoring import matches_adult_marker

logger = logging.getLogger("muuvies.prowlarr")

CATEGORY_MAP = {
    "Movies": [2000],
    "TV Shows": [5000],
    "Adult": [6000],
}

_RES_4K_RE = re.compile(r"\b4k\b", re.IGNORECASE)
_RES_2160P_RE = re.compile(r"\b2160p\b", re.IGNORECASE)


def expand_resolution_queries(query: str) -> List[str]:
    """Expand standalone 4K / 2160p terms so both tagging conventions are queried."""
    q = (query or "").strip()
    if not q:
        return []
    variants = [q]
    has_4k = bool(_RES_4K_RE.search(q))
    has_2160p = bool(_RES_2160P_RE.search(q))
    if has_4k and not has_2160p:
        alt = _RES_4K_RE.sub("2160p", q)
        if alt not in variants:
            variants.append(alt)
    elif has_2160p and not has_4k:
        alt = _RES_2160P_RE.sub("4K", q)
        if alt not in variants:
            variants.append(alt)
    return variants


def _extract_standard_top_categories(indexer_obj: Dict[str, Any]) -> Set[int]:
    """Return top-level Newznab category buckets (2000, 5000, 6000, ...) for an indexer."""
    buckets: Set[int] = set()

    def _walk(nodes: Any) -> None:
        if not isinstance(nodes, list):
            return
        for node in nodes:
            if not isinstance(node, dict):
                continue
            cid = node.get("id")
            if isinstance(cid, int) and 1000 <= cid < 10000:
                buckets.add((cid // 1000) * 1000)
            _walk(node.get("subCategories"))

    _walk(indexer_obj.get("capabilities", {}).get("categories"))
    return buckets


class ProwlarrClient:
    def __init__(self):
        self._indexer_cache: Optional[Tuple[float, List[Tuple[int, str, Set[int]]]]] = None
        self._indexer_cache_ttl = 120.0

    @property
    def base_url(self) -> str:
        return (settings.PROWLARR_URL or "").rstrip("/")

    @base_url.setter
    def base_url(self, value: str) -> None:
        settings.PROWLARR_URL = value

    @property
    def api_key(self) -> str:
        return settings.PROWLARR_API_KEY

    @api_key.setter
    def api_key(self, value: str) -> None:
        settings.PROWLARR_API_KEY = value

    def _get_headers(self) -> Dict[str, str]:
        headers = {"Accept": "application/json"}
        api_key = self.api_key
        if api_key:
            headers["X-Api-Key"] = api_key
        return headers

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path}"

    async def check_health(self) -> Tuple[bool, Optional[str], Optional[int]]:
        try:
            client = get_telemetry_client()
            res = await client.get(
                self._url("/api/v1/system/status"),
                headers=self._get_headers(),
                timeout=1.5,
            )
            if res.status_code == 200:
                data = res.json()
                version = data.get("version", "Connected")
                return True, version, 200
            return False, None, res.status_code
        except Exception:
            return False, None, None

    async def _get_enabled_indexer_ids(self, categories: List[int]) -> List[int]:
        """Return enabled Prowlarr indexer IDs matching any requested category bucket."""
        now = time.monotonic()
        if self._indexer_cache and (now - self._indexer_cache[0]) < self._indexer_cache_ttl:
            entries = self._indexer_cache[1]
        else:
            entries = []
            try:
                client = get_search_client()
                res = await client.get(
                    self._url("/api/v1/indexer"),
                    headers=self._get_headers(),
                    timeout=3.0,
                )
                if res.status_code == 200:
                    raw = res.json()
                    if isinstance(raw, list):
                        for item in raw:
                            if not isinstance(item, dict) or not item.get("enable"):
                                continue
                            idx_id = item.get("id")
                            if isinstance(idx_id, int):
                                buckets = _extract_standard_top_categories(item)
                                entries.append((idx_id, str(item.get("name") or idx_id), buckets))
                        if entries:
                            self._indexer_cache = (now, entries)
            except Exception as exc:
                logger.debug("Prowlarr indexer list unavailable, falling back to aggregate search: %r", exc)
                if self._indexer_cache:
                    entries = self._indexer_cache[1]

        wanted = set(categories)
        return [
            idx_id
            for idx_id, _name, buckets in entries
            if not buckets or (buckets & wanted)
        ]

    async def _fetch_search_items(
        self,
        query: str,
        categories: List[int],
        limit: int,
        indexer_id: Optional[int] = None,
        timeout_seconds: float = 6.5,
    ) -> List[Dict[str, Any]]:
        """Execute a single Prowlarr search call and return raw items or empty list on error."""
        params: Dict[str, Any] = {
            "query": query,
            "type": "search",
            "limit": limit,
            "categories": list(categories),
        }
        if indexer_id is not None:
            params["indexerIds"] = indexer_id

        try:
            client = get_search_client()
            res = await client.get(
                self._url("/api/v1/search"),
                headers=self._get_headers(),
                params=params,
                timeout=timeout_seconds,
            )
            if res.status_code != 200:
                logger.debug(
                    "Prowlarr search HTTP %s for query=%r indexer=%s",
                    res.status_code,
                    query,
                    indexer_id,
                )
                return []
            items = res.json()
            return items if isinstance(items, list) else []
        except Exception as exc:
            logger.debug(
                "Prowlarr partial search failed for query=%r indexer=%s: %s",
                query,
                indexer_id,
                type(exc).__name__,
            )
            return []

    def _sanitize_items(
        self,
        raw_items: List[Dict[str, Any]],
        vault_unlocked: bool,
    ) -> List[Dict[str, Any]]:
        """Normalize Prowlarr rows, deduplicate across variants, and enforce Adult Vault filtering."""
        deduped: Dict[str, Dict[str, Any]] = {}
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            cat_id = 0
            cat_name = "Other"
            categories_list = item.get("categories", [])
            if categories_list and isinstance(categories_list[0], dict):
                cat_id = categories_list[0].get("id", 0)
                cat_name = categories_list[0].get("name", "Other")

            is_adult = (
                (6000 <= cat_id < 7000)
                or "xxx" in cat_name.lower()
                or "adult" in cat_name.lower()
                # Any category entry, not just the first, and adult-only indexers.
                or any(
                    isinstance(c, dict) and 6000 <= int(c.get("id") or 0) < 7000
                    for c in categories_list
                )
                or matches_adult_marker(str(item.get("indexer") or ""))
            )
            if is_adult and not vault_unlocked:
                continue

            size = item.get("size", 0)
            seeders = item.get("seeders", 0) or 0
            leechers = item.get("leechers", 0) or 0
            guid = item.get("guid") or item.get("infoHash") or item.get("downloadUrl") or item.get("title", "")
            info_hash = item.get("infoHash")

            indexer_name = item.get("indexer", "Unknown")
            normalized = {
                "guid": str(guid),
                "title": item.get("title", "Untitled"),
                "category": cat_name,
                "category_id": cat_id,
                "indexer": indexer_name,
                "size": size,
                "size_formatted": format_bytes(size),
                "seeders": seeders,
                "leechers": leechers,
                "download_url": item.get("downloadUrl"),
                "magnet_url": item.get("magnetUrl"),
                "info_hash": info_hash,
                "publish_date": item.get("publishDate"),
                "is_adult": is_adult,
            }
            if is_adult:
                preview_indexer = canonical_preview_indexer(str(indexer_name))
                if preview_indexer is not None:
                    info_url = str(item.get("infoUrl") or item.get("info_url") or "").strip() or None
                    poster_url = str(item.get("posterUrl") or item.get("poster_url") or "").strip() or None
                    if info_url or (poster_url and supports_poster_fallback(preview_indexer)):
                        normalized["preview_indexer"] = preview_indexer
                        normalized["info_url"] = info_url
                        normalized["poster_url"] = poster_url

            dedup_key = (
                f"ih:{str(info_hash).strip().lower()}"
                if info_hash
                else f"g:{normalized['indexer']}:{normalized['guid']}"
            )
            existing = deduped.get(dedup_key)
            if existing is None or (normalized["seeders"] > existing["seeders"]):
                deduped[dedup_key] = normalized

        return list(deduped.values())

    async def search(
        self,
        query: str,
        category: str = "All",
        vault_unlocked: bool = False,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """Queries Prowlarr per-indexer in parallel for resilient partial results and enforces Adult Vault stealth mode."""
        if category == "Movies":
            categories = [2000]
        elif category == "TV Shows":
            categories = [5000]
        elif category == "Adult":
            if not vault_unlocked:
                return []
            categories = [6000]
        else:  # "All"
            categories = [2000, 5000]
            if vault_unlocked:
                categories.append(6000)

        query_variants = expand_resolution_queries(query)
        if not query_variants:
            return []

        indexer_ids = await self._get_enabled_indexer_ids(categories)
        tasks = []
        if indexer_ids:
            for idx_id in indexer_ids:
                for q in query_variants:
                    tasks.append(
                        self._fetch_search_items(
                            query=q,
                            categories=categories,
                            limit=limit,
                            indexer_id=idx_id,
                            timeout_seconds=8.5,
                        )
                    )
        else:
            for q in query_variants:
                tasks.append(
                    self._fetch_search_items(
                        query=q,
                        categories=categories,
                        limit=limit,
                        indexer_id=None,
                        timeout_seconds=8.0,
                    )
                )

        batches = await asyncio.gather(*tasks, return_exceptions=True)
        merged: List[Dict[str, Any]] = []
        for batch in batches:
            if isinstance(batch, list):
                merged.extend(batch)

        return self._sanitize_items(merged, vault_unlocked=vault_unlocked)

    async def get_health_and_indexer_status(self) -> Dict[str, Any]:
        """Fetch Prowlarr indexer definitions, indexerstatus backoff records, and health warnings."""
        if not self.api_key or not self.base_url:
            return {"reachable": False, "indexers": [], "statuses": [], "health": [], "error": "Not configured"}
        client = get_telemetry_client()
        headers = self._get_headers()
        try:
            idx_res, status_res, health_res = await asyncio.gather(
                client.get(self._url("/api/v1/indexer"), headers=headers, timeout=4.0),
                client.get(self._url("/api/v1/indexerstatus"), headers=headers, timeout=4.0),
                client.get(self._url("/api/v1/health"), headers=headers, timeout=4.0),
                return_exceptions=True,
            )
            indexers = idx_res.json() if not isinstance(idx_res, Exception) and idx_res.status_code == 200 else []
            statuses = status_res.json() if not isinstance(status_res, Exception) and status_res.status_code == 200 else []
            health = health_res.json() if not isinstance(health_res, Exception) and health_res.status_code == 200 else []
            reachable = not isinstance(idx_res, Exception) and getattr(idx_res, "status_code", 0) == 200
            return {
                "reachable": reachable,
                "indexers": indexers if isinstance(indexers, list) else [],
                "statuses": statuses if isinstance(statuses, list) else [],
                "health": health if isinstance(health, list) else [],
            }
        except Exception as exc:
            return {"reachable": False, "indexers": [], "statuses": [], "health": [], "error": str(exc)}


prowlarr_client = ProwlarrClient()

