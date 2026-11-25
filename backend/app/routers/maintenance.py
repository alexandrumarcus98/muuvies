"""Maintenance Watchdog, Prowlarr Health Triage, and Blacklist/Re-Search Router."""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from backend.app.ai_assistant import ai_assistant
from backend.app.routers._access import user_router
from backend.app.security import check_vault_unlocked, get_current_session, require_admin
from backend.app.services.arr_client import radarr_client, sonarr_client, whisparr_client
from backend.app.services.watchdog import clear_watchdog_cache, collect_maintenance_status

router = user_router(prefix="/api/maintenance", tags=["Maintenance & Watchdog"])

_SERVICE_CLIENTS = {
    "radarr": radarr_client,
    "sonarr": sonarr_client,
    "whisparr": whisparr_client,
}


class BlacklistResearchRequest(BaseModel):
    service: str = Field(..., min_length=3, max_length=20)
    queue_id: int = Field(..., ge=1)
    remove_from_client: bool = True
    blocklist: bool = True
    search_again: bool = True
    media_id: Optional[int] = None
    episode_ids: Optional[List[int]] = None


class AiTriageRequest(BaseModel):
    refresh: bool = False
    report: Optional[Dict[str, Any]] = None


def _redact_maintenance_for_non_admin(report: Dict[str, Any]) -> Dict[str, Any]:
    """Strip internal filesystem paths and tracker announce URLs for non-admin callers."""
    redacted = dict(report)
    redacted["stalled_torrents"] = [
        {**t, "tracker": ""}
        for t in (report.get("stalled_torrents") or [])
    ]
    redacted["heavy_files"] = [
        {**hf, "file_path": ""}
        for hf in (report.get("heavy_files") or [])
    ]
    redacted["duplicates"] = [
        {
            **grp,
            "items": [{**it, "file_path": ""} for it in (grp.get("items") or [])],
        }
        for grp in (report.get("duplicates") or [])
    ]
    return redacted


@router.get("/status")
async def get_maintenance_status(
    refresh: bool = Query(False),
    vault: Optional[bool] = Query(None),  # Hint only; session state is authoritative
    session: dict = Depends(get_current_session),
) -> Dict[str, Any]:
    """Return cached (120s TTL) or fresh watchdog, indexer, and duplicate audit snapshot."""
    vault_unlocked = check_vault_unlocked(session)
    report = await collect_maintenance_status(
        vault_unlocked=vault_unlocked,
        force_refresh=refresh,
    )
    if session.get("role") != "admin":
        return _redact_maintenance_for_non_admin(report)
    return report


@router.post("/blacklist-research")
async def blacklist_and_research_queue_item(
    payload: BlacklistResearchRequest,
    session: dict = Depends(require_admin),
) -> Dict[str, Any]:
    """Remove & blocklist a broken *arr queue entry and trigger an immediate replacement search."""
    vault_unlocked = check_vault_unlocked(session)
    svc = payload.service.strip().lower()
    if svc not in _SERVICE_CLIENTS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unsupported service; expected radarr, sonarr, or whisparr.")
    if svc == "whisparr" and not vault_unlocked:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Not Found",
        )

    client = _SERVICE_CLIENTS[svc]
    media_id = payload.media_id
    episode_ids = list(payload.episode_ids or [])

    if payload.search_again and media_id is None and not episode_ids:
        queue_items = await client.get_queue()
        matched = next((q for q in queue_items if int(q.get("id") or 0) == int(payload.queue_id)), None)
        if matched:
            media_id = matched.get("movieId") if matched.get("movieId") is not None else matched.get("seriesId")
            if matched.get("episodeId") is not None:
                episode_ids = [int(matched["episodeId"])]

    deleted_ok, del_err = await client.delete_queue_item(
        queue_id=payload.queue_id,
        remove_from_client=payload.remove_from_client,
        blocklist=payload.blocklist,
    )
    if not deleted_ok:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=del_err or "Failed deleting *arr queue entry.")

    search_triggered = False
    search_msg: Optional[str] = None
    if payload.search_again:
        search_triggered, search_msg = await client.trigger_search(
            media_id=int(media_id) if media_id is not None else None,
            episode_ids=episode_ids or None,
        )

    clear_watchdog_cache()
    return {
        "ok": True,
        "service": svc,
        "queue_id": payload.queue_id,
        "blocklisted": payload.blocklist,
        "search_triggered": search_triggered,
        "message": search_msg or f"Blocklisted queue #{payload.queue_id} on {svc} and triggered replacement search.",
    }


@router.post("/ai-triage")
async def run_ai_maintenance_triage(
    payload: Optional[AiTriageRequest] = None,
    session: dict = Depends(get_current_session),
) -> Dict[str, Any]:
    """On-demand AI diagnostic explanation & action plan over current maintenance alerts."""
    vault_unlocked = check_vault_unlocked(session)
    req = payload or AiTriageRequest()
    report = req.report if req.report is not None else await collect_maintenance_status(
        vault_unlocked=vault_unlocked,
        force_refresh=req.refresh,
    )
    return await ai_assistant.diagnose_maintenance_issues(report)
