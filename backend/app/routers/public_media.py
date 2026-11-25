"""Public cached TMDB trending showcase & same-origin backdrop proxy for the landing page."""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any, Dict, List, Tuple

from fastapi import HTTPException, Response

from backend.app.config import settings
from backend.app.routers._access import public_router
from backend.app.services.http_clients import get_search_client

logger = logging.getLogger(__name__)

router = public_router(prefix="/api/public", tags=["Public Showcase"])

_TMDB_BASE = "https://api.themoviedb.org/3"
_TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w1280"
_BACKDROP_RE = re.compile(r"^[A-Za-z0-9_-]+\.jpg$")
_TRENDING_TTL_SECONDS = 6 * 3600
_MAX_BACKDROP_CACHE = 24

_trending_cache: Tuple[float, List[Dict[str, Any]]] = (0.0, [])
_backdrop_cache: Dict[str, bytes] = {}
_trending_lock: asyncio.Lock | None = None
_backdrop_lock: asyncio.Lock | None = None


def _get_trending_lock() -> asyncio.Lock:
    global _trending_lock
    if _trending_lock is None:
        _trending_lock = asyncio.Lock()
    return _trending_lock


def _get_backdrop_lock() -> asyncio.Lock:
    global _backdrop_lock
    if _backdrop_lock is None:
        _backdrop_lock = asyncio.Lock()
    return _backdrop_lock


def _tmdb_auth(key: str) -> Tuple[Dict[str, str], Dict[str, str]]:
    """Support both TMDB v4 Read Access Token (JWT) and v3 API Key."""
    token = key.strip()
    if "." in token:
        return {"Authorization": f"Bearer {token}", "Accept": "application/json"}, {}
    return {"Accept": "application/json"}, {"api_key": token}


async def _fetch_director(movie_id: int, headers: Dict[str, str], params: Dict[str, str]) -> str:
    try:
        client = get_search_client()
        resp = await client.get(
            f"{_TMDB_BASE}/movie/{movie_id}/credits",
            headers=headers,
            params=params,
            timeout=4.0,
        )
        if resp.status_code != 200:
            return "Featured release"
        crew = resp.json().get("crew") or []
        for member in crew:
            if member.get("job") == "Director" and member.get("name"):
                return str(member["name"])
    except Exception:
        pass
    return "Featured release"


@router.get("/trending")
async def get_public_trending() -> Dict[str, Any]:
    """Return up to 10 weekly trending movies for the signed-out 3D corridor."""
    global _trending_cache

    key = (settings.TMDB_READ_ONLY_API_KEY or "").strip()
    if not key:
        return {"items": []}

    now = time.monotonic()
    cached_at, cached_items = _trending_cache
    if cached_items and (now - cached_at) < _TRENDING_TTL_SECONDS:
        return {"items": cached_items}

    async with _get_trending_lock():
        now = time.monotonic()
        cached_at, cached_items = _trending_cache
        if cached_items and (now - cached_at) < _TRENDING_TTL_SECONDS:
            return {"items": cached_items}

        headers, base_params = _tmdb_auth(key)
        try:
            client = get_search_client()
            resp = await client.get(
                f"{_TMDB_BASE}/trending/movie/week",
                headers=headers,
                params={**base_params, "language": "en-US"},
                timeout=6.0,
            )
            if resp.status_code != 200:
                logger.warning("TMDB trending returned status %s", resp.status_code)
                return {"items": cached_items}

            results = resp.json().get("results") or []
            candidates = [
                m for m in results
                if m.get("title") and m.get("backdrop_path")
            ][:10]

            directors = await asyncio.gather(
                *(_fetch_director(int(m["id"]), headers, base_params) for m in candidates)
            )

            items: List[Dict[str, Any]] = []
            for m, director in zip(candidates, directors):
                backdrop_file = str(m["backdrop_path"]).lstrip("/")
                if not _BACKDROP_RE.fullmatch(backdrop_file):
                    continue
                release_date = str(m.get("release_date") or "")
                year = release_date[:4] if len(release_date) >= 4 else "2025"
                rating = m.get("vote_average")
                rating_str = f"TMDB {float(rating):.1f}" if rating else "Trending"

                items.append(
                    {
                        "id": str(m["id"]),
                        "title": str(m["title"]),
                        "director": director,
                        "year": year,
                        "aspect": "1.78:1",
                        "specs": f"{rating_str} · Trending this week",
                        "synopsis": str(m.get("overview") or "No synopsis available."),
                        "imageSrc": f"/api/public/backdrop/{backdrop_file}",
                    }
                )

            if items:
                _trending_cache = (now, items)
            return {"items": items}
        except Exception as exc:
            logger.warning("TMDB trending fetch failed: %s", exc)
            return {"items": cached_items}


@router.get("/backdrop/{filename}")
async def get_public_backdrop(filename: str) -> Response:
    """Same-origin proxy for TMDB w1280 backdrop stills (avoids WebGL CORS tainting)."""
    if not _BACKDROP_RE.fullmatch(filename):
        raise HTTPException(status_code=404, detail="Not found")

    if filename in _backdrop_cache:
        return Response(
            content=_backdrop_cache[filename],
            media_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    try:
        client = get_search_client()
        resp = await client.get(f"{_TMDB_IMAGE_BASE}/{filename}", timeout=8.0)
        if resp.status_code != 200 or not resp.headers.get("content-type", "").startswith("image/"):
            raise HTTPException(status_code=404, detail="Not found")

        if len(_backdrop_cache) >= _MAX_BACKDROP_CACHE:
            _backdrop_cache.pop(next(iter(_backdrop_cache)))
        _backdrop_cache[filename] = resp.content

        return Response(
            content=resp.content,
            media_type=resp.headers.get("content-type", "image/jpeg"),
            headers={"Cache-Control": "public, max-age=86400"},
        )
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(status_code=404, detail="Not found")

