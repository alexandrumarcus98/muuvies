"""Deterministic Maintenance Watchdog, Prowlarr Health Triage, and Hardlink-Aware Library Audit."""

from __future__ import annotations

import asyncio
import html
import logging
import os
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from backend.app.config import settings
from backend.app.services.arr_client import radarr_client, sonarr_client, whisparr_client
from backend.app.services.cache import SearchMemoryCache
from backend.app.services.email_service import send_email
from backend.app.services.library_mirror import get_library_audit_rows
from backend.app.services.prowlarr import prowlarr_client
from backend.app.services.qbittorrent import format_bytes, qb_client
from backend.app.services.search_scoring import (
    _normalize_title,
    get_private_trackers,
    matches_adult_marker,
)

logger = logging.getLogger("muuvies.watchdog")

STALLED_THRESHOLD_SECONDS = 6 * 3600
HEAVY_FILE_THRESHOLD_BYTES = 45 * (1024 ** 3)
LOW_DISK_THRESHOLD_BYTES = 50 * (1024 ** 3)
ALERT_COOLDOWN_SECONDS = 6 * 3600

_STALLED_STATES = frozenset({"stalleddl", "metadl", "error", "missingfiles"})
_SEEDING_STATES = frozenset({"uploading", "stalledup", "queuedup", "forcedup", "checkingup"})

_WATCHDOG_CACHE = SearchMemoryCache(fresh_ttl_seconds=120, stale_ttl_seconds=120, max_entries=16)
_SENT_ALERT_TIMESTAMPS: Dict[str, float] = {}


def clear_watchdog_cache() -> None:
    """Invalidate cached maintenance snapshots after mutations (e.g. blacklist-research)."""
    _WATCHDOG_CACHE.clear()


def clear_watchdog_alert_state() -> None:
    """Reset alert deduplication cooldown state (used cleanly in tests)."""
    _SENT_ALERT_TIMESTAMPS.clear()


def _is_adult_torrent(torrent: Dict[str, Any]) -> bool:
    cat = str(torrent.get("category") or "").lower()
    tracker = str(torrent.get("tracker") or "")
    return "adult" in cat or "xxx" in cat or matches_adult_marker(tracker)


def _classify_indexer_issue(message: str) -> str:
    msg = (message or "").lower()
    if any(tok in msg for tok in ("401", "403", "unauthorized", "forbidden", "cookie", "login", "captcha", "invalid api")):
        return "auth_expired"
    if any(tok in msg for tok in ("429", "rate limit", "too many requests", "throttl")):
        return "rate_limited"
    if any(tok in msg for tok in ("timeout", "timed out", "unreachable", "connect", "refused", "reset by peer", "name or service", "502", "503", "504")):
        return "offline"
    return "warning"


def _inspect_stalled_torrents(
    torrents: List[Dict[str, Any]],
    vault_unlocked: bool,
    now: Optional[float] = None,
) -> List[Dict[str, Any]]:
    ts_now = now if now is not None else time.time()
    stalled: List[Dict[str, Any]] = []
    for t in torrents:
        if not vault_unlocked and _is_adult_torrent(t):
            continue
        state = str(t.get("state") or "")
        progress = float(t.get("progress") or 0.0)
        num_seeds = int(t.get("num_seeds") or 0)
        if state.lower() not in _STALLED_STATES or progress >= 1.0 or num_seeds != 0:
            continue

        last_act = int(t.get("last_activity") or 0)
        added_on = int(t.get("added_on") or 0)
        ref_ts = last_act if last_act > 0 else added_on
        if ref_ts <= 0 or (ts_now - ref_ts) < STALLED_THRESHOLD_SECONDS:
            continue

        size_bytes = int(t.get("size") or 0)
        stalled.append({
            "hash": str(t.get("hash") or ""),
            "name": str(t.get("name") or "Unknown"),
            "state": state,
            "progress_pct": round(progress * 100.0, 1),
            "num_seeds": num_seeds,
            "size_bytes": size_bytes,
            "size_formatted": format_bytes(size_bytes),
            "stalled_hours": round((ts_now - ref_ts) / 3600.0, 1),
            "category": str(t.get("category") or ""),
            "tracker": str(t.get("tracker") or ""),
        })
    return stalled


def _extract_arr_status_messages(record: Dict[str, Any]) -> List[str]:
    messages: List[str] = []
    for entry in record.get("statusMessages") or []:
        if not isinstance(entry, dict):
            continue
        nested = [str(m).strip() for m in (entry.get("messages") or []) if m]
        if nested:
            messages.extend(nested)
        elif entry.get("title"):
            messages.append(str(entry["title"]).strip())
    if not messages and record.get("errorMessage"):
        messages.append(str(record["errorMessage"]).strip())
    return list(dict.fromkeys(m for m in messages if m))


def _inspect_arr_queue(
    service: str,
    records: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    warnings: List[Dict[str, Any]] = []
    svc = service.strip().lower()
    for rec in records:
        if not isinstance(rec, dict):
            continue
        tracked_status = str(rec.get("trackedDownloadStatus") or "").strip().lower()
        tracked_state = str(rec.get("trackedDownloadState") or "").strip().lower()
        msgs = _extract_arr_status_messages(rec)
        if tracked_status not in {"warning", "error"} and tracked_state != "importblocked":
            continue
        size_bytes = int(rec.get("size") or 0)
        media_id = rec.get("movieId") if rec.get("movieId") is not None else rec.get("seriesId")
        warnings.append({
            "service": svc,
            "queue_id": int(rec.get("id") or 0),
            "title": str(rec.get("title") or "Unknown Queue Item"),
            "tracked_status": tracked_status or "warning",
            "tracked_state": tracked_state,
            "messages": msgs or [f"{svc.title()} reported download {tracked_status or 'warning'}"],
            "media_id": int(media_id) if media_id is not None else None,
            "episode_id": int(rec["episodeId"]) if rec.get("episodeId") is not None else None,
            "download_id": str(rec.get("downloadId") or ""),
            "size_bytes": size_bytes,
            "size_formatted": format_bytes(size_bytes),
        })
    return warnings


def _is_adult_indexer_def(idx_def: Dict[str, Any]) -> bool:
    name = str(idx_def.get("name") or "")
    if matches_adult_marker(name):
        return True
    caps = ((idx_def.get("capabilities") or {}).get("categories") or [])
    cat_ids = {int(c.get("id") or 0) for c in caps if isinstance(c, dict)}
    return bool(cat_ids) and all(6000 <= cid < 7000 for cid in cat_ids)


def _inspect_indexer_health(
    prowlarr_payload: Dict[str, Any],
    vault_unlocked: bool,
) -> List[Dict[str, Any]]:
    issues: List[Dict[str, Any]] = []
    if not prowlarr_payload.get("reachable", True) and prowlarr_payload.get("error"):
        issues.append({
            "indexer": "Prowlarr",
            "kind": _classify_indexer_issue(str(prowlarr_payload["error"])),
            "message": str(prowlarr_payload["error"]),
            "disabled_till": None,
        })
        return issues

    idx_map = {
        int(it["id"]): it
        for it in (prowlarr_payload.get("indexers") or [])
        if isinstance(it, dict) and it.get("id") is not None
    }

    seen_indexers: set[str] = set()
    for st in prowlarr_payload.get("statuses") or []:
        if not isinstance(st, dict):
            continue
        idx_id = st.get("indexerId")
        idx_def = idx_map.get(int(idx_id)) if idx_id is not None else None
        if idx_def and not idx_def.get("enable", True):
            continue
        if idx_def and not vault_unlocked and _is_adult_indexer_def(idx_def):
            continue
        name = str((idx_def or {}).get("name") or f"Indexer #{idx_id}")
        msg = str(st.get("mostRecentFailure") or st.get("disabledTill") or "Indexer temporarily failing")
        seen_indexers.add(name.lower())
        issues.append({
            "indexer": name,
            "kind": _classify_indexer_issue(msg),
            "message": msg[:240],
            "disabled_till": st.get("disabledTill"),
        })

    for h in prowlarr_payload.get("health") or []:
        if not isinstance(h, dict):
            continue
        level = str(h.get("type") or "").lower()
        if level not in {"error", "warning"}:
            continue
        msg = str(h.get("message") or "").strip()
        if not msg:
            continue
        if not vault_unlocked and matches_adult_marker(msg, include_adult_word=False):
            continue
        src = str(h.get("source") or "Prowlarr").strip()
        if src.lower() in seen_indexers:
            continue
        issues.append({
            "indexer": src,
            "kind": _classify_indexer_issue(msg),
            "message": msg[:240],
            "disabled_till": None,
        })
    return issues


def _build_seeding_index(torrents: List[Dict[str, Any]]) -> Tuple[set[str], set[str]]:
    """Index normalized torrent titles (when pathless) and exact content paths for seeding torrents."""
    seeding_titles: set[str] = set()
    seeding_paths: set[str] = set()
    private_trackers = get_private_trackers()
    for t in torrents:
        state = str(t.get("state") or "").lower()
        tracker = str(t.get("tracker") or "").lower()
        is_private = any(priv in tracker for priv in private_trackers)
        if state not in _SEEDING_STATES and not is_private:
            continue

        content_path = str(t.get("content_path") or "").strip().rstrip("/")
        save_path = str(t.get("save_path") or "").strip().rstrip("/")
        has_file_content_path = bool(content_path and content_path != save_path)

        if not has_file_content_path:
            norm = _normalize_title(str(t.get("name") or ""))
            if norm:
                seeding_titles.add(norm)

        for raw_p in (content_path, save_path):
            if raw_p:
                seeding_paths.add(raw_p.lower())
                if raw_p == content_path and has_file_content_path:
                    base = os.path.basename(raw_p).lower()
                    if base:
                        seeding_paths.add(base)
    return seeding_titles, seeding_paths


def _assess_file_seeding(
    item: Dict[str, Any],
    seeding_titles: set[str],
    seeding_paths: set[str],
) -> Tuple[bool, int, bool]:
    """Return (is_seeding, hardlink_count, safe_to_delete) for one mirrored library entry."""
    norm_title = _normalize_title(str(item.get("title") or ""))
    fp = str(item.get("file_path") or "").strip()
    fp_lower = fp.lower()
    base = os.path.basename(fp_lower) if fp_lower else ""

    path_match = bool(
        (fp_lower and fp_lower in seeding_paths)
        or (base and base in seeding_paths)
    )
    title_match = bool(norm_title and norm_title in seeding_titles)
    qbit_match = path_match or title_match

    hardlink_count = 2 if qbit_match else 1
    if fp and os.path.exists(fp):
        try:
            hardlink_count = max(hardlink_count, int(os.stat(fp).st_nlink))
        except OSError:
            pass

    is_seeding = qbit_match
    return is_seeding, hardlink_count, (not is_seeding)


def _audit_library_duplicates_and_heavy_files(
    library_rows: List[Dict[str, Any]],
    torrents: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], float, float]:
    seeding_titles, seeding_paths = _build_seeding_index(torrents)
    groups: Dict[Tuple[str, int], List[Dict[str, Any]]] = defaultdict(list)

    enriched_rows: List[Dict[str, Any]] = []
    for row in library_rows:
        is_seeding, hardlink_count, safe_to_delete = _assess_file_seeding(row, seeding_titles, seeding_paths)
        size_bytes = int(row.get("size_bytes") or 0)
        entry = {
            **row,
            "is_seeding": is_seeding,
            "hardlink_count": hardlink_count,
            "safe_to_delete": safe_to_delete,
            "size_gb": round(size_bytes / (1024 ** 3), 2) if size_bytes > 0 else 0.0,
            "size_formatted": format_bytes(size_bytes) if size_bytes > 0 else "0 B",
        }
        enriched_rows.append(entry)
        norm = _normalize_title(str(row.get("title") or ""))
        if norm:
            groups[(norm, int(row.get("year") or 0))].append(entry)

    duplicates: List[Dict[str, Any]] = []
    reclaimable_candidates: Dict[Tuple[str, int], Dict[str, Any]] = {}

    for (norm_title, year), items in groups.items():
        if len(items) < 2:
            continue
        ordered = sorted(items, key=lambda x: (bool(x.get("has_file")), x.get("size_bytes", 0)), reverse=True)
        for idx, entry in enumerate(ordered):
            entry_copy = {**entry, "keep_recommended": idx == 0}
            ordered[idx] = entry_copy
            if idx > 0 and entry_copy["size_bytes"] > 0:
                reclaimable_candidates[(entry_copy["service"], entry_copy["external_id"])] = entry_copy
        duplicates.append({
            "normalized_title": norm_title,
            "year": year or None,
            "count": len(ordered),
            "items": ordered,
        })

    heavy_files: List[Dict[str, Any]] = []
    for entry in enriched_rows:
        if entry["size_bytes"] > HEAVY_FILE_THRESHOLD_BYTES:
            heavy_files.append(entry)
            reclaimable_candidates.setdefault((entry["service"], entry["external_id"]), entry)

    heavy_files.sort(key=lambda x: x["size_bytes"], reverse=True)

    reclaimable_bytes = sum(
        c["size_bytes"] for c in reclaimable_candidates.values() if c["safe_to_delete"]
    )
    seeding_locked_bytes = sum(
        c["size_bytes"] for c in reclaimable_candidates.values() if not c["safe_to_delete"]
    )
    return (
        duplicates,
        heavy_files,
        round(reclaimable_bytes / (1024 ** 3), 2),
        round(seeding_locked_bytes / (1024 ** 3), 2),
    )


async def collect_maintenance_status(
    vault_unlocked: bool = False,
    force_refresh: bool = False,
) -> Dict[str, Any]:
    """Run all deterministic health, queue, Prowlarr, and hardlink-aware duplicate checks (120s TTL)."""
    cache_key = bool(vault_unlocked)
    if not force_refresh:
        cached = _WATCHDOG_CACHE.get(cache_key)
        if cached is not None:
            return cached

    arr_tasks = [radarr_client.get_queue(), sonarr_client.get_queue()]
    arr_names = ["radarr", "sonarr"]
    if vault_unlocked:
        arr_tasks.append(whisparr_client.get_queue())
        arr_names.append("whisparr")

    results = await asyncio.gather(
        qb_client.get_queue(filter_status="all"),
        qb_client.get_disk_free_space(),
        prowlarr_client.get_health_and_indexer_status(),
        get_library_audit_rows(vault_unlocked=vault_unlocked),
        *arr_tasks,
        return_exceptions=True,
    )

    torrents = results[0] if isinstance(results[0], list) else []
    disk_free_bytes = int(results[1]) if isinstance(results[1], (int, float)) else 0
    prowlarr_payload = results[2] if isinstance(results[2], dict) else {"reachable": False, "error": "Prowlarr probe error"}
    library_rows = results[3] if isinstance(results[3], list) else []

    arr_warnings: List[Dict[str, Any]] = []
    for svc_name, q_res in zip(arr_names, results[4:]):
        if isinstance(q_res, list):
            arr_warnings.extend(_inspect_arr_queue(svc_name, q_res))

    stalled_torrents = _inspect_stalled_torrents(torrents, vault_unlocked=vault_unlocked)
    indexer_issues = _inspect_indexer_health(prowlarr_payload, vault_unlocked=vault_unlocked)
    duplicates, heavy_files, reclaimable_gb, seeding_locked_gb = _audit_library_duplicates_and_heavy_files(
        library_rows, torrents
    )

    low_disk = 0 < disk_free_bytes < LOW_DISK_THRESHOLD_BYTES
    issue_count = (
        len(stalled_torrents)
        + len(arr_warnings)
        + len(indexer_issues)
        + len(duplicates)
        + (1 if low_disk else 0)
    )

    payload = {
        "checked_at": round(time.time(), 1),
        "vault_unlocked": bool(vault_unlocked),
        "disk_free_bytes": disk_free_bytes,
        "disk_free_formatted": format_bytes(disk_free_bytes) if disk_free_bytes > 0 else "Unknown",
        "low_disk_space": low_disk,
        "stalled_torrents": stalled_torrents,
        "arr_warnings": arr_warnings,
        "indexer_issues": indexer_issues,
        "duplicates": duplicates,
        "heavy_files": heavy_files,
        "reclaimable_gb": reclaimable_gb,
        "seeding_locked_gb": seeding_locked_gb,
        "issue_count": issue_count,
    }
    _WATCHDOG_CACHE.set(cache_key, payload)
    return payload


def _collect_pending_alerts(status_payload: Dict[str, Any], ts_now: float) -> List[Tuple[str, str]]:
    """Collect (alert_key, description) pairs whose 6-hour cooldown has expired."""
    candidates: List[Tuple[str, str]] = []
    for idx_issue in status_payload.get("indexer_issues") or []:
        kind = str(idx_issue.get("kind") or "").lower()
        if kind not in {"auth_expired", "offline", "down"}:
            continue
        name = str(idx_issue.get("indexer") or "Unknown Indexer").strip()
        msg = str(idx_issue.get("message") or "").strip()
        key = f"indexer:{kind}:{name.lower()}"
        candidates.append((key, f"Prowlarr indexer '{name}' [{kind}]: {msg or 'Unhealthy'}"))

    if status_payload.get("low_disk_space"):
        free_fmt = str(status_payload.get("disk_free_formatted") or "Unknown")
        candidates.append(
            ("system:low_disk_space", f"Seedbox free disk space is low ({free_fmt} remaining).")
        )

    pending: List[Tuple[str, str]] = []
    for key, desc in candidates:
        last_sent = _SENT_ALERT_TIMESTAMPS.get(key)
        if last_sent is None or (ts_now - last_sent) >= ALERT_COOLDOWN_SECONDS:
            pending.append((key, desc))
    return pending


async def check_and_send_watchdog_alerts(now: Optional[float] = None) -> int:
    """Run non-vault maintenance checks and send deduplicated email alerts (6h cooldown)."""
    recipients = sorted(settings.get_allowed_emails_set())
    if not recipients:
        return 0

    ts_now = now if now is not None else time.time()
    status_payload = await collect_maintenance_status(vault_unlocked=False, force_refresh=True)
    pending = _collect_pending_alerts(status_payload, ts_now)
    if not pending:
        return 0

    items_html = "".join(f"<li style='margin:6px 0;'>{html.escape(line)}</li>" for _, line in pending)
    html_body = (
        "<div style=\"font-family:Inter,system-ui,sans-serif;max-width:540px;margin:0 auto;padding:24px;\">"
        "<h2 style=\"margin:0 0 12px;font-size:18px;color:#09090b;\">Muuvies Watchdog Alert</h2>"
        "<p style=\"margin:0 0 12px;font-size:14px;color:#52525b;\">"
        "The following infrastructure issues were detected on your media node:</p>"
        f"<ul style=\"margin:0 0 16px;padding-left:18px;font-size:13px;color:#27272a;\">{items_html}</ul>"
        "<p style=\"margin:0;font-size:12px;color:#71717a;\">"
        "Alerts are deduplicated with a 6-hour cooldown per condition.</p></div>"
    )
    text_body = "Muuvies Watchdog Alert\n\n" + "\n".join(f"- {line}" for _, line in pending) + "\n"
    subject = f"[Muuvies] Watchdog Alert ({len(pending)} issue{'s' if len(pending) != 1 else ''})"

    sent_count = 0
    for recipient in recipients:
        if await send_email(recipient, subject, html_body, text_body):
            sent_count += 1

    if sent_count > 0:
        for key, _ in pending:
            _SENT_ALERT_TIMESTAMPS[key] = ts_now
    return sent_count


async def run_watchdog_alert_loop(interval_seconds: int = 1800) -> None:
    """Periodic background watchdog check dispatching deduplicated email alerts."""
    await asyncio.sleep(60)
    while True:
        try:
            await check_and_send_watchdog_alerts()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Background watchdog alert check failed: %s", exc)
        await asyncio.sleep(max(60, int(interval_seconds)))
