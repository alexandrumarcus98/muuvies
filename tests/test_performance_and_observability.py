import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text

from backend.app.ai_assistant import ai_assistant
from backend.app.config import settings
from backend.app.database import AsyncSessionLocal, engine, init_db
from backend.app.main import app
from backend.app.models import SearchLog
from backend.app.observability import init_sentry, scrub_sentry_event
from backend.app.services.cache import SearchMemoryCache, curator_cache, search_cache
from backend.app.services.http_clients import close_http_clients, get_search_client, get_telemetry_client, init_http_clients
from backend.app.services.library_mirror import (
    ensure_library_mirror_schema,
    query_library_fts,
    replace_service_library_items,
)


@pytest_asyncio.fixture(autouse=True)
async def _prepare_db():
    await init_db()
    await ensure_library_mirror_schema()


@pytest.mark.asyncio
async def test_sqlite_wal_and_performance_pragmas():
    async with engine.connect() as conn:
        journal = (await conn.execute(text("PRAGMA journal_mode;"))).scalar()
        sync_mode = (await conn.execute(text("PRAGMA synchronous;"))).scalar()
        cache_size = (await conn.execute(text("PRAGMA cache_size;"))).scalar()
        temp_store = (await conn.execute(text("PRAGMA temp_store;"))).scalar()

    assert str(journal).lower() == "wal"
    assert int(sync_mode) == 1  # NORMAL
    assert int(cache_size) == -32000
    assert int(temp_store) == 2  # MEMORY


@pytest.mark.asyncio
async def test_isolated_http_pools_initialized():
    search_c, telemetry_c = await init_http_clients()
    try:
        assert search_c is get_search_client()
        assert telemetry_c is get_telemetry_client()
        assert search_c is not telemetry_c
        assert telemetry_c.timeout.read == 1.5
        assert search_c.timeout.read == 15.0
    finally:
        await close_http_clients()


@pytest.mark.asyncio
async def test_swr_cache_fresh_stale_and_expired():
    cache = SearchMemoryCache(fresh_ttl_seconds=300, stale_ttl_seconds=1800)

    # 1. Fresh hit (<5m)
    cache.set("dune", {"v": 1})
    refresh_calls = 0

    async def _refresh():
        nonlocal refresh_calls
        refresh_calls += 1
        return {"v": 2}

    val, status = cache.get_swr("dune", refresh_factory=_refresh)
    assert status == "hit_fresh"
    assert val == {"v": 1}
    assert refresh_calls == 0

    # 2. Stale hit (between 5m and 30m): returns stale immediately and refreshes in background
    cache._cache["dune"] = (time.time() - 600, {"v": 1})
    val_stale, status_stale = cache.get_swr("dune", refresh_factory=_refresh)
    assert status_stale == "hit_stale"
    assert val_stale == {"v": 1}

    await asyncio.sleep(0.05)
    assert refresh_calls == 1
    val_updated, status_after = cache.get_swr("dune")
    assert status_after == "hit_fresh"
    assert val_updated == {"v": 2}

    # 3. Expired (>30m): miss and eviction
    cache._cache["dune"] = (time.time() - 1900, {"v": 2})
    val_expired, status_expired = cache.get_swr("dune")
    assert status_expired == "miss"
    assert val_expired is None


@pytest.mark.asyncio
async def test_local_fts5_library_mirror_and_vault_stealth():
    await replace_service_library_items(
        "radarr",
        [
            {"id": 101, "title": "Dune: Part Two", "clean_title": "duneparttwo", "year": 2024, "has_file": True},
            {"id": 102, "title": "Interstellar", "clean_title": "interstellar", "year": 2014, "has_file": False},
        ],
    )
    await replace_service_library_items(
        "sonarr",
        [{"id": 201, "title": "Severance", "clean_title": "severance", "year": 2022, "has_file": True}],
    )
    await replace_service_library_items(
        "whisparr",
        [{"id": 301, "title": "Secret Vault Film", "clean_title": "secretvaultfilm", "year": 2025, "has_file": True}],
    )

    matches = await query_library_fts("Dune Part Two 2160p REMUX", vault_unlocked=False)
    assert any(m["title"] == "Dune: Part Two" and m["has_file"] is True for m in matches)

    # Adult Vault item must stay invisible when vault_unlocked=False
    locked_secret = await query_library_fts("Secret Vault Film", vault_unlocked=False)
    assert locked_secret == []

    unlocked_secret = await query_library_fts("Secret Vault Film", vault_unlocked=True)
    assert len(unlocked_secret) == 1
    assert unlocked_secret[0]["service"] == "whisparr"

    # /api/library serves from SQLite mirror without hitting live *arr HTTP
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        login = await client.post(
            "/api/auth/login",
            json={"username": settings.ADMIN_USERNAME, "password": settings.ADMIN_PASSWORD},
        )
        assert login.status_code == 200
        with patch("backend.app.services.arr_client.radarr_client.fetch_library_titles_with_status") as mock_radarr:
            res = await client.get("/api/library")
            assert res.status_code == 200
            mock_radarr.assert_not_called()
            lib_data = res.json()
            assert lib_data["total_movies"] >= 2
            assert lib_data["total_shows"] >= 1
            assert lib_data["total_adult"] == 0


@pytest.mark.asyncio
async def test_candidate_compression_and_curator_caching():
    curator_cache.clear()
    releases = [
        {
            "guid": f"guid-{i}",
            "title": (
                f"Dune.Part.Two.2024.2160p.UHD.BluRay.REMUX.DV.HDR.TrueHD.Atmos-Framestor"
                if i == 11
                else f"Dune.Part.Two.2024.1080p.WEB-DL-GROUP{i}"
            ),
            "size": (50 + i) * (1024**3),
            "size_formatted": f"{50 + i} GB",
            "seeders": 30 + i,
            "indexer": "MockIndexer",
        }
        for i in range(15)
    ]

    enriched, heuristic_summary = ai_assistant.enrich_and_score_heuristically(
        "Dune Part Two", releases, []
    )
    assert len(enriched) == 15
    assert heuristic_summary is not None
    assert heuristic_summary.recommended_guid == "guid-11"
    assert heuristic_summary.ai_active is False

    top_originals, compressed, hash_digest = ai_assistant.compress_candidates_for_llm(enriched, top_n=8)
    assert len(top_originals) == 8
    assert len(compressed) == 8
    assert len(hash_digest) == 24
    for entry in compressed:
        assert set(entry.keys()) == {"title", "size_gb", "seeders", "indexer", "tags"}
    assert compressed[0]["title"].endswith("Framestor")

    # LLM mock called once, cached for subsequent call
    llm_payload = {
        "query_intent": "4K Dolby Vision REMUX",
        "recommended_title": top_originals[0]["title"],
        "recommendation_reason": "Highest bitrate Framestor REMUX with Atmos.",
        "ratings": [{"title": top_originals[0]["title"], "score": 99, "note": "Reference pick"}],
    }
    with patch.object(
        ai_assistant,
        "_dispatch_llm_providers",
        new=AsyncMock(return_value=(llm_payload, "Google Gemini (gemini-3.5-flash-lite)")),
    ) as mock_llm:
        summary1, ratings1, cached1 = await ai_assistant.curate_top_candidates(
            "Dune Part Two", enriched, [], category="Movies"
        )
        assert cached1 is False
        assert summary1 is not None and summary1.ai_active is True
        assert summary1.recommended_guid == "guid-11"
        assert ratings1.get("guid-11") == "Reference pick"
        assert mock_llm.await_count == 1

        # Second call with identical query + top 8 hits 30m cache
        summary2, _, cached2 = await ai_assistant.curate_top_candidates(
            "  dune part two ", enriched, [], category="Movies"
        )
        assert cached2 is True
        assert summary2 is not None and summary2.recommended_guid == "guid-11"
        assert mock_llm.await_count == 1

        # Adult Vault queries must NEVER call external LLM
        summary_adult, _, cached_adult = await ai_assistant.curate_top_candidates(
            "Dune Part Two", enriched, [], category="Adult"
        )
        assert cached_adult is False
        assert summary_adult is not None and summary_adult.ai_active is False
        assert mock_llm.await_count == 1


@pytest.mark.asyncio
async def test_nonblocking_search_and_curator_endpoint():
    search_cache.clear()
    curator_cache.clear()
    mock_releases = [
        {
            "guid": "rel-1",
            "title": "Inception.2010.2160p.BluRay.REMUX.DV.Atmos-Framestor",
            "category": "Movies",
            "category_id": 2000,
            "indexer": "PrivateMovies",
            "size": 60 * 1024**3,
            "size_formatted": "60.00 GB",
            "seeders": 85,
            "leechers": 4,
            "download_url": "http://example.invalid/dl/1",
            "magnet_url": None,
            "info_hash": "hashrel1",
            "publish_date": "2026-01-01T00:00:00Z",
            "is_adult": False,
        }
    ]

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        login = await client.post(
            "/api/auth/login",
            json={"username": settings.ADMIN_USERNAME, "password": settings.ADMIN_PASSWORD},
        )
        assert login.status_code == 200

        with (
            patch("backend.app.services.prowlarr.prowlarr_client.search", new=AsyncMock(return_value=mock_releases)),
            patch.object(ai_assistant, "_dispatch_llm_providers", new=AsyncMock()) as mock_llm,
        ):
            res = await client.post(
                "/api/search",
                json={"query": "InceptionTestUnique", "category": "Movies", "ai_smart_search": True},
            )
            assert res.status_code == 200
            data = res.json()
            assert data["total_found"] == 1
            assert data["results"][0]["guid"] == "rel-1"
            assert data["ai_ranking"]["ai_active"] is False
            # /api/search must NEVER block waiting on LLM inference
            mock_llm.assert_not_awaited()

        # Verify BackgroundTasks wrote SearchLog cleanly
        async with AsyncSessionLocal() as db:
            row = (
                await db.execute(
                    select(SearchLog).where(SearchLog.query == "InceptionTestUnique")
                )
            ).scalars().first()
            assert row is not None
            assert row.result_count == 1

        # Progressive curator endpoint executes LLM & updates result
        llm_payload = {
            "query_intent": "4K Inception REMUX",
            "recommended_title": mock_releases[0]["title"],
            "recommendation_reason": "Framestor 4K REMUX with Atmos.",
            "ratings": [],
        }
        with patch.object(
            ai_assistant,
            "_dispatch_llm_providers",
            new=AsyncMock(return_value=(llm_payload, "Google Gemini (gemini-3.5-flash-lite)")),
        ):
            cur_res = await client.post(
                "/api/search/curator",
                json={"query": "InceptionTestUnique", "category": "Movies"},
            )
            assert cur_res.status_code == 200
            cur_data = cur_res.json()
            assert cur_data["ai_ranking"]["ai_active"] is True
            assert cur_data["ai_ranking"]["recommended_guid"] == "rel-1"


def test_sentry_init_set_vs_unset_and_privacy_scrubbing():
    # Unset / blank DSN: does not initialize Sentry
    with patch("backend.app.observability.sentry_sdk.init") as mock_init:
        assert init_sentry("") is False
        assert init_sentry("   ") is False
        assert init_sentry(None) is False
        mock_init.assert_not_called()

    # Valid DSN: initializes with send_default_pii=False and traces_sample_rate=0.2
    with patch("backend.app.observability.sentry_sdk.init") as mock_init:
        ok = init_sentry("https://abc@o1.ingest.sentry.io/12345")
        assert ok is True
        mock_init.assert_called_once()
        kwargs = mock_init.call_args.kwargs
        assert kwargs["traces_sample_rate"] == 0.2
        assert kwargs["send_default_pii"] is False

    # Privacy scrubber drops Adult Vault requests and redacts cookies/secrets
    vault_event = {"request": {"url": "https://media.example.com/api/vault/unlock", "data": {"pin": "1234"}}}
    assert scrub_sentry_event(vault_event) is None

    adult_search_event = {
        "request": {
            "url": "https://media.example.com/api/search",
            "data": {"query": "private query", "category": "Adult"},
        }
    }
    assert scrub_sentry_event(adult_search_event) is None

    normal_event = {
        "request": {
            "url": "https://media.example.com/api/search",
            "cookies": {"media_session": "secret-jwt"},
            "headers": {"Cookie": "media_session=secret-jwt", "X-Api-Key": "prowlarr-secret", "Host": "media.example.com"},
            "data": {"query": "Inception", "category": "Movies", "gemini_api_key": "AIzaSecret"},
        },
        "extra": {"prowlarr_api_key": "raw-key", "safe_note": "ok"},
    }
    scrubbed = scrub_sentry_event(normal_event)
    assert scrubbed is not None
    assert "cookies" not in scrubbed["request"]
    assert scrubbed["request"]["headers"]["Cookie"] == "[Filtered]"
    assert scrubbed["request"]["headers"]["X-Api-Key"] == "[Filtered]"
    assert scrubbed["request"]["headers"]["Host"] == "media.example.com"
    assert scrubbed["request"]["data"]["gemini_api_key"] == "[Filtered]"
    assert scrubbed["extra"]["prowlarr_api_key"] == "[Filtered]"
    assert scrubbed["extra"]["safe_note"] == "ok"


def test_sentry_skipped_under_pytest_and_test_environment(monkeypatch):
    import os
    import sentry_sdk
    from backend.app import main as app_main

    assert sentry_sdk.get_client().is_active() is False
    assert os.getenv("SENTRY_DSN") == ""
    assert os.getenv("ENVIRONMENT") == "test"

    monkeypatch.setattr(app_main.settings, "SENTRY_DSN", "https://abc@o1.ingest.sentry.io/12345")
    assert app_main._should_enable_sentry() is False


