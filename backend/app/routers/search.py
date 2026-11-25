import asyncio
import json
import logging
import math
import re
import unicodedata
from typing import Any, Dict, List, Optional, Tuple

import sentry_sdk
from fastapi import BackgroundTasks, Depends, HTTPException, Query, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.ai_assistant import ai_assistant, is_adult_item
from backend.app.database import AsyncSessionLocal, get_db
from backend.app.models import GrabHistory, SearchLog
from backend.app.routers._access import user_router
from backend.app.schemas import (
    CuratorRequest,
    CuratorResponse,
    SearchRequest,
    SearchResponse,
    SearchResultItem,
    SearchSuggestionItem,
    SearchSuggestionsResponse,
    SmartRankingSummary,
)
from backend.app.security import check_vault_unlocked, get_current_session
from backend.app.services.adult_preview import (
    canonical_preview_indexer,
    is_allowed_image_url,
    is_allowed_page_url,
    supports_poster_fallback,
)
from backend.app.services.cache import (
    CACHE_TTL_SECONDS,
    MAX_CACHE_ENTRIES,
    POPULAR_MEDIA_CATALOGUE,
    _PROWLARR_CACHE,
    make_search_cache_key,
    search_cache,
)
from backend.app.services.library_mirror import query_library_fts, suggest_local_prefix
from backend.app.services.prowlarr import prowlarr_client
from backend.app.services.streaming import PREVIEW_TOKEN_TTL_SECONDS, create_preview_token
from backend.app.services.search_scoring import (
    _build_dedup_key,
    _normalize_title,
    deduplicate_and_score_results,
    expand_deep_queries,
    fuse_deep_search_lists,
)

expand_search_queries = expand_deep_queries

__all__ = [
    "router",
    "suggest_router",
    "_PROWLARR_CACHE",
    "CACHE_TTL_SECONDS",
    "MAX_CACHE_ENTRIES",
    "_normalize_title",
    "_build_dedup_key",
    "deduplicate_and_score_results",
    "expand_deep_queries",
    "expand_search_queries",
    "fuse_deep_search_lists",
]

router = user_router(prefix="/api/search", tags=["Unified Search"])
suggest_router = user_router(prefix="/api", tags=["Unified Search"])


async def _record_search_log(
    query: str,
    category: str,
    ai_smart_search: bool,
    ai_used: bool,
    result_count: int,
    vault_scoped: bool = False,
) -> None:
    """Persist search log asynchronously in a detached DB session.

    ``vault_scoped``: the search ran with the vault open, so its text may be
    adult even under "All". Such rows are never suggested to a locked session.
    """
    try:
        async with AsyncSessionLocal() as session:
            log_entry = SearchLog(
                query=query,
                category=category,
                parsed_filters=json.dumps({"ai_smart_search": ai_smart_search, "vault": bool(vault_scoped)}),
                ai_used=ai_used,
                result_count=result_count,
            )
            session.add(log_entry)
            await session.commit()
    except Exception:
        pass


def _not_vault_scoped():
    """SQL filter: drop search logs written while the vault was open."""
    return ~SearchLog.parsed_filters.like('%"vault": true%')


def _mint_preview_token_for_row(row: Dict[str, Any], session: dict) -> Optional[str]:
    """Mint a per-response Fernet preview token without mutating cached result dicts."""
    if not check_vault_unlocked(session) or not is_adult_item(row):
        return None
    preview_indexer = canonical_preview_indexer(str(row.get("preview_indexer") or row.get("indexer") or ""))
    if preview_indexer is None:
        return None
    raw_info = str(row.get("info_url") or "").strip()
    raw_poster = str(row.get("poster_url") or "").strip()
    info_url = raw_info if (raw_info and is_allowed_page_url(raw_info, preview_indexer)) else None
    poster_url = raw_poster if (raw_poster and is_allowed_image_url(raw_poster)) else None
    if not info_url and not (poster_url and supports_poster_fallback(preview_indexer)):
        return None
    return create_preview_token(
        {
            "indexer": preview_indexer,
            "info_url": info_url,
            "poster_url": poster_url,
            "sub": str(session.get("sub") or ""),
        },
        vault_exp=int(session.get("vault_exp", 0) or 0),
        ttl_seconds=PREVIEW_TOKEN_TTL_SECONDS,
        kind="adult-preview",
    )


def has_resolvable_download(item: Dict[str, Any]) -> bool:
    """Return True if a Prowlarr release has a download URL, magnet URI, or infoHash."""
    return any(
        str(item.get(k) or "").strip()
        for k in ("download_url", "magnet_url", "info_hash", "downloadUrl", "magnetUrl", "infoHash")
    )


async def _load_grab_history(vault_unlocked: bool, db: Optional[AsyncSession] = None):
    stmt = select(GrabHistory.info_hash, GrabHistory.title)
    if not vault_unlocked:
        stmt = stmt.where(GrabHistory.category != "adult")
    if db is not None:
        return await db.execute(stmt)
    async with AsyncSessionLocal() as bg_db:
        return await bg_db.execute(stmt)


def _annotate_actionable_releases(
    raw_results: List[Dict[str, Any]],
    grabbed_hashes: set[str],
    grabbed_titles: set[str],
) -> List[Dict[str, Any]]:
    """Filter ungrabbable releases and mark releases already present in GrabHistory."""
    actionable: List[Dict[str, Any]] = []
    for item in raw_results:
        if not has_resolvable_download(item):
            continue
        item["can_grab"] = True
        ih = item.get("info_hash")
        title = item.get("title", "")
        item["already_grabbed"] = bool(
            (ih and ih.lower() in grabbed_hashes) or (title.lower() in grabbed_titles)
        )
        actionable.append(item)
    return actionable


_CACHE_QUERY_RE = re.compile(r"[^a-z0-9]+")


def _prowlarr_cache_key(query: str, category: str, vault_unlocked: bool) -> Tuple[str, str, bool]:
    """Canonical TTL cache key keeping qualifiers (e.g. 2160p/4k) and Adult Vault state distinct."""
    decomposed = unicodedata.normalize("NFKD", query or "")
    ascii_lower = decomposed.encode("ascii", "ignore").decode("ascii").lower()
    norm_q = _CACHE_QUERY_RE.sub(" ", ascii_lower).strip()
    return (norm_q, category.strip().lower(), bool(vault_unlocked))


async def _fetch_prowlarr_with_cache(
    query: str,
    category: str,
    vault_unlocked: bool,
    limit: int,
) -> List[Dict[str, Any]]:
    key = _prowlarr_cache_key(query, category, vault_unlocked)
    cached = _PROWLARR_CACHE.get(key)
    if cached is not None:
        return [dict(item) for item in cached]

    results = await prowlarr_client.search(
        query=query,
        category=category,
        vault_unlocked=vault_unlocked,
        limit=limit,
    )
    if results:
        _PROWLARR_CACHE.set(key, [dict(item) for item in results])
    return list(results or [])


async def _fetch_deep_search_lists(
    queries: List[str],
    category: str,
    vault_unlocked: bool,
    limit: int,
) -> List[Tuple[str, List[Dict[str, Any]]]]:
    """Execute uncached expanded queries with Semaphore(2) and 200ms stagger to avoid 429."""
    sem = asyncio.Semaphore(2)
    uncached_order = [
        q for q in queries if _PROWLARR_CACHE.get(_prowlarr_cache_key(q, category, vault_unlocked)) is None
    ]
    stagger_index = {q: idx for idx, q in enumerate(uncached_order)}

    async def _run_one(q: str) -> List[Dict[str, Any]]:
        delay_idx = stagger_index.get(q)
        if delay_idx is None:
            return await _fetch_prowlarr_with_cache(q, category, vault_unlocked, limit)
        if delay_idx > 0:
            await asyncio.sleep(0.2 * delay_idx)
        async with sem:
            return await _fetch_prowlarr_with_cache(q, category, vault_unlocked, limit)

    raw_lists = await asyncio.gather(
        *[_run_one(q) for q in queries],
        return_exceptions=True,
    )
    logger = logging.getLogger("muuvies.search")
    collected: List[Tuple[str, List[Dict[str, Any]]]] = []
    for q, res in zip(queries, raw_lists):
        if isinstance(res, Exception):
            logger.warning("Deep search sub-query '%s' failed: %s", q, res)
            continue
        collected.append((q, list(res or [])))
    return collected


async def _execute_live_search(
    query: str,
    category: str,
    vault_unlocked: bool,
    limit: int,
    db: Optional[AsyncSession] = None,
    deep: bool = False,
) -> Tuple[List[Dict[str, Any]], Optional[SmartRankingSummary]]:
    """Run Prowlarr lookup (cached) + FTS5 mirror + WSM/RRF deduplication + heuristic scoring."""
    history_coro = _load_grab_history(vault_unlocked=vault_unlocked, db=db)
    mirror_coro = query_library_fts(query_terms=query, vault_unlocked=vault_unlocked)

    if deep:
        history_res, library_matches = await asyncio.gather(history_coro, mirror_coro)
    else:
        prowlarr_coro = _fetch_prowlarr_with_cache(
            query=query,
            category=category,
            vault_unlocked=vault_unlocked,
            limit=limit,
        )
        raw_results, history_res, library_matches = await asyncio.gather(
            prowlarr_coro,
            history_coro,
            mirror_coro,
        )

    history_rows = history_res.all() if history_res else []
    grabbed_hashes = {h[0].lower() for h in history_rows if h[0]}
    grabbed_titles = {h[1].lower() for h in history_rows if h[1]}

    if deep:
        norm_q = _normalize_title(query)
        history_aliases = [
            row[1]
            for row in history_rows
            if row[1] and (norm_q in _normalize_title(row[1]) or _normalize_title(row[1]) in norm_q)
        ]
        local_aliases = [
            *[str(m.get("clean_title") or m.get("title") or "") for m in library_matches],
            *history_aliases,
        ]
        queries = expand_deep_queries(
            query=query,
            category=category,
            library_items=library_matches,
            local_aliases=[a for a in local_aliases if a],
        )
        sub_lists = await _fetch_deep_search_lists(
            queries=queries,
            category=category,
            vault_unlocked=vault_unlocked,
            limit=limit,
        )
        ranked_lists: List[List[Dict[str, Any]]] = []
        for sub_q, raw_sub in sub_lists:
            actionable_sub = _annotate_actionable_releases(raw_sub, grabbed_hashes, grabbed_titles)
            scored_sub = deduplicate_and_score_results(
                query=sub_q,
                raw_results=actionable_sub,
                category=category,
            )
            if scored_sub:
                ranked_lists.append(scored_sub)
        merged_results = fuse_deep_search_lists(ranked_lists, k=60, wsm_weight=0.35)
    else:
        actionable_results = _annotate_actionable_releases(raw_results, grabbed_hashes, grabbed_titles)
        merged_results = deduplicate_and_score_results(
            query=query,
            raw_results=actionable_results,
            category=category,
        )

    return ai_assistant.enrich_and_score_heuristically(
        query=query,
        releases=merged_results,
        library_items=library_matches,
    )


@suggest_router.get("/suggest")
async def get_local_suggest(
    q: str = Query(""),
    session: dict = Depends(get_current_session),
) -> List[Dict[str, Any]]:
    """Ultra-fast (<15ms) local SQLite FTS5 + grab_history prefix autocomplete."""
    vault_unlocked = check_vault_unlocked(session)
    return await suggest_local_prefix(query=q, vault_unlocked=vault_unlocked, limit=8)


@router.get("/suggestions", response_model=SearchSuggestionsResponse)
async def get_search_suggestions(
    q: str = Query(..., min_length=1),
    category: str = Query("All"),
    session: dict = Depends(get_current_session),
    db: AsyncSession = Depends(get_db),
):
    """Instant Instagram/TikTok-style autocomplete suggestions (<5ms)."""
    vault_unlocked = check_vault_unlocked(session)
    q_clean = q.strip().lower()
    matches: List[SearchSuggestionItem] = []
    seen_titles = set()

    for item in POPULAR_MEDIA_CATALOGUE:
        t = item["title"]
        if q_clean in t.lower() and t.lower() not in seen_titles:
            matches.append(
                SearchSuggestionItem(
                    title=t,
                    category=item["category"],
                    year=item.get("year"),
                    source="trending",
                )
            )
            seen_titles.add(t.lower())
            if len(matches) >= 8:
                break

    if len(matches) < 8:
        escaped_q = q_clean.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        stmt = select(SearchLog.query, SearchLog.category).where(
            SearchLog.query.ilike(f"%{escaped_q}%", escape="\\")
        )
        if not vault_unlocked:
            stmt = stmt.where(SearchLog.category != "Adult").where(_not_vault_scoped())
        stmt = stmt.order_by(SearchLog.created_at.desc()).limit(10)
        res = await db.execute(stmt)
        for row in res.all():
            sq, scat = row[0], row[1]
            if sq.lower() not in seen_titles:
                matches.append(
                    SearchSuggestionItem(
                        title=sq,
                        category=scat or "Search History",
                        source="history",
                    )
                )
                seen_titles.add(sq.lower())
                if len(matches) >= 8:
                    break

    return SearchSuggestionsResponse(query=q, suggestions=matches)


@router.get("", response_model=SearchResponse)
async def perform_search_get(
    background_tasks: BackgroundTasks,
    request: Request,
    q: str = Query(..., min_length=1),
    category: str = Query("All"),
    deep: bool = Query(False),
    ai_curate: bool = Query(False),
    limit: int = Query(100, ge=1, le=200),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    sort_by: str = Query("smart"),
    session: dict = Depends(get_current_session),
    db: AsyncSession = Depends(get_db),
):
    payload = SearchRequest(
        query=q,
        category=category,
        ai_smart_search=ai_curate,
        deep=deep,
        limit=limit,
        page=page,
        page_size=page_size,
        sort_by=sort_by,
    )
    return await perform_search(
        payload=payload,
        background_tasks=background_tasks,
        request=request,
        session=session,
        db=db,
    )


@router.post("", response_model=SearchResponse)
async def perform_search(
    payload: SearchRequest,
    background_tasks: BackgroundTasks,
    request: Request,
    session: dict = Depends(get_current_session),
    db: AsyncSession = Depends(get_db),
):
    vault_unlocked = check_vault_unlocked(session)

    # Server-side guard: if user requests Adult category while vault is locked, return 404 stealth
    if "adult" in payload.category.lower() and not vault_unlocked:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Not Found",
        )

    is_prefetch = request.headers.get("x-prefetch", "").strip() == "1"
    cache_key = make_search_cache_key(
        payload.query, payload.category, vault_unlocked, deep=payload.deep
    )

    async def _bg_refresh_factory():
        refreshed = await _execute_live_search(
            query=payload.query,
            category=payload.category,
            vault_unlocked=vault_unlocked,
            limit=payload.limit,
            db=None,
            deep=payload.deep,
        )
        # Never overwrite cached hits with a transient empty result set.
        return refreshed if refreshed[0] else None

    cached_entry, cache_status = search_cache.get_swr(cache_key, refresh_factory=_bg_refresh_factory)

    if cached_entry is not None:
        all_enriched, ai_summary = cached_entry
    else:
        all_enriched, ai_summary = await _execute_live_search(
            query=payload.query,
            category=payload.category,
            vault_unlocked=vault_unlocked,
            limit=payload.limit,
            db=db,
            deep=payload.deep,
        )
        if all_enriched:
            search_cache.set(cache_key, (all_enriched, ai_summary))

        # Decouple SearchLog DB commit from request latency via BackgroundTasks
        if not is_prefetch:
            background_tasks.add_task(
                _record_search_log,
                payload.query,
                payload.category,
                payload.ai_smart_search,
                payload.ai_smart_search or (ai_summary is not None and ai_summary.ai_active),
                len(all_enriched),
                check_vault_unlocked(session),
            )

    # Server-Side Sorting
    sorted_items = list(all_enriched)
    sort_by = payload.sort_by.lower()
    if sort_by == "seeders":
        sorted_items.sort(key=lambda r: r.get("seeders", 0), reverse=True)
    elif sort_by == "size_desc":
        sorted_items.sort(key=lambda r: r.get("size", 0), reverse=True)
    elif sort_by == "size_asc":
        sorted_items.sort(key=lambda r: r.get("size", 0))
    elif sort_by == "newest":
        sorted_items.sort(key=lambda r: r.get("publish_date") or "", reverse=True)
    else:  # "smart" (default)
        sorted_items.sort(
            key=lambda r: (
                1 if r.get("ai_pick") else 0,
                float(
                    r.get("muuvies_score")
                    if r.get("muuvies_score") is not None
                    else r.get("quality_score", 0)
                ),
                r.get("seeders", 0),
            ),
            reverse=True,
        )

    # Server-Side Pagination
    total_found = len(sorted_items)
    page_size = max(1, min(100, payload.page_size))
    total_pages = max(1, math.ceil(total_found / page_size))
    current_page = max(1, min(payload.page, total_pages))

    start_idx = (current_page - 1) * page_size
    end_idx = start_idx + page_size
    paged_slice = sorted_items[start_idx:end_idx]

    result_items = [
        SearchResultItem(
            **r,
            preview_token=_mint_preview_token_for_row(r, session) if vault_unlocked else None,
        )
        for r in paged_slice
    ]

    return SearchResponse(
        query=payload.query,
        category=payload.category,
        total_results=len(result_items),
        page=current_page,
        page_size=page_size,
        total_pages=total_pages,
        total_found=total_found,
        results=result_items,
        ai_ranking=ai_summary,
        vault_active=not vault_unlocked,
        deep_active=payload.deep,
    )


@router.post("/ai-pick", response_model=CuratorResponse)
@router.post("/curator", response_model=CuratorResponse)
async def curate_search_results(
    payload: CuratorRequest,
    session: dict = Depends(get_current_session),
):
    """Non-blocking follow-up endpoint delivering cached or live LLM curation over top 15 candidates."""
    vault_unlocked = check_vault_unlocked(session)
    if "adult" in payload.category.lower() and not vault_unlocked:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Not Found",
        )

    cache_key = make_search_cache_key(payload.query, payload.category, vault_unlocked)
    cached_entry = search_cache.get(cache_key)
    if cached_entry is None:
        deep_key = make_search_cache_key(payload.query, payload.category, vault_unlocked, deep=True)
        cached_entry = search_cache.get(deep_key)
        if cached_entry is not None:
            cache_key = deep_key

    candidates: List[Dict[str, Any]] = []
    if payload.candidates:
        candidates = [c.model_dump() for c in payload.candidates]
    elif cached_entry is not None:
        candidates = cached_entry[0]

    # Strip Adult / Vault / Whisparr items even if category="All" and Vault is unlocked
    candidates = [c for c in candidates if not is_adult_item(c)]
    if not candidates:
        return CuratorResponse(ai_ranking=None, ratings={}, cached=False)

    lib_matches: List[Dict[str, Any]] = []
    try:
        # Never load Whisparr items when preparing context for external AI providers
        lib_matches = await query_library_fts(payload.query, vault_unlocked=False)
        summary, ratings_map, cached_hit = await ai_assistant.curate_top_candidates(
            query=payload.query,
            releases=candidates,
            library_items=lib_matches,
            category=payload.category,
        )
    except Exception as exc:
        logging.getLogger("muuvies.search").warning("Curator fallback triggered after error: %s", exc)
        sentry_sdk.add_breadcrumb(
            category="ai.curator",
            message="Curator route degraded to heuristic fallback",
            level="warning",
            data={"error": str(exc)[:200]},
        )
        _, summary = ai_assistant.enrich_and_score_heuristically(payload.query, candidates, lib_matches)
        ratings_map, cached_hit = {}, False

    # Merge AI recommendation back into cached search results so subsequent page switches keep it
    if summary and summary.ai_active and cached_entry is not None:
        all_enriched, _ = cached_entry
        for item in all_enriched:
            guid = item.get("guid")
            item["ai_pick"] = bool(guid and guid == summary.recommended_guid)
            if item["ai_pick"]:
                item["ai_analysis"] = summary.recommendation_reason
                item["quality_score"] = max(item.get("quality_score", 50), 98)
            elif guid in ratings_map:
                item["ai_analysis"] = ratings_map[guid]
        search_cache.set(cache_key, (all_enriched, summary))

    return CuratorResponse(ai_ranking=summary, ratings=ratings_map, cached=cached_hit)


@router.get("/recent-logs")
async def get_recent_search_logs(
    limit: int = Query(10, ge=1, le=100),
    session: dict = Depends(get_current_session),
    db: AsyncSession = Depends(get_db),
):
    vault_unlocked = check_vault_unlocked(session)
    stmt = select(SearchLog)
    if not vault_unlocked:
        stmt = stmt.where(SearchLog.category != "Adult").where(_not_vault_scoped())
    stmt = stmt.order_by(SearchLog.created_at.desc()).limit(limit)

    res = await db.execute(stmt)
    logs = res.scalars().all()
    return [
        {
            "id": log.id,
            "query": log.query,
            "category": log.category,
            "result_count": log.result_count,
            "ai_used": log.ai_used,
            "created_at": log.created_at,
        }
        for log in logs
    ]
