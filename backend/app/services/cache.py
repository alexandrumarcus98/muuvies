import asyncio
import logging
import time
from typing import Any, Callable, Coroutine, Dict, Optional, Set, Tuple

logger = logging.getLogger("muuvies.cache")

CACHE_TTL_SECONDS = 600
MAX_CACHE_ENTRIES = 250


class SearchMemoryCache:
    """High-performance Stale-While-Revalidate (SWR) search cache."""

    def __init__(
        self,
        fresh_ttl_seconds: int = 300,
        stale_ttl_seconds: int = 1800,
        ttl_seconds: Optional[int] = None,
        max_entries: int = MAX_CACHE_ENTRIES,
    ):
        # Allow callers passing legacy ttl_seconds to override stale window cleanly
        if ttl_seconds is not None:
            fresh_ttl_seconds = min(fresh_ttl_seconds, ttl_seconds)
            stale_ttl_seconds = ttl_seconds
        self.fresh_ttl = fresh_ttl_seconds
        self.stale_ttl = max(fresh_ttl_seconds, stale_ttl_seconds)
        self.ttl = self.stale_ttl
        self.max_entries = max(1, int(max_entries))
        self._cache: Dict[Any, Tuple[float, Any]] = {}
        self._refreshing_keys: Set[Any] = set()
        self._background_tasks: Set[asyncio.Task] = set()

    def get(self, key: str) -> Optional[Any]:
        """Return cached payload if within stale_ttl, or None when expired."""
        entry = self._cache.get(key)
        if not entry:
            return None
        ts, data = entry
        if time.time() - ts < self.stale_ttl:
            return data
        del self._cache[key]
        return None

    def get_swr(
        self,
        key: str,
        refresh_factory: Optional[Callable[[], Coroutine[Any, Any, Any]]] = None,
    ) -> Tuple[Optional[Any], str]:
        """
        Serve cached entries up to stale_ttl (30m) immediately.
        If age exceeds fresh_ttl (5m), trigger an async background refresh once.
        Returns (data, status) where status is 'hit_fresh', 'hit_stale', or 'miss'.
        """
        entry = self._cache.get(key)
        if not entry:
            return None, "miss"

        ts, data = entry
        age = time.time() - ts
        if age >= self.stale_ttl:
            del self._cache[key]
            return None, "miss"

        if age < self.fresh_ttl:
            return data, "hit_fresh"

        # Stale hit (fresh_ttl <= age < stale_ttl): serve immediately and refresh in background
        if refresh_factory is not None and key not in self._refreshing_keys:
            self._schedule_refresh(key, refresh_factory)
        return data, "hit_stale"

    def _schedule_refresh(
        self,
        key: str,
        refresh_factory: Callable[[], Coroutine[Any, Any, Any]],
    ) -> None:
        self._refreshing_keys.add(key)

        async def _runner() -> None:
            try:
                fresh_data = await refresh_factory()
                if fresh_data is not None:
                    self.set(key, fresh_data)
            except Exception as exc:
                logger.debug("Background SWR cache refresh failed for key %s: %s", key, exc)
            finally:
                self._refreshing_keys.discard(key)

        try:
            task = asyncio.create_task(_runner())
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)
        except RuntimeError:
            self._refreshing_keys.discard(key)

    def set(self, key: Any, data: Any) -> None:
        # FIFO/oldest eviction once cache reaches bounded max_entries
        while len(self._cache) >= self.max_entries and key not in self._cache:
            oldest_key = next(iter(self._cache))
            del self._cache[oldest_key]
        if key in self._cache:
            del self._cache[key]
        self._cache[key] = (time.time(), data)

    def clear(self) -> None:
        self._cache.clear()
        self._refreshing_keys.clear()


_PROWLARR_CACHE = SearchMemoryCache(
    fresh_ttl_seconds=CACHE_TTL_SECONDS,
    stale_ttl_seconds=CACHE_TTL_SECONDS,
    max_entries=MAX_CACHE_ENTRIES,
)
search_cache = SearchMemoryCache(fresh_ttl_seconds=300, stale_ttl_seconds=1800, max_entries=500)
curator_cache = SearchMemoryCache(fresh_ttl_seconds=1800, stale_ttl_seconds=1800, max_entries=500)
telemetry_cache = SearchMemoryCache(fresh_ttl_seconds=4, stale_ttl_seconds=12, max_entries=16)
queue_cache = SearchMemoryCache(fresh_ttl_seconds=2, stale_ttl_seconds=4, max_entries=8)

# Autocomplete preloaded catalogue
POPULAR_MEDIA_CATALOGUE = [
    # Top Trending & Classics
    {"title": "American Horror Story", "category": "TV Series", "year": 2011},
    {"title": "Stranger Things", "category": "TV Series", "year": 2016},
    {"title": "Breaking Bad", "category": "TV Series", "year": 2008},
    {"title": "Game of Thrones", "category": "TV Series", "year": 2011},
    {"title": "House of the Dragon", "category": "TV Series", "year": 2022},
    {"title": "The Last of Us", "category": "TV Series", "year": 2023},
    {"title": "The Bear", "category": "TV Series", "year": 2022},
    {"title": "Severance", "category": "TV Series", "year": 2022},
    {"title": "Succession", "category": "TV Series", "year": 2018},
    {"title": "True Detective", "category": "TV Series", "year": 2014},
    {"title": "Dexter: Resurrection", "category": "TV Series", "year": 2025},
    {"title": "Shogun", "category": "TV Series", "year": 2024},
    {"title": "Fargo", "category": "TV Series", "year": 2014},
    {"title": "Interstellar", "category": "Movie", "year": 2014},
    {"title": "Inception", "category": "Movie", "year": 2010},
    {"title": "Dune: Part Two", "category": "Movie", "year": 2024},
    {"title": "Dune", "category": "Movie", "year": 2021},
    {"title": "Oppenheimer", "category": "Movie", "year": 2023},
    {"title": "The Dark Knight", "category": "Movie", "year": 2008},
    {"title": "Gladiator II", "category": "Movie", "year": 2024},
    {"title": "Gladiator", "category": "Movie", "year": 2000},
    {"title": "Spider-Man: Across the Spider-Verse", "category": "Movie", "year": 2023},
    {"title": "The Batman", "category": "Movie", "year": 2022},
    {"title": "Blade Runner 2049", "category": "Movie", "year": 2017},
    {"title": "Poor Things", "category": "Movie", "year": 2023},
    {"title": "Alien: Romulus", "category": "Movie", "year": 2024},
    {"title": "Deadpool & Wolverine", "category": "Movie", "year": 2024},
    {"title": "Everything Everywhere All at Once", "category": "Movie", "year": 2022},
    {"title": "Pulp Fiction", "category": "Movie", "year": 1994},
    {"title": "Fight Club", "category": "Movie", "year": 1999},
    {"title": "The Matrix", "category": "Movie", "year": 1999},
]


def make_search_cache_key(
    query: str,
    category: str,
    vault_unlocked: bool,
    ai_smart_search: bool = False,
    deep: bool = False,
) -> str:
    """Canonical cache key for Prowlarr + heuristic enriched results."""
    base = f"{query.strip().lower()}_{category.strip().lower()}_{vault_unlocked}"
    return f"{base}_deep" if deep else base


async def prewarm_popular_catalogue(limit: int = 15) -> int:
    """Warm Prowlarr + heuristic search cache for top popular titles without blocking requests."""
    from backend.app.services.prowlarr import prowlarr_client
    from backend.app.services.library_mirror import query_library_fts
    from backend.app.ai_assistant import ai_assistant

    warmed = 0
    for item in POPULAR_MEDIA_CATALOGUE[:limit]:
        title = item["title"]
        key = make_search_cache_key(title, "All", False, False)
        _, status = search_cache.get_swr(key)
        if status == "hit_fresh":
            continue
        try:
            raw_results = await prowlarr_client.search(
                query=title,
                category="All",
                vault_unlocked=False,
                limit=60,
            )
            if not raw_results:
                continue
            lib_matches = await query_library_fts(title, vault_unlocked=False)
            enriched, summary = ai_assistant.enrich_and_score_heuristically(
                query=title,
                releases=raw_results,
                library_items=lib_matches,
            )
            search_cache.set(key, (enriched, summary))
            warmed += 1
        except Exception as exc:
            logger.debug("Pre-warm skipped for '%s': %s", title, exc)
        await asyncio.sleep(0.25)
    return warmed


async def run_catalogue_prewarm_loop(interval_seconds: int = 3600, top_n: int = 15) -> None:
    """Hourly background pre-warmer for top 15 POPULAR_MEDIA_CATALOGUE entries."""
    # Give startup sync a few seconds before initial prewarm
    await asyncio.sleep(5)
    while True:
        try:
            warmed = await prewarm_popular_catalogue(limit=top_n)
            if warmed:
                logger.info("Pre-warmed %d top catalogue entries in search cache", warmed)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Catalogue pre-warmer loop error: %s", exc)
        await asyncio.sleep(interval_seconds)
