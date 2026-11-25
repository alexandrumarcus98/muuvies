import re
from typing import Any, Dict, List

from fastapi import Depends, HTTPException, status

from backend.app.routers._access import user_router
from backend.app.schemas import TorrentQueueItem, QueueActionRequest
from backend.app.security import get_current_session, check_vault_unlocked, require_admin
from backend.app.services.cache import queue_cache, telemetry_cache
from backend.app.services.qbittorrent import (
    qb_client,
    format_bytes,
    format_speed,
    format_eta,
)

router = user_router(prefix="/api/queue", tags=["Download Queue"])

_TORRENT_HASH_RE = re.compile(r"\A(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


def _is_adult_torrent(raw_torrent: Dict[str, Any]) -> bool:
    category = raw_torrent.get("category", "") or "default"
    return category.lower() == "adult" or "xxx" in category.lower()


async def _get_visible_torrents(session: dict) -> List[Dict[str, Any]]:
    """Return queue entries visible to the caller, hiding adult items while locked."""
    vault_unlocked = check_vault_unlocked(session)
    raw_torrents = await qb_client.get_queue(filter_status="all")
    if vault_unlocked:
        return raw_torrents
    return [t for t in raw_torrents if not _is_adult_torrent(t)]


@router.get("", response_model=List[TorrentQueueItem])
async def get_download_queue(
    session: dict = Depends(get_current_session),
):
    visible_torrents = await _get_visible_torrents(session)

    items = []
    for t in visible_torrents:
        category = t.get("category", "") or "default"

        size = t.get("size", 0)
        progress = round(t.get("progress", 0.0) * 100.0, 1)
        dl_speed = t.get("dlspeed", 0)
        up_speed = t.get("upspeed", 0)
        eta = t.get("eta", 8640000)

        items.append(
            TorrentQueueItem(
                hash=t.get("hash", ""),
                name=t.get("name", "Unknown"),
                size=size,
                size_formatted=format_bytes(size),
                progress=progress,
                download_speed=dl_speed,
                upload_speed=up_speed,
                download_speed_formatted=format_speed(dl_speed),
                upload_speed_formatted=format_speed(up_speed),
                eta_seconds=eta,
                eta_formatted=format_eta(eta),
                state=t.get("state", "paused"),
                category=category,
                seeds=t.get("num_seeds", 0),
                peers=t.get("num_leechs", 0),
                streaming=bool(t.get("seq_dl")) and bool(t.get("f_l_piece_prio")),
            )
        )

    return items


@router.post("/action")
async def queue_action(
    payload: QueueActionRequest,
    session: dict = Depends(require_admin),
):
    if payload.action == "delete" and payload.delete_files and session.get("role") != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Deleting torrent data files from disk requires administrator privileges.",
        )

    if not payload.hashes:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No torrent hashes provided",
        )

    hashes = list(dict.fromkeys(payload.hashes))
    if any(h.lower() == "all" or not _TORRENT_HASH_RE.fullmatch(h) for h in hashes):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid torrent hash; expected 40- or 64-character lowercase hex.",
        )

    visible_torrents = await _get_visible_torrents(session)
    visible_hashes = {
        str(t.get("hash", "")).lower()
        for t in visible_torrents
        if t.get("hash")
    }
    if any(h not in visible_hashes for h in hashes):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="One or more torrents not found in queue",
        )

    success = await qb_client.control_torrents(
        action=payload.action,
        hashes=hashes,
        delete_files=payload.delete_files,
    )

    if not success:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to execute '{payload.action}' on selected torrents",
        )

    queue_cache.clear()
    telemetry_cache.clear()
    return {"success": True, "action": payload.action, "count": len(hashes)}

