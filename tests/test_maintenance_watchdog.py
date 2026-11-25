"""Unit and endpoint tests for Phase 2 Maintenance Watchdog, Prowlarr Health, & Duplicate Audit."""

from __future__ import annotations

import os
import time
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from backend.app.config import settings
from backend.app.main import app
from backend.app.services import watchdog
from backend.app.services.watchdog import (
    HEAVY_FILE_THRESHOLD_BYTES,
    STALLED_THRESHOLD_SECONDS,
    _audit_library_duplicates_and_heavy_files,
    _inspect_arr_queue,
    _inspect_indexer_health,
    _inspect_stalled_torrents,
    clear_watchdog_cache,
    collect_maintenance_status,
)


@pytest.fixture(autouse=True)
def reset_watchdog_cache(monkeypatch):
    clear_watchdog_cache()
    monkeypatch.setattr(settings, "PRIVATE_TRACKERS", "private-tracker.example,privatetracker")
    monkeypatch.setattr(settings, "ADULT_INDEXER_KEYWORDS", "vaultforum,vault-forum,vault-gallery")
    yield
    clear_watchdog_cache()


def test_stalled_torrent_detection_thresholds_and_vault_isolation():
    now = 1_700_000_000.0
    torrents = [
        {
            "hash": "stalled1",
            "name": "Dune.Part.Two.2024.2160p.mkv",
            "state": "stalledDL",
            "progress": 0.42,
            "num_seeds": 0,
            "size": 12 * 1024**3,
            "last_activity": int(now - STALLED_THRESHOLD_SECONDS - 600),
            "category": "radarr",
            "tracker": "https://private-tracker.example/announce",
        },
        {
            "hash": "stalled_fresh",
            "name": "Fresh.Stall.2025.1080p.mkv",
            "state": "stalledDL",
            "progress": 0.15,
            "num_seeds": 0,
            "size": 4 * 1024**3,
            "last_activity": int(now - 3600),  # Only 1h < 6h threshold
            "category": "radarr",
            "tracker": "",
        },
        {
            "hash": "completed_dl",
            "name": "Completed.Seed.2024.mkv",
            "state": "stalledDL",
            "progress": 1.0,  # Completed files must not be flagged as stalled downloads
            "num_seeds": 0,
            "size": 8 * 1024**3,
            "last_activity": int(now - STALLED_THRESHOLD_SECONDS - 7200),
            "category": "radarr",
            "tracker": "",
        },
        {
            "hash": "seeded_meta",
            "name": "Has.Seeds.2024.mkv",
            "state": "metaDL",
            "progress": 0.0,
            "num_seeds": 3,  # num_seeds > 0 must not trigger stalled-0-seed alert
            "size": 2 * 1024**3,
            "last_activity": int(now - STALLED_THRESHOLD_SECONDS - 7200),
            "category": "tv-sonarr",
            "tracker": "",
        },
        {
            "hash": "adult_stalled",
            "name": "Secret.Vault.Release.2025.XXX.mp4",
            "state": "error",
            "progress": 0.1,
            "num_seeds": 0,
            "size": 3 * 1024**3,
            "last_activity": int(now - STALLED_THRESHOLD_SECONDS - 1000),
            "category": "whisparr-adult",
            "tracker": "https://vault-forum.example/announce",
        },
    ]

    locked = _inspect_stalled_torrents(torrents, vault_unlocked=False, now=now)
    assert [t["hash"] for t in locked] == ["stalled1"]
    assert locked[0]["progress_pct"] == 42.0
    assert locked[0]["stalled_hours"] >= 6.1

    unlocked = _inspect_stalled_torrents(torrents, vault_unlocked=True, now=now)
    assert {t["hash"] for t in unlocked} == {"stalled1", "adult_stalled"}


def test_arr_queue_warning_and_error_extraction():
    records = [
        {
            "id": 101,
            "movieId": 55,
            "title": "Severance S02E01 Broken",
            "trackedDownloadStatus": "warning",
            "trackedDownloadState": "importPending",
            "statusMessages": [
                {"title": "Import", "messages": ["Not a custom format upgrade", "Sample file missing"]},
            ],
            "size": 5 * 1024**3,
            "downloadId": "HASH101",
        },
        {
            "id": 102,
            "seriesId": 88,
            "episodeId": 401,
            "title": "Healthy.Release.1080p",
            "trackedDownloadStatus": "ok",
            "trackedDownloadState": "downloading",
            "statusMessages": [],
            "size": 2 * 1024**3,
        },
        {
            "id": 103,
            "seriesId": 88,
            "episodeId": 402,
            "title": "Errored.Pack.1080p",
            "trackedDownloadStatus": "error",
            "trackedDownloadState": "importBlocked",
            "errorMessage": "CRC mismatch in archive",
            "size": 3 * 1024**3,
        },
    ]

    warnings = _inspect_arr_queue("sonarr", records)
    assert len(warnings) == 2
    assert warnings[0]["queue_id"] == 101
    assert warnings[0]["messages"] == ["Not a custom format upgrade", "Sample file missing"]
    assert warnings[1]["queue_id"] == 103
    assert warnings[1]["messages"] == ["CRC mismatch in archive"]
    assert warnings[1]["episode_id"] == 402


def test_prowlarr_indexer_health_classification_and_vault_isolation():
    payload = {
        "reachable": True,
        "indexers": [
            {"id": 1, "name": "PrivateTracker", "enable": True, "capabilities": {"categories": [{"id": 2000}]}},
            {"id": 2, "name": "1337x", "enable": True, "capabilities": {"categories": [{"id": 2000}]}},
            {"id": 3, "name": "PublicArchive", "enable": True, "capabilities": {"categories": [{"id": 2000}]}},
            {"id": 4, "name": "VaultForum", "enable": True, "capabilities": {"categories": [{"id": 6000}]}},
        ],
        "statuses": [
            {"indexerId": 1, "mostRecentFailure": "HTTP 403 Forbidden: session cookie expired"},
            {"indexerId": 2, "mostRecentFailure": "HTTP 429 Too Many Requests: rate limit reached"},
            {"indexerId": 3, "mostRecentFailure": "Connection timed out after 10000ms"},
            {"indexerId": 4, "mostRecentFailure": "HTTP 401 Unauthorized login required"},
        ],
        "health": [
            {"type": "warning", "source": "ProwlarrProxy", "message": "VaultForum proxy returned 502"},
        ],
    }

    locked = _inspect_indexer_health(payload, vault_unlocked=False)
    by_name_locked = {item["indexer"]: item["kind"] for item in locked}
    assert by_name_locked == {
        "PrivateTracker": "auth_expired",
        "1337x": "rate_limited",
        "PublicArchive": "offline",
    }
    assert "VaultForum" not in by_name_locked

    unlocked = _inspect_indexer_health(payload, vault_unlocked=True)
    by_name_unlocked = {item["indexer"]: item["kind"] for item in unlocked}
    assert by_name_unlocked["VaultForum"] == "auth_expired"
    assert "ProwlarrProxy" in by_name_unlocked


def test_library_duplicate_and_heavy_file_audit_with_seeding_protection(tmp_path):
    orphan_file = tmp_path / "Oppenheimer.2023.1080p.mkv"
    orphan_file.write_bytes(b"x")
    seeding_file = tmp_path / "Oppenheimer.2023.2160p.REMUX.mkv"
    seeding_file.write_bytes(b"y")
    heavy_orphan = tmp_path / "Lord.Of.The.Rings.2001.2160p.REMUX.mkv"
    heavy_orphan.write_bytes(b"z")

    library_rows = [
        {
            "service": "radarr",
            "external_id": 1,
            "title": "Oppenheimer",
            "year": 2023,
            "has_file": True,
            "file_path": str(seeding_file),
            "size_bytes": 22 * 1024**3,
        },
        {
            "service": "radarr",
            "external_id": 2,
            "title": "Oppenheimer",
            "year": 2023,
            "has_file": True,
            "file_path": str(orphan_file),
            "size_bytes": 10 * 1024**3,
        },
        {
            "service": "radarr",
            "external_id": 3,
            "title": "The Lord of the Rings",
            "year": 2001,
            "has_file": True,
            "file_path": str(heavy_orphan),
            "size_bytes": HEAVY_FILE_THRESHOLD_BYTES + 5 * 1024**3,
        },
        {
            "service": "radarr",
            "external_id": 4,
            "title": "Heavy Private Seeding Remux",
            "year": 2022,
            "has_file": True,
            "file_path": "/downloads/Heavy.Private.Seeding.Remux.2022.2160p.mkv",
            "size_bytes": 55 * 1024**3,
        },
    ]
    torrents = [
        {
            "name": "Oppenheimer",
            "state": "uploading",
            "tracker": "https://private-tracker.example/announce",
            "content_path": str(seeding_file),
            "save_path": str(tmp_path),
        },
        {
            "name": "Heavy.Private.Seeding.Remux.2022.2160p.mkv",
            "state": "stoppedUP",
            "tracker": "https://private-tracker.example/announce",
            "content_path": "/downloads/Heavy.Private.Seeding.Remux.2022.2160p.mkv",
            "save_path": "/downloads",
        },
    ]

    dups, heavy, reclaimable_gb, seeding_locked_gb = _audit_library_duplicates_and_heavy_files(
        library_rows, torrents
    )
    assert len(dups) == 1
    assert dups[0]["normalized_title"] == "oppenheimer"
    assert dups[0]["count"] == 2
    assert len(heavy) == 2

    heavy_by_id = {h["external_id"]: h for h in heavy}
    assert heavy_by_id[3]["safe_to_delete"] is True
    assert heavy_by_id[4]["is_seeding"] is True
    assert heavy_by_id[4]["safe_to_delete"] is False
    assert heavy_by_id[4]["hardlink_count"] >= 2

    assert reclaimable_gb >= 50.0  # 50 GB heavy orphan (+ non-seeding copy if applicable)
    assert seeding_locked_gb == 55.0


@pytest.mark.asyncio
async def test_collect_maintenance_status_caches_120s_and_isolates_vault():
    qb_queue_mock = AsyncMock(return_value=[])
    qb_disk_mock = AsyncMock(return_value=40 * 1024**3)  # < 50 GB triggers low_disk_space
    prowlarr_mock = AsyncMock(return_value={"reachable": True, "indexers": [], "statuses": [], "health": []})
    audit_mock = AsyncMock(return_value=[])
    radarr_mock = AsyncMock(return_value=[])
    sonarr_mock = AsyncMock(return_value=[])
    whisparr_mock = AsyncMock(return_value=[{
        "id": 999,
        "movieId": 12,
        "title": "Adult.Release.2025",
        "trackedDownloadStatus": "warning",
        "statusMessages": [{"messages": ["Adult warning"]}],
    }])

    with (
        patch.object(watchdog.qb_client, "get_queue", qb_queue_mock),
        patch.object(watchdog.qb_client, "get_disk_free_space", qb_disk_mock),
        patch.object(watchdog.prowlarr_client, "get_health_and_indexer_status", prowlarr_mock),
        patch.object(watchdog, "get_library_audit_rows", audit_mock),
        patch.object(watchdog.radarr_client, "get_queue", radarr_mock),
        patch.object(watchdog.sonarr_client, "get_queue", sonarr_mock),
        patch.object(watchdog.whisparr_client, "get_queue", whisparr_mock),
    ):
        locked_first = await collect_maintenance_status(vault_unlocked=False)
        locked_second = await collect_maintenance_status(vault_unlocked=False)
        assert locked_first is locked_second
        assert qb_queue_mock.await_count == 1  # Served from 120s TTL cache on second call
        assert whisparr_mock.await_count == 0
        assert locked_first["low_disk_space"] is True
        assert locked_first["arr_warnings"] == []

        unlocked = await collect_maintenance_status(vault_unlocked=True)
        assert whisparr_mock.await_count == 1
        assert len(unlocked["arr_warnings"]) == 1
        assert unlocked["arr_warnings"][0]["service"] == "whisparr"


@pytest.mark.asyncio
async def test_maintenance_endpoints_auth_vault_blacklist_and_ai_triage():
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        unauth = await client.get("/api/maintenance/status")
        assert unauth.status_code == 401

        login = await client.post(
            "/api/auth/login",
            json={"username": settings.ADMIN_USERNAME, "password": settings.ADMIN_PASSWORD},
        )
        assert login.status_code == 200

        with (
            patch.object(watchdog.qb_client, "get_queue", AsyncMock(return_value=[])),
            patch.object(watchdog.qb_client, "get_disk_free_space", AsyncMock(return_value=500 * 1024**3)),
            patch.object(
                watchdog.prowlarr_client,
                "get_health_and_indexer_status",
                AsyncMock(return_value={
                    "reachable": True,
                    "indexers": [{"id": 1, "name": "PrivateTracker", "enable": True}],
                    "statuses": [{"indexerId": 1, "mostRecentFailure": "HTTP 403 cookie expired"}],
                    "health": [],
                }),
            ),
            patch.object(watchdog, "get_library_audit_rows", AsyncMock(return_value=[])),
            patch.object(watchdog.radarr_client, "get_queue", AsyncMock(return_value=[{
                "id": 77,
                "movieId": 501,
                "title": "Broken.Movie.2024",
                "trackedDownloadStatus": "warning",
                "statusMessages": [{"messages": ["Stalled release"]}],
                "size": 8 * 1024**3,
            }])),
            patch.object(watchdog.sonarr_client, "get_queue", AsyncMock(return_value=[])),
        ):
            status_res = await client.get("/api/maintenance/status")
            assert status_res.status_code == 200
            data = status_res.json()
            assert data["issue_count"] == 2
            assert data["indexer_issues"][0]["kind"] == "auth_expired"
            assert data["arr_warnings"][0]["queue_id"] == 77

        # Vault locked -> Whisparr blacklist MUST be 404 stealth
        forbidden = await client.post(
            "/api/maintenance/blacklist-research",
            json={"service": "whisparr", "queue_id": 12},
        )
        assert forbidden.status_code == 404

        # Radarr blacklist & re-search -> deletes queue item, triggers search, invalidates cache
        del_mock = AsyncMock(return_value=(True, None))
        search_mock = AsyncMock(return_value=(True, "MoviesSearch queued on Radarr"))
        with (
            patch.object(watchdog.radarr_client, "get_queue", AsyncMock(return_value=[{"id": 77, "movieId": 501}])),
            patch.object(watchdog.radarr_client, "delete_queue_item", del_mock),
            patch.object(watchdog.radarr_client, "trigger_search", search_mock),
        ):
            res = await client.post(
                "/api/maintenance/blacklist-research",
                json={"service": "radarr", "queue_id": 77},
            )
            assert res.status_code == 200
            body = res.json()
            assert body["ok"] is True
            assert body["search_triggered"] is True
            del_mock.assert_awaited_once_with(queue_id=77, remove_from_client=True, blocklist=True)
            search_mock.assert_awaited_once_with(media_id=501, episode_ids=None)

        # AI triage (heuristic fallback when external LLM providers return None)
        from backend.app.ai_assistant import ai_assistant

        with patch.object(ai_assistant, "_dispatch_llm_providers", AsyncMock(return_value=None)):
            triage = await client.post(
                "/api/maintenance/ai-triage",
                json={
                    "report": {
                        "issue_count": 2,
                        "disk_free_formatted": "400.0 GB",
                        "low_disk_space": False,
                        "stalled_torrents": [],
                        "arr_warnings": [{"service": "radarr", "title": "Broken", "messages": ["Stalled"]}],
                        "indexer_issues": [{"indexer": "PrivateTracker", "kind": "auth_expired", "message": "403"}],
                        "duplicates": [],
                        "heavy_files": [],
                        "reclaimable_gb": 0.0,
                        "seeding_locked_gb": 0.0,
                    }
                },
            )
        assert triage.status_code == 200
        triage_body = triage.json()
        assert triage_body["ai_active"] is False
        assert "summary" in triage_body
        assert len(triage_body["recommended_actions"]) >= 2

        # AI triage with mocked external LLM verifies Adult title scrubbing before prompt dispatch
        captured_prompts = []

        async def fake_dispatch(**kwargs):
            captured_prompts.append(kwargs.get("user_content_override", ""))
            return {"summary": "Mock LLM summary", "recommended_actions": ["Replace stalled release."]}, "MockGemini"

        with patch.object(ai_assistant, "_dispatch_llm_providers", side_effect=fake_dispatch):
            llm_triage = await client.post(
                "/api/maintenance/ai-triage",
                json={
                    "report": {
                        "issue_count": 2,
                        "disk_free_formatted": "250.0 GB",
                        "low_disk_space": False,
                        "stalled_torrents": [
                            {"name": "ConfidentialAdultTitle", "category": "whisparr-adult", "state": "stalledDL", "stalled_hours": 10.0}
                        ],
                        "arr_warnings": [
                            {"service": "whisparr", "title": "SecretAdultScene", "messages": ["No files found"]}
                        ],
                        "indexer_issues": [],
                        "duplicates": [],
                        "heavy_files": [],
                        "reclaimable_gb": 0.0,
                        "seeding_locked_gb": 0.0,
                    }
                },
            )
            assert llm_triage.status_code == 200
            llm_body = llm_triage.json()
            assert llm_body["ai_active"] is True
            assert llm_body["model_used"] == "MockGemini"
            assert llm_body["summary"] == "Mock LLM summary"
            assert captured_prompts
            assert "ConfidentialAdultTitle" not in captured_prompts[0]
            assert "SecretAdultScene" not in captured_prompts[0]
            assert "[Vault Item]" in captured_prompts[0]


@pytest.mark.asyncio
async def test_watchdog_proactive_alert_emails_deduplicated_by_cooldown(monkeypatch):
    from backend.app.services.watchdog import (
        ALERT_COOLDOWN_SECONDS,
        check_and_send_watchdog_alerts,
        clear_watchdog_alert_state,
    )

    clear_watchdog_alert_state()
    sent_emails = []

    async def fake_collect(vault_unlocked: bool = False, force_refresh: bool = False):
        assert vault_unlocked is False
        return {
            "disk_free_bytes": 15 * 1024**3,
            "disk_free_formatted": "15.0 GB",
            "low_disk_space": True,
            "indexer_issues": [
                {"indexer": "PrivateTracker", "kind": "auth_expired", "message": "401 Unauthorized"},
                {"indexer": "MinorWarn", "kind": "warning", "message": "Slow response"},
            ],
        }

    async def fake_send_email(to_email: str, subject: str, html_body: str, text_body: str = "") -> bool:
        sent_emails.append((to_email, subject, html_body, text_body))
        return True

    monkeypatch.setattr(settings, "ALLOWED_EMAILS", "ops@example.com")
    monkeypatch.setattr(watchdog, "collect_maintenance_status", fake_collect)
    monkeypatch.setattr(watchdog, "send_email", fake_send_email)

    base_ts = 1_700_000_000.0
    sent_1 = await check_and_send_watchdog_alerts(now=base_ts)
    assert sent_1 == 1
    assert len(sent_emails) == 1
    assert sent_emails[0][0] == "ops@example.com"
    assert "PrivateTracker" in sent_emails[0][3]
    assert "15.0 GB" in sent_emails[0][3]
    assert "MinorWarn" not in sent_emails[0][3]

    # Within 6-hour cooldown -> suppressed cleanly
    sent_2 = await check_and_send_watchdog_alerts(now=base_ts + 1800)
    assert sent_2 == 0
    assert len(sent_emails) == 1

    # After 6-hour cooldown expires -> re-notifies cleanly
    sent_3 = await check_and_send_watchdog_alerts(now=base_ts + ALERT_COOLDOWN_SECONDS + 1)
    assert sent_3 == 1
    assert len(sent_emails) == 2
    clear_watchdog_alert_state()
