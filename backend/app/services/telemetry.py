import asyncio
import time
from typing import List, Dict, Any

from backend.app.config import settings
from backend.app.schemas import ServiceStatus, TelemetryResponse
from backend.app.services.cache import telemetry_cache
from backend.app.services.qbittorrent import (
    qb_client,
    format_bytes,
    format_speed,
)
from backend.app.services.prowlarr import prowlarr_client
from backend.app.services.arr_client import (
    radarr_client,
    sonarr_client,
    whisparr_client,
)
from backend.app.services.cross_seed import cross_seed_client


async def probe_service(name: str, check_coro, url: str, launcher_url: str, protected: bool = False) -> ServiceStatus:
    start = time.time()
    try:
        ok, ver, code = await check_coro()
        latency = (time.time() - start) * 1000.0
        return ServiceStatus(
            name=name,
            connected=ok,
            status_code=code,
            latency_ms=round(latency, 1),
            version=ver or ("Connected" if ok else "Offline"),
            url=url,
            launcher_url=launcher_url,
            protected_by_vault=protected,
        )
    except Exception:
        return ServiceStatus(
            name=name,
            connected=False,
            url=url,
            launcher_url=launcher_url,
            protected_by_vault=protected,
        )


async def _fetch_live_telemetry(vault_unlocked: bool = False, include_infra: bool = False) -> TelemetryResponse:
    """include_infra adds the seedbox host and Jellyfin web URL; only pass it for admins."""
    # Service probes
    def _vis_url(raw_url: str) -> str:
        return raw_url if include_infra else ""

    tasks = [
        probe_service("qBittorrent", qb_client.check_health, _vis_url(settings.QBITTORRENT_URL), _vis_url(settings.QBITTORRENT_URL)),
        probe_service("Prowlarr", prowlarr_client.check_health, _vis_url(settings.PROWLARR_URL), _vis_url(settings.PROWLARR_URL)),
        probe_service("Radarr", radarr_client.check_health, _vis_url(settings.RADARR_URL), _vis_url(settings.RADARR_URL)),
        probe_service("Sonarr", sonarr_client.check_health, _vis_url(settings.SONARR_URL), _vis_url(settings.SONARR_URL)),
        probe_service("cross-seed", cross_seed_client.check_health, _vis_url(settings.CROSS_SEED_URL), _vis_url(settings.CROSS_SEED_URL)),
    ]

    # Whisparr probe only if vault unlocked
    if vault_unlocked:
        tasks.append(
            probe_service("Whisparr", whisparr_client.check_health, _vis_url(settings.WHISPARR_URL), _vis_url(settings.WHISPARR_URL), protected=True)
        )

    # Metrics tasks
    metrics_tasks = [
        qb_client.get_transfer_info(),
        qb_client.get_disk_free_space(),
        qb_client.get_queue(filter_status="active"),
    ]

    service_results, metrics_results = await asyncio.gather(
        asyncio.gather(*tasks),
        asyncio.gather(*metrics_tasks),
    )

    transfer_info = metrics_results[0] or {}
    free_disk_bytes = metrics_results[1] or 0
    active_queue = metrics_results[2] or []

    # Filter out adult torrents from count if vault locked
    if not vault_unlocked:
        # Same rule as /api/queue: "adult" or anything containing "xxx".
        active_queue = [
            t for t in active_queue
            if not ((t.get("category") or "").lower() == "adult" or "xxx" in (t.get("category") or "").lower())
        ]

    dl_speed = transfer_info.get("dl_info_speed", 0)
    up_speed = transfer_info.get("up_info_speed", 0)

    # Fallback default disk space display if seedbox qBittorrent doesn't report it yet
    if free_disk_bytes <= 0:
        # 1.5 TB estimated default for typical slot if offline
        free_disk_bytes = 0

    return TelemetryResponse(
        services=service_results,
        download_speed_bps=dl_speed,
        upload_speed_bps=up_speed,
        download_speed_formatted=format_speed(dl_speed),
        upload_speed_formatted=format_speed(up_speed),
        free_disk_space_bytes=free_disk_bytes,
        free_disk_space_formatted=format_bytes(free_disk_bytes) if free_disk_bytes > 0 else "Seedbox",
        seedbox_host=settings.SEEDBOX_HOST if include_infra else "",
        jellyfin_web_url=(
            f"{settings.JELLYFIN_URL.rstrip('/')}/web/#/home" if include_infra and settings.JELLYFIN_URL else ""
        ),
        vault_unlocked=vault_unlocked,
        active_torrents_count=len(active_queue),
    )


async def get_live_telemetry(
    vault_unlocked: bool = False,
    include_infra: bool = False,
    force_refresh: bool = False,
) -> TelemetryResponse:
    """Return cached (4s fresh / 12s SWR) or live seedbox telemetry."""
    cache_key = f"telemetry:{int(bool(vault_unlocked))}:{int(bool(include_infra))}"
    if not force_refresh:
        cached, status = telemetry_cache.get_swr(
            cache_key,
            refresh_factory=lambda: _fetch_live_telemetry(
                vault_unlocked=vault_unlocked,
                include_infra=include_infra,
            ),
        )
        if cached is not None and status in ("hit_fresh", "hit_stale"):
            return cached

    fresh = await _fetch_live_telemetry(
        vault_unlocked=vault_unlocked,
        include_infra=include_infra,
    )
    telemetry_cache.set(cache_key, fresh)
    return fresh

