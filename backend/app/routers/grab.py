import re
from typing import Optional

from fastapi import Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.database import get_db
from backend.app.models import GrabHistory
from backend.app.routers._access import user_router
from backend.app.schemas import (
    QuickGrabRequest,
    ArrMonitorRequest,
    GrabResponse,
    GrabHistoryItem,
)
from backend.app.security import get_current_session, check_vault_unlocked
from backend.app.services.qbittorrent import qb_client, format_bytes
from backend.app.services.arr_client import radarr_client, sonarr_client, whisparr_client
from backend.app.services.cross_seed import cross_seed_client
from backend.app.services.search_scoring import matches_adult_marker
from backend.app.services.torrent_stream import normalize_info_hash
from backend.app.services.url_safety import (
    has_control_chars,
    is_prowlarr_origin,
    is_valid_magnet_uri,
    validate_download_url,
)

router = user_router(prefix="/api/grab", tags=["Release Grabbing"])


def _is_adult_grab(category: str, indexer: str = "", download_url: Optional[str] = None) -> bool:
    """Determine adult status server-side from category, indexer, and download URL."""
    cat_lower = (category or "").strip().lower()
    if "adult" in cat_lower or "xxx" in cat_lower:
        return True
    if matches_adult_marker(indexer or ""):
        return True
    if download_url and matches_adult_marker(download_url, include_adult_word=False):
        return True
    return False


async def _validate_grab_urls(
    download_url: Optional[str],
    magnet_url: Optional[str],
    info_hash: Optional[str],
) -> None:
    if info_hash and has_control_chars(info_hash):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid info_hash.")
    if magnet_url and not is_valid_magnet_uri(magnet_url):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid magnet_url.")
    if download_url:
        raw_dl = download_url.strip()
        if has_control_chars(raw_dl):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid download_url.")
        if raw_dl.lower().startswith("magnet:"):
            if not is_valid_magnet_uri(raw_dl):
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid magnet download_url.")
        else:
            ok_url, reason = await validate_download_url(raw_dl, allow_prowlarr=is_prowlarr_origin(raw_dl))
            if not ok_url:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=reason)


@router.post("/quick", response_model=GrabResponse)
async def quick_grab(
    payload: QuickGrabRequest,
    session: dict = Depends(get_current_session),
    db: AsyncSession = Depends(get_db),
):
    vault_unlocked = check_vault_unlocked(session)

    # Server-side category & adult validation (return 404 stealth when Vault locked)
    cat_lower = (payload.category or "").strip().lower()
    if has_control_chars(payload.category or ""):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid category.")

    is_adult = _is_adult_grab(payload.category, payload.indexer, payload.download_url)
    if is_adult and not vault_unlocked:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Not Found",
        )

    await _validate_grab_urls(payload.download_url, payload.magnet_url, payload.info_hash)

    # Normalize qBittorrent category (adult always stays isolated in 'adult')
    qb_category = "Movies"
    if is_adult:
        qb_category = "adult"
    elif "tv" in cat_lower or "series" in cat_lower:
        qb_category = "TV Shows"

    url_to_add = payload.download_url or payload.magnet_url
    if not url_to_add and not payload.info_hash:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Either download_url, magnet_url, or info_hash must be provided.",
        )

    # 1. Add to qBittorrent (retry .torrent download once, then fall back to magnet/infoHash; 422 on dead link)
    add_result = await qb_client.add_torrent_with_status(
        urls=url_to_add or "",
        category=qb_category,
        paused=False,
        magnet_url=payload.magnet_url,
        info_hash=payload.info_hash,
        title=payload.title,
        sequential=payload.stream,
    )
    added, error_detail, err_status = add_result
    # The hash the player needs for `qb:<hash>`: from the .torrent/magnet when we
    # could read it, else whatever the indexer reported.
    added_hash = getattr(add_result, "info_hash", None) or normalize_info_hash(payload.info_hash)
    if not added:
        raise HTTPException(
            status_code=err_status,
            detail=error_detail or "qBittorrent is unreachable.",
        )

    # 2. Trigger cross-seed search if info_hash is available (skip adult grabs)
    if payload.info_hash and not is_adult:
        try:
            await cross_seed_client.trigger_search(info_hash=payload.info_hash)
        except Exception:
            pass

    # 3. Record in grab_history
    history_entry = GrabHistory(
        info_hash=payload.info_hash,
        title=payload.title,
        category=qb_category,
        indexer=payload.indexer,
        size=payload.size,
        target="qbittorrent",
        status="grabbed",
    )
    db.add(history_entry)
    await db.commit()
    await db.refresh(history_entry)

    return GrabResponse(
        success=True,
        target="qbittorrent",
        message=(
            f"Sent to qBittorrent in category '{qb_category}' in streaming mode."
            if payload.stream
            else f"Sent to qBittorrent in category '{qb_category}'. Auto-monitoring enabled."
        ),
        info_hash=added_hash or payload.info_hash,
        history_id=history_entry.id,
    )


@router.post("/arr", response_model=GrabResponse)
async def arr_monitor(
    payload: ArrMonitorRequest,
    session: dict = Depends(get_current_session),
    db: AsyncSession = Depends(get_db),
):
    vault_unlocked = check_vault_unlocked(session)

    cat_lower = (payload.category or "").strip().lower()
    if has_control_chars(payload.category or ""):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid category.")

    is_adult = _is_adult_grab(payload.category, payload.indexer, payload.download_url)
    if is_adult and not vault_unlocked:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Not Found",
        )
    if is_adult:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Adult releases are sent directly to qBittorrent; Whisparr monitoring is disabled.",
        )

    await _validate_grab_urls(payload.download_url, payload.magnet_url, payload.info_hash)

    target_name = "radarr"
    normalized_cat = "Movies"
    if "tv" in cat_lower or "series" in cat_lower:
        target_name = "sonarr"
        normalized_cat = "TV Shows"
        client = sonarr_client
    else:
        target_name = "radarr"
        normalized_cat = "Movies"
        client = radarr_client

    success, msg = await client.add_and_monitor(
        title=payload.title,
        year=payload.year,
        tmdb_id=payload.tmdb_id,
        tvdb_id=payload.tvdb_id,
        download_url=payload.download_url,
    )

    if not success:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=msg,
        )

    # Record in grab history
    history_entry = GrabHistory(
        info_hash=payload.info_hash,
        title=payload.title,
        category=normalized_cat,
        indexer=payload.indexer,
        size=payload.size,
        target=target_name,
        status="monitored",
    )
    db.add(history_entry)
    await db.commit()
    await db.refresh(history_entry)

    return GrabResponse(
        success=True,
        target=target_name,
        message=msg,
        info_hash=payload.info_hash,
        history_id=history_entry.id,
    )


@router.get("/history", response_model=list[GrabHistoryItem])
async def get_grab_history(
    limit: int = Query(50, ge=1, le=200),
    session: dict = Depends(get_current_session),
    db: AsyncSession = Depends(get_db),
):
    vault_unlocked = check_vault_unlocked(session)

    stmt = select(GrabHistory)
    # Server-side stealth mode filter:
    if not vault_unlocked:
        stmt = stmt.where(GrabHistory.category != "adult").where(GrabHistory.target != "whisparr")

    stmt = stmt.order_by(GrabHistory.created_at.desc()).limit(limit)
    res = await db.execute(stmt)
    rows = res.scalars().all()

    return [
        GrabHistoryItem(
            id=r.id,
            title=r.title,
            category=r.category,
            indexer=r.indexer,
            size=r.size,
            size_formatted=format_bytes(r.size),
            target=r.target,
            status=r.status,
            info_hash=r.info_hash,
            created_at=r.created_at,
        )
        for r in rows
    ]

